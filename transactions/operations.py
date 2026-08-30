"""Movimentação > Lançamentos n+1 (Operações): agrupa lançamentos compostos
(parcelados, recorrentes ou pares de transferência interna) por operação.

O agrupamento é feito por `bank_operation_id`: `BankOperation` é uma entidade
real, criada por `create_transaction_batch` (e reaproveitada pelos caminhos de
edição), e o identificador exibido e filtrável é `BankOperation.operation_key`.
As duas funções que este comentário citava antes -- `create_installment_entries`
e `create_internal_transfer` -- nunca foram chamadas por nada e saíram em
29/08.

`BankOperation.legacy_operation_id` é apenas um campo legado: não é chave de
agrupamento nem deve ser usado em consultas.

Só entram aqui os tipos listados em COMPOSITE_OPERATION_TYPES.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Max

from accounts.services import accessible_owner_ids
from core.domain.finance import COMPOSITE_OPERATION_TYPES, OPERATION_INTERNAL_TRANSFER
from core.services import system_start_date

from .models import CashFlowEntry

OPERATION_LABELS = {
    "installment": "Parcelas",
    "recurring": "Recorrência",
    OPERATION_INTERNAL_TRANSFER: "Transferência interna",
}

DEFAULT_PAGE_SIZE = 20


@dataclass
class OperationSummary:
    operation_id: str
    operation_type: str
    operation_label: str
    description: str
    origin_account: str
    destination_account: str
    category: str
    start_date: date
    end_date: date
    entries_count: int
    total_amount: Decimal
    status_summary: str
    entries: list[CashFlowEntry]


def _account_label(account) -> str:
    if not account:
        return "-"
    owner = account.owner.name if account.owner else "-"
    institution = account.institution.institution_name if account.institution else "-"
    return f"{owner} / {institution} / {account.account_name}"


def _status_summary(entries: Iterable[CashFlowEntry]) -> str:
    statuses = sorted({entry.status or "-" for entry in entries})
    if len(statuses) == 1:
        return statuses[0].capitalize()
    return "Misto: " + ", ".join(statuses)


def _base_description(entries: list[CashFlowEntry]) -> str:
    for entry in entries:
        description = (entry.description or "").strip()
        if description:
            return description
    return "-"


def _operation_total(entries: list[CashFlowEntry]) -> Decimal:
    if entries[0].operation_type == OPERATION_INTERNAL_TRANSFER:
        return max((entry.entry_amount for entry in entries), default=Decimal("0.00"))
    return sum((entry.entry_amount for entry in entries), Decimal("0.00"))


def _build_summary(operation_key: str, entries: list[CashFlowEntry]) -> OperationSummary:
    entries = sorted(entries, key=lambda entry: (entry.due_date, entry.id))
    operation_type = entries[0].operation_type or "single"

    accounts = []
    seen_account_ids = set()
    for entry in entries:
        if entry.account_id not in seen_account_ids:
            accounts.append(entry.account)
            seen_account_ids.add(entry.account_id)

    category_names = []
    seen_categories = set()
    for entry in entries:
        category_name = entry.category.category_name if entry.category else "-"
        if category_name not in seen_categories:
            category_names.append(category_name)
            seen_categories.add(category_name)

    return OperationSummary(
        operation_id=operation_key,
        operation_type=operation_type,
        operation_label=OPERATION_LABELS.get(operation_type, operation_type or "-"),
        description=_base_description(entries),
        origin_account=_account_label(accounts[0] if accounts else None),
        destination_account=_account_label(accounts[1] if len(accounts) > 1 else None),
        category=", ".join(category_names) if category_names else "-",
        start_date=entries[0].due_date,
        end_date=entries[-1].due_date,
        entries_count=len(entries),
        total_amount=_operation_total(entries),
        status_summary=_status_summary(entries),
        entries=entries,
    )


@dataclass
class OperationsPage:
    operations: list[OperationSummary]
    total_operations: int
    system_start_date: date | None


def operations_page_for_user(
    user,
    *,
    operation_type: str = "",
    status: str = "",
    start: str = "",
    end: str = "",
    operation_id: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> OperationsPage:
    minimum_date = system_start_date()

    owner_ids = accessible_owner_ids(user, "view")
    if not owner_ids:
        return OperationsPage(operations=[], total_operations=0, system_start_date=minimum_date)

    base_qs = CashFlowEntry.objects.filter(
        account__owner_id__in=owner_ids,
        bank_operation__isnull=False,
        operation_type__in=COMPOSITE_OPERATION_TYPES,
    )
    if operation_id:
        base_qs = base_qs.filter(bank_operation__operation_key=operation_id)
    if operation_type:
        base_qs = base_qs.filter(operation_type=operation_type)
    if status:
        base_qs = base_qs.filter(status=status)
    if start:
        base_qs = base_qs.filter(due_date__gte=start)
    if end:
        base_qs = base_qs.filter(due_date__lte=end)
    if minimum_date:
        base_qs = base_qs.filter(due_date__gte=minimum_date)

    grouped = (
        base_qs.values("bank_operation_id")
        .annotate(max_due=Max("due_date"))
        .order_by("-max_due", "-bank_operation_id")
    )
    total_operations = grouped.count()

    offset = max(page - 1, 0) * page_size
    page_operation_ids = [
        row["bank_operation_id"] for row in grouped[offset:offset + page_size]
    ]
    if not page_operation_ids:
        return OperationsPage(operations=[], total_operations=total_operations, system_start_date=minimum_date)

    entries = list(
        CashFlowEntry.objects.select_related(
            "account__owner", "account__institution", "category", "bank_operation"
        ).filter(bank_operation_id__in=page_operation_ids).order_by("due_date", "id")
    )
    grouped_entries: dict[int, list[CashFlowEntry]] = {}
    for entry in entries:
        grouped_entries.setdefault(entry.bank_operation_id, []).append(entry)

    operations = [
        _build_summary(grouped_entries[bank_operation_id][0].bank_operation.operation_key, grouped_entries[bank_operation_id])
        for bank_operation_id in page_operation_ids
        if bank_operation_id in grouped_entries
    ]
    return OperationsPage(operations=operations, total_operations=total_operations, system_start_date=minimum_date)
