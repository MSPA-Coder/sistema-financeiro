"""Serviços de relatórios: projeções, movimentos futuros e posição por conta.

O domínio tem duas matrizes de filtro distintas, e confundi-las é a origem
mais provável de um relatório que "quase" fecha:

- "balance mode" (`_balance_*`): usada para calcular saldos (abertura,
  fechamento, série diária). Segue a matriz de domínio:
  realizado = só realizado; vencidos = realizado + vencidos/a_vencer vencidos;
  a_vencer = realizado + a_vencer futuro.
- "listing mode" (estratégias de projeção): usada para listar lançamentos que
  compõem cada bucket mensal, evitando contar duas vezes o que já entrou no
  saldo de abertura.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncMonth

from accounts.models import AccountOwner
from accounts.services import accessible_owner_ids, hidden_account_ids
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ENTRY_TYPE_INCOME,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_ALL,
    VIEW_PENDING,
    VIEW_PROJECTED,
    VIEW_REALIZED,
)
from core.services import system_start_date
from transactions.models import CashFlowEntry

MONEY_QUANT = Decimal("0.01")
_AMOUNT_FIELD: DecimalField = DecimalField(max_digits=14, decimal_places=2)


def to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ---------------------------------------------------------------------------
# Datas e períodos
# ---------------------------------------------------------------------------

def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    return start, add_months(start, 1)


def month_input_value(month_start: date) -> str:
    return month_start.strftime("%Y-%m")


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_month_input(value: str | None) -> date | None:
    if not value:
        return None
    try:
        year_raw, month_raw = value.split("-", 1)
        year = int(year_raw)
        month = int(month_raw)
        if not 1 <= month <= 12:
            return None
        return date(year, month, 1)
    except (TypeError, ValueError):
        return None


def current_week_period(today: date | None = None) -> tuple[date, date]:
    base = today or date.today()
    start = base - timedelta(days=base.weekday())
    return start, start + timedelta(days=6)


def enforce_system_start_month(month_start: date) -> date:
    floor_date = system_start_date()
    if floor_date is None:
        return month_start
    floor_month = date(floor_date.year, floor_date.month, 1)
    return max(month_start, floor_month)


def resolve_month_period(period_value: str | None, year_value: int | None, month_value: int | None, today: date) -> tuple[int, int]:
    period_month = parse_month_input(period_value)
    if period_month:
        resolved = enforce_system_start_month(period_month)
        return resolved.year, resolved.month
    year = year_value or today.year
    month = month_value or today.month
    if not 1 <= month <= 12:
        month = today.month
    resolved = enforce_system_start_month(date(year, month, 1))
    return resolved.year, resolved.month


MAX_PROJECTION_RANGE_MONTHS = 120
MAX_PROJECTION_END_MONTH = date(9999, 11, 1)


def bounded_projection_month_range(start_month: date, end_month: date) -> tuple[date, date]:
    """Normaliza e limita intervalos para evitar trabalho e datas sem limite."""
    start_month = date(start_month.year, start_month.month, 1)
    end_month = date(end_month.year, end_month.month, 1)
    if end_month < start_month:
        start_month, end_month = end_month, start_month
    start_month = min(start_month, MAX_PROJECTION_END_MONTH)
    end_month = min(end_month, MAX_PROJECTION_END_MONTH)
    month_span = (end_month.year - start_month.year) * 12 + end_month.month - start_month.month + 1
    if month_span > MAX_PROJECTION_RANGE_MONTHS:
        end_month = add_months(start_month, MAX_PROJECTION_RANGE_MONTHS - 1)
    return start_month, end_month


def default_projection_month_range(today: date | None = None) -> tuple[date, date]:
    base_date = today or date.today()
    current_month = date(base_date.year, base_date.month, 1)
    return add_months(current_month, -6), add_months(current_month, 6)


def resolve_projection_month_range(start_value: str | None, end_value: str | None, *, today: date | None = None) -> tuple[date, date]:
    default_start, default_end = default_projection_month_range(today)
    start_month = parse_month_input(start_value) or default_start
    end_month = parse_month_input(end_value) or default_end
    start_month, end_month = bounded_projection_month_range(start_month, end_month)
    start_month = enforce_system_start_month(start_month)
    end_month = enforce_system_start_month(end_month)
    if end_month < start_month:
        end_month = start_month
    return bounded_projection_month_range(start_month, end_month)


# ---------------------------------------------------------------------------
# Filtros de acesso (owner/instituicao/conta) por usuario
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinancialContext:
    owner_id: int | None
    institution_id: int | None
    account_id: int | None


@dataclass(frozen=True)
class ContextOptions:
    owners: list[AccountOwner]
    institutions: list[FinancialInstitution]
    accounts: list[FinancialAccount]
    account_ids: list[int]


def selected_context(user, params) -> FinancialContext:
    def _int(name: str) -> int | None:
        raw = params.get(name)
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    owner_id = _int("owner_id")
    institution_id = _int("institution_id")
    account_id = _int("account_id")

    allowed_owner_ids = set(accessible_owner_ids(user))
    if owner_id is not None and owner_id not in allowed_owner_ids:
        owner_id = None

    if account_id is not None:
        account = FinancialAccount.objects.filter(pk=account_id, owner_id__in=allowed_owner_ids).first()
        if account is None:
            account_id = None
        else:
            if owner_id and account.owner_id != owner_id:
                account_id = None
            if institution_id and account.institution_id != institution_id:
                account_id = None

    return FinancialContext(owner_id=owner_id, institution_id=institution_id, account_id=account_id)


def context_options(user, ctx: FinancialContext, *, hidden_scope: str | None = None) -> ContextOptions:
    """Retorna opções para os filtros (dropdowns) e os ids de conta resultantes.

    `hidden_scope` ("dashboard" ou "projections") aplica a preferência pessoal
    de Configurações > Contas em análises: as contas marcadas como ocultas
    saem de `account_ids` e, portanto, de todo agregado calculado a partir
    dele. Elas continuam listadas em `accounts` (o seletor), porque o usuário
    não perdeu acesso — só pediu para não vê-las somadas.

    A ocultação vale para a visão agregada. Se o usuário escolher
    explicitamente uma conta no filtro (`ctx.account_id`), essa escolha vence:
    caso contrário a tela ficaria vazia sem explicar por quê.
    """
    allowed_owner_ids = accessible_owner_ids(user)
    if not allowed_owner_ids:
        return ContextOptions(owners=[], institutions=[], accounts=[], account_ids=[])

    base_qs = FinancialAccount.objects.select_related("owner", "institution").filter(
        owner_id__in=allowed_owner_ids
    )

    selected_qs = base_qs
    if ctx.owner_id:
        selected_qs = selected_qs.filter(owner_id=ctx.owner_id)
    if ctx.institution_id:
        selected_qs = selected_qs.filter(institution_id=ctx.institution_id)
    if ctx.account_id:
        selected_qs = selected_qs.filter(pk=ctx.account_id)

    account_ids = list(selected_qs.order_by("account_name").values_list("id", flat=True))

    if hidden_scope and not ctx.account_id:
        hidden = hidden_account_ids(user, hidden_scope)
        if hidden:
            account_ids = [account_id for account_id in account_ids if account_id not in hidden]

    owners = list(
        AccountOwner.objects.filter(id__in=base_qs.values_list("owner_id", flat=True)).distinct().order_by("name")
    )
    institutions = list(
        FinancialInstitution.objects.filter(id__in=base_qs.values_list("institution_id", flat=True))
        .distinct()
        .order_by("institution_name")
    )
    accounts = list(base_qs.order_by("owner__name", "institution__institution_name", "account_name"))

    return ContextOptions(owners=owners, institutions=institutions, accounts=accounts, account_ids=account_ids)


# ---------------------------------------------------------------------------
# Matriz de saldo ("balance mode")
# ---------------------------------------------------------------------------

def _balance_status_q(view_mode: str, today: date) -> Q:
    """Aplica a matriz de status do domínio ao cálculo de saldo."""
    projected_future = Q(status=STATUS_PROJECTED, due_date__gte=today)
    projected_overdue = Q(status=STATUS_PROJECTED, due_date__lt=today)
    pending_overdue = Q(status=STATUS_PENDING, due_date__lt=today)
    realized = Q(status=STATUS_REALIZED)
    if view_mode == VIEW_REALIZED:
        return realized
    if view_mode == VIEW_PENDING:
        return realized | pending_overdue | projected_overdue
    if view_mode == VIEW_PROJECTED:
        return realized | projected_future
    if view_mode == VIEW_ALL:
        return realized | pending_overdue | projected_future
    return realized


def _balance_date_case():
    return Case(
        When(status=STATUS_REALIZED, then=F("realized_date")),
        default=F("due_date"),
    )


def _balance_date_expr(view_mode: str):
    if view_mode == VIEW_REALIZED:
        return F("realized_date")
    return _balance_date_case()


def _balance_amount_expr(view_mode: str):
    coalesced = Coalesce("realized_amount", "entry_amount", output_field=_AMOUNT_FIELD)
    if view_mode == VIEW_REALIZED:
        return coalesced
    return Case(
        When(status=STATUS_REALIZED, then=coalesced),
        default=F("entry_amount"),
        output_field=_AMOUNT_FIELD,
    )


def _signed_entries_total(account_ids: list[int], *, view_mode: str, start_date: date, end_date: date) -> Decimal:
    if not account_ids or start_date >= end_date:
        return Decimal("0.00")
    amount_expr = _balance_amount_expr(view_mode)
    signed_expr = Case(
        When(entry_type=ENTRY_TYPE_INCOME, then=amount_expr),
        default=amount_expr * -1,
        output_field=_AMOUNT_FIELD,
    )
    qs = (
        CashFlowEntry.objects.annotate(balance_date=_balance_date_expr(view_mode))
        .filter(account_id__in=account_ids, balance_date__gte=start_date, balance_date__lt=end_date)
        .filter(_balance_status_q(view_mode, date.today()))
    )
    total = qs.aggregate(total=Coalesce(Sum(signed_expr), Value(Decimal("0.00")), output_field=_AMOUNT_FIELD))["total"]
    return to_decimal(total)


def _signed_entries_totals_by_account(account_ids: list[int], *, view_mode: str, start_date: date, end_date: date) -> dict[int, Decimal]:
    if not account_ids or start_date >= end_date:
        return {}
    amount_expr = _balance_amount_expr(view_mode)
    signed_expr = Case(
        When(entry_type=ENTRY_TYPE_INCOME, then=amount_expr),
        default=amount_expr * -1,
        output_field=_AMOUNT_FIELD,
    )
    qs = (
        CashFlowEntry.objects.annotate(balance_date=_balance_date_expr(view_mode))
        .filter(account_id__in=account_ids, balance_date__gte=start_date, balance_date__lt=end_date)
        .filter(_balance_status_q(view_mode, date.today()))
        .values("account_id")
        .annotate(total=Coalesce(Sum(signed_expr), Value(Decimal("0.00")), output_field=_AMOUNT_FIELD))
    )
    return {int(row["account_id"]): to_decimal(row["total"]) for row in qs}


def _signed_entries_totals_by_month(
    account_ids: list[int], *, view_mode: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Como `_signed_entries_total`, mas devolve o total assinado por mês
    (chave = primeiro dia do mês) dentro do intervalo, numa única consulta
    agregada em vez de uma por mês -- mesma técnica (TruncMonth + Sum +
    GROUP BY) que `dashboard_view` já usa para totais multi-mês."""
    if not account_ids or start_date >= end_date:
        return {}
    amount_expr = _balance_amount_expr(view_mode)
    signed_expr = Case(
        When(entry_type=ENTRY_TYPE_INCOME, then=amount_expr),
        default=amount_expr * -1,
        output_field=_AMOUNT_FIELD,
    )
    qs = (
        CashFlowEntry.objects.annotate(balance_date=_balance_date_expr(view_mode))
        .filter(account_id__in=account_ids, balance_date__gte=start_date, balance_date__lt=end_date)
        .filter(_balance_status_q(view_mode, date.today()))
        .annotate(month=TruncMonth("balance_date"))
        .values("month")
        .annotate(total=Coalesce(Sum(signed_expr), Value(Decimal("0.00")), output_field=_AMOUNT_FIELD))
    )
    return {row["month"]: to_decimal(row["total"]) for row in qs}


def decimal_base_balance(account_ids: Iterable[int]) -> Decimal:
    ids = list(account_ids)
    if not ids:
        return Decimal("0.00")
    total = FinancialAccount.objects.filter(id__in=ids).aggregate(total=Sum("initial_balance"))["total"]
    return to_decimal(total).quantize(MONEY_QUANT)


def decimal_balance_before(account_ids: list[int], start_date: date, view_mode: str) -> Decimal:
    minimum_date = system_start_date() or date.min
    balance = decimal_base_balance(account_ids) + _signed_entries_total(
        account_ids, view_mode=view_mode, start_date=minimum_date, end_date=start_date
    )
    return balance.quantize(MONEY_QUANT)


def decimal_period_start_balance(account_ids: list[int], start_date: date, end_date_exclusive: date, view_mode: str) -> Decimal:
    """Saldo de abertura para telas/relatórios com período explícito.

    Em modos A vencer e Vencidos, a abertura do período incorpora também os
    lançamentos realizados dentro do próprio período filtrado.
    """
    balance = decimal_balance_before(account_ids, start_date, view_mode)
    if view_mode not in {VIEW_PROJECTED, VIEW_PENDING} or not account_ids or start_date >= end_date_exclusive:
        return balance

    realized_in_period = _signed_entries_total(
        account_ids,
        view_mode=VIEW_REALIZED,
        start_date=max(start_date, system_start_date() or date.min),
        end_date=end_date_exclusive,
    )
    return (balance + realized_in_period).quantize(MONEY_QUANT)


def decimal_period_start_balances_by_account(account_ids: Iterable[int], start_date: date, end_date_exclusive: date, view_mode: str) -> dict[int, Decimal]:
    ids = [int(account_id) for account_id in account_ids if account_id]
    if not ids:
        return {}

    balances = {
        row["id"]: to_decimal(row["initial_balance"])
        for row in FinancialAccount.objects.filter(id__in=ids).values("id", "initial_balance")
    }
    for account_id in ids:
        balances.setdefault(account_id, Decimal("0.00"))

    minimum_date = system_start_date() or date.min
    history_totals = _signed_entries_totals_by_account(ids, view_mode=view_mode, start_date=minimum_date, end_date=start_date)
    for account_id, total in history_totals.items():
        balances[account_id] += total

    if view_mode in {VIEW_PROJECTED, VIEW_PENDING} and start_date < end_date_exclusive:
        realized_totals = _signed_entries_totals_by_account(
            ids, view_mode=VIEW_REALIZED, start_date=max(start_date, minimum_date), end_date=end_date_exclusive
        )
        for account_id, total in realized_totals.items():
            balances[account_id] += total

    return {account_id: balance.quantize(MONEY_QUANT) for account_id, balance in balances.items()}


# ---------------------------------------------------------------------------
# Matriz de listagem ("listing mode") usada pelo motor de projeções
# ---------------------------------------------------------------------------

def _listing_status_q(view_mode: str, today: date) -> Q:
    projected_future = Q(status=STATUS_PROJECTED, due_date__gte=today)
    projected_overdue = Q(status=STATUS_PROJECTED, due_date__lt=today)
    pending_overdue = Q(status=STATUS_PENDING, due_date__lt=today)
    if view_mode == VIEW_ALL:
        return Q()
    if view_mode == VIEW_PENDING:
        return pending_overdue | projected_overdue
    if view_mode == VIEW_PROJECTED:
        return projected_future
    return Q(status=view_mode)


def _listing_date_expr(view_mode: str):
    if view_mode == VIEW_ALL:
        return _balance_date_case()
    if view_mode == VIEW_REALIZED:
        return F("realized_date")
    return F("due_date")


def entries_for_period(account_ids: list[int], start: date, end_exclusive: date, view_mode: str) -> list[CashFlowEntry]:
    """Lançamentos que compõem os buckets do motor de projeções (não inclui saldo de abertura)."""
    ids = list(account_ids)
    if not ids:
        return []
    minimum_date = system_start_date() or date.min
    floor_start = max(start, minimum_date)
    if floor_start >= end_exclusive:
        return []
    qs = (
        CashFlowEntry.objects.select_related("category")
        .annotate(proj_date=_listing_date_expr(view_mode))
        .filter(account_id__in=ids, proj_date__gte=floor_start, proj_date__lt=end_exclusive)
        .filter(_listing_status_q(view_mode, date.today()))
        # A tabela de lançamentos usa esta mesma ordenação. Manter o saldo
        # acumulado na sequência visível evita associar o fechamento do dia à
        # linha errada quando existem vários lançamentos na mesma data.
        .order_by("proj_date", "-entry_type", "category__category_name", "id")
    )
    return list(qs)


def _entry_amount(entry: CashFlowEntry, *, realized: bool) -> Decimal:
    if realized and entry.realized_amount is not None:
        return to_decimal(entry.realized_amount)
    return to_decimal(entry.entry_amount)


def _entry_signed_amount(entry: CashFlowEntry, *, realized: bool) -> Decimal:
    value = _entry_amount(entry, realized=realized)
    return value if entry.entry_type == ENTRY_TYPE_INCOME else -value


def _entry_is_realized_for_mode(entry: CashFlowEntry, view_mode: str) -> bool:
    """Determina se o valor/data realizados devem ser usados para o lançamento.

    Nos modos PROJECTED/PENDING os filtros de listagem já excluem lançamentos
    realizados, então basta checar o modo ou o status: nenhum caso alcançável
    distingue os dois critérios.
    """
    return view_mode == VIEW_REALIZED or entry.status == STATUS_REALIZED


def entry_date_for_view_mode(entry: CashFlowEntry, view_mode: str) -> date | None:
    if view_mode == VIEW_REALIZED or (view_mode == VIEW_ALL and entry.status == STATUS_REALIZED):
        return entry.realized_date
    return entry.due_date


# ---------------------------------------------------------------------------
# Motor de projeções mensais
# ---------------------------------------------------------------------------

def _month_key(d: date | None) -> date | None:
    return date(d.year, d.month, 1) if d is not None else None


def _entry_month_key(entry: CashFlowEntry, view_mode: str) -> date | None:
    if view_mode == VIEW_REALIZED:
        return _month_key(entry.realized_date)
    if view_mode == VIEW_ALL:
        d = entry.realized_date if entry.status == STATUS_REALIZED else entry.due_date
        return _month_key(d)
    return _month_key(entry.due_date)


def _empty_month(month_start: date, saldo_inicial: Decimal) -> dict:
    return {
        "month": month_start.strftime("%Y-%m"),
        "saldo_inicial": saldo_inicial,
        "receita": Decimal("0.00"),
        "despesa": Decimal("0.00"),
        "rec_int": Decimal("0.00"),
        "desp_int": Decimal("0.00"),
        "receita_realizada": Decimal("0.00"),
        "despesa_realizada": Decimal("0.00"),
        "receita_atrasado": Decimal("0.00"),
        "despesa_atrasado": Decimal("0.00"),
        "receita_projetada": Decimal("0.00"),
        "despesa_projetada": Decimal("0.00"),
        "geracao_realizada": Decimal("0.00"),
        "geracao_atrasado": Decimal("0.00"),
        "geracao_projetada": Decimal("0.00"),
        "geracao_int": Decimal("0.00"),
        "geracao": Decimal("0.00"),
        "saldo": saldo_inicial,
        "total_lancamentos": 0,
        "total_realizados": 0,
        "total_atrasados": 0,
        "total_planejados": 0,
    }


def _add_entry_to_bucket(month: dict, entry: CashFlowEntry, view_mode: str) -> None:
    realized = _entry_is_realized_for_mode(entry, view_mode)
    amount = _entry_amount(entry, realized=realized)
    is_internal = bool(entry.category and entry.category.is_internal)

    if entry.entry_type == ENTRY_TYPE_INCOME:
        if is_internal:
            month["rec_int"] += amount
        else:
            month["receita"] += amount
            if entry.status == STATUS_REALIZED:
                month["receita_realizada"] += amount
            elif entry.status == STATUS_PENDING:
                month["receita_atrasado"] += amount
            else:
                month["receita_projetada"] += amount
    else:
        if is_internal:
            month["desp_int"] += amount
        else:
            month["despesa"] += amount
            if entry.status == STATUS_REALIZED:
                month["despesa_realizada"] += amount
            elif entry.status == STATUS_PENDING:
                month["despesa_atrasado"] += amount
            else:
                month["despesa_projetada"] += amount


def projection_months_between(account_ids: list[int], start_month: date, end_month: date, view_mode: str) -> list[dict]:
    """Calcula projeção mensal em intervalo inclusivo de meses."""
    first_month, last_month = bounded_projection_month_range(start_month, end_month)
    final_end = add_months(last_month, 1)

    all_entries = entries_for_period(account_ids, first_month, final_end, view_mode)
    entries_by_month: dict[date, list[CashFlowEntry]] = defaultdict(list)
    for entry in all_entries:
        mk = _entry_month_key(entry, view_mode)
        if mk is not None:
            entries_by_month[mk].append(entry)

    use_running_balance = view_mode == VIEW_REALIZED
    if use_running_balance:
        running_balance = decimal_balance_before(account_ids, first_month, view_mode)
    else:
        # `decimal_period_start_balance` por mês seria uma consulta agregada
        # (duas, em modo A vencer/Vencidos) por mês do intervalo. Em vez
        # disso, calcula o saldo do primeiro mês uma única vez e acumula com
        # os totais mensais já agregados em `_signed_entries_totals_by_month`
        # -- mesmo raciocínio: `decimal_balance_before` de um mês é o do mês
        # anterior mais o total assinado daquele mês, então a soma
        # incremental produz o mesmo valor que recomputar do zero a cada mês.
        running_before_month = decimal_balance_before(account_ids, first_month, view_mode)
        monthly_deltas = _signed_entries_totals_by_month(
            account_ids, view_mode=view_mode, start_date=first_month, end_date=final_end
        )
        realized_in_period_by_month = (
            _signed_entries_totals_by_month(
                account_ids,
                view_mode=VIEW_REALIZED,
                start_date=max(first_month, system_start_date() or date.min),
                end_date=final_end,
            )
            if view_mode in {VIEW_PROJECTED, VIEW_PENDING}
            else {}
        )

    months: list[dict] = []
    month_start = first_month
    while month_start <= last_month:
        if use_running_balance:
            saldo_atual = running_balance
        else:
            saldo_atual = (
                running_before_month + realized_in_period_by_month.get(month_start, Decimal("0.00"))
            ).quantize(MONEY_QUANT)

        month = _empty_month(month_start, saldo_atual)
        month_entries = entries_by_month.get(month_start, [])
        month["total_lancamentos"] = len(month_entries)

        for entry in month_entries:
            _add_entry_to_bucket(month, entry, view_mode)
            if not (view_mode == STATUS_PROJECTED and entry.status == STATUS_REALIZED):
                saldo_atual += _entry_signed_amount(entry, realized=_entry_is_realized_for_mode(entry, view_mode))
            if entry.status == STATUS_REALIZED:
                month["total_realizados"] += 1
            elif entry.status == STATUS_PENDING:
                month["total_atrasados"] += 1
            elif entry.status == STATUS_PROJECTED:
                month["total_planejados"] += 1

        month["geracao_realizada"] = month["receita_realizada"] - month["despesa_realizada"]
        month["geracao_atrasado"] = month["receita_atrasado"] - month["despesa_atrasado"]
        month["geracao_projetada"] = month["receita_projetada"] - month["despesa_projetada"]
        month["geracao_int"] = month["rec_int"] - month["desp_int"]
        month["geracao"] = month["receita"] - month["despesa"]
        month["saldo"] = saldo_atual.quantize(MONEY_QUANT)

        if use_running_balance:
            running_balance = month["saldo"]
        else:
            running_before_month += monthly_deltas.get(month_start, Decimal("0.00"))

        months.append(month)
        month_start = add_months(month_start, 1)

    return months


def projection_period_totals(month_data: list[dict]) -> dict | None:
    """Soma as colunas monetárias de `month_data` para a linha de total do período."""
    if not month_data:
        return None
    keys = (
        "receita", "despesa", "geracao", "rec_int", "desp_int",
        "geracao_atrasado", "geracao_realizada", "geracao_projetada",
    )
    totals = {key: sum((m[key] for m in month_data), Decimal("0.00")) for key in keys}
    totals["saldo"] = month_data[-1]["saldo"]
    return totals


# ---------------------------------------------------------------------------
# Relatorio: Posicao por conta
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountCashReportRow:
    account_id: int
    owner_name: str
    institution_name: str
    account_name: str
    start_balance: Decimal
    cash_generation: Decimal
    end_balance: Decimal


def account_cash_report_rows(account_ids: list[int], start_month: date, end_month: date, view_mode: str) -> list[AccountCashReportRow]:
    ids = [int(account_id) for account_id in account_ids if account_id]
    if not ids:
        return []

    first_month = date(start_month.year, start_month.month, 1)
    last_month = date(end_month.year, end_month.month, 1)
    if last_month < first_month:
        last_month = first_month
    first_month_end = add_months(first_month, 1)
    final_end = add_months(last_month, 1)

    accounts = list(
        FinancialAccount.objects.select_related("owner", "institution")
        .filter(id__in=ids)
        .order_by("owner__name", "institution__institution_name", "account_name")
    )
    ordered_ids = [account.id for account in accounts]
    if not ordered_ids:
        return []

    start_by_account = decimal_period_start_balances_by_account(ordered_ids, first_month, first_month_end, view_mode)
    end_by_account = dict(decimal_period_start_balances_by_account(ordered_ids, last_month, final_end, view_mode))
    generation_by_account = {account_id: Decimal("0.00") for account_id in ordered_ids}

    for entry in entries_for_period(ordered_ids, first_month, final_end, view_mode):
        realized = _entry_is_realized_for_mode(entry, view_mode)
        amount = _entry_amount(entry, realized=realized)
        is_internal = bool(entry.category and entry.category.is_internal)
        if not is_internal:
            if entry.entry_type == ENTRY_TYPE_INCOME:
                generation_by_account[entry.account_id] += amount
            else:
                generation_by_account[entry.account_id] -= amount

        if _entry_month_key(entry, view_mode) == last_month and not (view_mode == STATUS_PROJECTED and entry.status == STATUS_REALIZED):
            end_by_account[entry.account_id] = end_by_account.get(entry.account_id, Decimal("0.00")) + _entry_signed_amount(entry, realized=realized)

    return [
        AccountCashReportRow(
            account_id=account.id,
            owner_name=account.owner.name if account.owner else "-",
            institution_name=account.institution.institution_name if account.institution else "-",
            account_name=account.account_name,
            start_balance=start_by_account.get(account.id, Decimal("0.00")).quantize(MONEY_QUANT),
            cash_generation=generation_by_account.get(account.id, Decimal("0.00")).quantize(MONEY_QUANT),
            end_balance=end_by_account.get(account.id, Decimal("0.00")).quantize(MONEY_QUANT),
        )
        for account in accounts
    ]


# ---------------------------------------------------------------------------
# Relatorio: Movimentos futuros
# ---------------------------------------------------------------------------

UPCOMING_MOVEMENT_VIEW_MODES = {VIEW_PROJECTED, VIEW_PENDING, VIEW_REALIZED}


def normalize_upcoming_movement_mode(value: str | None) -> str:
    return value if value in UPCOMING_MOVEMENT_VIEW_MODES else VIEW_PROJECTED


@dataclass(frozen=True)
class UpcomingMovementLine:
    entry_id: int
    movement_date: date
    account_id: int
    owner_name: str
    institution_name: str
    account_name: str
    category_name: str
    description: str
    entry_type: str
    status: str
    is_internal: bool
    amount: Decimal
    signed_amount: Decimal
    balance_after: Decimal


@dataclass(frozen=True)
class UpcomingMovementAccountRow:
    account_id: int
    owner_name: str
    institution_name: str
    account_name: str
    start_balance: Decimal
    income: Decimal
    expense: Decimal
    internal_income: Decimal
    internal_expense: Decimal
    net_movement: Decimal
    end_balance: Decimal
    minimum_balance: Decimal
    minimum_balance_date: date
    action_level: str
    action_label: str
    action_message: str
    action_amount: Decimal

    @property
    def internal_net(self) -> Decimal:
        return self.internal_income - self.internal_expense


@dataclass(frozen=True)
class UpcomingMovementsSummary:
    start_balance: Decimal
    income: Decimal
    expense: Decimal
    internal_income: Decimal
    internal_expense: Decimal
    net_movement: Decimal
    end_balance: Decimal
    minimum_balance: Decimal
    minimum_balance_date: date
    action_level: str
    action_label: str
    action_message: str
    action_amount: Decimal

    @property
    def internal_net(self) -> Decimal:
        return self.internal_income - self.internal_expense


@dataclass(frozen=True)
class UpcomingMovementsReport:
    start_date: date
    end_date: date
    view_mode: str
    summary: UpcomingMovementsSummary
    account_rows: list[UpcomingMovementAccountRow]
    movement_lines: list[UpcomingMovementLine]


def _list_upcoming_movement_entries(account_ids: list[int], start_date: date, end_date_exclusive: date, view_mode: str) -> list[CashFlowEntry]:
    ids = [int(account_id) for account_id in account_ids if account_id]
    if not ids or start_date >= end_date_exclusive:
        return []
    minimum_date = system_start_date() or date.min
    floor_start = max(start_date, minimum_date)
    if floor_start >= end_date_exclusive:
        return []
    qs = (
        CashFlowEntry.objects.select_related("account", "account__owner", "account__institution", "category")
        .annotate(proj_date=_listing_date_expr(view_mode))
        .filter(account_id__in=ids, proj_date__gte=floor_start, proj_date__lt=end_date_exclusive)
        .filter(_listing_status_q(view_mode, date.today()))
        .order_by("proj_date", "-entry_type", "category__category_name", "id")
    )
    return list(qs)


def _account_action(minimum_balance: Decimal, minimum_balance_date: date, *, has_movements: bool) -> tuple[str, str, str, Decimal]:
    if minimum_balance < 0:
        return (
            "danger",
            "Transferir/cobrir",
            f"Saldo fica negativo em {minimum_balance_date.strftime('%d/%m/%Y')}.",
            (-minimum_balance).quantize(MONEY_QUANT),
        )
    if not has_movements:
        return ("neutral", "Sem movimentos", "Não há lançamentos no período.", Decimal("0.00"))
    return ("success", "Suficiente", "Saldo cobre os movimentos previstos.", Decimal("0.00"))


def _summary_action(rows: list[UpcomingMovementAccountRow], minimum_balance: Decimal, minimum_balance_date: date, *, has_movements: bool, has_candidate_accounts: bool) -> tuple[str, str, str, Decimal]:
    if not has_candidate_accounts:
        return ("neutral", "Sem contas", "Nenhuma conta disponível para os filtros selecionados.", Decimal("0.00"))
    if not has_movements:
        return ("neutral", "Sem movimentos", "Não há lançamentos no período selecionado.", Decimal("0.00"))
    if minimum_balance < 0:
        return (
            "danger",
            "Caixa insuficiente",
            f"O conjunto das contas fica negativo em {minimum_balance_date.strftime('%d/%m/%Y')}.",
            (-minimum_balance).quantize(MONEY_QUANT),
        )
    deficit_rows = [row for row in rows if row.minimum_balance < 0]
    if deficit_rows:
        total_transfer = sum((row.action_amount for row in deficit_rows), Decimal("0.00")).quantize(MONEY_QUANT)
        return (
            "warning",
            "Avaliar transferência",
            f"{len(deficit_rows)} conta(s) ficam negativas, mas o caixa total cobre o período.",
            total_transfer,
        )
    return ("success", "Tudo coberto", "Os saldos previstos cobrem os movimentos do período.", Decimal("0.00"))


def upcoming_movements_report(account_ids: list[int], start_date: date, end_date: date, view_mode: str) -> UpcomingMovementsReport:
    mode = normalize_upcoming_movement_mode(view_mode)
    if end_date < start_date:
        end_date = start_date
    end_exclusive = end_date + timedelta(days=1)

    candidate_accounts = list(
        FinancialAccount.objects.select_related("owner", "institution")
        .filter(id__in=[int(a) for a in account_ids if a])
        .order_by("owner__name", "institution__institution_name", "account_name")
    )
    candidate_ids = [account.id for account in candidate_accounts]
    entries = _list_upcoming_movement_entries(candidate_ids, start_date, end_exclusive, mode)

    movement_account_ids = {entry.account_id for entry in entries}
    accounts = [account for account in candidate_accounts if account.id in movement_account_ids]
    start_by_account = decimal_period_start_balances_by_account([a.id for a in accounts], start_date, end_exclusive, mode)
    running_by_account = dict(start_by_account)
    minimum_by_account = dict(start_by_account)
    minimum_date_by_account = {account.id: start_date for account in accounts}

    totals_by_account = {
        account.id: {"income": Decimal("0.00"), "expense": Decimal("0.00"), "internal_income": Decimal("0.00"), "internal_expense": Decimal("0.00")}
        for account in accounts
    }
    daily_delta: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    lines: list[UpcomingMovementLine] = []

    for entry in entries:
        if entry.account_id not in running_by_account:
            continue
        movement_date = entry_date_for_view_mode(entry, mode)
        if movement_date is None:
            continue

        realized = _entry_is_realized_for_mode(entry, mode)
        amount = _entry_amount(entry, realized=realized).quantize(MONEY_QUANT)
        signed_amount = _entry_signed_amount(entry, realized=realized).quantize(MONEY_QUANT)
        is_internal = bool(entry.category and entry.category.is_internal)
        bucket = totals_by_account[entry.account_id]
        if entry.entry_type == ENTRY_TYPE_INCOME:
            bucket["internal_income" if is_internal else "income"] += amount
        else:
            bucket["internal_expense" if is_internal else "expense"] += amount

        running_by_account[entry.account_id] = (running_by_account[entry.account_id] + signed_amount).quantize(MONEY_QUANT)
        daily_delta[movement_date] += signed_amount
        if running_by_account[entry.account_id] < minimum_by_account[entry.account_id]:
            minimum_by_account[entry.account_id] = running_by_account[entry.account_id]
            minimum_date_by_account[entry.account_id] = movement_date

        account = entry.account
        lines.append(
            UpcomingMovementLine(
                entry_id=entry.id,
                movement_date=movement_date,
                account_id=entry.account_id,
                owner_name=account.owner.name if account and account.owner else "-",
                institution_name=account.institution.institution_name if account and account.institution else "-",
                account_name=account.account_name if account else "-",
                category_name=entry.category.category_name if entry.category else "-",
                description=entry.description or "",
                entry_type=entry.entry_type,
                status=entry.status,
                is_internal=is_internal,
                amount=amount,
                signed_amount=signed_amount,
                balance_after=running_by_account[entry.account_id],
            )
        )

    account_rows = []
    for account in accounts:
        totals = totals_by_account[account.id]
        income = totals["income"].quantize(MONEY_QUANT)
        expense = totals["expense"].quantize(MONEY_QUANT)
        internal_income = totals["internal_income"].quantize(MONEY_QUANT)
        internal_expense = totals["internal_expense"].quantize(MONEY_QUANT)
        net_movement = (income - expense + internal_income - internal_expense).quantize(MONEY_QUANT)
        action_level, action_label, action_message, action_amount = _account_action(
            minimum_by_account[account.id], minimum_date_by_account[account.id], has_movements=True
        )
        account_rows.append(
            UpcomingMovementAccountRow(
                account_id=account.id,
                owner_name=account.owner.name if account.owner else "-",
                institution_name=account.institution.institution_name if account.institution else "-",
                account_name=account.account_name,
                start_balance=start_by_account[account.id].quantize(MONEY_QUANT),
                income=income,
                expense=expense,
                internal_income=internal_income,
                internal_expense=internal_expense,
                net_movement=net_movement,
                end_balance=running_by_account[account.id].quantize(MONEY_QUANT),
                minimum_balance=minimum_by_account[account.id].quantize(MONEY_QUANT),
                minimum_balance_date=minimum_date_by_account[account.id],
                action_level=action_level,
                action_label=action_label,
                action_message=action_message,
                action_amount=action_amount,
            )
        )

    start_balance = sum((row.start_balance for row in account_rows), Decimal("0.00")).quantize(MONEY_QUANT)
    income = sum((row.income for row in account_rows), Decimal("0.00")).quantize(MONEY_QUANT)
    expense = sum((row.expense for row in account_rows), Decimal("0.00")).quantize(MONEY_QUANT)
    internal_income = sum((row.internal_income for row in account_rows), Decimal("0.00")).quantize(MONEY_QUANT)
    internal_expense = sum((row.internal_expense for row in account_rows), Decimal("0.00")).quantize(MONEY_QUANT)
    net_movement = (income - expense + internal_income - internal_expense).quantize(MONEY_QUANT)
    end_balance = sum((row.end_balance for row in account_rows), Decimal("0.00")).quantize(MONEY_QUANT)

    running_total = start_balance
    minimum_balance = start_balance
    minimum_balance_date = start_date
    for movement_date in sorted(daily_delta):
        running_total = (running_total + daily_delta[movement_date]).quantize(MONEY_QUANT)
        if running_total < minimum_balance:
            minimum_balance = running_total
            minimum_balance_date = movement_date

    action_level, action_label, action_message, action_amount = _summary_action(
        account_rows, minimum_balance, minimum_balance_date, has_movements=bool(lines), has_candidate_accounts=bool(candidate_accounts)
    )
    summary = UpcomingMovementsSummary(
        start_balance=start_balance,
        income=income,
        expense=expense,
        internal_income=internal_income,
        internal_expense=internal_expense,
        net_movement=net_movement,
        end_balance=end_balance,
        minimum_balance=minimum_balance.quantize(MONEY_QUANT),
        minimum_balance_date=minimum_balance_date,
        action_level=action_level,
        action_label=action_label,
        action_message=action_message,
        action_amount=action_amount,
    )
    return UpcomingMovementsReport(
        start_date=start_date, end_date=end_date, view_mode=mode, summary=summary, account_rows=account_rows, movement_lines=lines
    )
