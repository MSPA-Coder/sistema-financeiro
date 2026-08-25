"""Serviços de domínio para transações e fluxo de caixa."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import transaction as db_transaction
from django.db.models import Count
from django.utils import timezone

from banking.models import FinancialAccount
from banking.services import can_access_account
from core.domain.finance import (
    CALC_DIVIDE,
    CALC_REPEAT,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    MAX_TRANSACTION_DESCRIPTION_LENGTH,
    MAX_TRANSACTION_INSTALLMENTS,
    OPERATION_INSTALLMENT,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_RECURRING,
    OPERATION_SCOPE_ALL,
    OPERATION_SCOPE_CURRENT_FUTURE,
    OPERATION_SCOPE_SINGLE,
    OPERATION_SINGLE,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions.models import (
    AccountMonthClose,
    BankOperation,
    CashFlowCategory,
    CashFlowEntry,
)


def is_month_closed(account: FinancialAccount, year: int, month: int) -> bool:
    """Verifica se um mês está fechado para uma conta específica."""
    return AccountMonthClose.objects.filter(
        account=account,
        year=year,
        month=month,
        active=True,
    ).exists()


def validate_month_not_closed(account: FinancialAccount, due_date: date) -> None:
    """Valida que o mês da data de vencimento não está fechado."""
    if is_month_closed(account, due_date.year, due_date.month):
        raise ValueError(f"Mês {due_date.month}/{due_date.year} está fechado para a conta {account}")


def assert_entry_period_open(entry: CashFlowEntry, action_label: str = "alterar") -> None:
    """Garante que o período do lançamento não está fechado.

    Checa vencimento e, quando existir, data de realização: um lançamento pode
    ter as duas pontas em meses diferentes, e basta uma estar fechada para a
    alteração ser proibida."""
    validate_month_not_closed(entry.account, entry.due_date)
    if entry.realized_date:
        validate_month_not_closed(entry.account, entry.realized_date)


def transfer_counterparty(entry: CashFlowEntry) -> CashFlowEntry | None:
    """Retorna a outra ponta de uma transferência interna, se houver.

    A relação é assimétrica: a ponta "destino" aponta para a origem via
    `source_entry`, e a partir da origem a contraparte é o lançamento
    derivado dela. Por isso a busca precisa cobrir os dois sentidos.
    """
    if entry is None or entry.operation_type != OPERATION_INTERNAL_TRANSFER:
        return None
    if entry.source_entry_id:
        return entry.source_entry
    return entry.derived_entries.first()


def create_installment_entries(
    account: FinancialAccount,
    category: CashFlowCategory,
    entry_type: str,
    description: str,
    total_amount: Decimal,
    first_due_date: date,
    installments: int,
    calc_mode: str = "repeat",
    status: str = STATUS_PROJECTED,
) -> list[CashFlowEntry]:
    """Cria lançamentos parcelados."""
    from core.domain.finance import CALC_DIVIDE
    
    validate_month_not_closed(account, first_due_date)
    
    if installments < 1 or installments > MAX_TRANSACTION_INSTALLMENTS:
        raise ValueError(f"Número de parcelas deve estar entre 1 e {MAX_TRANSACTION_INSTALLMENTS}")
    
    if total_amount <= 0:
        raise ValueError("O valor total deve ser positivo")
    
    # Calcular valor por parcela. O if/else fica no lugar de um ternário porque
    # o comentário do ramo `else` nomeia o modo (CALC_REPEAT), que é a
    # informação que faz a linha ser entendida sem consultar a constante.
    if calc_mode == CALC_DIVIDE:  # noqa: SIM108
        installment_amount = total_amount / installments
    else:  # CALC_REPEAT
        installment_amount = total_amount
    
    # Criar operação agrupadora
    last_due_date = first_due_date + timedelta(days=30 * (installments - 1))
    operation_key = f"installment_{timezone.now().timestamp()}_{description[:50]}"
    
    bank_operation = BankOperation.objects.create(
        operation_key=operation_key,
        operation_type=OPERATION_INSTALLMENT,
        description=description[:255],
        status=status,
        installment_total=installments,
        first_due_date=first_due_date,
        last_due_date=last_due_date,
        entry_count=installments,
    )
    
    entries = []
    for i in range(installments):
        installment_due_date = first_due_date + timedelta(days=30 * i)
        validate_month_not_closed(account, installment_due_date)
        
        entry = CashFlowEntry.objects.create(
            account=account,
            category=category,
            entry_type=entry_type,
            description=description[:255],
            entry_amount=installment_amount,
            installments=installments,
            current_installment=i + 1,
            due_date=installment_due_date,
            status=status,
            operation_type=OPERATION_INSTALLMENT,
            bank_operation=bank_operation,
        )
        entries.append(entry)
    
    return entries


@db_transaction.atomic
def realize_transaction(
    entry: CashFlowEntry,
    realized_date: date | None = None,
    realized_amount: Decimal | None = None,
) -> CashFlowEntry:
    """Marca um lançamento como realizado.

    Transferências internas são realizadas em par: a contraparte (a outra
    ponta da mesma `BankOperation`) é realizada junto, com a mesma data e o
    mesmo valor. Sem isso, conciliar (ou realizar manualmente) apenas uma ponta
    deixaria a outra pendente e violaria a coerencia entre as duas pontas.
    """
    if entry.status == STATUS_REALIZED:
        raise ValueError("Lançamento já está realizado")

    final_date = realized_date or date.today()
    # Os dois meses precisam estar abertos: o do vencimento e o da data de
    # realização informada agora. Fechar um mês tem de impedir tanto mexer no
    # que vencia nele quanto lançar realização dentro dele.
    validate_month_not_closed(entry.account, entry.due_date)
    validate_month_not_closed(entry.account, final_date)

    counterpart = transfer_counterparty(entry)
    realize_counterpart = counterpart is not None and counterpart.status != STATUS_REALIZED
    if realize_counterpart:
        validate_month_not_closed(counterpart.account, counterpart.due_date)
        validate_month_not_closed(counterpart.account, final_date)

    final_amount = entry.entry_amount if realized_amount is None else realized_amount
    if final_amount <= 0:
        raise ValueError("O valor realizado deve ser positivo.")

    entry.status = STATUS_REALIZED
    entry.realized_date = final_date
    entry.realized_amount = final_amount
    entry.save(update_fields=['status', 'realized_date', 'realized_amount', 'updated_at'])

    if realize_counterpart:
        counterpart.status = STATUS_REALIZED
        counterpart.realized_date = final_date
        counterpart.realized_amount = final_amount
        counterpart.save(update_fields=['status', 'realized_date', 'realized_amount', 'updated_at'])

    # Atualizar status na operação pai se existir. Origem e destino de uma
    # transferência compartilham a mesma BankOperation (ver
    # create_internal_transfer), então uma única atualização já cobre o par.
    if entry.bank_operation_id:
        BankOperation.objects.filter(
            id=entry.bank_operation_id,
            status__in=[STATUS_PROJECTED, STATUS_PENDING],
        ).update(status=STATUS_REALIZED)

    return entry


def unrealize_transaction(entry: CashFlowEntry) -> CashFlowEntry:
    """Reverte a realização de um lançamento (usado por Bancos > Conciliação
    ao desfazer uma conciliação).

    O lançamento volta sempre para STATUS_PENDING (vencidos), independente da
    data de vencimento: inferir o status a partir da data poderia devolvê-lo
    como "a vencer" e escondê-lo da tela onde o usuário acabou de agir. A
    contraparte de uma transferência interna é revertida junto, a não ser que
    já esteja cancelada.
    """
    if entry.status != STATUS_REALIZED:
        raise ValueError("Lançamento não está realizado.")

    assert_entry_period_open(entry, action_label="desfazer a realização de")
    counterpart = transfer_counterparty(entry)
    revert_counterpart = counterpart is not None
    if revert_counterpart:
        assert_entry_period_open(counterpart, action_label="desfazer a realização de")

    entry.status = STATUS_PENDING
    entry.realized_date = None
    entry.realized_amount = None
    entry.save(update_fields=['status', 'realized_date', 'realized_amount', 'updated_at'])

    if revert_counterpart:
        counterpart.status = STATUS_PENDING
        counterpart.realized_date = None
        counterpart.realized_amount = None
        counterpart.save(update_fields=['status', 'realized_date', 'realized_amount', 'updated_at'])

    if entry.bank_operation_id:
        still_realized = CashFlowEntry.objects.filter(
            bank_operation_id=entry.bank_operation_id, status=STATUS_REALIZED,
        ).exists()
        if not still_realized:
            BankOperation.objects.filter(
                id=entry.bank_operation_id, status=STATUS_REALIZED,
            ).update(status=STATUS_PENDING)

    return entry


@db_transaction.atomic
def close_month(
    account: FinancialAccount,
    year: int,
    month: int,
    closing_balance: Decimal,
    user,
) -> AccountMonthClose:
    """Fecha um mês para uma conta específica."""
    if not can_access_account(user, account.id, "update"):
        raise ValueError("Acesso negado: usuário sem permissão para fechar este mês.")
    if is_month_closed(account, year, month):
        raise ValueError(f"Mês {month}/{year} já está fechado para esta conta")

    month_close = AccountMonthClose.objects.create(
        account=account,
        year=year,
        month=month,
        closing_balance=closing_balance,
        closed_at=timezone.now(),
        closed_by_user=user,
    )
    from core.services import log_audit_event
    log_audit_event(
        "account_month_close", month_close.id, "close",
        new_values={"account_id": account.id, "year": year, "month": month, "closing_balance": str(closing_balance)},
        user=user,
    )
    return month_close


@db_transaction.atomic
def reopen_month(
    account: FinancialAccount,
    year: int,
    month: int,
    reason: str,
    user,
) -> AccountMonthClose:
    """Reabre um mês fechado."""
    if not can_access_account(user, account.id, "update"):
        raise ValueError("Acesso negado: usuário sem permissão para reabrir este mês.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("O motivo da reabertura é obrigatório.")
    try:
        month_close = AccountMonthClose.objects.select_for_update().get(
            account=account,
            year=year,
            month=month,
            active=True,
        )
    except AccountMonthClose.DoesNotExist as err:
        raise ValueError(f"Mês {month}/{year} não está fechado para esta conta") from err

    month_close.active = False
    month_close.reopened_at = timezone.now()
    month_close.reopened_by_user = user
    month_close.reopen_reason = reason[:255]
    month_close.save(update_fields=['active', 'reopened_at', 'reopened_by_user', 'reopen_reason', 'updated_at'])

    from core.services import log_audit_event
    log_audit_event(
        "account_month_close", month_close.id, "reopen",
        new_values={"active": False, "reopen_reason": month_close.reopen_reason},
        user=user,
    )
    return month_close


@db_transaction.atomic
def create_internal_transfer(
    source_account: FinancialAccount,
    target_account: FinancialAccount,
    amount: Decimal,
    description: str,
    due_date: date,
) -> tuple[CashFlowEntry, CashFlowEntry]:
    """Cria uma transferência interna entre contas."""
    validate_month_not_closed(source_account, due_date)
    validate_month_not_closed(target_account, due_date)
    
    if amount <= 0:
        raise ValueError("O valor da transferência deve ser positivo")
    
    if source_account == target_account:
        raise ValueError("Contas de origem e destino devem ser diferentes")
    
    # Criar operação agrupadora
    operation_key = f"transfer_{timezone.now().timestamp()}_{description[:50]}"
    bank_operation = BankOperation.objects.create(
        operation_key=operation_key,
        operation_type=OPERATION_INTERNAL_TRANSFER,
        description=description[:255],
        status=STATUS_PROJECTED,
        installment_total=1,
        first_due_date=due_date,
        last_due_date=due_date,
        entry_count=2,
    )
    
    # Lançamento de saída (despesa na conta de origem)
    expense_category = CashFlowCategory.objects.get_or_create(
        category_name="Transferência Interna",
        defaults={'is_internal': True},
    )[0]
    
    source_entry = CashFlowEntry.objects.create(
        account=source_account,
        category=expense_category,
        entry_type=ENTRY_TYPE_EXPENSE,
        description=f"Transf.: {description}"[:255],
        entry_amount=amount,
        installments=1,
        current_installment=1,
        due_date=due_date,
        status=STATUS_PROJECTED,
        operation_type=OPERATION_INTERNAL_TRANSFER,
        bank_operation=bank_operation,
    )
    
    # Lançamento de entrada (receita na conta de destino)
    target_entry = CashFlowEntry.objects.create(
        account=target_account,
        category=expense_category,
        entry_type=ENTRY_TYPE_INCOME,
        description=f"Transf.: {description}"[:255],
        entry_amount=amount,
        installments=1,
        current_installment=1,
        due_date=due_date,
        status=STATUS_PROJECTED,
        operation_type=OPERATION_INTERNAL_TRANSFER,
        bank_operation=bank_operation,
        source_entry=source_entry,
    )
    
    return source_entry, target_entry


# --- Cadastros: Categorias ---

_MAX_CATEGORY_NAME_LENGTH = 100


def list_categories(type_filter: str | None = None):
    queryset = CashFlowCategory.objects.all()
    if type_filter == 'internal':
        queryset = queryset.filter(is_internal=True)
    elif type_filter == 'normal':
        queryset = queryset.filter(is_internal=False)
    return queryset


def _clean_category_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Nome da categoria é obrigatório.")
    if len(name) > _MAX_CATEGORY_NAME_LENGTH:
        raise ValueError(f"Nome da categoria não pode exceder {_MAX_CATEGORY_NAME_LENGTH} caracteres.")
    return name


def create_category(name: str, is_internal: bool) -> CashFlowCategory:
    from django.db import IntegrityError

    clean_name = _clean_category_name(name)
    try:
        return CashFlowCategory.objects.create(category_name=clean_name, is_internal=bool(is_internal))
    except IntegrityError as exc:
        raise ValueError("Já existe uma categoria com este nome.") from exc


def update_category(category: CashFlowCategory, name: str, is_internal: bool) -> CashFlowCategory:
    from django.db import IntegrityError

    clean_name = _clean_category_name(name)
    category.category_name = clean_name
    category.is_internal = bool(is_internal)
    try:
        category.save(update_fields=["category_name", "is_internal", "updated_at"])
    except IntegrityError as exc:
        raise ValueError("Já existe uma categoria com este nome.") from exc
    return category


def delete_category(category: CashFlowCategory) -> None:
    from django.db.models import ProtectedError

    try:
        category.delete()
    except ProtectedError as exc:
        raise ValueError(
            "Não é possível excluir esta categoria: existem lançamentos vinculados a ela."
        ) from exc


# ============================================================================
# Tela "Transações" (Movimentação > Lançamentos): criação, edição e exclusão
# com escopo de operação (parcelas, recorrência, transferência interna).
#
#
# O agrupamento usa a FK `bank_operation`. A coluna legada `operation_id` nao
# recebe escritas e nao deve participar das consultas (ver
# `transactions/operations.py`).
# ============================================================================

MONEY_QUANT = Decimal("0.01")


@dataclass(frozen=True)
class TransactionRequest:
    """Payload validado de criação/edição de lançamento.

    Frozen de propósito: o caso de uso não deve reescrever a entrada enquanto
    a processa."""

    account_id: int
    category_id: int
    entry_type: str
    description: str
    entry_amount: Decimal
    installments: int
    due_date: date
    calc_mode: str = CALC_REPEAT
    is_recurring: bool = False
    status: str = STATUS_PROJECTED
    realized_date: date | None = None
    realized_amount: Decimal | None = None
    counterparty_account_id: int | None = None


def _opposite_entry_type(entry_type: str) -> str:
    return ENTRY_TYPE_INCOME if entry_type == ENTRY_TYPE_EXPENSE else ENTRY_TYPE_EXPENSE


def _account_label(account: FinancialAccount | None) -> str:
    if not account:
        return ""
    owner_name = account.owner.name if account.owner_id else ""
    institution_name = account.institution.institution_name if account.institution_id else ""
    return " / ".join(p for p in (owner_name, institution_name, account.account_name) if p)


def _monthly_amount_for(entry_amount: Decimal, installments: int, calc_mode: str) -> Decimal:
    amount = entry_amount if isinstance(entry_amount, Decimal) else Decimal(str(entry_amount))
    if calc_mode == CALC_DIVIDE and installments > 1:
        return (amount / Decimal(installments)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def operation_type_for(req: TransactionRequest, is_internal: bool) -> str:
    if is_internal:
        return OPERATION_INTERNAL_TRANSFER
    if req.is_recurring:
        return OPERATION_RECURRING
    if req.installments > 1:
        return OPERATION_INSTALLMENT
    return OPERATION_SINGLE


def _normalize_open_entry_status(status: str, due_date: date, today: date | None = None) -> str:
    ref_date = today or date.today()
    if status == STATUS_REALIZED:
        return STATUS_REALIZED
    if due_date < ref_date:
        return STATUS_PENDING
    if status == STATUS_PENDING:
        return STATUS_PROJECTED
    return status


def supports_operation_scope(entry: CashFlowEntry) -> bool:
    """True se `entry` pertence a um grupo (parcelas/recorrência/transferência
    recorrente ou parcelada) onde faz sentido escolher o escopo da alteração."""
    operation_type = entry.operation_type or OPERATION_SINGLE
    if operation_type in (OPERATION_INSTALLMENT, OPERATION_RECURRING):
        return True
    if operation_type == OPERATION_INTERNAL_TRANSFER:
        return bool(entry.is_recurring or (entry.installments or 1) > 1)
    return False


def _internal_transfer_bucket_key(entry: CashFlowEntry):
    if entry.is_recurring:
        return entry.due_date
    if (entry.installments or 1) > 1:
        return entry.current_installment or 1
    return entry.due_date


def operation_entries(entry: CashFlowEntry) -> list[CashFlowEntry]:
    """Todos os lançamentos da mesma `BankOperation` de `entry` (ou só `entry`)."""
    if not entry.bank_operation_id:
        return [entry]
    return list(
        CashFlowEntry.objects.select_related("account__owner", "account__institution", "category")
        .filter(bank_operation_id=entry.bank_operation_id)
        .order_by("due_date", "id")
    )


def scoped_entries(tx: CashFlowEntry, entries: list[CashFlowEntry], scope: str) -> list[CashFlowEntry]:
    """Filtra `entries` conforme o escopo escolhido (all/single/current_future)."""
    if scope == OPERATION_SCOPE_ALL or not supports_operation_scope(tx):
        return list(entries)
    operation_type = tx.operation_type or OPERATION_SINGLE
    if operation_type == OPERATION_INTERNAL_TRANSFER:
        if scope == OPERATION_SCOPE_SINGLE:
            key = _internal_transfer_bucket_key(tx)
            return [e for e in entries if _internal_transfer_bucket_key(e) == key]
        if scope == OPERATION_SCOPE_CURRENT_FUTURE:
            if tx.is_recurring:
                return [e for e in entries if e.due_date >= tx.due_date]
            current = tx.current_installment or 1
            return [e for e in entries if (e.current_installment or 1) >= current]
        return list(entries)
    if scope == OPERATION_SCOPE_SINGLE:
        return [tx]
    if scope == OPERATION_SCOPE_CURRENT_FUTURE:
        if operation_type == OPERATION_INSTALLMENT:
            current = tx.current_installment or 1
            return [e for e in entries if (e.current_installment or 1) >= current]
        if operation_type == OPERATION_RECURRING:
            return [e for e in entries if e.due_date >= tx.due_date]
    return list(entries)


def _origin_counterparty_pairs(entries: list[CashFlowEntry]) -> list[tuple[CashFlowEntry, CashFlowEntry]]:
    buckets: dict[object, list[CashFlowEntry]] = {}
    for entry in entries:
        buckets.setdefault(_internal_transfer_bucket_key(entry), []).append(entry)
    pairs = []
    for group in buckets.values():
        origin = next((e for e in group if e.source_entry_id is None), group[0])
        counterparty = next((e for e in group if e.id != origin.id), None)
        if counterparty is None:
            raise ValueError("Transferência interna sem contraparte.")
        pairs.append((origin, counterparty))
    return sorted(pairs, key=lambda p: (p[0].current_installment or 1, p[0].due_date, p[0].id))


def with_transfer_counterparts(
    entries: list[CashFlowEntry], all_operation_entries: list[CashFlowEntry]
) -> list[CashFlowEntry]:
    """Garante que toda entrada de transferência interna carregue sua contraparte.

    Um lado de uma transferência interna nunca pode ser excluído sem o outro
    (a FK `source_entry` ficaria órfã). Expande `entries` incluindo a
    contraparte de cada par ainda não presente na lista selecionada.
    """
    if not entries:
        return entries
    if not any(e.operation_type == OPERATION_INTERNAL_TRANSFER for e in entries):
        return entries

    selected_ids = {e.id for e in entries}
    extras: list[CashFlowEntry] = []
    for entry in list(entries):
        if entry.operation_type != OPERATION_INTERNAL_TRANSFER:
            continue
        if entry.source_entry_id is not None:
            origin = next((e for e in all_operation_entries if e.id == entry.source_entry_id), None)
            if origin and origin.id not in selected_ids:
                extras.append(origin)
                selected_ids.add(origin.id)
        else:
            counterpart = next(
                (e for e in all_operation_entries if e.source_entry_id == entry.id), None
            )
            if counterpart and counterpart.id not in selected_ids:
                extras.append(counterpart)
                selected_ids.add(counterpart.id)
    return entries + extras


def renumber_remaining_installments(operation_type: str, entries: list[CashFlowEntry]) -> None:
    """Renumera parcelas remanescentes após uma exclusão parcial de um grupo."""
    if not entries:
        return
    if operation_type == OPERATION_INSTALLMENT:
        ordered = sorted(entries, key=lambda e: (e.current_installment or 1, e.due_date, e.id))
        total = len(ordered)
        for index, entry in enumerate(ordered, start=1):
            entry.current_installment = index
            entry.installments = total
            entry.save(update_fields=["current_installment", "installments", "updated_at"])
    elif (
        operation_type == OPERATION_INTERNAL_TRANSFER
        and not entries[0].is_recurring
        and (entries[0].installments or 1) > 1
    ):
        pairs = _origin_counterparty_pairs(entries)
        total = len(pairs)
        for index, (origin, counterparty) in enumerate(pairs, start=1):
            origin.current_installment = index
            origin.installments = total
            counterparty.current_installment = index
            counterparty.installments = total
            origin.save(update_fields=["current_installment", "installments", "updated_at"])
            counterparty.save(update_fields=["current_installment", "installments", "updated_at"])


def _snapshot_entry(entry: CashFlowEntry) -> dict:
    return {
        "account_id": entry.account_id,
        "category_id": entry.category_id,
        "entry_type": entry.entry_type,
        "description": entry.description,
        "entry_amount": str(entry.entry_amount),
        "installments": entry.installments,
        "current_installment": entry.current_installment,
        "due_date": entry.due_date.isoformat() if entry.due_date else None,
        "status": entry.status,
        "realized_date": entry.realized_date.isoformat() if entry.realized_date else None,
        "realized_amount": str(entry.realized_amount) if entry.realized_amount is not None else None,
        "is_recurring": entry.is_recurring,
        "operation_type": entry.operation_type,
        "bank_operation_id": entry.bank_operation_id,
        "source_entry_id": entry.source_entry_id,
    }


def _sync_bank_operation_status(bank_operation_id: int | None) -> None:
    if not bank_operation_id:
        return
    statuses = set(
        CashFlowEntry.objects.filter(bank_operation_id=bank_operation_id).values_list("status", flat=True)
    )
    if not statuses:
        return
    # Uma operacao so fica realizada quando todas as entradas estao realizadas.
    # Qualquer pendencia prevalece; nos demais casos, permanece projetada.
    if statuses == {STATUS_REALIZED}:
        new_status = STATUS_REALIZED
    elif STATUS_PENDING in statuses:
        new_status = STATUS_PENDING
    else:
        new_status = STATUS_PROJECTED
    BankOperation.objects.filter(id=bank_operation_id).update(status=new_status, updated_at=timezone.now())


def _apply_fields(
    entry: CashFlowEntry,
    req: TransactionRequest,
    *,
    due_date: date,
    monthly_amount: Decimal,
    installments: int,
    description: str | None = None,
    entry_type: str | None = None,
    account_id: int | None = None,
    source_entry: CashFlowEntry | None = None,
    clear_source_entry: bool = False,
) -> None:
    entry.account_id = account_id if account_id is not None else req.account_id
    entry.category_id = req.category_id
    entry.entry_type = entry_type if entry_type is not None else req.entry_type
    entry.description = (req.description if description is None else description)[:255]
    entry.entry_amount = monthly_amount
    entry.installments = installments
    entry.due_date = due_date
    entry.is_recurring = req.is_recurring
    entry.status = _normalize_open_entry_status(req.status, due_date)
    entry.realized_date = req.realized_date if entry.status == STATUS_REALIZED else None
    entry.realized_amount = req.realized_amount if entry.status == STATUS_REALIZED else None
    if clear_source_entry:
        entry.source_entry = None
    elif source_entry is not None:
        entry.source_entry = source_entry


def _validate_common_payload(req: TransactionRequest, monthly_amount: Decimal, installments: int) -> None:
    if monthly_amount <= 0:
        raise ValueError("O valor do lançamento deve ser positivo.")
    if req.realized_amount is not None and req.realized_amount <= 0:
        raise ValueError("O valor realizado deve ser positivo.")
    if not 1 <= installments <= MAX_TRANSACTION_INSTALLMENTS:
        raise ValueError(f"Número de parcelas deve estar entre 1 e {MAX_TRANSACTION_INSTALLMENTS}.")
    if len(req.description or "") > MAX_TRANSACTION_DESCRIPTION_LENGTH:
        raise ValueError(f"Descrição não pode exceder {MAX_TRANSACTION_DESCRIPTION_LENGTH} caracteres.")


# --- Criação --------------------------------------------------------------


@db_transaction.atomic
def create_transaction_batch(req: TransactionRequest) -> list[CashFlowEntry]:
    """Cria um lote de lançamentos: único, parcelado ou recorrente, incluindo
    transferência interna quando a categoria informada é `is_internal`.

    Recorrências são projetadas até o horizonte configurado em Configurações >
    Parâmetros (`recurring_projection_horizon_end`).
    """
    from core.services import log_audit_event
    from reports.services import add_months
    from transactions.recurring_projection import recurring_projection_horizon_end

    try:
        category = CashFlowCategory.objects.get(id=req.category_id)
    except CashFlowCategory.DoesNotExist as exc:
        raise ValueError("Categoria inválida.") from exc
    is_internal = bool(category.is_internal)

    counterparty_account = None
    if is_internal:
        if not req.counterparty_account_id:
            raise ValueError("Conta destino é obrigatória para categoria interna.")
        if req.counterparty_account_id == req.account_id:
            raise ValueError("Conta destino deve ser diferente da conta de origem.")
        try:
            counterparty_account = FinancialAccount.objects.select_related("owner", "institution").get(
                id=req.counterparty_account_id
            )
        except FinancialAccount.DoesNotExist as exc:
            raise ValueError("Conta destino inválida.") from exc

    try:
        account = FinancialAccount.objects.select_related("owner", "institution").get(id=req.account_id)
    except FinancialAccount.DoesNotExist as exc:
        raise ValueError("Conta inválida.") from exc

    installments = 1 if req.is_recurring else max(1, req.installments)
    monthly_amount = _monthly_amount_for(req.entry_amount, installments, req.calc_mode)
    _validate_common_payload(req, monthly_amount, installments)

    description = (req.description or "").strip()[:MAX_TRANSACTION_DESCRIPTION_LENGTH]
    operation_type = operation_type_for(req, is_internal)

    bank_operation = None
    if operation_type != OPERATION_SINGLE:
        bank_operation = BankOperation.objects.create(
            operation_key=f"{operation_type}-{uuid4().hex}",
            operation_type=operation_type,
            description=description,
            status=req.status,
            installment_total=installments,
            first_due_date=req.due_date,
            last_due_date=req.due_date,
            entry_count=0,
        )

    original_description = description
    counterparty_description = description
    if is_internal:
        original_description = f"Conta Destino: {_account_label(counterparty_account)}"
        counterparty_description = f"Conta Origem: {_account_label(account)}"

    entries: list[CashFlowEntry] = []

    def _add_pair(due_date: date, current_installment: int, installments_total: int) -> None:
        validate_month_not_closed(account, due_date)
        if is_internal:
            validate_month_not_closed(counterparty_account, due_date)
        status = _normalize_open_entry_status(req.status, due_date)
        realized_date = req.realized_date if status == STATUS_REALIZED else None
        realized_amount = req.realized_amount if status == STATUS_REALIZED else None
        original = CashFlowEntry.objects.create(
            account=account,
            category=category,
            entry_type=req.entry_type,
            description=original_description,
            entry_amount=monthly_amount,
            installments=installments_total,
            current_installment=current_installment,
            due_date=due_date,
            is_recurring=req.is_recurring,
            status=status,
            realized_date=realized_date,
            realized_amount=realized_amount,
            operation_type=operation_type,
            bank_operation=bank_operation,
        )
        entries.append(original)
        if is_internal:
            counterparty_entry = CashFlowEntry.objects.create(
                account=counterparty_account,
                category=category,
                entry_type=_opposite_entry_type(req.entry_type),
                description=counterparty_description,
                entry_amount=monthly_amount,
                installments=installments_total,
                current_installment=current_installment,
                due_date=due_date,
                is_recurring=req.is_recurring,
                status=status,
                realized_date=realized_date,
                realized_amount=realized_amount,
                operation_type=operation_type,
                bank_operation=bank_operation,
                source_entry=original,
            )
            entries.append(counterparty_entry)

    if req.is_recurring:
        horizon = max(recurring_projection_horizon_end(), req.due_date)
        offset = 0
        while True:
            occurrence_due = add_months(req.due_date, offset)
            if occurrence_due > horizon:
                break
            _add_pair(occurrence_due, 1, 1)
            offset += 1
    else:
        for i in range(1, installments + 1):
            _add_pair(add_months(req.due_date, i - 1), i, installments)

    if bank_operation is not None and entries:
        due_dates = [e.due_date for e in entries]
        bank_operation.first_due_date = min(due_dates)
        bank_operation.last_due_date = max(due_dates)
        bank_operation.entry_count = len(entries)
        bank_operation.save(update_fields=["first_due_date", "last_due_date", "entry_count", "updated_at"])

    for entry in entries:
        log_audit_event("cash_flow_entry", entry.id, "create", new_values=_snapshot_entry(entry))

    return entries


def possible_duplicate_entries(
    account_id: int, entry_type: str, amount: Decimal, due_date: date, exclude_ids: set | None = None
) -> list[CashFlowEntry]:
    """Lançamentos existentes com mesma conta/tipo/valor/vencimento (aviso de duplicidade)."""
    exclude_ids = exclude_ids or set()
    qs = CashFlowEntry.objects.filter(
        account_id=account_id, entry_type=entry_type, entry_amount=abs(amount), due_date=due_date,
    ).order_by("-id")
    if exclude_ids:
        qs = qs.exclude(id__in=exclude_ids)
    return list(qs[:10])


def possible_duplicates_for_created_entries(
    req: TransactionRequest, entries: list[CashFlowEntry]
) -> list[CashFlowEntry]:
    amount = abs(entries[0].entry_amount) if entries else req.entry_amount
    return possible_duplicate_entries(
        req.account_id, req.entry_type, amount, req.due_date, exclude_ids={e.id for e in entries}
    )


# --- Edição -----------------------------------------------------------------


def _get_counterparty_account_id(tx: CashFlowEntry) -> int | None:
    if tx.operation_type != OPERATION_INTERNAL_TRANSFER:
        return None
    if tx.source_entry_id:
        origin = tx.source_entry
        return origin.account_id if origin else None
    counterpart = CashFlowEntry.objects.filter(source_entry_id=tx.id).first()
    if counterpart:
        return counterpart.account_id
    if tx.bank_operation_id:
        other = (
            CashFlowEntry.objects.filter(bank_operation_id=tx.bank_operation_id)
            .exclude(id=tx.id)
            .exclude(account_id=tx.account_id)
            .first()
        )
        return other.account_id if other else None
    return None


def counterparty_account_map(entries: list[CashFlowEntry]) -> dict[int, int | None]:
    """Resolve, em lote, a conta contraparte de cada lançamento de transferência interna."""
    mapping: dict[int, int | None] = {e.id: None for e in entries}
    targets = [e for e in entries if e.operation_type == OPERATION_INTERNAL_TRANSFER and e.bank_operation_id]
    if not targets:
        return mapping

    bank_operation_ids = {e.bank_operation_id for e in targets}
    op_entries = list(CashFlowEntry.objects.filter(bank_operation_id__in=bank_operation_ids))
    by_operation: dict[int, list[CashFlowEntry]] = {}
    for op_entry in op_entries:
        by_operation.setdefault(op_entry.bank_operation_id, []).append(op_entry)

    for entry in targets:
        rows = by_operation.get(entry.bank_operation_id, [])
        if not rows:
            continue
        if entry.source_entry_id:
            origin = next((r for r in rows if r.id == entry.source_entry_id), None)
            mapping[entry.id] = origin.account_id if origin else None
            continue
        counterpart = next((r for r in rows if r.source_entry_id == entry.id), None)
        if counterpart:
            mapping[entry.id] = counterpart.account_id
            continue
        other = next((r for r in rows if r.id != entry.id and r.account_id != entry.account_id), None)
        mapping[entry.id] = other.account_id if other else None
    return mapping


def current_future_attachment_counts(entries: list[CashFlowEntry]) -> dict[int, int]:
    """Para cada `entry` parcelado/recorrente, conta em lote quantos
    comprovantes seriam apagados se a edição fosse salva com o escopo 'este
    registro e os próximos'.

    Só se aplica a `OPERATION_INSTALLMENT`/`OPERATION_RECURRING`: é o único
    caminho que apaga e recria o bloco atual/futuro (`_replace_current_future_block`).
    Transferência interna, mesmo suportando escopo, atualiza os lançamentos
    no lugar (sem apagar), então não arrisca o anexo.
    """
    from bank_statements.models import EntryAttachment

    targets = [
        e for e in entries
        if supports_operation_scope(e)
        and (e.operation_type or OPERATION_SINGLE) in (OPERATION_INSTALLMENT, OPERATION_RECURRING)
    ]
    mapping: dict[int, int] = {e.id: 0 for e in entries}
    if not targets:
        return mapping

    bank_operation_ids = {e.bank_operation_id for e in targets if e.bank_operation_id}
    by_operation: dict[int, list[CashFlowEntry]] = {}
    if bank_operation_ids:
        for op_entry in CashFlowEntry.objects.filter(bank_operation_id__in=bank_operation_ids):
            by_operation.setdefault(op_entry.bank_operation_id, []).append(op_entry)

    all_ids = {e.id for group in by_operation.values() for e in group}
    all_ids.update(e.id for e in targets if not e.bank_operation_id)
    attachment_counts = dict(
        EntryAttachment.objects.filter(entry_id__in=all_ids)
        .values("entry_id")
        .annotate(total=Count("id"))
        .values_list("entry_id", "total")
    )

    for tx in targets:
        rows = by_operation.get(tx.bank_operation_id) if tx.bank_operation_id else None
        rows = rows or [tx]
        block = scoped_entries(tx, rows, OPERATION_SCOPE_CURRENT_FUTURE)
        mapping[tx.id] = sum(attachment_counts.get(e.id, 0) for e in block)
    return mapping


def _update_single(tx: CashFlowEntry, req: TransactionRequest) -> list[CashFlowEntry]:
    category = CashFlowCategory.objects.get(id=req.category_id)
    if category.is_internal:
        return _convert_single_to_internal_transfer(tx, req)

    installments = 1 if req.is_recurring else max(1, req.installments)
    monthly_amount = _monthly_amount_for(req.entry_amount, installments, req.calc_mode)
    _validate_common_payload(req, monthly_amount, installments)

    account = FinancialAccount.objects.get(id=req.account_id)
    validate_month_not_closed(account, req.due_date)

    _apply_fields(tx, req, due_date=req.due_date, monthly_amount=monthly_amount, installments=installments)
    tx.current_installment = 1
    tx.operation_type = operation_type_for(req, False)
    tx.save()
    return [tx]


def _convert_single_to_internal_transfer(tx: CashFlowEntry, req: TransactionRequest) -> list[CashFlowEntry]:
    if not req.counterparty_account_id:
        raise ValueError("Conta destino é obrigatória para categoria interna.")
    if req.counterparty_account_id == req.account_id:
        raise ValueError("Conta destino deve ser diferente da conta de origem.")
    try:
        counterparty_account = FinancialAccount.objects.select_related("owner", "institution").get(
            id=req.counterparty_account_id
        )
    except FinancialAccount.DoesNotExist as exc:
        raise ValueError("Conta destino inválida.") from exc
    account = FinancialAccount.objects.select_related("owner", "institution").get(id=req.account_id)

    installments = 1 if req.is_recurring else max(1, req.installments)
    monthly_amount = _monthly_amount_for(req.entry_amount, installments, req.calc_mode)
    _validate_common_payload(req, monthly_amount, installments)
    validate_month_not_closed(account, req.due_date)
    validate_month_not_closed(counterparty_account, req.due_date)

    bank_operation = tx.bank_operation
    if bank_operation is None:
        bank_operation = BankOperation.objects.create(
            operation_key=f"{OPERATION_INTERNAL_TRANSFER}-{uuid4().hex}",
            operation_type=OPERATION_INTERNAL_TRANSFER,
            description=(req.description or "")[:255],
            status=req.status,
            installment_total=installments,
            first_due_date=req.due_date,
            last_due_date=req.due_date,
            entry_count=2,
        )

    original_description = f"Conta Destino: {_account_label(counterparty_account)}"
    counterparty_description = f"Conta Origem: {_account_label(account)}"

    _apply_fields(
        tx, req, due_date=req.due_date, monthly_amount=monthly_amount, installments=installments,
        description=original_description, clear_source_entry=True,
    )
    tx.operation_type = OPERATION_INTERNAL_TRANSFER
    tx.current_installment = 1
    tx.bank_operation = bank_operation
    tx.save()

    counterpart = CashFlowEntry.objects.create(
        account=counterparty_account,
        category_id=req.category_id,
        entry_type=_opposite_entry_type(req.entry_type),
        description=counterparty_description,
        entry_amount=monthly_amount,
        installments=installments,
        current_installment=1,
        due_date=req.due_date,
        is_recurring=req.is_recurring,
        status=tx.status,
        realized_date=tx.realized_date,
        realized_amount=tx.realized_amount,
        operation_type=OPERATION_INTERNAL_TRANSFER,
        bank_operation=bank_operation,
        source_entry=tx,
    )
    return [tx, counterpart]


def _replace_current_future_block(
    tx: CashFlowEntry, req: TransactionRequest, entries: list[CashFlowEntry],
    operation_type: str, installments: int, monthly_amount: Decimal,
) -> list[CashFlowEntry]:
    """Substitui deterministicamente o bloco atual/futuro de uma operação
    (apaga e recria, já que os offsets de data mudam com o novo vencimento)."""
    from core.services import log_audit_event
    from reports.services import add_months

    if not entries:
        return []

    start_installment = entries[0].current_installment or 1
    if operation_type == OPERATION_INSTALLMENT and start_installment + len(entries) - 1 > installments:
        raise ValueError("Quantidade de parcelas menor que o bloco atual/futuro selecionado.")

    account = FinancialAccount.objects.get(id=req.account_id)
    bank_operation = tx.bank_operation

    old_ids = [e.id for e in entries]
    for entry in entries:
        log_audit_event("cash_flow_entry", entry.id, "delete", old_values=_snapshot_entry(entry))
    CashFlowEntry.objects.filter(id__in=old_ids).delete()

    rebuilt: list[CashFlowEntry] = []
    for offset in range(len(entries)):
        due_date = add_months(req.due_date, offset)
        validate_month_not_closed(account, due_date)
        current_installment = 1 if operation_type == OPERATION_RECURRING else start_installment + offset
        status = _normalize_open_entry_status(req.status, due_date)
        new_entry = CashFlowEntry.objects.create(
            account=account,
            category_id=req.category_id,
            entry_type=req.entry_type,
            description=(req.description or "")[:255],
            entry_amount=monthly_amount,
            installments=installments,
            current_installment=current_installment,
            due_date=due_date,
            is_recurring=req.is_recurring,
            status=status,
            realized_date=req.realized_date if status == STATUS_REALIZED else None,
            realized_amount=req.realized_amount if status == STATUS_REALIZED else None,
            operation_type=operation_type,
            bank_operation=bank_operation,
        )
        rebuilt.append(new_entry)
    return rebuilt


_CURRENT_FUTURE_CONFIRMATION_SALT = "transactions.current-future-confirmation"
_CURRENT_FUTURE_CONFIRMATION_MAX_AGE = 10 * 60


def current_future_confirmation_token(entry_id: int) -> str:
    """Gera um token curto, assinado e vinculado ao lançamento exibido.

    A confirmação visual continua sendo responsabilidade do navegador, mas a
    gravação não pode depender apenas de JavaScript. O token faz com que um
    POST direto sem a confirmação concluída seja rejeitado pelo service e
    impede reutilizar o token para outro lançamento ou escopo.
    """
    return dumps(
        {"entry_id": entry_id, "scope": OPERATION_SCOPE_CURRENT_FUTURE},
        salt=_CURRENT_FUTURE_CONFIRMATION_SALT,
        compress=True,
    )


def _assert_current_future_confirmation(entry_id: int, token: str | None) -> None:
    if not token:
        raise ValueError(
            "Confirme explicitamente a alteração deste registro e dos próximos."
        )
    try:
        payload = loads(
            token,
            salt=_CURRENT_FUTURE_CONFIRMATION_SALT,
            max_age=_CURRENT_FUTURE_CONFIRMATION_MAX_AGE,
        )
    except (BadSignature, SignatureExpired) as exc:
        raise ValueError("A confirmação da alteração expirou. Reabra o formulário e confirme novamente.") from exc
    if payload.get("entry_id") != entry_id or payload.get("scope") != OPERATION_SCOPE_CURRENT_FUTURE:
        raise ValueError("A confirmação não corresponde ao lançamento selecionado.")


def _update_installment_or_recurring(
    tx: CashFlowEntry, req: TransactionRequest, entries: list[CashFlowEntry], scope: str,
) -> list[CashFlowEntry]:
    from reports.services import add_months

    operation_type = tx.operation_type or OPERATION_INSTALLMENT
    installments = 1 if operation_type == OPERATION_RECURRING else max(1, req.installments)
    monthly_amount = _monthly_amount_for(req.entry_amount, installments, req.calc_mode)
    _validate_common_payload(req, monthly_amount, installments)

    ordered = sorted(scoped_entries(tx, entries, scope), key=lambda e: (e.current_installment or 1, e.due_date, e.id))
    if not ordered:
        return []

    if scope == OPERATION_SCOPE_CURRENT_FUTURE:
        return _replace_current_future_block(tx, req, ordered, operation_type, installments, monthly_amount)

    account = FinancialAccount.objects.get(id=req.account_id)
    for offset, entry in enumerate(ordered):
        due_date = add_months(req.due_date, offset)
        validate_month_not_closed(account, due_date)
        if scope == OPERATION_SCOPE_ALL:
            entry.current_installment = offset + 1 if operation_type == OPERATION_INSTALLMENT else 1
        _apply_fields(entry, req, due_date=due_date, monthly_amount=monthly_amount, installments=installments)
        entry.operation_type = operation_type
        entry.save()
    return ordered


def _update_internal_transfer(
    tx: CashFlowEntry, req: TransactionRequest, entries: list[CashFlowEntry], scope: str,
) -> list[CashFlowEntry]:
    from reports.services import add_months

    category = CashFlowCategory.objects.get(id=req.category_id)
    if not category.is_internal:
        raise ValueError("Transferência interna exige categoria interna.")

    counterparty_account_id = req.counterparty_account_id or _get_counterparty_account_id(tx)
    if not counterparty_account_id:
        raise ValueError("Conta destino é obrigatória para categoria interna.")
    if counterparty_account_id == req.account_id:
        raise ValueError("Conta destino deve ser diferente da conta de origem.")

    installments = 1 if req.is_recurring else max(1, req.installments)
    monthly_amount = _monthly_amount_for(req.entry_amount, installments, req.calc_mode)
    _validate_common_payload(req, monthly_amount, installments)

    account = FinancialAccount.objects.select_related("owner", "institution").get(id=req.account_id)
    counterparty_account = FinancialAccount.objects.select_related("owner", "institution").get(
        id=counterparty_account_id
    )
    original_description = f"Conta Destino: {_account_label(counterparty_account)}"
    counterparty_description = f"Conta Origem: {_account_label(account)}"

    pairs = _origin_counterparty_pairs(scoped_entries(tx, entries, scope))
    updated: list[CashFlowEntry] = []
    for offset, (origin, counterpart) in enumerate(pairs):
        preserve_future_realization = (
            req.is_recurring and scope == OPERATION_SCOPE_CURRENT_FUTURE
            and req.status == STATUS_REALIZED and offset > 0
        )
        original_state = (
            origin.status, origin.realized_date, origin.realized_amount,
            counterpart.status, counterpart.realized_date, counterpart.realized_amount,
        )
        due_date = add_months(req.due_date, offset)
        validate_month_not_closed(account, due_date)
        validate_month_not_closed(counterparty_account, due_date)

        if req.is_recurring:
            current_installment = 1
        elif installments > 1 and scope == OPERATION_SCOPE_ALL:
            current_installment = offset + 1
        else:
            current_installment = origin.current_installment or 1
        origin.current_installment = current_installment
        counterpart.current_installment = current_installment

        _apply_fields(
            origin, req, due_date=due_date, monthly_amount=monthly_amount, installments=installments,
            description=original_description, entry_type=req.entry_type, account_id=account.id,
            clear_source_entry=True,
        )
        _apply_fields(
            counterpart, req, due_date=due_date, monthly_amount=monthly_amount, installments=installments,
            description=counterparty_description, entry_type=_opposite_entry_type(req.entry_type),
            account_id=counterparty_account.id, source_entry=origin,
        )
        origin.operation_type = OPERATION_INTERNAL_TRANSFER
        counterpart.operation_type = OPERATION_INTERNAL_TRANSFER
        origin.bank_operation = tx.bank_operation
        counterpart.bank_operation = tx.bank_operation

        if preserve_future_realization:
            (
                origin.status, origin.realized_date, origin.realized_amount,
                counterpart.status, counterpart.realized_date, counterpart.realized_amount,
            ) = original_state

        origin.save()
        counterpart.save()
        updated.extend([origin, counterpart])
    return updated


@db_transaction.atomic
def update_transaction_operation(
    tx: CashFlowEntry, req: TransactionRequest, operation_scope: str = OPERATION_SCOPE_ALL,
    current_future_confirmation_token: str | None = None,
) -> list[CashFlowEntry]:
    """Atualiza um lançamento (ou o grupo ao qual pertence) respeitando o
    escopo escolhido: `all`, `single` ou `current_future`."""
    from core.services import log_audit_event

    entries = operation_entries(tx)
    if operation_scope == OPERATION_SCOPE_CURRENT_FUTURE:
        _assert_current_future_confirmation(tx.id, current_future_confirmation_token)
    for entry in scoped_entries(tx, entries, operation_scope):
        assert_entry_period_open(entry)
    old_snapshots = {entry.id: _snapshot_entry(entry) for entry in entries}
    operation_type = tx.operation_type or OPERATION_SINGLE

    if operation_type == OPERATION_INTERNAL_TRANSFER:
        updated = _update_internal_transfer(tx, req, entries, operation_scope)
    elif operation_type in (OPERATION_INSTALLMENT, OPERATION_RECURRING):
        updated = _update_installment_or_recurring(tx, req, entries, operation_scope)
    else:
        updated = _update_single(tx, req)

    for entry in updated:
        assert_entry_period_open(entry)

    for bank_operation_id in {entry.bank_operation_id for entry in updated if entry.bank_operation_id}:
        _sync_bank_operation_status(bank_operation_id)

    for entry in updated:
        log_audit_event(
            "cash_flow_entry", entry.id, "update",
            old_values=old_snapshots.get(entry.id), new_values=_snapshot_entry(entry),
        )
    return updated


# --- Exclusão ----------------------------------------------------------------


@db_transaction.atomic
def delete_transaction_or_operation(
    tx: CashFlowEntry,
    operation_scope: str = OPERATION_SCOPE_ALL,
    current_future_confirmation_token: str | None = None,
) -> int:
    """Exclui um lançamento (ou o grupo/bloco escolhido pelo escopo)."""
    from core.services import log_audit_event

    if operation_scope == OPERATION_SCOPE_CURRENT_FUTURE:
        _assert_current_future_confirmation(tx.id, current_future_confirmation_token)
    all_entries = operation_entries(tx) if tx.bank_operation_id else [tx]
    scoped = scoped_entries(tx, all_entries, operation_scope)
    operation_type = tx.operation_type or OPERATION_SINGLE
    if operation_type == OPERATION_INTERNAL_TRANSFER:
        scoped = with_transfer_counterparts(scoped, all_entries)

    for entry in scoped:
        assert_entry_period_open(entry)

    deleted_ids = {entry.id for entry in scoped}
    remaining: list[CashFlowEntry] = []
    if operation_type in (OPERATION_INSTALLMENT, OPERATION_INTERNAL_TRANSFER):
        remaining = [entry for entry in all_entries if entry.id not in deleted_ids]

    try:
        from bank_statements.models import EntryAttachment

        EntryAttachment.objects.filter(entry_id__in=deleted_ids).delete()
    except Exception:  # pragma: no cover - app pode não estar instalado em algum contexto
        pass

    count = len(scoped)
    for entry in scoped:
        log_audit_event("cash_flow_entry", entry.id, "delete", old_values=_snapshot_entry(entry))

    bank_operation_id = tx.bank_operation_id
    if operation_type == OPERATION_INTERNAL_TRANSFER:
        counterparts = [e for e in scoped if e.source_entry_id is not None]
        origins = [e for e in scoped if e.source_entry_id is None]
        for entry in counterparts:
            entry.delete()
        for entry in origins:
            entry.delete()
    else:
        for entry in scoped:
            entry.delete()

    renumber_remaining_installments(operation_type, remaining)

    if bank_operation_id:
        remaining_count = CashFlowEntry.objects.filter(bank_operation_id=bank_operation_id).count()
        if remaining_count == 0:
            BankOperation.objects.filter(id=bank_operation_id).delete()
        else:
            BankOperation.objects.filter(id=bank_operation_id).update(
                entry_count=remaining_count, updated_at=timezone.now()
            )
            _sync_bank_operation_status(bank_operation_id)

    return count


# --- Consulta para a tela de Transações --------------------------------------


def list_transactions_for_view(
    *, account_ids: list[int], view_mode: str, start_selected: date, end_selected: date,
    filter_type: str = "", filter_category: str = "", filter_date: date | None = None,
    operation_key: str = "", entry_id: int | None = None, exclude_internal: bool = False,
) -> list[CashFlowEntry]:
    """Consulta de lançamentos para a listagem: filtros por tipo, categoria,
    data, operação e entrada específica, mais a matriz de status/data por
    `view_mode`.

    Usa a mesma "listing mode" do motor de projeções (reports/services.py) de
    propósito: se as duas divergirem, a listagem e o relatório passam a
    discordar sobre quais lançamentos compõem o mesmo período."""
    from core.services import system_start_date
    from reports.services import _listing_date_expr, _listing_status_q

    if not account_ids:
        return []

    today = date.today()
    minimum_date = system_start_date() or date.min

    qs = (
        CashFlowEntry.objects.select_related("account__owner", "account__institution", "category", "bank_operation")
        .annotate(proj_date=_listing_date_expr(view_mode))
        .filter(account_id__in=account_ids)
    )
    if filter_type:
        qs = qs.filter(entry_type=filter_type)
    if filter_category:
        qs = qs.filter(category__category_name=filter_category)
    if exclude_internal:
        qs = qs.filter(category__is_internal=False)
    if filter_date is not None:
        qs = qs.filter(proj_date=filter_date)
    if operation_key:
        qs = qs.filter(bank_operation__operation_key=operation_key)
    if entry_id:
        qs = qs.filter(id=entry_id)
    if not entry_id:
        qs = qs.filter(proj_date__gte=minimum_date)
    if not operation_key and not entry_id:
        qs = qs.filter(
            proj_date__gte=max(start_selected, minimum_date), proj_date__lte=end_selected,
        ).filter(_listing_status_q(view_mode, today))

    return list(qs.order_by("proj_date", "-entry_type", "category__category_name", "id"))


def list_category_names(*, include_internal: bool = True) -> list[str]:
    qs = CashFlowCategory.objects.all()
    if not include_internal:
        qs = qs.filter(is_internal=False)
    return list(qs.order_by("category_name").values_list("category_name", flat=True))


def creatable_accounts_for_user(user):
    from accounts.services import accessible_owner_ids

    owner_ids = accessible_owner_ids(user, "create")
    return list(
        FinancialAccount.objects.select_related("owner", "institution")
        .filter(owner_id__in=owner_ids)
        .order_by("owner__name", "institution__institution_name", "account_name")
    )


def updatable_accounts_for_user(user):
    from accounts.services import accessible_owner_ids

    owner_ids = accessible_owner_ids(user, "update")
    return list(
        FinancialAccount.objects.select_related("owner", "institution")
        .filter(owner_id__in=owner_ids)
        .order_by("owner__name", "institution__institution_name", "account_name")
    )


def counterparty_accounts_for_transfer():
    """Contas elegíveis como destino de transferência interna: todas as
    contas do sistema, sem restrição de titular (ver `transactions.access`:
    a autorização de uma transferência é dada pela conta de origem)."""
    return list(
        FinancialAccount.objects.select_related("owner", "institution").order_by(
            "owner__name", "institution__institution_name", "account_name"
        )
    )


# --- Contexto da tela "Transações" (listagem + filtros + resumo) ------------

TRANSACTIONS_QUERY_PARAMS = (
    "period", "year", "month", "mode", "owner_id", "institution_id", "account_id",
    "filter_type", "filter_category", "filter_date", "operation_id", "entry_id",
    "dashboard_drilldown",
)


def _parse_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def transactions_query_params(get_params) -> dict[str, str]:
    params = {}
    for key in TRANSACTIONS_QUERY_PARAMS:
        value = get_params.get(key)
        if value:
            params[key] = value
    return params


def _entry_date_for_view_mode(entry: CashFlowEntry, view_mode: str):
    from core.domain.finance import VIEW_ALL, VIEW_REALIZED

    if view_mode == VIEW_REALIZED or (view_mode == VIEW_ALL and entry.status == STATUS_REALIZED):
        return entry.realized_date
    return entry.due_date


def _mark_last_of_day(current_txs: list[CashFlowEntry], filters_active: bool) -> None:
    if filters_active:
        for tx in current_txs:
            tx.is_last_of_day = True
        return
    last_date = None
    for tx in reversed(current_txs):
        if tx.display_date != last_date:
            tx.is_last_of_day = True
            last_date = tx.display_date
        else:
            tx.is_last_of_day = False


def _new_entry_defaults(get_params, today: date) -> dict[str, str]:
    from core.services import system_start_date
    from reports.services import parse_iso_date

    floor_date = system_start_date()
    default_due_date = today.isoformat()
    if floor_date and floor_date > today:
        default_due_date = floor_date.isoformat()
    requested_due_date = get_params.get("new_due_date") or default_due_date
    parsed = parse_iso_date(requested_due_date)
    if parsed is None or (floor_date and parsed < floor_date):
        requested_due_date = default_due_date
    return {
        "account_id": get_params.get("new_account_id", ""),
        "entry_type": get_params.get("new_entry_type", ENTRY_TYPE_EXPENSE),
        "status": get_params.get("new_status", STATUS_PROJECTED),
        "due_date": requested_due_date,
    }


def build_transactions_view_context(user, get_params, session) -> dict:
    """Monta o contexto completo da tela Movimentação > Lançamentos.

    Resolve período/mês, `view_mode`, filtros por coluna, saldo corrente por
    linha (`running_balance`), totais de resumo e as opções dos seletores de
    filtro e formulário.

    Concentrar isso aqui mantém a view fina e garante que o `running_balance`
    seja calculado sobre exatamente o mesmo recorte que a tela exibe.
    """
    from core.domain.finance import VIEW_REALIZED, normalize_view_mode
    from core.services import system_start_date
    from reports import services as report_services

    today = date.today()
    year_param = get_params.get("year")
    month_param = get_params.get("month")
    year_value = _parse_int(year_param) if year_param else session.get("tx_sel_year", today.year)
    month_value = _parse_int(month_param) if month_param else session.get("tx_sel_month", today.month)
    year, month = report_services.resolve_month_period(get_params.get("period"), year_value, month_value, today)
    session["tx_sel_year"] = year
    session["tx_sel_month"] = month

    view_mode = normalize_view_mode(get_params.get("mode", STATUS_PROJECTED))
    filter_type = get_params.get("filter_type", "")
    filter_category = get_params.get("filter_category", "")
    filter_date_raw = get_params.get("filter_date", "")
    operation_key = get_params.get("operation_id", "")
    entry_id = _parse_int(get_params.get("entry_id"))
    if entry_id is not None and entry_id <= 0:
        entry_id = None
    dashboard_drilldown = get_params.get("dashboard_drilldown") == "1"

    minimum_date = system_start_date()
    filter_date_parsed = report_services.parse_iso_date(filter_date_raw) if filter_date_raw else None
    if filter_date_raw and (filter_date_parsed is None or (minimum_date and filter_date_parsed < minimum_date)):
        filter_date_raw = ""
        filter_date_parsed = None

    ctx = report_services.selected_context(user, get_params)
    options = report_services.context_options(user, ctx)
    account_ids = options.account_ids

    start_selected = report_services.enforce_system_start_month(date(year, month, 1))
    _month_start, _next_month_start = report_services.month_bounds(year, month)
    end_selected = _next_month_start - timedelta(days=1)
    if minimum_date and end_selected < minimum_date:
        end_selected = minimum_date

    current_txs = list_transactions_for_view(
        account_ids=account_ids, view_mode=view_mode, start_selected=start_selected, end_selected=end_selected,
        filter_type=filter_type, filter_category=filter_category, filter_date=filter_date_parsed,
        operation_key=operation_key, entry_id=entry_id, exclude_internal=dashboard_drilldown,
    )

    counterparty_map = counterparty_account_map(current_txs)
    attachment_loss_map = current_future_attachment_counts(current_txs)
    for tx in current_txs:
        tx.display_date = _entry_date_for_view_mode(tx, view_mode)
        tx.counterparty_account_id = counterparty_map.get(tx.id)
        tx.supports_scope = supports_operation_scope(tx)
        tx.current_future_attachment_count = attachment_loss_map.get(tx.id, 0)
        tx.current_future_confirmation_token = current_future_confirmation_token(tx.id)

    end_exclusive = end_selected + timedelta(days=1)
    saldo_inicial = report_services.decimal_period_start_balance(account_ids, start_selected, end_exclusive, view_mode)
    balance_entries = report_services.entries_for_period(account_ids, start_selected, end_exclusive, view_mode)

    running_for_balance = saldo_inicial
    running_by_entry_id: dict[int, Decimal] = {}
    for tx in balance_entries:
        if view_mode in {STATUS_PROJECTED, STATUS_PENDING} and tx.status == STATUS_REALIZED:
            continue
        realized = tx.status == STATUS_REALIZED
        val = report_services.to_decimal(tx.realized_amount if realized and tx.realized_amount is not None else tx.entry_amount)
        if tx.entry_type == ENTRY_TYPE_INCOME:
            running_for_balance += val
        else:
            running_for_balance -= val
        running_by_entry_id[tx.id] = running_for_balance.quantize(MONEY_QUANT)

    total_receitas = Decimal("0.00")
    total_despesas = Decimal("0.00")
    total_movimentacoes_internas = Decimal("0.00")
    visible_running = saldo_inicial.quantize(MONEY_QUANT)

    for tx in current_txs:
        realized_for_mode = view_mode == VIEW_REALIZED or tx.status == STATUS_REALIZED
        val = report_services.to_decimal(
            tx.realized_amount if realized_for_mode and tx.realized_amount is not None else tx.entry_amount
        )
        is_internal = bool(tx.category and tx.category.is_internal)
        if tx.entry_type == ENTRY_TYPE_INCOME:
            if is_internal:
                total_movimentacoes_internas += val
            else:
                total_receitas += val
        else:
            if is_internal:
                total_movimentacoes_internas -= val
            else:
                total_despesas += val
        visible_running = running_by_entry_id.get(tx.id, visible_running)
        tx.running_balance = visible_running

    filters_active = bool(
        filter_date_raw or filter_type or filter_category or operation_key or entry_id or dashboard_drilldown
    )
    _mark_last_of_day(current_txs, filters_active)

    available_types = sorted({tx.entry_type for tx in current_txs})
    available_dates = sorted({
        _entry_date_for_view_mode(tx, view_mode).isoformat()
        for tx in current_txs if _entry_date_for_view_mode(tx, view_mode)
    })
    available_categories = list_category_names(include_internal=not dashboard_drilldown)

    saldo_final = running_for_balance.quantize(MONEY_QUANT)

    return {
        "txs": current_txs,
        "today": today,
        "total_receitas": total_receitas.quantize(MONEY_QUANT),
        "total_despesas": total_despesas.quantize(MONEY_QUANT),
        "total_movimentacoes_internas": total_movimentacoes_internas.quantize(MONEY_QUANT),
        "geracao_caixa": (total_receitas - total_despesas).quantize(MONEY_QUANT),
        "saldo_inicial": saldo_inicial.quantize(MONEY_QUANT),
        "saldo_final": saldo_final,
        "selected_period": report_services.month_input_value(start_selected),
        "current_period": report_services.month_input_value(date(today.year, today.month, 1)),
        "view_mode": view_mode,
        "filter_type": filter_type,
        "filter_category": filter_category,
        "filter_date": filter_date_raw,
        "operation_id": operation_key,
        "entry_id": entry_id,
        "dashboard_drilldown": dashboard_drilldown,
        "available_types": available_types,
        "available_categories": available_categories,
        "available_dates": available_dates,
        "categories": list(CashFlowCategory.objects.order_by("category_name")),
        "owners": options.owners,
        "banks": options.institutions,
        "accounts": options.accounts,
        "create_accounts": creatable_accounts_for_user(user),
        "update_accounts": updatable_accounts_for_user(user),
        "counterparty_accounts": counterparty_accounts_for_transfer(),
        "current_owner_id": ctx.owner_id,
        "current_institution_id": ctx.institution_id,
        "current_account_id": ctx.account_id,
        "current_query_params": transactions_query_params(get_params),
        "new_entry_open": get_params.get("new_entry_open") == "1",
        "new_entry_defaults": _new_entry_defaults(get_params, today),
        "system_start_date": minimum_date.isoformat() if minimum_date else "",
    }
