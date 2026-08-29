"""Views do módulo Gestão: tags, projetos/centros de custo e orçamento mensal."""
from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.htmx import quer_fragmento
from core.permissions import permission_required
from core.services import system_start_date
from reports import services as reports_services
from transactions.models import CashFlowCategory

from . import services


def _redirect_to_panel(request):
    query_string = request.POST.get("redirect_qs", "") or request.META.get("QUERY_STRING", "")
    url = "/management/"
    if query_string:
        url = f"{url}?{query_string}"
    return redirect(url)


@login_required
@permission_required("management.view")
def management_view(request):
    today = date.today()
    period_value = request.GET.get("month") or reports_services.month_input_value(date(today.year, today.month, 1))
    period_month = reports_services.parse_month_input(period_value) or date(today.year, today.month, 1)

    ctx = reports_services.selected_context(request.user, request.GET, request=request)
    options = reports_services.context_options(request.user, ctx)

    budget_rows = services.budget_rows_for_period(
        [o.id for o in options.owners], period_month.year, period_month.month
    )
    recent_entries = services.recent_classified_entries(options.account_ids, limit=50)

    context = {
        "title": "Gestão",
        "owners": options.owners,
        "banks": options.institutions,
        "accounts": options.accounts,
        "current_owner_id": ctx.owner_id,
        "current_institution_id": ctx.institution_id,
        "current_account_id": ctx.account_id,
        "period_value": reports_services.month_input_value(period_month),
        "system_start_date": system_start_date(),
        "categories": CashFlowCategory.objects.order_by("category_name"),
        "tags": services.list_tags(),
        "projects": services.list_projects(),
        "budget_rows": budget_rows,
        "recent_entries": recent_entries,
        "can_manage": request.user.has_perm("management.manage"),
    }
    if quer_fragmento(request):
        return render(request, "management/partials/management_content.html", context)
    return render(request, "management/index.html", context)


@login_required
@permission_required("management.manage", fallback="management:management_view")
@require_POST
def create_tag_view(request):
    if request.method == "POST":
        try:
            services.create_tag(request.POST.get("tag_name", ""))
            messages.success(request, "Tag criada com sucesso.")
        except ValueError as exc:
            messages.error(request, str(exc))
    return _redirect_to_panel(request)


@login_required
@permission_required("management.manage", fallback="management:management_view")
@require_POST
def create_project_view(request):
    if request.method == "POST":
        try:
            services.create_project(request.POST.get("project_name", ""), request.POST.get("description", ""))
            messages.success(request, "Projeto/centro de custo criado com sucesso.")
        except ValueError as exc:
            messages.error(request, str(exc))
    return _redirect_to_panel(request)


@login_required
@permission_required("management.manage", fallback="management:management_view")
@require_POST
def save_budget_view(request):
    if request.method == "POST":
        try:
            budget_month = reports_services.parse_month_input(request.POST.get("budget_month"))
            if budget_month is None:
                raise ValueError("Mês do orçamento é obrigatório.")
            budget = services.save_budget(
                request.user,
                owner_id=request.POST.get("owner_id"),
                category_id=request.POST.get("category_id"),
                year=budget_month.year,
                month=budget_month.month,
                planned_amount=request.POST.get("planned_amount"),
            )
            messages.success(
                request,
                f"Orçamento salvo: {budget.category.category_name} - {budget.month:02d}/{budget.year}.",
            )
        except ValueError as exc:
            messages.error(request, str(exc))
    return _redirect_to_panel(request)


@login_required
@permission_required("management.manage", fallback="management:management_view")
@require_POST
def assign_tag_view(request):
    if request.method == "POST":
        try:
            services.assign_tag_to_entry(request.user, request.POST.get("entry_id"), request.POST.get("tag_id"))
            messages.success(request, "Tag vinculada ao lançamento.")
        except ValueError as exc:
            messages.error(request, str(exc))
    return _redirect_to_panel(request)


@login_required
@permission_required("management.manage", fallback="management:management_view")
@require_POST
def assign_project_view(request):
    if request.method == "POST":
        try:
            services.assign_project_to_entry(
                request.user, request.POST.get("entry_id"), request.POST.get("project_id")
            )
            messages.success(request, "Projeto vinculado ao lançamento.")
        except ValueError as exc:
            messages.error(request, str(exc))
    return _redirect_to_panel(request)
