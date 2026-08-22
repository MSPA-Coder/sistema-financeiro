"""Projeção dinâmica de lançamentos recorrentes (Configurações > Parâmetros).

Estende, até um horizonte configurável, as ocorrências futuras de cada
lançamento recorrente. O agrupamento é por `bank_operation_id`.

A execução é idempotente, e **não há** guarda de "já rodou este mês" -- as
duas coisas juntas, porque a segunda existia e foi removida em 2026-08-22.

Idempotente porque `_extend_operation` só olha para frente: parte da MAIOR
data existente do grupo e preenche até o horizonte, pulando data que já
existe. Três consequências, todas testadas em
`tests/test_projecao_recorrente_idempotente.py`:

- reexecutar no mesmo mês gera zero, porque o horizonte não se move dentro do
  mês e as ocorrências já alcançam o horizonte;
- ocorrência apagada à mão no meio da série NÃO ressuscita, porque o
  preenchimento nunca volta antes da maior data;
- num mês novo o horizonte avança, e aí reexecutar gera o que falta -- que é
  o objetivo.

A guarda removida lia `confirm_current_month` do POST, e o template mandava
esse campo FIXO em `value="on"`: nunca podia reprovar. Não foi "consertada"
porque consertá-la seria pior. Ela obstruiria o caminho legítimo: depois de
aumentar o horizonte na mesma tela, reexecutar é exatamente o que se quer, e
o usuário levaria um "já executada no mês atual" no lugar do resultado. Numa
operação que não duplica nada, pedir confirmação treina a clicar "sim" sem
ler -- ver a regra da Fase 9 em `_manutencao/PLANO_SINAL_E_DEFEITOS.md`.

O disparo é manual, pela tela de Parâmetros. O `generated_count` da mensagem
diz ao usuário quanto foi criado; num clique redundante ele lê "0", que é a
informação certa.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby

from django.db import connection, transaction

from core.domain.finance import (
    OPERATION_INSTALLMENT,
    OPERATION_RECURRING,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from core.services import (
    get_recurring_projection_settings,
    upsert_app_setting,
)
from reports.services import add_months

from .models import CashFlowEntry


@dataclass(frozen=True)
class RecurringProjectionResult:
    horizon_end: date
    generated_count: int
    processed_operations: int


def recurring_projection_horizon_end(today: date | None = None, horizon_months: int | None = None) -> date:
    base_date = today or date.today()
    months = horizon_months if horizon_months is not None else get_recurring_projection_settings().horizon_months
    current_month_start = date(base_date.year, base_date.month, 1)
    horizon_month_start = add_months(current_month_start, months)
    return add_months(horizon_month_start, 1) - timedelta(days=1)


def _projected_status(template: CashFlowEntry, due_date: date, today: date) -> str:
    if template.status == STATUS_REALIZED:
        return STATUS_PROJECTED
    if template.status == STATUS_PENDING and due_date >= today:
        return STATUS_PROJECTED
    return template.status or STATUS_PROJECTED


def _copy_occurrence(template_rows: list[CashFlowEntry], due_date: date, today: date) -> int:
    created_by_template_id: dict[int, CashFlowEntry] = {}
    for template in template_rows:
        entry = CashFlowEntry.objects.create(
            account_id=template.account_id,
            category_id=template.category_id,
            entry_type=template.entry_type,
            description=template.description,
            entry_amount=template.entry_amount,
            installments=1,
            current_installment=1,
            due_date=due_date,
            is_recurring=True,
            status=_projected_status(template, due_date, today),
            realized_date=None,
            realized_amount=None,
            bank_operation_id=template.bank_operation_id,
            operation_type=template.operation_type or OPERATION_RECURRING,
        )
        created_by_template_id[template.id] = entry

    for template in template_rows:
        if template.source_entry_id:
            created = created_by_template_id[template.id]
            source = created_by_template_id.get(template.source_entry_id)
            if source:
                created.source_entry = source
                created.save(update_fields=["source_entry"])
    return len(created_by_template_id)


def _extend_operation(entries: list[CashFlowEntry], horizon_end: date, today: date) -> int:
    existing_dates = {entry.due_date for entry in entries}
    latest_due_date = max(existing_dates)
    if latest_due_date >= horizon_end:
        return 0

    template_rows = [entry for entry in entries if entry.due_date == latest_due_date]
    next_due_date = add_months(latest_due_date, 1)
    generated_count = 0
    while next_due_date <= horizon_end:
        if next_due_date not in existing_dates:
            generated_count += _copy_occurrence(template_rows, next_due_date, today)
            existing_dates.add(next_due_date)
        next_due_date = add_months(next_due_date, 1)
    return generated_count


@transaction.atomic
def ensure_recurring_projection_horizon(
    *,
    today: date | None = None,
    horizon_months: int | None = None,
    update_last_run: bool = True,
) -> RecurringProjectionResult:
    base_date = today or date.today()
    horizon_end = recurring_projection_horizon_end(base_date, horizon_months)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(81288431)")

    all_entries = list(
        CashFlowEntry.objects.filter(
            is_recurring=True,
            bank_operation__isnull=False,
        )
        .exclude(operation_type=OPERATION_INSTALLMENT)
        .order_by("bank_operation_id", "due_date", "id")
    )

    generated_count = 0
    processed_operations = 0
    for _bank_operation_id, group in groupby(all_entries, key=lambda e: e.bank_operation_id):
        entries = list(group)
        generated_count += _extend_operation(entries, horizon_end, base_date)
        processed_operations += 1

    if update_last_run:
        from core.domain.settings import APP_SETTING_LAST_PROJECTION_RUN
        upsert_app_setting(APP_SETTING_LAST_PROJECTION_RUN, datetime.now().isoformat(timespec="seconds"))

    return RecurringProjectionResult(
        horizon_end=horizon_end,
        generated_count=generated_count,
        processed_operations=processed_operations,
    )
