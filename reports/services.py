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
    OPERATION_INTERNAL_TRANSFER,
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


def selected_context(user, params, *, request=None) -> FinancialContext:
    """Contexto financeiro pedido, já saneado.

    Filtro incoerente é descartado em silêncio -- escolher um titular que não é
    dono da conta selecionada anula a conta, e a tela volta correta. O que NÃO
    volta sozinho é o endereço na barra: ele continua com a conta que deixou de
    valer, e um F5 ou um favorito reaplicariam o filtro que o servidor acabou de
    recusar.

    Por isso, quando recebe `request`, esta função anota ali o que descartou.
    `core.navegacao.UrlCanonicaMiddleware` lê essa anotação e devolve o endereço
    já sem os parâmetros mortos. É o servidor corrigindo a barra na mesma
    resposta -- sem segunda requisição e sem o cliente tendo que adivinhar o que
    aconteceu aqui dentro.
    """

    def _int(name: str) -> int | None:
        raw = params.get(name)
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    descartados: set[str] = set()

    def _descartar(nome: str) -> None:
        if params.get(nome):
            descartados.add(nome)

    owner_id = _int("owner_id")
    institution_id = _int("institution_id")
    account_id = _int("account_id")

    allowed_owner_ids = set(accessible_owner_ids(user))
    if owner_id is not None and owner_id not in allowed_owner_ids:
        owner_id = None
        _descartar("owner_id")

    if account_id is not None:
        account = FinancialAccount.objects.filter(pk=account_id, owner_id__in=allowed_owner_ids).first()
        if account is None:
            account_id = None
        else:
            if owner_id and account.owner_id != owner_id:
                account_id = None
            if institution_id and account.institution_id != institution_id:
                account_id = None
        if account_id is None:
            _descartar("account_id")

    if request is not None and descartados:
        request.filtros_descartados = descartados

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
# Planejamento anual por titular
# ---------------------------------------------------------------------------

# O relatório é deliberadamente uma visão de planejamento: movimentos
# realizados não entram em nenhuma das suas colunas.  Manter os valores aqui,
# em vez de reaproveitar VIEW_PROJECTED, é importante porque VIEW_PROJECTED
# inclui somente o futuro e não os lançamentos vencidos.
ANNUAL_PLANNING_CALENDAR = "calendar_year"
ANNUAL_PLANNING_ROLLING_13 = "rolling_13"
ANNUAL_PLANNING_LAYOUTS = {
    ANNUAL_PLANNING_CALENDAR,
    ANNUAL_PLANNING_ROLLING_13,
}
_PLANNING_OPEN_STATUSES = (STATUS_PENDING, STATUS_PROJECTED)


@dataclass(frozen=True)
class PlanningCategoryTotals:
    """Totais gerenciais separados pela regra de recorrência do lançamento."""

    recurring: Decimal = Decimal("0.00")
    non_recurring: Decimal = Decimal("0.00")

    @property
    def total(self) -> Decimal:
        return (self.recurring + self.non_recurring).quantize(MONEY_QUANT)


@dataclass(frozen=True)
class PlanningTransferTotals:
    """Resumo de transferências; nunca é misturado a receita ou despesa."""

    incoming: Decimal = Decimal("0.00")
    outgoing: Decimal = Decimal("0.00")
    entries: int = 0

    @property
    def net(self) -> Decimal:
        return (self.incoming - self.outgoing).quantize(MONEY_QUANT)

    @property
    def total(self) -> Decimal:
        """Volume bruto das pontas de transferência selecionadas."""
        return (self.incoming + self.outgoing).quantize(MONEY_QUANT)


@dataclass(frozen=True)
class AnnualPlanningMonth:
    month: date
    expenses: PlanningCategoryTotals
    income: PlanningCategoryTotals
    transfers: PlanningTransferTotals


@dataclass(frozen=True)
class AnnualPlanningOwnerColumn:
    """Coluna do titular no mês de referência (somente movimentos abertos)."""

    owner_id: int
    owner_name: str
    expenses: PlanningCategoryTotals
    income: PlanningCategoryTotals
    transfers: PlanningTransferTotals


@dataclass(frozen=True)
class AnnualPlanningReport:
    reference_month: date
    layout: str
    months: list[AnnualPlanningMonth]
    owner_columns: list[AnnualPlanningOwnerColumn]
    transfer_summary: PlanningTransferTotals
    account_ids: list[int]

    @property
    def monthly_rows(self) -> list[AnnualPlanningMonth]:
        """Nome semântico útil para views e consumidores do relatório."""
        return self.months


def _planning_months(reference_month: date, layout: str) -> list[date]:
    """Retorna meses inclusivos de um dos dois layouts suportados."""
    reference_month = date(reference_month.year, reference_month.month, 1)
    if layout == ANNUAL_PLANNING_ROLLING_13:
        first_month = add_months(reference_month, -6)
        count = 13
    else:
        first_month = date(reference_month.year, 1, 1)
        count = 12
    return [add_months(first_month, offset) for offset in range(count)]


def _planning_id_filter(values: Iterable[int] | None) -> list[int] | None:
    """Normaliza IDs de filtros sem aceitar valores nulos, negativos ou bool."""
    if values is None:
        return None
    if isinstance(values, int) and not isinstance(values, bool):
        values = [values]
    if isinstance(values, (str, bytes)):
        values = [values]  # type: ignore[list-item]
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if normalized > 0 and normalized not in result:
            result.append(normalized)
    return result


def _authorized_planning_accounts(
    user,
    owner_ids: Iterable[int] | None,
    account_ids: Iterable[int] | None,
) -> tuple[list[FinancialAccount], list[int]]:
    """Resolve o filtro no servidor e devolve apenas contas autorizadas.

    A função não transforma um ID inválido em "todas as contas": filtros
    explícitos são sempre uma interseção com o escopo de titulares do usuário.
    Isso evita que uma conta de outro titular seja incluída por adulteração da
    query string.  Usuários anônimos falham fechado, antes de qualquer
    consulta de lançamentos.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return [], []

    allowed_owner_ids = set(accessible_owner_ids(user, "view"))
    requested_owner_ids = _planning_id_filter(owner_ids)
    if requested_owner_ids is None:
        selected_owner_ids = allowed_owner_ids
    else:
        selected_owner_ids = allowed_owner_ids.intersection(requested_owner_ids)
    if not selected_owner_ids:
        return [], []

    requested_account_ids = _planning_id_filter(account_ids)
    queryset = FinancialAccount.objects.select_related("owner").filter(
        owner_id__in=selected_owner_ids,
    )
    if requested_account_ids is not None:
        queryset = queryset.filter(id__in=requested_account_ids)
    accounts = list(queryset.order_by("owner__name", "account_name", "id"))
    return accounts, [account.id for account in accounts]


def _planning_add_category(bucket: dict, entry: CashFlowEntry, amount: Decimal) -> None:
    # A parcela só é não recorrente por ser parcelada. A regra de negócio é
    # explicitamente o booleano is_recurring, não operation_type/installments.
    key = "recurring" if entry.is_recurring else "non_recurring"
    bucket[key] += amount


def _planning_add_transfer(bucket: dict, entry: CashFlowEntry, amount: Decimal) -> None:
    if entry.entry_type == ENTRY_TYPE_INCOME:
        bucket["incoming"] += amount
    else:
        bucket["outgoing"] += amount
    bucket["entries"] += 1


def _planning_category_totals(bucket: dict) -> PlanningCategoryTotals:
    return PlanningCategoryTotals(
        recurring=bucket["recurring"].quantize(MONEY_QUANT),
        non_recurring=bucket["non_recurring"].quantize(MONEY_QUANT),
    )


def _planning_transfer_totals(bucket: dict) -> PlanningTransferTotals:
    return PlanningTransferTotals(
        incoming=bucket["incoming"].quantize(MONEY_QUANT),
        outgoing=bucket["outgoing"].quantize(MONEY_QUANT),
        entries=bucket["entries"],
    )


def annual_planning_report(
    user,
    reference_month: date | str | None = None,
    *,
    owner_ids: Iterable[int] | None = None,
    account_ids: Iterable[int] | None = None,
    layout: str = ANNUAL_PLANNING_CALENDAR,
) -> AnnualPlanningReport:
    """Agrega planejamento aberto por titular e por mês.

    ``calendar_year`` produz janeiro--dezembro do ano de referência;
    ``rolling_13`` produz seis meses anteriores, o mês de referência e seis
    meses seguintes. Em
    ambos os layouts a consolidação mensal cobre todos os titulares/contas
    selecionados, enquanto ``owner_columns`` cobre exclusivamente o mês de
    referência. Status realizado é excluído, inclusive quando há
    ``realized_amount`` preenchido.

    ``owner_ids`` e ``account_ids`` são filtros opcionais. A conta final é
    sempre limitada pelos titulares com ``can_view`` do usuário; preferências
    de ocultação analítica não são autorização e, portanto, não são aplicadas.
    """
    if reference_month is None:
        today = date.today()
        reference = date(today.year, today.month, 1)
    elif isinstance(reference_month, str):
        reference = parse_month_input(reference_month)
        if reference is None:
            raise ValueError("Mês de referência inválido.")
    elif isinstance(reference_month, date):
        reference = date(reference_month.year, reference_month.month, 1)
    else:
        raise ValueError("Mês de referência inválido.")

    selected_layout = layout if layout in ANNUAL_PLANNING_LAYOUTS else ANNUAL_PLANNING_CALENDAR
    months = _planning_months(reference, selected_layout)
    first_month = months[0]
    end_exclusive = add_months(months[-1], 1)
    accounts, selected_account_ids = _authorized_planning_accounts(user, owner_ids, account_ids)

    empty_category = {"recurring": Decimal("0.00"), "non_recurring": Decimal("0.00")}
    empty_transfer = {"incoming": Decimal("0.00"), "outgoing": Decimal("0.00"), "entries": 0}
    monthly_buckets = {
        month: {"expenses": empty_category.copy(), "income": empty_category.copy(), "transfers": empty_transfer.copy()}
        for month in months
    }
    owner_buckets: dict[int, dict] = {
        account.owner_id: {
            "expenses": empty_category.copy(),
            "income": empty_category.copy(),
            "transfers": empty_transfer.copy(),
        }
        for account in accounts
        if account.owner_id
    }

    entries = (
        CashFlowEntry.objects.select_related("account", "account__owner", "category")
        .filter(
            account_id__in=selected_account_ids,
            due_date__gte=first_month,
            due_date__lt=end_exclusive,
            status__in=_PLANNING_OPEN_STATUSES,
        )
        .order_by("due_date", "id")
    )
    for entry in entries:
        month = date(entry.due_date.year, entry.due_date.month, 1)
        month_bucket = monthly_buckets.get(month)
        if month_bucket is None:
            continue
        amount = to_decimal(entry.entry_amount).quantize(MONEY_QUANT)
        is_internal = bool(
            (entry.category and entry.category.is_internal)
            or entry.operation_type == OPERATION_INTERNAL_TRANSFER
        )
        if is_internal:
            _planning_add_transfer(month_bucket["transfers"], entry, amount)
        else:
            target = "income" if entry.entry_type == ENTRY_TYPE_INCOME else "expenses"
            _planning_add_category(month_bucket[target], entry, amount)

        if month == reference:
            owner_bucket = owner_buckets.get(entry.account.owner_id)
            if owner_bucket is None:
                continue
            if is_internal:
                _planning_add_transfer(owner_bucket["transfers"], entry, amount)
            else:
                target = "income" if entry.entry_type == ENTRY_TYPE_INCOME else "expenses"
                _planning_add_category(owner_bucket[target], entry, amount)

    owner_names = {
        owner.id: owner.name
        for owner in AccountOwner.objects.filter(id__in=owner_buckets).order_by("name")
    }
    owner_columns = [
        AnnualPlanningOwnerColumn(
            owner_id=owner_id,
            owner_name=owner_names.get(owner_id, "-"),
            expenses=_planning_category_totals(bucket["expenses"]),
            income=_planning_category_totals(bucket["income"]),
            transfers=_planning_transfer_totals(bucket["transfers"]),
        )
        for owner_id, bucket in sorted(
            owner_buckets.items(), key=lambda item: (owner_names.get(item[0], ""), item[0])
        )
    ]
    month_rows = [
        AnnualPlanningMonth(
            month=month,
            expenses=_planning_category_totals(monthly_buckets[month]["expenses"]),
            income=_planning_category_totals(monthly_buckets[month]["income"]),
            transfers=_planning_transfer_totals(monthly_buckets[month]["transfers"]),
        )
        for month in months
    ]
    transfer_summary_bucket = empty_transfer.copy()
    for row in month_rows:
        transfer_summary_bucket["incoming"] += row.transfers.incoming
        transfer_summary_bucket["outgoing"] += row.transfers.outgoing
        transfer_summary_bucket["entries"] += row.transfers.entries

    return AnnualPlanningReport(
        reference_month=reference,
        layout=selected_layout,
        months=month_rows,
        owner_columns=owner_columns,
        transfer_summary=_planning_transfer_totals(transfer_summary_bucket),
        account_ids=selected_account_ids,
    )


# Nome explícito para consumidores que preferem o termo usado na interface.
local_annual_planning_report = annual_planning_report


def annual_planning_presentation(
    user,
    reference_month: date,
    *,
    owner_ids: Iterable[int] | None = None,
    account_ids: Iterable[int] | None = None,
    layout: str = ANNUAL_PLANNING_CALENDAR,
    view_mode: str = VIEW_ALL,
    show_descriptions: bool = False,
) -> dict:
    """Monta o contrato de apresentação da grade anual por titular.

    O mês de referência mostra, por titular, somente lançamentos abertos
    (``vencidos`` e ``a_vencer``) cuja data de vencimento pertence ao mês.
    Os meses consolidados mostram o histórico realizado pela data de
    realização e os demais lançamentos pela data de vencimento, mantendo a
    mesma semântica da visão ``Todos os modos`` dos relatórios existentes.
    """
    reference_month = date(reference_month.year, reference_month.month, 1)
    selected_layout = layout if layout in ANNUAL_PLANNING_LAYOUTS else ANNUAL_PLANNING_CALENDAR
    selected_view_mode = view_mode if view_mode in {VIEW_ALL, VIEW_PROJECTED, VIEW_PENDING, VIEW_REALIZED} else VIEW_ALL
    months = _planning_months(reference_month, selected_layout)
    first_month, end_exclusive = months[0], add_months(months[-1], 1)
    if user is None or not getattr(user, "is_authenticated", False):
        return {
            "reference_month_label": reference_month.strftime("%m/%Y"),
            "owner_columns": [],
            "months": [
                {
                    "key": month.strftime("%Y-%m"),
                    "label": month.strftime("%b/%y").capitalize(),
                    "is_current": month == reference_month,
                }
                for month in months
            ],
            "rows": [],
            "summary_rows": [],
            "totals": None,
            "account_ids": [],
        }
    accounts, selected_account_ids = _authorized_planning_accounts(user, owner_ids, account_ids)

    allowed_owner_ids = set(accessible_owner_ids(user, "view"))
    requested_owner_ids = _planning_id_filter(owner_ids)
    selected_owner_ids = (
        allowed_owner_ids if requested_owner_ids is None else allowed_owner_ids.intersection(requested_owner_ids)
    )
    selected_owners = list(AccountOwner.objects.filter(id__in=selected_owner_ids).order_by("name", "id"))
    owner_positions = {owner.id: index for index, owner in enumerate(selected_owners)}

    section_specs = (
        ("expense_recurring", "Despesas recorrentes", "despesa", True, -1),
        ("expense_non_recurring", "Despesas não recorrentes", "despesa", False, -1),
        ("income_recurring", "Receitas recorrentes", ENTRY_TYPE_INCOME, True, 1),
        ("income_non_recurring", "Receitas não recorrentes", ENTRY_TYPE_INCOME, False, 1),
    )
    spec_by_key = {spec[0]: spec for spec in section_specs}
    month_positions = {month: index for index, month in enumerate(months)}
    def zeroes() -> list[Decimal]:
        return [Decimal("0.00") for _ in months]

    def owner_zeroes() -> list[Decimal]:
        return [Decimal("0.00") for _ in selected_owners]
    buckets: dict[tuple, dict] = {}
    section_totals = {
        spec[0]: {"owner_values": owner_zeroes(), "months": zeroes()} for spec in section_specs
    }
    transfer_owner_values, transfer_months = owner_zeroes(), zeroes()
    transfer_volume_operations: set[tuple[int, int]] = set()

    if selected_view_mode == VIEW_ALL:
        in_months = Q(status=STATUS_REALIZED, realized_date__gte=first_month, realized_date__lt=end_exclusive) | Q(
            status__in=_PLANNING_OPEN_STATUSES, due_date__gte=first_month, due_date__lt=end_exclusive
        )
    elif selected_view_mode == VIEW_REALIZED:
        in_months = Q(status=STATUS_REALIZED, realized_date__gte=first_month, realized_date__lt=end_exclusive)
    else:
        in_months = Q(
            status=selected_view_mode,
            due_date__gte=first_month,
            due_date__lt=end_exclusive,
        )
    entries = CashFlowEntry.objects.select_related("account__owner", "category").filter(
        account_id__in=selected_account_ids
    ).filter(in_months).order_by("category__category_name", "description", "id")
    for entry in entries:
        amount = to_decimal(entry.realized_amount if entry.status == STATUS_REALIZED else entry.entry_amount).quantize(
            MONEY_QUANT
        )
        period_date = entry.realized_date if entry.status == STATUS_REALIZED else entry.due_date
        if period_date is None:
            continue
        month_index = month_positions.get(date(period_date.year, period_date.month, 1))
        if month_index is None:
            continue
        is_internal = bool(
            (entry.category and entry.category.is_internal)
            or entry.operation_type == OPERATION_INTERNAL_TRANSFER
        )
        if is_internal:
            signed_transfer = amount if entry.entry_type == ENTRY_TYPE_INCOME else -amount
            # Cada transferência possui duas pontas. O resumo mensal mostra
            # seu volume uma única vez, sem cancelar as pontas nem dobrar o
            # valor por somar crédito e débito do mesmo agrupamento.
            operation_id = entry.bank_operation_id or entry.id
            volume_key = (month_index, operation_id)
            if volume_key not in transfer_volume_operations:
                transfer_volume_operations.add(volume_key)
                transfer_months[month_index] += amount
            # Transferências já realizadas também precisam aparecer na coluna
            # do mês-base: elas afetam o saldo, mesmo não compondo a geração
            # de caixa. Para as demais categorias, a coluna permanece uma
            # projeção de lançamentos em aberto.
            if date(period_date.year, period_date.month, 1) == reference_month:
                owner_index = owner_positions.get(entry.account.owner_id)
                if owner_index is not None:
                    transfer_owner_values[owner_index] += signed_transfer
            continue

        section_key = (
            "income" if entry.entry_type == ENTRY_TYPE_INCOME else "expense"
        ) + ("_recurring" if entry.is_recurring else "_non_recurring")
        spec = spec_by_key[section_key]
        signed_amount = amount * spec[4]
        category_id = entry.category_id
        category_name = entry.category.category_name if entry.category else "Sem categoria"
        category_key = (section_key, category_id, category_name)
        category_bucket = buckets.setdefault(
            category_key,
            {"owner_values": owner_zeroes(), "months": zeroes(), "descriptions": {}},
        )
        category_bucket["months"][month_index] += signed_amount
        section_totals[section_key]["months"][month_index] += signed_amount
        if entry.status in _PLANNING_OPEN_STATUSES and date(entry.due_date.year, entry.due_date.month, 1) == reference_month:
            owner_index = owner_positions.get(entry.account.owner_id)
            if owner_index is not None:
                category_bucket["owner_values"][owner_index] += signed_amount
                section_totals[section_key]["owner_values"][owner_index] += signed_amount

        if show_descriptions:
            description = entry.description.strip() or "Sem descrição"
            description_bucket = category_bucket["descriptions"].setdefault(
                description, {"owner_values": owner_zeroes(), "months": zeroes()}
            )
            description_bucket["months"][month_index] += signed_amount
            if entry.status in _PLANNING_OPEN_STATUSES and date(entry.due_date.year, entry.due_date.month, 1) == reference_month:
                owner_index = owner_positions.get(entry.account.owner_id)
                if owner_index is not None:
                    description_bucket["owner_values"][owner_index] += signed_amount

    def _month_values(values: list[Decimal]) -> list[dict]:
        return [
            {"value": value.quantize(MONEY_QUANT), "is_current": month == reference_month}
            for month, value in zip(months, values, strict=True)
        ]

    rows = []
    grand_owner_values, grand_months = owner_zeroes(), zeroes()
    for section_key, section_label, _entry_type, _recurring, _sign in section_specs:
        section = section_totals[section_key]
        if not any(section["owner_values"]) and not any(section["months"]):
            continue
        rows.append(
            {
                "kind": "total",
                "label": section_label,
                "level": 0,
                "owner_values": section["owner_values"],
                "months": _month_values(section["months"]),
            }
        )
        for key, bucket in sorted(
            (item for item in buckets.items() if item[0][0] == section_key), key=lambda item: item[0][2].lower()
        ):
            rows.append(
                {
                    "kind": "category",
                    "label": key[2],
                    "category_path": section_label,
                    "level": 1,
                    "owner_values": bucket["owner_values"],
                    "months": _month_values(bucket["months"]),
                }
            )
            for description, description_bucket in sorted(bucket["descriptions"].items()):
                rows.append(
                    {
                        "kind": "description",
                        "label": key[2],
                        "description": description,
                        "category_path": section_label,
                        "level": 2,
                        "owner_values": description_bucket["owner_values"],
                        "months": _month_values(description_bucket["months"]),
                    }
                )
        for index, value in enumerate(section["owner_values"]):
            grand_owner_values[index] += value
        for index, value in enumerate(section["months"]):
            grand_months[index] += value

    def _combined_values(*section_keys: str, field: str) -> list[Decimal]:
        result = owner_zeroes() if field == "owner_values" else zeroes()
        for section_key in section_keys:
            for index, value in enumerate(section_totals[section_key][field]):
                result[index] += value
        return [value.quantize(MONEY_QUANT) for value in result]

    income_owner = _combined_values("income_recurring", "income_non_recurring", field="owner_values")
    income_months = _combined_values("income_recurring", "income_non_recurring", field="months")
    expense_owner = _combined_values("expense_recurring", "expense_non_recurring", field="owner_values")
    expense_months = _combined_values("expense_recurring", "expense_non_recurring", field="months")
    generation_owner = [
        (income + expense).quantize(MONEY_QUANT)
        for income, expense in zip(income_owner, expense_owner, strict=True)
    ]
    generation_months = [
        (income + expense).quantize(MONEY_QUANT)
        for income, expense in zip(income_months, expense_months, strict=True)
    ]
    account_ids_by_owner: dict[int, list[int]] = defaultdict(list)
    for account in accounts:
        account_ids_by_owner[account.owner_id].append(account.id)
    start_owner = [
        decimal_balance_before(account_ids_by_owner.get(owner.id, []), reference_month, selected_view_mode)
        for owner in selected_owners
    ]
    end_owner = [
        (start + generation + transfer).quantize(MONEY_QUANT)
        for start, generation, transfer in zip(start_owner, generation_owner, transfer_owner_values, strict=True)
    ]
    balance_rows = projection_months_between(selected_account_ids, first_month, months[-1], selected_view_mode)
    balance_by_month = {date.fromisoformat(row["month"] + "-01"): row for row in balance_rows}
    start_months = [
        to_decimal(balance_by_month.get(month, {}).get("saldo_inicial", Decimal("0.00"))).quantize(MONEY_QUANT)
        for month in months
    ]
    end_months = [
        to_decimal(balance_by_month.get(month, {}).get("saldo", Decimal("0.00"))).quantize(MONEY_QUANT)
        for month in months
    ]
    summary_rows = [
        {"label": "Geração de Caixa", "owner_values": generation_owner, "months": _month_values(generation_months)},
        {"label": "Saldo Previsto / Final", "owner_values": end_owner, "months": _month_values(end_months)},
        {"label": "Total Receitas", "owner_values": income_owner, "months": _month_values(income_months)},
        {"label": "Total Despesas", "owner_values": expense_owner, "months": _month_values(expense_months)},
        {"label": "Movimentações Internas", "owner_values": transfer_owner_values, "months": _month_values(transfer_months)},
        {"label": "Saldo Atual / Inicial", "owner_values": start_owner, "months": _month_values(start_months)},
    ]

    return {
        "reference_month_label": reference_month.strftime("%m/%Y"),
        "owner_columns": [{"id": owner.id, "name": owner.name} for owner in selected_owners],
        "months": [
            {
                "key": month.strftime("%Y-%m"),
                "label": month.strftime("%b/%y").capitalize(),
                "is_current": month == reference_month,
            }
            for month in months
        ],
        "rows": rows,
        "summary_rows": summary_rows,
        "totals": (
            {"owner_values": grand_owner_values, "months": _month_values(grand_months)}
            if rows
            else None
        ),
        "account_ids": selected_account_ids,
    }


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

    # O filtro de status define os buckets exibidos, não o saldo patrimonial.
    # Sem uma base canônica, uma linha parcialmente decorrida podia exibir um
    # saldo e alimentar o mês seguinte com outro. O saldo usa sempre o livro
    # completo (realizados pela data de realização, demais pela de vencimento).
    balance_entries_by_month: dict[date, list[CashFlowEntry]] = defaultdict(list)
    for entry in entries_for_period(account_ids, first_month, final_end, VIEW_ALL):
        mk = _entry_month_key(entry, VIEW_ALL)
        if mk is not None:
            balance_entries_by_month[mk].append(entry)
    running_balance = decimal_balance_before(account_ids, first_month, VIEW_ALL)

    months: list[dict] = []
    month_start = first_month
    while month_start <= last_month:
        saldo_atual = running_balance

        month = _empty_month(month_start, saldo_atual)
        month_entries = entries_by_month.get(month_start, [])
        month["total_lancamentos"] = len(month_entries)

        for entry in month_entries:
            _add_entry_to_bucket(month, entry, view_mode)
            if entry.status == STATUS_REALIZED:
                month["total_realizados"] += 1
            elif entry.status == STATUS_PENDING:
                month["total_atrasados"] += 1
            elif entry.status == STATUS_PROJECTED:
                month["total_planejados"] += 1

        for entry in balance_entries_by_month.get(month_start, []):
            saldo_atual += _entry_signed_amount(entry, realized=_entry_is_realized_for_mode(entry, VIEW_ALL))

        month["geracao_realizada"] = month["receita_realizada"] - month["despesa_realizada"]
        month["geracao_atrasado"] = month["receita_atrasado"] - month["despesa_atrasado"]
        month["geracao_projetada"] = month["receita_projetada"] - month["despesa_projetada"]
        month["geracao_int"] = month["rec_int"] - month["desp_int"]
        month["geracao"] = month["receita"] - month["despesa"]
        month["saldo"] = saldo_atual.quantize(MONEY_QUANT)

        running_balance = month["saldo"]

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
    internal_transfers: Decimal
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
    transfers_by_account = {account_id: Decimal("0.00") for account_id in ordered_ids}

    for entry in entries_for_period(ordered_ids, first_month, final_end, view_mode):
        realized = _entry_is_realized_for_mode(entry, view_mode)
        amount = _entry_amount(entry, realized=realized)
        is_internal = bool(entry.category and entry.category.is_internal)
        if is_internal:
            transfers_by_account[entry.account_id] += _entry_signed_amount(entry, realized=realized)
        else:
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
            internal_transfers=transfers_by_account.get(account.id, Decimal("0.00")).quantize(MONEY_QUANT),
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
