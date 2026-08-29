"""Cabecalhos de seguranca aplicados a toda resposta HTTP.

A Content Security Policy e estrita: nenhum script ou estilo inline, apenas
origem propria. A aplicacao nao depende de CDN externo -- HTMX e Chart.js sao
servidos localmente por `django.contrib.staticfiles`/WhiteNoise -- entao
``default-src 'self'`` cobre todos os assets legitimos.

Manter a politica sem `'unsafe-inline'` e o que transforma um XSS refletido em
um script bloqueado. Se algum template precisar de dados no cliente, use
`json_script` (nao executavel) em vez de abrir a politica.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from sharedauth.security import SECURITY_HEADERS, montar_csp

__all__ = ["CONTENT_SECURITY_POLICY", "SECURITY_HEADERS", "ContentSecurityPolicyMiddleware"]

# A politica e os valores dos cabecalhos vem de `sharedauth.security`, o mesmo
# lugar que os tres apps Flask usam. Este projeto instala so o nucleo do
# pacote (Python puro, sem Flask): a biblioteca nao aplica nada aqui, ela so
# guarda os valores -- quem aplica continua sendo o middleware abaixo.
#
# A politica fechada da biblioteca serve este projeto sem excecao nenhuma. A
# folga de `img-src ... data:` existia por um motivo unico: o favicon do
# `base.html` era um SVG embutido no proprio `<link rel="icon">`. Ele passou a
# ser `static/favicon.svg`, servido pelo WhiteNoise com hash como o restante,
# e a excecao saiu -- era a unica ocorrencia de URI `data:` no projeto (os
# `data:` de `dashboard.js` sao chaves de objeto do Chart.js, nao URIs).
#
# Nao e necessario abrir `font-src` para `data:`: o projeto nao usa
# `@font-face` nem arquivos de fonte.
CONTENT_SECURITY_POLICY = montar_csp()

# O dicionario inclui os cabecalhos que o Django tambem emite por configuracao
# (`SECURE_CONTENT_TYPE_NOSNIFF`, `X_FRAME_OPTIONS`,
# `SECURE_REFERRER_POLICY`). Nao ha conflito: os valores coincidem e os dois
# lados usam `setdefault`.
# `tests/test_security_headers.py` afirma que as settings do Django e o
# dicionario nao podem discordar.


class ContentSecurityPolicyMiddleware:
    """Define ``Content-Security-Policy`` e os defensivos em toda resposta.

    Implementado como middleware simples (sem dependencia nova) porque a
    politica e fixa para toda a aplicacao: **nao ha nonce**, e nao ha por que
    haver.

    Cheguei a introduzir um, para o unico estilo que varia por usuario -- o
    numero de linhas que cabem numa tabela antes de a rolagem comecar. Nonce
    exige `<style>` embutido, `<style>` embutido no `<head>` faz o HTMX
    tentar reinjeta-lo a cada troca de tela (e ele perde o nonce no caminho), e
    a saida documentada para isso seria expor o nonce num `<meta>` --
    entregando ao DOM justamente o segredo que o nonce e.

    A preferencia virou `core.views.preferencias_css`, uma folha de estilo de
    verdade, servida da propria origem. `style-src 'self'` ja a autoriza, sem
    excecao nenhuma.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        for header, value in SECURITY_HEADERS.items():
            response.setdefault(header, value)
        return response
