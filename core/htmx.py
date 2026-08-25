"""Middleware que resolve o atraso de mensagens (`django.contrib.messages`)
em respostas HTMX parciais.

Uma view que atende um `hx-post`/`hx-get` normalmente devolve só o fragmento
trocado (`hx-target`), não a página inteira - e o bloco de mensagens
(`#flashMessages`) mora em `base.html`, fora de qualquer fragmento trocado.
Sem tratamento, a mensagem enfileirada por `messages.success`/`.error` nunca
é lida nessa resposta: o Django só marca uma mensagem como exibida quando
algum template efetivamente itera `{{ messages }}`, e o fragmento parcial não
faz isso. A mensagem continua pendente na sessão e só aparece na próxima
navegação que renderize a página inteira - o sintoma relatado é "a mensagem
(ou o erro) só aparece depois, na tela seguinte".

Aqui toda resposta HTMX ganha, além do fragmento pedido pela view, um swap
fora de banda (`hx-swap-oob`) do bloco de mensagens. O HTMX (já carregado em
toda página) resolve isso sozinho no cliente: identifica o elemento
`#flashMessages` na resposta e substitui o da página atual, sem que a view
precise saber disso. Centralizar aqui evita repetir esse tratamento em cada
view que hoje devolve um `render()` parcial para `HX-Request`.
"""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string


class HtmxFlashMessagesMiddleware:
    """Anexa um swap fora de banda do bloco de mensagens a toda resposta HTMX.

    Precisa rodar depois de `django.contrib.messages.middleware.MessageMiddleware`
    na lista `MIDDLEWARE` (mais perto da view): a leitura de `messages.get_messages`
    abaixo marca as mensagens como exibidas, e isso só deve acontecer antes do
    `MessageMiddleware.process_response` decidir o que persistir na sessão para a
    próxima requisição - senão as mensagens some daqui sem nunca aparecer.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if not getattr(request, "htmx", False):
            return response
        # Qualquer redirect faz uma navegação completa; o corpo com um
        # fragmento OOB não será processado. Preserve as mensagens para a
        # próxima página, tanto no HX-Redirect quanto no redirect HTTP comum.
        if response.has_header("HX-Redirect") or 300 <= response.status_code < 400:
            return response
        if getattr(response, "streaming", False):
            return response
        if "text/html" not in response.get("Content-Type", ""):
            return response

        pending = messages.get_messages(request)
        if not len(pending):
            return response

        fragment = render_to_string(
            "components/_flash_messages.html", {"messages": pending}, request=request
        )
        # 204 responses cannot carry a processable body. Some mutation views
        # intentionally use 204 + HX-Trigger to refresh a table, so promote
        # only responses that actually need the OOB flash fragment.
        if response.status_code == 204:
            response.status_code = 200
            response["HX-Reswap"] = "none"
        response.content = response.content + fragment.encode(response.charset or "utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = len(response.content)
        return response


class HtmxAuthenticationMiddleware:
    """Converte o redirect de sessão expirada em navegação completa no HTMX.

    O HTMX não troca um fragmento de login no lugar da tela operacional. Como
    respostas 3xx não processam `HX-Redirect`, a camada transforma somente o
    redirect para `LOGIN_URL` em uma resposta 200 com esse cabeçalho; redirects
    normais, inclusive os de permissões, permanecem intactos.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not getattr(request, "htmx", False) or response.status_code not in {301, 302, 303, 307, 308}:
            return response

        location = response.get("Location", "")
        login_path = urlsplit(str(settings.LOGIN_URL)).path
        if urlsplit(location).path.rstrip("/") != login_path.rstrip("/"):
            return response

        htmx_response = HttpResponse(status=200)
        htmx_response["HX-Redirect"] = location
        return htmx_response
