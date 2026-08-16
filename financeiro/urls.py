"""URL configuration for financeiro project."""

from django.contrib import admin
from django.contrib.auth import logout
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import include, path
from django.views.generic import RedirectView
from django_htmx.http import HttpResponseClientRedirect

from accounts.views import AppLoginView


def health_check(_request):
    return HttpResponse("ok", content_type="text/plain", status=200)


def logout_and_redirect(request):
    """Encerra sessao e redireciona para a tela de login.

    Requisicoes HTMX (o botao "Sair" usa hx-post) precisam de um redirect
    client-side explicito: um 302 comum some no meio do swap, deixando a
    sessao encerrada no servidor mas a tela intacta na tela do usuario.
    """
    logout(request)
    if getattr(request, "htmx", False):
        return HttpResponseClientRedirect(redirect('login').url)
    return redirect('login')


urlpatterns = [
    path('', RedirectView.as_view(pattern_name='login', permanent=False)),
    path('health/', health_check, name='health_check'),
    path('login/', RedirectView.as_view(pattern_name='login', permanent=False)),
    path(
        'login',
        AppLoginView.as_view(
            template_name='registration/login.html',
            redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path('logout', logout_and_redirect, name='logout'),
    path('logout/', logout_and_redirect),
    path('', include('core.urls')),
    path('', include('dashboard.urls')),
    path('', include('transactions.urls')),
    path('', include('accounts.urls')),
    path('', include('banking.urls')),
    path('', include('bank_statements.urls')),
    path('', include('reports.urls')),
    path('', include('management.urls')),
    path('admin/', admin.site.urls),
]
