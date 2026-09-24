"""Views do dashboard com suporte a HTMX.

Nenhum gráfico do painel tem cálculo próprio. Eles tinham, e o cálculo próprio
divergia do resto do sistema em quatro pontos ao mesmo tempo: somava
transferência e movimentação como receita e despesa (um pagamento de fatura
inflava os dois lados), usava vencimento e valor previsto para o que já foi
realizado, lia o modo como um status só, e chamava de "saldo" uma soma que
começava em zero. Os números agora saem do mesmo motor dos relatórios:

- receitas, despesas, geração e cobertura por mês: `projection_months_between`,
  o da tela Projeções -- só categorias gerenciais;
- saldo: o saldo real, abertura mais movimentos, o mesmo da tela Lançamentos e
  da coluna Saldo das Projeções;
- data e valor por modo: realizado vale pela data e pelo valor da realização.

As contas saem de `selected_context` + `context_options`, como nas outras
telas: titular, instituição, conta e o filtro global de grupos
valem aqui exatamente como lá.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse

from banking.services import account_ids_by_currency
from core.account_group_filter import ACCOUNT_GROUP_OPTIONS, GROUPS_PARAM, is_filtering
from core.currency_filter import ALL_CURRENCIES, parse_currency_filter
from core.domain.finance import (
	BASE_CURRENCY,
	CURRENCY_OPTIONS,
	CURRENCY_SYMBOLS,
	ENTRY_TYPE_EXPENSE,
	ENTRY_TYPE_INCOME,
	STATUS_REALIZED,
	VIEW_ALL,
	VIEW_MODE_OPTIONS,
	VIEW_PENDING,
	VIEW_PROJECTED,
	normalize_view_mode,
)
from core.htmx import invalid_period_response, quer_fragmento, recusa_moedas_misturadas
from core.permissions import permission_required
from core.services import system_start_date
from reports.services import (
	InvalidMonthPeriodError,
	ReportSizeLimitError,
	context_options,
	decimal_period_start_balance,
	entries_for_period,
	entry_amount_for_view_mode,
	entry_date_for_view_mode,
	month_bounds,
	projection_months_between,
	selected_context,
)

MONEY_QUANT = Decimal("0.01")
# O painel mostra o mês escolhido no meio de uma janela de 13 meses.
WINDOW_BEFORE = 6
WINDOW_AFTER = 6


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
	total = (year * 12 + (month - 1)) + delta
	shifted_year = total // 12
	shifted_month = (total % 12) + 1
	return shifted_year, shifted_month


def _to_float(value: Decimal | int | float | None) -> float:
	if value is None:
		return 0.0
	return float(value)


def _month_label(year: int, month: int) -> str:
	return f"{month:02d}/{year}"


def _moeda_do_painel(pedida: str, moedas: tuple[str, ...]) -> tuple[str, bool]:
	"""A moeda que o painel mostra, e se ele teve de escolher no lugar da pessoa.

	Gráfico não tem bloco por moeda como as tabelas: duas moedas não cabem no
	mesmo eixo, e somá-las é o erro que `MixedCurrencyError` existe para
	impedir. Então o painel mostra uma moeda por vez:

	- a pedida, se houver conta dela no escopo;
	- a única do escopo, se só houver uma: a corretora em dólar traz o dólar;
	- com "Todas" e mais de uma moeda, a moeda base, avisando.
	"""
	if pedida in moedas:
		return pedida, False
	if len(moedas) == 1:
		return moedas[0], False
	if not moedas:
		return (BASE_CURRENCY if pedida == ALL_CURRENCIES else pedida), False
	return (BASE_CURRENCY if BASE_CURRENCY in moedas else moedas[0]), True


def _saldo_diario(account_ids, month_start: date, next_month: date, view_mode: str, month_entries) -> list[tuple[date, Decimal]]:
	"""Saldo ao fim de cada dia com movimento, pelo mesmo cálculo do extrato.

	Abertura do período mais cada lançamento, na ordem da listagem -- é o
	`running_balance` da tela Lançamentos (`compute_statement`). Aqui entra tudo,
	inclusive transferência: ela não é receita nem despesa, mas muda o saldo.
	"""
	saldo = decimal_period_start_balance(account_ids, month_start, next_month, view_mode)
	por_dia: dict[date, Decimal] = {}
	for entry in month_entries:
		if view_mode in {VIEW_PROJECTED, VIEW_PENDING} and entry.status == STATUS_REALIZED:
			continue
		valor = entry_amount_for_view_mode(entry, view_mode)
		saldo += valor if entry.entry_type == ENTRY_TYPE_INCOME else -valor
		por_dia[entry_date_for_view_mode(entry, view_mode)] = saldo.quantize(MONEY_QUANT)
	return list(por_dia.items())


def _categorias(month_entries, view_mode: str, filter_type: str) -> list[tuple[str, Decimal]]:
	"""Total por categoria gerencial no mês, maior primeiro.

	Transferência e movimentação ficam fora, como no drill-down para
	Lançamentos (`dashboard_drilldown=1`): a fatia do gráfico tem de ser a
	soma da tela que ele abre.
	"""
	totais: dict[str, Decimal] = {}
	for entry in month_entries:
		if entry.entry_type != filter_type or entry.category is None or entry.category.is_internal:
			continue
		nome = entry.category.category_name
		totais[nome] = totais.get(nome, Decimal("0.00")) + entry_amount_for_view_mode(entry, view_mode)
	return sorted(totais.items(), key=lambda item: (-item[1], item[0]))


def _saude(months: list[dict], selected_index: int) -> dict:
	income = [m["receita"].quantize(MONEY_QUANT) for m in months]
	expense = [m["despesa"].quantize(MONEY_QUANT) for m in months]
	generation = [(i - e).quantize(MONEY_QUANT) for i, e in zip(income, expense, strict=True)]
	coverage = [None if e <= 0 else (i / e).quantize(MONEY_QUANT) for i, e in zip(income, expense, strict=True)]

	moving_average: list[Decimal | None] = []
	for idx in range(len(generation)):
		if idx < 2:
			moving_average.append(None)
			continue
		moving_average.append((sum(generation[idx - 2 : idx + 1], Decimal("0.00")) / 3).quantize(MONEY_QUANT))

	valid_coverage = [v for v in coverage if v is not None]
	avg_coverage = (sum(valid_coverage, Decimal("0.00")) / len(valid_coverage)) if valid_coverage else Decimal("0.00")

	# Mês sem movimento nenhum não entra na conta, e mês que empatou não
	# "fechou com sobra": o cartão diz sobra, então só conta geração positiva.
	with_movement = [idx for idx in range(len(months)) if income[idx] or expense[idx]]
	positive_months = len([idx for idx in with_movement if generation[idx] > 0])

	# Os 3 meses que terminam no escolhido contra os 3 anteriores. Era o fim da
	# janela contra o meio dela -- dois trimestres futuros, que o rótulo
	# "últimos 3m" não descrevia.
	last3 = generation[max(0, selected_index - 2) : selected_index + 1]
	prev3 = generation[max(0, selected_index - 5) : max(0, selected_index - 2)]
	prev_sum = sum(prev3, Decimal("0.00"))
	last_sum = sum(last3, Decimal("0.00"))
	if prev3 and prev_sum != 0:
		trend_percent = ((last_sum - prev_sum) / abs(prev_sum)) * 100
	elif prev3:
		trend_percent = Decimal("100.0") if last_sum > 0 else Decimal("0.0")
	else:
		trend_percent = Decimal("0.0")

	if trend_percent > Decimal("5"):
		trend = ("amount-positive", "↑", "Melhora consistente")
	elif trend_percent < Decimal("-5"):
		trend = ("amount-negative", "↓", "Queda de geração")
	else:
		trend = ("amount-neutral", "→", "Estabilidade")

	return {
		"income": income,
		"expense": expense,
		"generation": generation,
		"summary": {
			"average_coverage": f"{avg_coverage:.2f}x",
			"average_coverage_class": "amount-positive" if avg_coverage >= 1 else "amount-negative",
			"positive_months": positive_months,
			"total_months": len(with_movement),
			"trend_class": trend[0],
			"trend_symbol": trend[1],
			"trend_percent": f"{trend_percent:+.1f}%",
			"trend_caption": trend[2],
			"coverage": [_to_float(v) if v is not None else None for v in coverage],
			"generation": [_to_float(v) for v in generation],
			"moving_average": [_to_float(v) if v is not None else None for v in moving_average],
		},
	}


@login_required
@permission_required("dashboard.view")
@recusa_moedas_misturadas
def dashboard_view(request):
	"""Renderiza o dashboard completo ou parcial (HTMX)."""
	today = date.today()
	raw_period = (request.GET.get("period") or today.strftime("%Y-%m")).strip()
	try:
		selected_year, selected_month = [int(p) for p in raw_period.split("-", 1)]
		if selected_month < 1 or selected_month > 12:
			raise ValueError
	except (TypeError, ValueError):
		selected_year = today.year
		selected_month = today.month
	selected_period = f"{selected_year:04d}-{selected_month:02d}"

	view_mode = normalize_view_mode((request.GET.get("mode") or "").strip().lower(), default=VIEW_ALL)

	filter_type = (request.GET.get("filter_type") or ENTRY_TYPE_EXPENSE).strip().lower()
	if filter_type not in {ENTRY_TYPE_INCOME, ENTRY_TYPE_EXPENSE}:
		filter_type = ENTRY_TYPE_EXPENSE

	try:
		month_bounds(selected_year, selected_month)
		month_bounds(*_shift_month(selected_year, selected_month, WINDOW_AFTER))
	except InvalidMonthPeriodError as exc:
		return invalid_period_response(request, str(exc))

	ctx = selected_context(request.user, request.GET, request=request)
	options = context_options(request.user, ctx)
	# Das contas do escopo, e não da lista do seletor: o seletor já vem
	# recortado pela moeda pedida, e é daqui que sai a moeda que o painel
	# escolhe quando a pedida não tem conta (a corretora em dólar traz o dólar).
	contas_por_moeda = account_ids_by_currency(options.account_ids)
	moedas = tuple(contas_por_moeda)
	currency, currency_notice = _moeda_do_painel(parse_currency_filter(request.GET), moedas)
	account_ids = contas_por_moeda.get(currency, [])

	first_month = date(*_shift_month(selected_year, selected_month, -WINDOW_BEFORE), 1)
	last_month = date(*_shift_month(selected_year, selected_month, WINDOW_AFTER), 1)
	month_start = date(selected_year, selected_month, 1)
	next_month = date(*_shift_month(selected_year, selected_month, 1), 1)
	try:
		# A janela é lida uma vez: o motor mensal e o recorte do mês usam a mesma lista.
		window_entries = entries_for_period(
			account_ids, first_month, date(*_shift_month(last_month.year, last_month.month, 1), 1), view_mode
		)
		months = projection_months_between(
			account_ids, first_month, last_month, view_mode, period_entries=window_entries
		)
	except (InvalidMonthPeriodError, ReportSizeLimitError) as exc:
		return invalid_period_response(request, str(exc))
	month_entries = [
		entry for entry in window_entries
		if month_start <= entry_date_for_view_mode(entry, view_mode) < next_month
	]

	categorias = _categorias(month_entries, view_mode, filter_type)
	saldo_diario = _saldo_diario(account_ids, month_start, next_month, view_mode, month_entries)

	chart_periods = [m["month"] for m in months]
	chart_labels = [_month_label(*(int(part) for part in period.split("-"))) for period in chart_periods]
	selected_index = chart_periods.index(selected_period)
	saude = _saude(months, selected_index)
	chart_income = [_to_float(v) for v in saude["income"]]
	chart_expense = [_to_float(v) for v in saude["expense"]]
	chart_generation = saude["summary"]["generation"]
	chart_saldo = [_to_float(m["saldo"]) for m in months]

	financial_health = {**saude["summary"], "labels": chart_labels}
	account_groups = ctx.account_groups
	groups_param = (
		",".join(code for code, _label in ACCOUNT_GROUP_OPTIONS if code in account_groups)
		if is_filtering(account_groups)
		else ""
	)

	chart_data = {
		"projMonths": chart_labels,
		"projRec": chart_income,
		"projDesp": chart_expense,
		"projSaldo": chart_saldo,
		"catLabels": [nome for nome, _total in categorias],
		"catValues": [_to_float(total) for _nome, total in categorias],
		"dailyDates": [dia.strftime("%d/%m") for dia, _saldo in saldo_diario],
		"dailyBal": [_to_float(saldo) for _dia, saldo in saldo_diario],
		"chartPeriods": chart_periods,
		"chartLabels": chart_labels,
		"chartIncome": chart_income,
		"chartExpense": chart_expense,
		"chartBalance": chart_generation,
		"selectedPeriod": selected_period,
		# Os gráficos escreviam `R$` fixo em tooltips e eixos. O símbolo é da
		# moeda que o painel está mostrando, e quem sabe qual é é o servidor.
		"currencySymbol": CURRENCY_SYMBOLS.get(currency, CURRENCY_SYMBOLS[BASE_CURRENCY]),
		"viewMode": view_mode,
		"filterType": filter_type,
		"currentOwnerId": ctx.owner_id,
		"currentInstitutionId": ctx.institution_id,
		"currentAccountId": ctx.account_id,
		# O drill-down abre Lançamentos na mesma moeda e nos mesmos grupos.
		"currency": currency,
		"accountGroups": groups_param,
		"groupsParam": GROUPS_PARAM,
		"transactionsUrl": reverse("transactions:transactions_view"),
		"health": financial_health,
	}

	context = {
		"currency": currency,
		"currency_label": dict(CURRENCY_OPTIONS).get(currency, currency),
		"currency_notice": currency_notice,
		"selected_period": selected_period,
		"selected_year": selected_year,
		"selected_month": selected_month,
		"today_period": today.strftime("%Y-%m"),
		"system_start_date": system_start_date(),
		"view_mode": view_mode,
		"view_mode_label": dict(VIEW_MODE_OPTIONS).get(view_mode, view_mode),
		"view_mode_options": VIEW_MODE_OPTIONS,
		"filter_type": filter_type,
		"owners": options.owners,
		"banks": options.institutions,
		"accounts": options.accounts,
		"current_owner_id": ctx.owner_id,
		"current_institution_id": ctx.institution_id,
		"current_account_id": ctx.account_id,
		"chart_proj_months": chart_labels,
		"chart_cats_labels": chart_data["catLabels"],
		"daily_dates": chart_data["dailyDates"],
		"financial_health": financial_health,
		"chart_data": chart_data,
	}

	if quer_fragmento(request):
		return render(request, "dashboard/_content.html", context)
	return render(request, "dashboard/index.html", context)


@login_required
@permission_required("dashboard.view")
def dashboard_content(request):
	"""Alias para carregamento parcial via HTMX."""
	return dashboard_view(request)
