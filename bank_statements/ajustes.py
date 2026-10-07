"""Ajustes pontuais de dados a partir de um plano declarativo (JSON).

Serve às correções que não cabem numa regra geral: o lançamento que foi gravado
na conta errada, a conta que ganhou número de extrato, o lançamento agregado que
o extrato passou a mostrar em várias linhas. O plano **não** mora no repositório
(descreve dinheiro real); só estas operações moram, cada uma conferindo o que
espera achar e recusando tudo se não achar.

Operações, nesta ordem:

- `criar_contas`: cria uma conta (ex.: a aplicação Rende Fácil), com tipo, saldo
  inicial e, se aplicação, a conta de movimento. `liberar_destino` concede a
  quem roda o plano a permissão de transferir para ela, que nenhuma conta nova
  tem (nem para administrador);
- `contas`: define o identificador no extrato, o saldo inicial e/ou a conta de
  movimento (`conta_de_movimento`, só para aplicação) de uma conta;
- `mover_pontas`: troca a conta de um lançamento (uma ponta de transferência
  ou um avulso), refazendo o texto das duas pontas;
- `converter_em_transferencia`: transforma um lançamento avulso gerencial
  (receita ou despesa) em ponta de transferência com `outra_conta`, criando a
  ponta que falta lá. O saldo da conta do lançamento não muda, por isso o mês
  fechado dela é reaberto e fechado de novo com o mesmo saldo; o mês da outra
  conta precisa estar aberto, porque ela ganha um lançamento;
- `excluir`: exclui um lançamento (a transferência inteira, se for ponta), com a
  linha de extrato ligada a ele voltando para "novo".

Um lançamento é descrito por `conta`, `data`, `tipo`, `valor` e, se preciso,
`descricao_contem`; precisa casar com **um** só. Uma conta é descrita por
`titular`, `instituicao` (um nome ou uma lista de nomes) e, se preciso, `nome`.

Tudo ou nada. Mover e excluir exigem os meses abertos: mudar de conta mexe no
saldo das duas, e isso não cabe no contrato "o saldo de fechamento não muda".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction

from accounts.services import can_use_transfer_destination
from banking.models import FinancialAccount
from banking.services import can_access_account, update_account
from core.domain.finance import OPERATION_INTERNAL_TRANSFER
from transactions.models import CashFlowEntry
from transactions.services import (
    _account_label,
    assert_entry_period_open,
    delete_transaction_or_operation,
    is_month_closed,
    transfer_counterparty,
)


@dataclass
class Relatorio:
    linhas: list[str] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)

    def ok(self, texto: str) -> None:
        self.linhas.append(texto)

    def erro(self, texto: str) -> None:
        self.erros.append(texto)


def _decimal(valor, rotulo: str) -> Decimal:
    try:
        return Decimal(str(valor))
    except InvalidOperation as exc:
        raise ValueError(f"{rotulo} inválido: {valor!r}.") from exc


def achar_conta(spec: dict) -> FinancialAccount:
    instituicoes = spec.get("instituicao")
    if isinstance(instituicoes, str):
        instituicoes = [instituicoes]
    if not spec.get("titular") or not instituicoes:
        raise ValueError(f"A conta precisa de titular e instituição: {spec!r}.")
    consulta = FinancialAccount.objects.select_related("owner", "institution")
    achadas = [
        conta for conta in consulta.filter(owner__name__iexact=spec["titular"])
        if conta.institution.institution_name.lower() in {nome.lower() for nome in instituicoes}
        and (not spec.get("nome") or conta.account_name.lower() == spec["nome"].lower())
    ]
    if len(achadas) != 1:
        raise ValueError(f"A conta {spec!r} casa com {len(achadas)} contas; esperava exatamente uma.")
    return achadas[0]


def achar_lancamento(spec: dict) -> CashFlowEntry:
    conta = achar_conta(spec["conta"])
    data = date.fromisoformat(spec["data"])
    valor = _decimal(spec["valor"], "Valor")
    consulta = CashFlowEntry.objects.select_related("account__owner", "account__institution", "category").filter(
        account=conta, entry_type=spec["tipo"], entry_amount=valor
    )
    achados = [
        e for e in consulta
        if (e.realized_date or e.due_date) == data
        and (not spec.get("descricao_contem") or spec["descricao_contem"].lower() in e.description.lower())
    ]
    if len(achados) != 1:
        raise ValueError(f"O lançamento {spec!r} casa com {len(achados)} lançamentos; esperava exatamente um.")
    return achados[0]


def _definir_conta(user, spec: dict, relatorio: Relatorio) -> None:
    conta = achar_conta(spec["conta"])
    novo_identificador = spec.get("identificador", conta.statement_identifier)
    novo_saldo = spec.get("saldo_inicial", conta.initial_balance)
    nova_data = spec.get("data_do_saldo_inicial", conta.initial_balance_date.isoformat())
    nova_movimentacao = (
        achar_conta(spec["conta_de_movimento"]).id if spec.get("conta_de_movimento") else conta.movement_account_id
    )
    update_account(
        user, conta,
        owner_id=str(conta.owner_id), institution_id=str(conta.institution_id), account_name=conta.account_name,
        initial_balance=str(novo_saldo), currency=conta.currency, initial_balance_date=str(nova_data),
        purpose=conta.purpose, account_kind=conta.account_kind,
        card_closing_day=str(conta.card_closing_day or ""), card_due_day=str(conta.card_due_day or ""),
        card_payment_account_id=str(conta.card_payment_account_id or ""),
        card_estimated_spend=str(conta.card_estimated_spend or ""),
        statement_identifier=str(novo_identificador),
        movement_account_id=str(nova_movimentacao or ""),
    )
    relatorio.ok(
        f"Conta {_account_label(conta)}: identificador \"{novo_identificador}\", "
        f"saldo inicial {novo_saldo} em {nova_data}"
        + (f", movimenta por {_account_label(conta.movement_account)}." if conta.movement_account_id else ".")
    )


def _refazer_textos(entrada: CashFlowEntry) -> None:
    contraparte = transfer_counterparty(entrada)
    if contraparte is None:
        return
    origem, destino = (entrada, contraparte) if entrada.source_entry_id is None else (contraparte, entrada)
    origem.description = f"Conta Destino: {_account_label(destino.account)}"
    destino.description = f"Conta Origem: {_account_label(origem.account)}"
    origem.save(update_fields=["description", "updated_at"])
    destino.save(update_fields=["description", "updated_at"])


def _mover_ponta(user, spec: dict, relatorio: Relatorio, audit_context) -> None:
    from bank_statements.models import BankStatementLine
    from core.services import log_audit_event

    entrada = achar_lancamento(spec["lancamento"])
    destino = achar_conta(spec["para_conta"])
    if destino.currency != entrada.account.currency:
        raise ValueError("Não dá para mover lançamento entre contas de moedas diferentes.")
    if destino.id == entrada.account_id:
        raise ValueError(f"O lançamento #{entrada.id} já está na conta de destino.")
    if not (can_access_account(user, entrada.account_id, "update") and can_access_account(user, destino.id, "update")):
        raise ValueError("Acesso negado para mover este lançamento.")
    if entrada.operation_type == OPERATION_INTERNAL_TRANSFER:
        contraparte = transfer_counterparty(entrada)
        if contraparte is not None and contraparte.account_id == destino.id:
            raise ValueError("A conta de destino é a outra ponta da própria transferência.")
        if not can_use_transfer_destination(user, destino.id):
            raise ValueError("Sem permissão de destino de transferência para a conta.")
    assert_entry_period_open(entrada, action_label="mover")
    for dia in {entrada.due_date, entrada.realized_date} - {None}:
        if is_month_closed(destino, dia.year, dia.month):
            raise ValueError(f"O mês {dia.month:02d}/{dia.year} está fechado na conta de destino.")
    if BankStatementLine.objects.filter(matched_entry=entrada).exists():
        raise ValueError(
            f"O lançamento #{entrada.id} está conciliado com uma linha de extrato da conta de origem: "
            "desfaça a conciliação antes de mover."
        )
    antes = _account_label(entrada.account)
    entrada.account = destino
    entrada.save(update_fields=["account", "updated_at"])
    entrada.refresh_from_db()
    _refazer_textos(entrada)
    log_audit_event(
        "cash_flow_entry", entrada.id, "update",
        old_values={"account": antes}, new_values={"account": _account_label(destino)},
        user=user, request_context=audit_context, summary="Lançamento movido de conta por ajuste de dados.",
    )
    relatorio.ok(f"Lançamento #{entrada.id} ({entrada.entry_amount}) movido de {antes} para {_account_label(destino)}.")


def _criar_conta(user, spec: dict, relatorio: Relatorio) -> None:
    from accounts.models import AccountOwner
    from banking.models import FinancialInstitution
    from banking.services import create_account

    titular = AccountOwner.objects.filter(name__iexact=spec["titular"]).first()
    instituicao = FinancialInstitution.objects.filter(institution_name__iexact=spec["instituicao"]).first()
    if titular is None or instituicao is None:
        raise ValueError(f"Titular ou instituição não encontrados: {spec!r}.")
    if FinancialAccount.objects.filter(
        owner=titular, institution=instituicao, account_name__iexact=spec["nome"]
    ).exists():
        raise ValueError(f"A conta {spec['titular']} / {spec['instituicao']} / {spec['nome']} já existe.")
    movimento = achar_conta(spec["conta_de_movimento"]).id if spec.get("conta_de_movimento") else ""
    conta = create_account(
        user,
        owner_id=str(titular.id), institution_id=str(instituicao.id), account_name=spec["nome"],
        initial_balance=str(spec.get("saldo_inicial", "0")), currency=spec.get("moeda", "BRL"),
        initial_balance_date=str(spec.get("data_do_saldo_inicial", "")),
        account_kind=spec.get("tipo", "conta"), purpose=spec.get("finalidade", "pessoal"),
        movement_account_id=str(movimento),
    )
    if spec.get("liberar_destino"):
        from accounts.models import UserTransferDestinationAccess

        UserTransferDestinationAccess.objects.get_or_create(user=user, destination_account=conta)
    relatorio.ok(
        f"Conta criada: {_account_label(conta)} ({conta.account_kind}), saldo inicial {conta.initial_balance} "
        f"em {conta.initial_balance_date}"
        + (f", movimenta por {_account_label(conta.movement_account)}" if conta.movement_account_id else "")
        + ("; destino de transferência liberado para " + user.username if spec.get("liberar_destino") else "")
        + "."
    )


def _converter_em_transferencia(user, spec: dict, relatorio: Relatorio, audit_context) -> None:
    from uuid import uuid4

    from core.domain.finance import (
        CATEGORY_KIND_MANAGERIAL,
        ENTRY_TYPE_EXPENSE,
        ENTRY_TYPE_INCOME,
        OPERATION_SINGLE,
        STATUS_REALIZED,
    )
    from core.services import log_audit_event
    from transactions.models import BankOperation

    from .extrato import _categoria_de_transferencia
    from .meses import meses_reabertos

    entrada = achar_lancamento(spec["lancamento"])
    outra = achar_conta(spec["outra_conta"])
    if (
        entrada.status != STATUS_REALIZED
        or entrada.operation_type != OPERATION_SINGLE
        or entrada.source_entry_id is not None
        or entrada.category.kind != CATEGORY_KIND_MANAGERIAL
    ):
        raise ValueError(f"O lançamento #{entrada.id} não é um avulso gerencial realizado.")
    if outra.id == entrada.account_id or outra.owner_id != entrada.account.owner_id:
        raise ValueError("A outra conta tem de ser outra conta do mesmo titular.")
    if outra.currency != entrada.account.currency:
        raise ValueError("Transferência entre moedas diferentes é lançada à mão.")
    if not (can_access_account(user, entrada.account_id, "update") and can_access_account(user, outra.id, "create")):
        raise ValueError("Acesso negado para converter este lançamento.")
    dia = entrada.realized_date
    if is_month_closed(outra, dia.year, dia.month):
        raise ValueError(f"O mês {dia.month:02d}/{dia.year} está fechado na conta {_account_label(outra)}.")
    recebe_na_conta = entrada.entry_type == ENTRY_TYPE_INCOME
    if not can_use_transfer_destination(user, (entrada.account if recebe_na_conta else outra).id):
        raise ValueError("Sem permissão de destino de transferência para a conta que recebe.")

    categoria = _categoria_de_transferencia()
    antes = (entrada.category.category_name, entrada.description)
    meses = {(entrada.account_id, dia.year, dia.month)}
    with meses_reabertos(user, meses, autorizar=True, motivo="Lançamento convertido em transferência", audit_context=audit_context):
        operacao = BankOperation.objects.create(
            operation_key=f"{OPERATION_INTERNAL_TRANSFER}-{uuid4().hex}",
            operation_type=OPERATION_INTERNAL_TRANSFER, description="", status=STATUS_REALIZED,
            installment_total=1, responsible_user=user,
        )

        def nova_ponta(*, tipo, descricao, origem=None):
            return CashFlowEntry.objects.create(
                account=outra, category=categoria, entry_type=tipo, description=descricao,
                entry_amount=entrada.realized_amount, installments=1, current_installment=1,
                due_date=dia, realized_date=dia, realized_amount=entrada.realized_amount,
                status=STATUS_REALIZED, operation_type=OPERATION_INTERNAL_TRANSFER,
                bank_operation=operacao, source_entry=origem,
            )

        if recebe_na_conta:
            # O dinheiro sai da outra conta (origem) e entra na do lançamento.
            nova = nova_ponta(tipo=ENTRY_TYPE_EXPENSE, descricao=f"Conta Destino: {_account_label(entrada.account)}")
            entrada.source_entry = nova
            entrada.description = f"Conta Origem: {_account_label(outra)}"
        else:
            entrada.description = f"Conta Destino: {_account_label(outra)}"
        entrada.category = categoria
        entrada.operation_type = OPERATION_INTERNAL_TRANSFER
        entrada.bank_operation = operacao
        entrada.save(update_fields=["category", "operation_type", "bank_operation", "source_entry", "description", "updated_at"])
        if not recebe_na_conta:
            nova = nova_ponta(tipo=ENTRY_TYPE_INCOME, descricao=f"Conta Origem: {_account_label(entrada.account)}", origem=entrada)
    log_audit_event(
        "cash_flow_entry", entrada.id, "update",
        old_values={"category": antes[0], "description": antes[1]},
        new_values={"category": categoria.category_name, "description": entrada.description},
        user=user, request_context=audit_context, summary="Lançamento convertido em ponta de transferência por ajuste de dados.",
    )
    log_audit_event(
        "cash_flow_entry", nova.id, "create", user=user, request_context=audit_context,
        summary="Ponta de transferência criada por ajuste de dados.",
    )
    relatorio.ok(
        f"Lançamento #{entrada.id} ({entrada.realized_amount} em {dia:%d/%m/%Y}) virou transferência "
        f"{'de' if recebe_na_conta else 'para'} {_account_label(outra)} (ponta nova #{nova.id})."
    )


def _excluir(user, spec: dict, relatorio: Relatorio, audit_context) -> None:
    entrada = achar_lancamento(spec["lancamento"])
    descricao = f"#{entrada.id} {entrada.entry_amount} ({_account_label(entrada.account)})"
    apagados = delete_transaction_or_operation(entrada, audit_context=audit_context, user=user)
    relatorio.ok(f"Excluído o lançamento {descricao}: {apagados} lançamento(s) removido(s).")


def executar(user, plano: dict, *, aplicar: bool = False, audit_context=None) -> Relatorio:
    """Valida e, com `aplicar`, grava o plano inteiro. Sem `aplicar`, tudo é
    desfeito no fim: a simulação roda as operações de verdade dentro de uma
    transação revertida, então ela recusa exatamente o que a aplicação recusaria."""
    relatorio = Relatorio()
    ordem = (
        ("criar_contas", lambda item: _criar_conta(user, item, relatorio)),
        ("contas", lambda item: _definir_conta(user, item, relatorio)),
        ("mover_pontas", lambda item: _mover_ponta(user, item, relatorio, audit_context)),
        ("converter_em_transferencia", lambda item: _converter_em_transferencia(user, item, relatorio, audit_context)),
        ("excluir", lambda item: _excluir(user, item, relatorio, audit_context)),
    )
    desconhecidas = set(plano) - {nome for nome, _ in ordem}
    if desconhecidas:
        raise ValueError(f"Operações desconhecidas no plano: {sorted(desconhecidas)}.")
    try:
        with db_transaction.atomic():
            for nome, operacao in ordem:
                for item in plano.get(nome, []):
                    try:
                        operacao(item)
                    except ValueError as exc:
                        relatorio.erro(f"[{nome}] {exc}")
            if relatorio.erros or not aplicar:
                raise _ReversaoError
    except _ReversaoError:
        pass
    return relatorio


class _ReversaoError(Exception):
    """Desfaz a transação: houve erro, ou é só simulação."""
