"""Views de Cadastros: Instituições e Contas financeiras."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.services import accessible_owner_ids
from core.permissions import permission_required

from .models import FinancialAccount, FinancialInstitution
from .services import (
    create_account,
    create_institution,
    delete_account,
    delete_institution,
    list_accounts_for_user,
    list_institutions,
    update_account,
    update_institution,
)

# --- Instituições ---

@login_required
@permission_required('tables.view')
@permission_required('tables.institutions.manage')
def institutions_view(request):
    """Lista e cadastro de instituições financeiras, com suporte a HTMX."""
    current_filter_type = request.GET.get('filter_type') or ''
    context = {
        "institutions": list_institutions(current_filter_type or None),
        "current_filter_type": current_filter_type,
    }
    if request.headers.get('HX-Request'):
        return render(request, 'tables/_institutions_table.html', context)
    return render(request, 'tables/banks.html', context)


@login_required
@permission_required('tables.view', fallback='banking:institutions_view')
@permission_required('tables.institutions.manage', fallback='banking:institutions_view')
@require_POST
def create_institution_view(request):
    try:
        create_institution(request.POST.get('institution_name', ''), request.POST.get('institution_type', ''))
        messages.success(request, "Instituição cadastrada com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request, 'banking:institutions_view')


@login_required
@permission_required('tables.view', fallback='banking:institutions_view')
@permission_required('tables.institutions.manage', fallback='banking:institutions_view')
@require_POST
def update_institution_view(request, institution_id):
    institution = get_object_or_404(FinancialInstitution, id=institution_id)
    try:
        update_institution(
            institution,
            request.POST.get('institution_name', ''),
            request.POST.get('institution_type', ''),
        )
        messages.success(request, "Instituição atualizada com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request, 'banking:institutions_view')


@login_required
@permission_required('tables.view', fallback='banking:institutions_view')
@permission_required('tables.institutions.manage', fallback='banking:institutions_view')
@require_POST
def delete_institution_view(request, institution_id):
    institution = get_object_or_404(FinancialInstitution, id=institution_id)
    try:
        delete_institution(institution)
        messages.success(request, "Instituição excluída com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request, 'banking:institutions_view')


# --- Contas ---

@login_required
@permission_required('tables.view')
@permission_required('tables.accounts.manage')
def accounts_view(request):
    """Lista e cadastro de contas financeiras, com suporte a HTMX."""
    current_filter_owner_id = request.GET.get('filter_owner_id') or ''
    current_filter_institution_id = request.GET.get('filter_institution_id') or ''
    context = {
        "accounts": list_accounts_for_user(
            request.user,
            owner_id=current_filter_owner_id or None,
            institution_id=current_filter_institution_id or None,
        ),
        "owners": _owners_for_form(request.user),
        "institutions": list_institutions(),
        "current_filter_owner_id": int(current_filter_owner_id) if current_filter_owner_id else None,
        "current_filter_institution_id": int(current_filter_institution_id) if current_filter_institution_id else None,
    }
    if request.headers.get('HX-Request'):
        return render(request, 'tables/_accounts_table.html', context)
    return render(request, 'tables/accounts.html', context)


def _owners_for_form(user):
    from accounts.models import AccountOwner

    return AccountOwner.objects.filter(id__in=accessible_owner_ids(user, "create"))


@login_required
@permission_required('tables.view', fallback='banking:accounts_view')
@permission_required('tables.accounts.manage', fallback='banking:accounts_view')
@require_POST
def create_account_view(request):
    try:
        create_account(
            request.user,
            owner_id=request.POST.get('owner_id', ''),
            institution_id=request.POST.get('institution_id', ''),
            account_name=request.POST.get('account_name', ''),
            initial_balance=request.POST.get('initial_balance', ''),
        )
        messages.success(request, "Conta cadastrada com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request, 'banking:accounts_view')


@login_required
@permission_required('tables.view', fallback='banking:accounts_view')
@permission_required('tables.accounts.manage', fallback='banking:accounts_view')
@require_POST
def update_account_view(request, account_id):
    account = get_object_or_404(FinancialAccount, id=account_id)
    try:
        update_account(
            request.user,
            account,
            owner_id=request.POST.get('owner_id', ''),
            institution_id=request.POST.get('institution_id', ''),
            account_name=request.POST.get('account_name', ''),
            initial_balance=request.POST.get('initial_balance', ''),
        )
        messages.success(request, "Conta atualizada com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request, 'banking:accounts_view')


@login_required
@permission_required('tables.view', fallback='banking:accounts_view')
@permission_required('tables.accounts.manage', fallback='banking:accounts_view')
@require_POST
def delete_account_view(request, account_id):
    account = get_object_or_404(FinancialAccount, id=account_id)
    try:
        delete_account(request.user, account)
        messages.success(request, "Conta excluída com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request, 'banking:accounts_view')


def _respond(request, redirect_name):
    if request.headers.get('HX-Request'):
        response = HttpResponse(status=204)
        response.headers['HX-Trigger'] = 'tableRefresh'
        return response
    return redirect(redirect_name)
