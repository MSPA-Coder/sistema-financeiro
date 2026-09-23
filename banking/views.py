"""Views de Cadastros e detalhamento de contas financeiras."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.services import accessible_owner_ids
from core.domain.finance import (
    ACCOUNT_KIND_OPTIONS,
    ACCOUNT_KIND_REGULAR,
    CURRENCY_OPTIONS,
    VIEW_PROJECTED,
    VIEW_REALIZED,
)
from core.htmx import quer_fragmento
from core.patrimonio import saldo_da_conta
from core.permissions import permission_required
from reports.services import month_input_value
from transactions.models import AccountMonthClose
from transactions.services import compute_statement, resolve_statement_request

from .models import FinancialAccount, FinancialInstitution
from .services import (
    can_access_account,
    create_account,
    create_institution,
    delete_account,
    delete_institution,
    list_accounts_for_user,
    list_institutions,
    update_account,
    update_institution,
)


def _referencia_da_conta(request) -> date:
    """Data pontual para o saldo que o contrato patrimonial publica.

    ``data`` é opcional na navegação normal, mas precisa manter o significado
    quando o NetWorth abre uma foto histórica. O período mensal continua sendo
    resolvido pelo contexto de transações.
    """
    raw_value = request.GET.get("data", "")
    if raw_value:
        try:
            return date.fromisoformat(raw_value)
        except ValueError as exc:
            raise ValueError("Data informada é inválida.") from exc

    raw_period = request.GET.get("period", "")
    if raw_period:
        from reports.services import month_bounds, parse_month_input

        period = parse_month_input(raw_period)
        if period is None:
            raise ValueError("Período informado é inválido.")
        # Para um período que ainda está aberto, "fim do mês" ainda não é uma
        # foto real. O saldo não pode incorporar um futuro que não aconteceu.
        return min(month_bounds(period.year, period.month)[1] - timedelta(days=1), timezone.localdate())
    return timezone.localdate()


def _recorte_da_conta(request, account_id: int, referencia: date):
    """Recorte do extrato de uma conta, o mesmo da tela Lançamentos.

    É resolvido uma vez e serve aos dois modos: previsto e realizado diferem só
    no cálculo, não em conta, período ou filtros.
    """
    params = request.GET.copy()
    params["account_id"] = str(account_id)
    # Um endereço publicado traz somente ``data``. Sem este recorte, ele
    # explicaria o saldo histórico com o extrato do mês atual.
    if not params.get("period"):
        params["period"] = referencia.strftime("%Y-%m")
    # Sessão descartável: com ``period`` sempre presente, a sessão não é lida,
    # e gravá-la faria esta página trocar o mês lembrado pela tela Lançamentos.
    # O link "Ver lançamentos" já leva o período na URL.
    return resolve_statement_request(request.user, params, {}, request=request).scope


@login_required
@permission_required("transactions.view")
def account_detail_view(request, account_id):
    """Posição de uma conta, com extrato e comparação previsto x realizado."""
    account = get_object_or_404(
        FinancialAccount.objects.select_related("owner", "institution"),
        id=account_id,
    )
    # O identificador da conta é opaco fora deste sistema. Responder 404 para
    # quem não tem acesso evita confirmar que uma conta de outro titular existe.
    if not can_access_account(request.user, account.id, "view"):
        raise Http404

    try:
        referencia = _referencia_da_conta(request)
        recorte = _recorte_da_conta(request, account.id, referencia)
        # O cálculo também pode recusar o recorte (`ReportSizeLimitError` é um
        # `ValueError`), e a recusa tem de chegar como a mesma resposta.
        txs, blocos_realizados = compute_statement(recorte, VIEW_REALIZED)
        _txs_previstas, blocos_previstos = compute_statement(recorte, VIEW_PROJECTED)
    except ValueError as exc:
        from core.htmx import invalid_period_response

        return invalid_period_response(request, str(exc))

    bloco_realizado = blocos_realizados[0]
    bloco_previsto = blocos_previstos[0]
    ultimo_fechamento = (
        AccountMonthClose.objects.select_related("closed_by_user")
        .filter(account=account, active=True)
        .order_by("-year", "-month")
        .first()
    )
    context = {
        "account": account,
        "reference_date": referencia,
        "saldo_atual": saldo_da_conta(account, referencia),
        "realizado": bloco_realizado,
        "previsto": bloco_previsto,
        "variacao_previsto_realizado": (
            bloco_previsto["saldo_final"] - bloco_realizado["saldo_final"]
        ).quantize(Decimal("0.01")),
        "ultimo_fechamento": ultimo_fechamento,
        "txs": txs,
        "selected_period": month_input_value(recorte.start_selected),
    }
    return render(request, "banking/account_detail.html", context)

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
    if quer_fragmento(request):
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
        "currencies": CURRENCY_OPTIONS,
        "account_kinds": ACCOUNT_KIND_OPTIONS,
        # Candidatas a conta de pagamento padrão de um cartão: contas comuns
        # que o usuário enxerga. A validação final é do service.
        "payment_accounts": [
            account for account in list_accounts_for_user(request.user)
            if account.account_kind == ACCOUNT_KIND_REGULAR
        ],
        "current_filter_owner_id": int(current_filter_owner_id) if current_filter_owner_id else None,
        "current_filter_institution_id": int(current_filter_institution_id) if current_filter_institution_id else None,
        "can_view_account_details": request.user.has_perm("transactions.view"),
    }
    if quer_fragmento(request):
        return render(request, 'tables/_accounts_table.html', context)
    return render(request, 'tables/accounts.html', context)


def _card_fields_from_post(request) -> dict[str, str]:
    return {
        "account_kind": request.POST.get("account_kind", ""),
        "card_closing_day": request.POST.get("card_closing_day", ""),
        "card_due_day": request.POST.get("card_due_day", ""),
        "card_payment_account_id": request.POST.get("card_payment_account_id", ""),
    }


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
            currency=request.POST.get('currency', ''),
            initial_balance_date=request.POST.get('initial_balance_date', ''),
            **_card_fields_from_post(request),
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
            currency=request.POST.get('currency', ''),
            initial_balance_date=request.POST.get('initial_balance_date', ''),
            **_card_fields_from_post(request),
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
    if quer_fragmento(request):
        response = HttpResponse(status=200)
        response.headers['HX-Redirect'] = reverse(redirect_name)
        return response
    return redirect(redirect_name)
