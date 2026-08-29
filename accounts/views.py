"""Views de Cadastros: Titulares (AccountOwner) e sessão de conta."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.login_lockout import (
    LoginThrottledError,
    assert_login_not_throttled,
    clear_login_failures,
    failed_login_message,
    register_failed_login_attempt,
)
from accounts.models import AccountOwner
from accounts.services import (
    change_user_password,
    create_owner,
    delete_owner,
    list_owners,
    update_owner,
)
from core.htmx import quer_fragmento
from core.permissions import permission_required


def _owners_context():
    return {"owners": list_owners()}


@login_required
@permission_required('tables.view', fallback='accounts:owners_view')
@permission_required('tables.owners.manage', fallback='accounts:owners_view')
def owners_view(request):
    """Lista e cadastro de titulares, com suporte a HTMX."""
    context = _owners_context()
    if quer_fragmento(request):
        return render(request, 'tables/_owners_table.html', context)
    return render(request, 'tables/owners.html', context)


@login_required
@permission_required('tables.view', fallback='accounts:owners_view')
@permission_required('tables.owners.manage', fallback='accounts:owners_view')
@require_POST
def create_owner_view(request):
    try:
        create_owner(request.POST.get('name', ''))
        messages.success(request, "Titular cadastrado com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request)


@login_required
@permission_required('tables.view', fallback='accounts:owners_view')
@permission_required('tables.owners.manage', fallback='accounts:owners_view')
@require_POST
def update_owner_view(request, owner_id):
    owner = get_object_or_404(AccountOwner, id=owner_id)
    try:
        update_owner(owner, request.POST.get('name', ''))
        messages.success(request, "Titular atualizado com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request)


@login_required
@permission_required('tables.view', fallback='accounts:owners_view')
@permission_required('tables.owners.manage', fallback='accounts:owners_view')
@require_POST
def delete_owner_view(request, owner_id):
    owner = get_object_or_404(AccountOwner, id=owner_id)
    try:
        delete_owner(owner)
        messages.success(request, "Titular excluído com sucesso.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return _respond(request)


def _respond(request):
    if quer_fragmento(request):
        response = HttpResponse(status=200)
        response.headers['HX-Redirect'] = reverse('accounts:owners_view')
        return response
    return redirect('accounts:owners_view')


def _safe_next_url(request, default_url: str) -> str:
    """Aceita apenas redirecionamentos internos para evitar open redirect."""
    candidate = request.POST.get('next') or request.GET.get('next')
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return candidate
    return default_url


class AppLoginView(LoginView):
    """LoginView padrao com bloqueio de tentativas e desvio para troca obrigatoria de senha."""

    def post(self, request, *args, **kwargs):
        remote_addr = request.META.get('REMOTE_ADDR')
        try:
            assert_login_not_throttled(request.POST.get('username'), remote_addr)
        except LoginThrottledError as exc:
            messages.error(request, str(exc))
            return self.render_to_response(self.get_context_data())
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        clear_login_failures(form.get_user().username, self.request.META.get('REMOTE_ADDR'))
        response = super().form_valid(form)
        user = form.get_user()
        if getattr(user, 'must_change_password', False):
            next_url = self.get_success_url()
            change_url = reverse('accounts:change_password')
            return redirect(f"{change_url}?next={next_url}")
        return response

    def form_invalid(self, form):
        attempts_remaining, wait_seconds = register_failed_login_attempt(
            self.request.POST.get('username'), self.request.META.get('REMOTE_ADDR')
        )
        messages.error(self.request, failed_login_message(attempts_remaining, wait_seconds))
        return super().form_invalid(form)


@login_required
def change_password_view(request):
    """Troca de senha autoatendida (equivalente a owner_session.change_password)."""
    next_url = _safe_next_url(request, settings.LOGIN_REDIRECT_URL)
    if request.method == 'POST':
        try:
            change_user_password(
                request.user,
                request.POST.get('current_password'),
                request.POST.get('new_password'),
                request.POST.get('password_confirm'),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            update_session_auth_hash(request, request.user)
            messages.success(request, "Senha alterada com sucesso.")
            return redirect(next_url)

    return render(request, 'accounts/change_password.html', {
        'next_url': next_url,
        'forced_change': bool(request.user.must_change_password),
    })
