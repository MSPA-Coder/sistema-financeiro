"""Casos de uso de conciliação bancária (Bancos > Conciliação).

Uma linha de extrato só concilia com um lançamento da mesma conta, do mesmo
sinal (receita para valor positivo, despesa para negativo) e do mesmo valor.
Desfazer uma conciliação sempre devolve o lançamento para "vencidos", nunca
para um status inferido. Ignorar só é permitido para linhas ainda novas.

Quando o lançamento já está realizado com a mesma data e valor da linha, a
conciliação apenas vincula os dois: não tenta realizá-lo de novo, porque
`mark_transaction_realized` recusa lançamentos já realizados e a operação
falharia sempre.

Períodos fechados bloqueiam conciliação, e conciliar uma ponta de
transferência interna realiza a contraparte junto — as duas pontas nunca
ficam em estados diferentes.
"""
from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction as db_transaction

from banking.services import accessible_account_ids, can_access_account
from core.domain.finance import ENTRY_TYPE_EXPENSE, ENTRY_TYPE_INCOME, STATUS_REALIZED
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import (
    TransactionRequest,
    assert_entry_period_open,
    create_transaction_batch,
    is_month_closed,
    list_categories,
    realize_transaction,
    unrealize_transaction,
)

from .models import (
    LINE_STATUS_IGNORED,
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    BankStatementLine,
)

_CANDIDATE_LIMIT = 10


def pending_statement_lines_for_user(user, limit: int = 100) -> Iterable[BankStatementLine]:
    """Linhas novas (não conciliadas nem ignoradas) visíveis para `user`."""
    account_ids = accessible_account_ids(user, "view")
    if not account_ids:
        return BankStatementLine.objects.none()
    return BankStatementLine.objects.select_related(
        "account__owner", "account__institution"
    ).filter(account_id__in=account_ids, status=LINE_STATUS_NEW)[:limit]


def reconciled_statement_lines_for_user(user, limit: int = 25) -> Iterable[BankStatementLine]:
    """Últimas linhas conciliadas visíveis para `user`."""
    account_ids = accessible_account_ids(user, "view")
    if not account_ids:
        return BankStatementLine.objects.none()
    return BankStatementLine.objects.select_related(
        "account__owner", "account__institution", "matched_entry"
    ).filter(
        account_id__in=account_ids, status=LINE_STATUS_RECONCILED, matched_entry__isnull=False
    )[:limit]


def candidate_entries_for_line(line: BankStatementLine, limit: int = _CANDIDATE_LIMIT):
    """Lançamentos candidatos a conciliar com `line`: mesma conta, mesmo
    sinal (receita se valor > 0, despesa se < 0), mesmo valor absoluto, com
    vencimento entre o início do mês e a data do extrato."""
    value = abs(line.amount)
    entry_type = ENTRY_TYPE_INCOME if line.amount > 0 else ENTRY_TYPE_EXPENSE
    month_start = line.statement_date.replace(day=1)
    return CashFlowEntry.objects.filter(
        account_id=line.account_id,
        entry_type=entry_type,
        entry_amount=value,
        due_date__range=(month_start, line.statement_date),
    ).order_by("-due_date", "-id")[:limit]


def candidate_entries_for_lines(
    lines: Iterable[BankStatementLine], limit: int = _CANDIDATE_LIMIT
) -> dict[int, list[CashFlowEntry]]:
    """Candidatos a conciliar para várias linhas de uma vez.

    Mesmo critério e mesmo limite de `candidate_entries_for_line`, mas
    agrupado por (conta, tipo, valor) para evitar uma consulta por linha: a
    tela de conciliação mostra até 100 linhas, e é comum poucas contas e
    poucos valores se repetirem entre elas (contas fixas, salário). Busca o
    conjunto que cobre a união das datas de cada grupo numa única consulta e
    aplica a data e o limite exatos de cada linha em Python -- resultado
    idêntico ao de chamar a versão de uma linha para cada, com uma fração
    das consultas."""
    lines = list(lines)
    if not lines:
        return {}

    groups: dict[tuple[int, str, object], list[BankStatementLine]] = {}
    for line in lines:
        value = abs(line.amount)
        entry_type = ENTRY_TYPE_INCOME if line.amount > 0 else ENTRY_TYPE_EXPENSE
        groups.setdefault((line.account_id, entry_type, value), []).append(line)

    candidates_by_line: dict[int, list[CashFlowEntry]] = {line.id: [] for line in lines}
    for (account_id, entry_type, value), group_lines in groups.items():
        earliest_month_start = min(gl.statement_date.replace(day=1) for gl in group_lines)
        latest_statement_date = max(gl.statement_date for gl in group_lines)
        pool = list(
            CashFlowEntry.objects.filter(
                account_id=account_id,
                entry_type=entry_type,
                entry_amount=value,
                due_date__range=(earliest_month_start, latest_statement_date),
            ).order_by("-due_date", "-id")
        )
        for gl in group_lines:
            month_start = gl.statement_date.replace(day=1)
            candidates_by_line[gl.id] = [
                entry for entry in pool if month_start <= entry.due_date <= gl.statement_date
            ][:limit]

    return candidates_by_line


_FALLBACK_CATEGORY_NAME = "Outros"


def _category_hint(description: str) -> str | None:
    """Extrai o prefixo de categoria de uma descrição de linha de extrato.

    Adapters de PDF (ex. `GenialPdfStatementAdapter`) gravam a descrição como
    "<categoria do extrato> - <descrição>" - é a mesma categoria que aparece
    numa linha antes da descrição do lançamento no PDF original. CSV/OFX não
    seguem esse formato, então o prefixo simplesmente não bate com nenhuma
    categoria cadastrada e a sugestão cai no fallback.
    """
    prefix, sep, _ = description.partition(" - ")
    return prefix.strip() if sep else None


def suggested_category_for_line(line: BankStatementLine) -> CashFlowCategory | None:
    """Categoria sugerida para criar um lançamento a partir da linha.

    Tenta casar o prefixo de categoria do extrato (quando existe) com uma
    categoria cadastrada pelo nome; sem casamento - caso comum de categorias
    do extrato sem equivalente direto, como "Rendimentos" - cai para "Outros"
    quando essa categoria existir."""
    hint = _category_hint(line.description)
    if hint:
        match = CashFlowCategory.objects.filter(category_name__iexact=hint).first()
        if match:
            return match
    return CashFlowCategory.objects.filter(category_name__iexact=_FALLBACK_CATEGORY_NAME).first()


def _get_line_in_scope(user, line_id, action: str = "view") -> BankStatementLine:
    if not line_id:
        raise ValueError("Linha de extrato inválida.")
    try:
        line = BankStatementLine.objects.select_related("account").get(id=line_id)
    except BankStatementLine.DoesNotExist as exc:
        raise ValueError("Linha de extrato não encontrada.") from exc
    if not can_access_account(user, line.account_id, action):
        raise ValueError("Acesso negado para esta linha de extrato.")
    return line


def reconciliation_view_data(user, target_line_id: int | None = None) -> dict:
    """Dados para a tela de conciliação: linhas pendentes, candidatos por
    linha e conciliações recentes, com destaque opcional de uma linha alvo
    (usado após uma ação HTMX, para manter a linha visível mesmo que ela
    tenha saído da lista padrão)."""
    lines = list(pending_statement_lines_for_user(user))
    reconciled_lines = list(reconciled_statement_lines_for_user(user))

    target_line = None
    if target_line_id:
        try:
            target_line = BankStatementLine.objects.select_related(
                "account__owner", "account__institution", "matched_entry"
            ).get(id=target_line_id)
        except BankStatementLine.DoesNotExist:
            target_line = None
        if target_line is not None and not can_access_account(user, target_line.account_id, "view"):
            target_line = None

    if target_line is not None:
        if target_line.status == LINE_STATUS_RECONCILED:
            if all(existing.id != target_line.id for existing in reconciled_lines):
                reconciled_lines = [target_line, *reconciled_lines]
        elif all(existing.id != target_line.id for existing in lines):
            lines = [target_line, *lines]

    # Candidatos anexados diretamente em cada linha (em vez de um dict
    # separado por id) porque o Django Template Language não suporta lookup
    # de dicionário por variável (`candidates[line.id]`); anexar o atributo
    # aqui é mais simples do que registrar um template filter só para isso.
    candidates_by_line = candidate_entries_for_lines(lines)
    for line in lines:
        line.reconcile_candidates = candidates_by_line.get(line.id, [])
        suggested = suggested_category_for_line(line)
        line.suggested_category_id = suggested.id if suggested else None

    return {
        "lines": lines,
        "reconciled_lines": reconciled_lines,
        "target_line_id": target_line.id if target_line else None,
        "categories": list(list_categories().order_by("category_name")),
    }


@db_transaction.atomic
def reconcile_line_with_entry(user, *, line_id, entry_id) -> BankStatementLine:
    """Concilia uma linha de extrato com um lançamento existente.

    Realiza o lançamento com a data e o valor da linha (a menos que já
    esteja realizado com a mesma data/valor — ver nota de módulo). Ver
    `transactions.services.realize_transaction` para o tratamento de
    transferências internas (contraparte realizada junto).
    """
    line = _get_line_in_scope(user, line_id, "update")
    if not entry_id:
        raise ValueError("Selecione a linha do extrato e o movimento a conciliar.")
    try:
        entry = CashFlowEntry.objects.select_related("account").get(id=entry_id)
    except CashFlowEntry.DoesNotExist as exc:
        raise ValueError("Movimento não encontrado.") from exc
    if not can_access_account(user, entry.account_id, "update"):
        raise ValueError("Acesso negado para este movimento.")

    if line.status != LINE_STATUS_NEW or line.matched_entry_id is not None:
        raise ValueError("Linha de extrato já conciliada ou ignorada.")
    if BankStatementLine.objects.filter(
        matched_entry_id=entry.id, status=LINE_STATUS_RECONCILED
    ).exclude(id=line.id).exists():
        raise ValueError("Movimento já está conciliado com outra linha de extrato.")
    if line.account_id != entry.account_id:
        raise ValueError("A linha de extrato e o movimento pertencem a contas diferentes.")

    expected_type = ENTRY_TYPE_INCOME if line.amount > 0 else ENTRY_TYPE_EXPENSE
    if entry.entry_type != expected_type:
        raise ValueError("Tipo do movimento incompatível com o sinal da linha de extrato.")

    line_value = abs(line.amount)
    if entry.entry_amount != line_value:
        raise ValueError("Valor do movimento incompatível com a linha de extrato.")

    already_realized_matching = False
    if entry.status == STATUS_REALIZED:
        matches = (
            entry.realized_date == line.statement_date
            and (entry.realized_amount or entry.entry_amount) == line_value
        )
        if not matches:
            raise ValueError("Movimento já realizado com data ou valor diferente da linha de extrato.")
        already_realized_matching = True

    assert_entry_period_open(entry, action_label="conciliar")
    if is_month_closed(entry.account, line.statement_date.year, line.statement_date.month):
        raise ValueError(
            f"Não é possível conciliar: mês {line.statement_date.month:02d}/"
            f"{line.statement_date.year} fechado para a conta {entry.account}."
        )

    line.matched_entry = entry
    line.status = LINE_STATUS_RECONCILED
    line.save(update_fields=["matched_entry", "status", "updated_at"])

    if not already_realized_matching:
        realize_transaction(entry, realized_date=line.statement_date, realized_amount=line_value)

    return line


@db_transaction.atomic
def undo_reconciliation(user, *, line_id) -> BankStatementLine:
    """Desfaz a conciliação de uma linha, revertendo o lançamento vinculado
    para "vencidos" (e sua contraparte de transferência, se houver)."""
    line = _get_line_in_scope(user, line_id, "update")
    if line.status != LINE_STATUS_RECONCILED or line.matched_entry_id is None:
        raise ValueError("Linha de extrato não está conciliada.")

    entry = line.matched_entry
    if entry.status == STATUS_REALIZED:
        unrealize_transaction(entry)

    line.matched_entry = None
    line.status = LINE_STATUS_NEW
    line.save(update_fields=["matched_entry", "status", "updated_at"])
    return line


@db_transaction.atomic
def create_entry_from_line(user, *, line_id, category_id) -> BankStatementLine:
    """Cria um lançamento novo a partir de uma linha de extrato e já a
    concilia com ele.

    Existe porque nem toda linha de extrato tem um lançamento prévio para
    conciliar - é o caso comum de taxas, impostos e rendimentos de corretora
    importados de PDF, que não passam por um cadastro manual antes do
    extrato chegar. O lançamento criado nasce "Realizado", com data e valor
    de realização iguais aos da linha (mesma convenção usada por
    `reconcile_line_with_entry`).
    """
    line = _get_line_in_scope(user, line_id, "update")
    if line.status != LINE_STATUS_NEW or line.matched_entry_id is not None:
        raise ValueError("Linha de extrato já conciliada ou ignorada.")
    if not category_id:
        raise ValueError("Selecione uma categoria para criar o lançamento.")

    if is_month_closed(line.account, line.statement_date.year, line.statement_date.month):
        raise ValueError(
            f"Não é possível criar lançamento: mês {line.statement_date.month:02d}/"
            f"{line.statement_date.year} fechado para a conta {line.account}."
        )

    value = abs(line.amount)
    hint = _category_hint(line.description)
    entry_description = line.description
    if hint and CashFlowCategory.objects.filter(id=category_id, category_name__iexact=hint).exists():
        # A categoria escolhida já é a do prefixo do extrato: não duplica a
        # informação na descrição do lançamento (ex. "Corretagem - Corretagem
        # Executor - Btc" viraria só "Corretagem Executor - Btc").
        entry_description = line.description[len(hint) + 3:]
    req = TransactionRequest(
        account_id=line.account_id,
        category_id=category_id,
        entry_type=ENTRY_TYPE_INCOME if line.amount > 0 else ENTRY_TYPE_EXPENSE,
        description=entry_description,
        entry_amount=value,
        installments=1,
        due_date=line.statement_date,
        status=STATUS_REALIZED,
        realized_date=line.statement_date,
        realized_amount=value,
    )
    entries = create_transaction_batch(req)

    line.matched_entry = entries[0]
    line.status = LINE_STATUS_RECONCILED
    line.save(update_fields=["matched_entry", "status", "updated_at"])
    return line


def ignore_statement_line(user, *, line_id) -> BankStatementLine:
    """Marca uma linha nova como ignorada (não deve ser conciliada)."""
    line = _get_line_in_scope(user, line_id, "update")
    if line.status != LINE_STATUS_NEW or line.matched_entry_id is not None:
        raise ValueError("Apenas linhas novas e não conciliadas podem ser ignoradas.")
    line.status = LINE_STATUS_IGNORED
    line.save(update_fields=["status", "updated_at"])
    return line


def bulk_create_entries_from_lines(user, *, line_ids: Iterable) -> tuple[int, list[tuple[str, str]]]:
    """Cria lançamentos para várias linhas de uma vez, cada uma com a
    categoria sugerida pelo prefixo do extrato (ver `suggested_category_for_line`)
    - não há seleção manual de categoria em lote. Cada linha é processada em
    sua própria transação (via `create_entry_from_line`), então uma falha
    isolada não desfaz as demais."""
    created = 0
    errors: list[tuple[str, str]] = []
    for line_id in line_ids:
        try:
            line = _get_line_in_scope(user, line_id, "update")
            suggested = suggested_category_for_line(line)
            if suggested is None:
                raise ValueError("Nenhuma categoria disponível para sugerir (nem 'Outros').")
            create_entry_from_line(user, line_id=line_id, category_id=suggested.id)
            created += 1
        except ValueError as exc:
            errors.append((str(line_id), str(exc)))
    return created, errors


def bulk_reconcile_lines(user, *, line_ids: Iterable) -> tuple[int, list[tuple[str, str]]]:
    """Concilia várias linhas de uma vez, cada uma com seu único candidato
    (mesma conta, sinal e valor - ver `candidate_entries_for_line`).

    Linhas sem candidato ou com mais de um exigem escolha manual (não há como
    adivinhar qual movimento é o certo) e são reportadas como erro, sem
    bloquear as demais - mesma lógica de resiliência de
    `bulk_create_entries_from_lines`."""
    reconciled = 0
    errors: list[tuple[str, str]] = []
    for line_id in line_ids:
        try:
            line = _get_line_in_scope(user, line_id, "update")
            candidates = list(candidate_entries_for_line(line, limit=2))
            if not candidates:
                raise ValueError("Nenhum movimento candidato para conciliar.")
            if len(candidates) > 1:
                raise ValueError("Mais de um movimento candidato - concilie manualmente.")
            reconcile_line_with_entry(user, line_id=line_id, entry_id=candidates[0].id)
            reconciled += 1
        except ValueError as exc:
            errors.append((str(line_id), str(exc)))
    return reconciled, errors


def bulk_ignore_lines(user, *, line_ids: Iterable) -> tuple[int, list[tuple[str, str]]]:
    """Ignora várias linhas de uma vez; cada uma isoladamente, mesma lógica
    de resiliência de `bulk_create_entries_from_lines`."""
    ignored = 0
    errors: list[tuple[str, str]] = []
    for line_id in line_ids:
        try:
            ignore_statement_line(user, line_id=line_id)
            ignored += 1
        except ValueError as exc:
            errors.append((str(line_id), str(exc)))
    return ignored, errors
