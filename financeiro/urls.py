"""URL configuration for financeiro project."""

from django.contrib import admin
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

    Ate 2026-08-21 esta view devolvia `"ok"` fixo, sem tocar no banco. E o
    mesmo defeito que `sharedauth.health` documenta ter corrigido nos tres
    apps Flask: o `healthcheck:` do Compose bate aqui, entao o Docker
    considerava o conteiner saudavel com o PostgreSQL inteiramente fora --
    exatamente a situacao que um health check existe para detectar.

    A rodada que unificou o `/health` cobriu os tres Flask via SharedAuth e
    deixou este de fora: o pacote entra aqui sem o extra `[flask]`, e
    `registrar_health` depende de `flask.jsonify`. O contrato de resposta e
    replicado a mao por isso. Extrair o nucleo do health para a parte de
    Python puro do SharedAuth esta na Fase 8 do PLANO_SINAL_E_DEFEITOS.

    Formato identico ao dos outros tres, de proposito: quem vigia os quatro
    faz uma pergunta so e le uma resposta so.
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
    path('admin/', admin.site.urls),
]
