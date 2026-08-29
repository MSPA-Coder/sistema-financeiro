"""Views do dashboard com suporte a HTMX."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import render
from django.urls import reverse

from accounts.models import AccountOwner
from accounts.services import accessible_owner_ids, hidden_account_ids
from core.domain.finance import ENTRY_TYPE_EXPENSE, ENTRY_TYPE_INCOME, STATUS_REALIZED
from core.htmx import quer_fragmento
from core.permissions import permission_required
from core.services import system_start_date
from transactions.models import CashFlowEntry


def _month_start_end(year: int, month: int) -> tuple[date, date]:
	end_day = monthrange(year, month)[1]
	return date(year, month, 1), date(year, month, end_day)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
	total = (year * 12 + (month - 1)) + delta
	shifted_year = total // 12
	shifted_month = (total % 12) + 1
	return shifted_year, shifted_month


def _parse_int(value: str | None) -> int | None:
	if value is None or value == "":
		return None
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _to_float(value: Decimal | int | float | None) -> float:
	if value is None:
		return 0.0
	return float(value)


def _month_label(year: int, month: int) -> str:
	return f"{month:02d}/{year}"


@login_required
@permission_required("dashboard.view", fallback="reports:upcoming_movements_view")
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
		raw_period = f"{selected_year:04d}-{selected_month:02d}"

	view_mode = (request.GET.get("mode") or "todos").strip().lower()
	valid_modes = {"todos", "a_vencer", "vencidos", "realizado"}
	if view_mode not in valid_modes:
		view_mode = "todos"

	filter_type = (request.GET.get("filter_type") or ENTRY_TYPE_EXPENSE).strip().lower()
	if filter_type not in {ENTRY_TYPE_INCOME, ENTRY_TYPE_EXPENSE}:
		filter_type = ENTRY_TYPE_EXPENSE

	owner_id = _parse_int(request.GET.get("owner_id"))
	institution_id = _parse_int(request.GET.get("institution_id"))
	account_id = _parse_int(request.GET.get("account_id"))

	# Mesmo escopo de acesso das demais telas. O join direto em
	# UserOwnerAccess que existia aqui ignorava o acesso amplo de
	# administrador/super user: quem nao tivesse concessao explicita via um
	# dashboard vazio enquanto enxergava tudo no resto do sistema.
	allowed_owner_ids = accessible_owner_ids(request.user, "view")

	entries_qs = CashFlowEntry.objects.select_related("account", "category").filter(
		account__owner_id__in=allowed_owner_ids,
	)

	if owner_id:
		entries_qs = entries_qs.filter(account__owner_id=owner_id)
	if institution_id:
		entries_qs = entries_qs.filter(account__institution_id=institution_id)
	if account_id:
		entries_qs = entries_qs.filter(account_id=account_id)

	# Os seletores de banco/conta continuam listando tudo a que o usuario tem
	# acesso, inclusive o que ele ocultou: se a conta sumisse da lista, ele
	# nao teria mais como escolhe-la para ver isoladamente.
	selector_qs = entries_qs

	if not account_id:
		# Preferencia pessoal de Configuracoes > Contas em analises: as contas
		# marcadas saem dos agregados do dashboard. Vale so para a visao
		# agregada -- escolher a conta explicitamente no filtro vence, senao a
		# tela ficaria vazia sem explicar por que.
		hidden_ids = hidden_account_ids(request.user, "dashboard")
		if hidden_ids:
			entries_qs = entries_qs.exclude(account_id__in=hidden_ids)

	if view_mode != "todos":
		entries_qs = entries_qs.filter(status=view_mode)
		selector_qs = selector_qs.filter(status=view_mode)

	month_start, month_end = _month_start_end(selected_year, selected_month)
	month_entries = entries_qs.filter(due_date__gte=month_start, due_date__lte=month_end)

	categories_data = list(
		month_entries.filter(entry_type=filter_type)
		.values("category__category_name")
		.annotate(total=Sum("entry_amount"))
		.order_by("-total")
	)
	chart_cats_labels = [row["category__category_name"] for row in categories_data]
	chart_cats_values = [_to_float(row["total"]) for row in categories_data]

	daily_data = list(
		month_entries.values("due_date", "entry_type").annotate(total=Sum("entry_amount")).order_by("due_date")
	)
	daily_delta: dict[date, float] = {}
	for row in daily_data:
		row_date = row["due_date"]
		signed = _to_float(row["total"]) if row["entry_type"] == ENTRY_TYPE_INCOME else -_to_float(row["total"])
		daily_delta[row_date] = daily_delta.get(row_date, 0.0) + signed
	daily_dates = [d.strftime("%d/%m") for d in sorted(daily_delta)]
	daily_balance: list[float] = []
	acc = 0.0
	for row_date in sorted(daily_delta):
		acc += daily_delta[row_date]
		daily_balance.append(acc)

	# Janela comum aos gráficos de projeção e evolução: mês selecionado ±6.
	# A agregação única evita uma consulta por mês para cada gráfico.
	month_offsets = [_shift_month(selected_year, selected_month, offset) for offset in range(-6, 7)]
	range_start, _ = _month_start_end(*month_offsets[0])
	_, range_end = _month_start_end(*month_offsets[-1])
	grouped_by_month = (
		entries_qs.filter(due_date__gte=range_start, due_date__lte=range_end)
		.annotate(bucket_month=TruncMonth("due_date"))
		.values("bucket_month", "entry_type")
		.annotate(total=Sum("entry_amount"))
	)
	totals_by_month: dict[tuple[int, int], dict[str, float]] = {}
	for row in grouped_by_month:
		key = (row["bucket_month"].year, row["bucket_month"].month)
		bucket = totals_by_month.setdefault(key, {"income": 0.0, "expense": 0.0})
		if row["entry_type"] == ENTRY_TYPE_INCOME:
			bucket["income"] = _to_float(row["total"])
		else:
			bucket["expense"] = _to_float(row["total"])

	monthly_points: list[tuple[str, str, float, float]] = []
	for year_i, month_i in month_offsets:
		bucket = totals_by_month.get((year_i, month_i), {"income": 0.0, "expense": 0.0})
		monthly_points.append((f"{year_i:04d}-{month_i:02d}", _month_label(year_i, month_i), bucket["income"], bucket["expense"]))

	chart_periods = [p[0] for p in monthly_points]
	chart_labels = [p[1] for p in monthly_points]
	chart_income = [p[2] for p in monthly_points]
	chart_expense = [p[3] for p in monthly_points]
	chart_balance = [round(p[2] - p[3], 2) for p in monthly_points]
	chart_proj_months = chart_labels[:]
	chart_proj_receitas = chart_income[:]
	chart_proj_despesas = chart_expense[:]
	chart_proj_saldo: list[float] = []
	running_balance = 0.0
	for generation in chart_balance:
		running_balance += generation
		chart_proj_saldo.append(round(running_balance, 2))

	health_labels = chart_labels[:]
	health_coverage: list[float | None] = []
	health_generation: list[float] = []
	for income, expense in zip(chart_income, chart_expense, strict=True):
		generation = round(income - expense, 2)
		health_generation.append(generation)
		if expense <= 0:
			health_coverage.append(None)
		else:
			health_coverage.append(round(income / expense, 2))

	moving_average: list[float | None] = []
	for idx in range(len(health_generation)):
		if idx < 2:
			moving_average.append(None)
			continue
		avg3 = sum(health_generation[idx - 2 : idx + 1]) / 3
		moving_average.append(round(avg3, 2))

	valid_coverage = [v for v in health_coverage if v is not None]
	avg_coverage_val = (sum(valid_coverage) / len(valid_coverage)) if valid_coverage else 0.0
	avg_coverage_class = "amount-positive" if avg_coverage_val >= 1 else "amount-negative"
	positive_months = len([v for v in health_generation if v >= 0])
	total_months = len(health_generation)

	prev3 = health_generation[-6:-3]
	last3 = health_generation[-3:]
	prev_sum = sum(prev3) if prev3 else 0.0
	last_sum = sum(last3) if last3 else 0.0
	if prev3 and prev_sum != 0:
		trend_percent = ((last_sum - prev_sum) / abs(prev_sum)) * 100.0
	elif prev3:
		trend_percent = 100.0 if last_sum > 0 else 0.0
	else:
		trend_percent = 0.0

	if trend_percent > 5:
		trend_class = "amount-positive"
		trend_symbol = "↑"
		trend_caption = "Melhora consistente"
	elif trend_percent < -5:
		trend_class = "amount-negative"
		trend_symbol = "↓"
		trend_caption = "Queda de geração"
	else:
		trend_class = "amount-neutral"
		trend_symbol = "→"
		trend_caption = "Estabilidade"

	financial_health = {
		"average_coverage": f"{avg_coverage_val:.2f}x",
		"average_coverage_class": avg_coverage_class,
		"positive_months": positive_months,
		"total_months": total_months,
		"trend_class": trend_class,
		"trend_symbol": trend_symbol,
		"trend_percent": f"{trend_percent:+.1f}%",
		"trend_caption": trend_caption,
		"labels": health_labels,
		"coverage": health_coverage,
		"generation": health_generation,
		"moving_average": moving_average,
	}

	chart_data = {
		"projMonths": chart_proj_months,
		"projRec": chart_proj_receitas,
		"projDesp": chart_proj_despesas,
		"projSaldo": chart_proj_saldo,
		"catLabels": chart_cats_labels,
		"catValues": chart_cats_values,
		"dailyDates": daily_dates,
		"dailyBal": daily_balance,
		"chartPeriods": chart_periods,
		"chartLabels": chart_labels,
		"chartIncome": chart_income,
		"chartExpense": chart_expense,
		"chartBalance": chart_balance,
		"selectedPeriod": raw_period,
		"viewMode": view_mode,
		"filterType": filter_type,
		"currentOwnerId": owner_id,
		"currentInstitutionId": institution_id,
		"currentAccountId": account_id,
		"transactionsUrl": reverse("transactions:transactions_view"),
		"health": financial_health,
	}

	owner_options = AccountOwner.objects.filter(id__in=allowed_owner_ids).order_by("name")

	context = {
		"selected_period": raw_period,
		"selected_year": selected_year,
		"selected_month": selected_month,
		"today_period": today.strftime("%Y-%m"),
		"system_start_date": system_start_date(),
		"view_mode": view_mode,
		"view_mode_options": [
			("todos", "Todos"),
			("a_vencer", "A vencer"),
			("vencidos", "Vencidos"),
			(STATUS_REALIZED, "Realizado"),
		],
		"filter_type": filter_type,
		"owners": owner_options,
		"banks": selector_qs.values("account__institution_id", "account__institution__institution_name").distinct().order_by("account__institution__institution_name"),
		"accounts": selector_qs.values("account_id", "account__account_name").distinct().order_by("account__account_name"),
		"current_owner_id": owner_id,
		"current_institution_id": institution_id,
		"current_account_id": account_id,
		"chart_proj_months": chart_proj_months,
		"chart_proj_receitas": chart_proj_receitas,
		"chart_proj_despesas": chart_proj_despesas,
		"chart_proj_saldo": chart_proj_saldo,
		"chart_cats_labels": chart_cats_labels,
		"chart_cats_values": chart_cats_values,
		"daily_dates": daily_dates,
		"daily_balance": daily_balance,
		"chart_periods": chart_periods,
		"chart_labels": chart_labels,
		"chart_income": chart_income,
		"chart_expense": chart_expense,
		"chart_balance": chart_balance,
		"financial_health": financial_health,
		"chart_data": chart_data,
	}

	if quer_fragmento(request):
		return render(request, "dashboard/_content.html", context)
	return render(request, "dashboard/index.html", context)


@login_required
@permission_required("dashboard.view", fallback="reports:upcoming_movements_view")
def dashboard_content(request):
	"""Alias para carregamento parcial via HTMX."""
	return dashboard_view(request)
