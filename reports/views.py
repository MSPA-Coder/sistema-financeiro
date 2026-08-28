"""Views de Relatórios: Projeções, Movimentos futuros e Posição por conta."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.domain.finance import (
    VIEW_ALL,
    VIEW_MODE_OPTIONS,
    VIEW_PROJECTED,
    VIEW_REALIZED,
    normalize_view_mode,
)
from core.permissions import permission_required
from core.services import system_start_date

from . import services


def _parse_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@login_required
def projections_view(request):
    today = date.today()
    view_mode = normalize_view_mode(request.GET.get("mode", VIEW_PROJECTED))
    projection_detail = request.GET.get("detail", "complete")
    if projection_detail not in {"complete", "summary"}:
        projection_detail = "complete"
    show_status_columns = projection_detail == "complete"

    start_month, end_month = services.resolve_projection_month_range(
        request.GET.get("start_month"), request.GET.get("end_month"), today=today
    )
    default_start_month, default_end_month = services.resolve_projection_month_range(None, None, today=today)

    ctx = services.selected_context(request.user, request.GET)
    options = services.context_options(request.user, ctx, hidden_scope="projections")
    month_data = services.projection_months_between(options.account_ids, start_month, end_month, view_mode)

    context = {
        "month_data": month_data,
        "period_totals": services.projection_period_totals(month_data),
        "start_month": services.month_input_value(start_month),
        "end_month": services.month_input_value(end_month),
        "default_start_month": services.month_input_value(default_start_month),
        "default_end_month": services.month_input_value(default_end_month),
        "view_mode": view_mode,
        "view_mode_options": VIEW_MODE_OPTIONS,
        "projection_detail": projection_detail,
        "show_status_columns": show_status_columns,
        "owners": options.owners,
        "banks": options.institutions,
        "accounts": options.accounts,
        "current_owner_id": ctx.owner_id,
        "current_institution_id": ctx.institution_id,
        "current_account_id": ctx.account_id,
        "system_start_date": system_start_date(),
    }

    if request.headers.get("HX-Request"):
        return render(request, "reports/partials/projections_content.html", context)
    return render(request, "reports/projections.html", context)


@login_required
def upcoming_movements_view(request):
    default_start, default_end = services.current_week_period()
    start_date = services.parse_iso_date(request.GET.get("start_date")) or default_start
    end_date = services.parse_iso_date(request.GET.get("end_date")) or default_end
    if end_date < start_date:
        end_date = start_date

    view_mode = services.normalize_upcoming_movement_mode(request.GET.get("mode", VIEW_PROJECTED))
    ctx = services.selected_context(request.user, request.GET)
    options = services.context_options(request.user, ctx)
    report = services.upcoming_movements_report(options.account_ids, start_date, end_date, view_mode)

    status_options = [opt for opt in VIEW_MODE_OPTIONS if opt[0] in services.UPCOMING_MOVEMENT_VIEW_MODES]

    context = {
        "report": report,
        "start_date": start_date,
        "end_date": end_date,
        "default_start_date": default_start,
        "default_end_date": default_end,
        "view_mode": view_mode,
        "status_options": status_options,
        "owners": options.owners,
        "banks": options.institutions,
        "accounts": options.accounts,
        "current_owner_id": ctx.owner_id,
        "current_institution_id": ctx.institution_id,
        "current_account_id": ctx.account_id,
        "system_start_date": system_start_date(),
    }

    if request.headers.get("HX-Request"):
        return render(request, "reports/partials/upcoming_movements_content.html", context)
    return render(request, "reports/upcoming_movements.html", context)


@login_required
def account_position_view(request):
    today = date.today()
    view_mode = normalize_view_mode(request.GET.get("mode"), default=VIEW_REALIZED)
    year, month = services.resolve_month_period(
        request.GET.get("period"),
        _parse_int(request.GET.get("year")),
        _parse_int(request.GET.get("month")),
        today,
    )
    selected_month = date(year, month, 1)
    selected_period = services.month_input_value(selected_month)
    today_period = services.month_input_value(date(today.year, today.month, 1))

    selected_ctx = services.selected_context(request.user, request.GET)
    ctx = services.FinancialContext(owner_id=selected_ctx.owner_id, institution_id=selected_ctx.institution_id, account_id=None)
    options = services.context_options(request.user, ctx)
    rows = services.account_cash_report_rows(options.account_ids, selected_month, selected_month, view_mode)

    context = {
        "report_rows": rows,
        "total_start": sum((row.start_balance for row in rows), Decimal("0.00")),
        "total_generation": sum((row.cash_generation for row in rows), Decimal("0.00")),
        "total_end": sum((row.end_balance for row in rows), Decimal("0.00")),
        "selected_period": selected_period,
        "today_period": today_period,
        "view_mode": view_mode,
        "view_mode_options": VIEW_MODE_OPTIONS,
        "owners": options.owners,
        "banks": options.institutions,
        "current_owner_id": ctx.owner_id,
        "current_institution_id": ctx.institution_id,
        "current_account_id": selected_ctx.account_id,
        "system_start_date": system_start_date(),
    }

    if request.headers.get("HX-Request"):
        return render(request, "reports/partials/account_position_content.html", context)
    return render(request, "reports/account_position.html", context)


@login_required
@permission_required("reports.annual_planning.view")
def annual_planning_view(request):
    """Planejamento por categoria: mês-base individual e meses consolidados."""
    today = date.today()
    reference_month = services.parse_month_input(request.GET.get("reference_month")) or date(
        today.year, today.month, 1
    )
    layout_value = request.GET.get("layout", "calendar")
    layout = (
        services.ANNUAL_PLANNING_ROLLING_13
        if layout_value == "rolling"
        else services.ANNUAL_PLANNING_CALENDAR
    )
    view_mode = normalize_view_mode(request.GET.get("mode"), default=VIEW_ALL)
    owner_ids = services._planning_id_filter(request.GET.getlist("owner_ids"))
    account_ids = services._planning_id_filter(request.GET.getlist("account_ids"))
    allowed_owner_ids = set(services.accessible_owner_ids(request.user, "view"))
    selected_owner_ids = (
        sorted(allowed_owner_ids)
        if owner_ids is None
        else sorted(allowed_owner_ids.intersection(owner_ids))
    )
    owners = list(
        services.AccountOwner.objects.filter(id__in=allowed_owner_ids).order_by("name", "id")
    )
    accounts = list(
        services.FinancialAccount.objects.select_related("owner", "institution")
        .filter(owner_id__in=selected_owner_ids)
        .order_by("owner__name", "institution__institution_name", "account_name", "id")
    )
    allowed_account_ids = {account.id for account in accounts}
    selected_account_ids = (
        sorted(allowed_account_ids)
        if account_ids is None
        else sorted(allowed_account_ids.intersection(account_ids))
    )
    show_descriptions = request.GET.get("show_descriptions") == "1"
    report = services.annual_planning_presentation(
        request.user,
        reference_month,
        owner_ids=owner_ids,
        account_ids=account_ids,
        layout=layout,
        view_mode=view_mode,
        show_descriptions=show_descriptions,
    )
    context = {
        "report": report,
        "reference_month": services.month_input_value(reference_month),
        "default_reference_month": services.month_input_value(date(today.year, today.month, 1)),
        "layout": layout_value if layout_value in {"calendar", "rolling"} else "calendar",
        "view_mode": view_mode,
        "status_options": VIEW_MODE_OPTIONS,
        "owners": owners,
        "accounts": accounts,
        "selected_owner_ids": selected_owner_ids,
        "selected_account_ids": selected_account_ids,
        "filter_panel_open": request.GET.get("filters_open") == "1",
        "show_descriptions": show_descriptions,
        "system_start_date": system_start_date(),
    }
    if request.headers.get("HX-Request"):
        return render(request, "reports/partials/annual_planning_content.html", context)
    return render(request, "reports/annual_planning.html", context)
