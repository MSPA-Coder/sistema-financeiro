"""URL configuration for financeiro project."""

from django.contrib.auth import logout
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import include, path
from django.views.generic import RedirectView
from django_htmx.http import HttpResponseClientRedirect

from accounts.views import AppLoginView


def health_check(_request):
    """Responde a pergunta "o serviço atende requisição que depende do banco?".

    O `healthcheck` do Compose usa esta rota, por isso a sonda executa uma
    consulta simples e devolve 503 quando o PostgreSQL nao esta disponivel.
    O formato da resposta segue o contrato comum aos aplicativos do
    mantenedor. A implementacao permanece local porque este projeto instala
    somente o nucleo, sem Flask, do SharedAuth.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - qualquer falha e "nao apto"
        # 503 e nao 500: indisponibilidade temporaria e a resposta correta, e
        # nao produz traceback no log a cada sonda de 60 segundos.
        return JsonResponse(
            {"servico": "controle-bancario", "status": "erro"}, status=503
        )
    return JsonResponse({"servico": "controle-bancario", "status": "ok"})


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
    # Sem barra tambem: os tres apps Flask servem `/health`, e o `APPEND_SLASH`
    # do Django respondia 301 ao caminho sem barra. Vigia externo que nao segue
    # redirecionamento marcava este projeto como fora do ar. Sem `name=`: a
    # rota canonica para `reverse()` continua sendo a de cima, uma so.
    path('health', health_check),
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
]
