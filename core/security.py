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
# `imagens_data_uri=True` porque o favicon do `base.html` e um SVG embutido no
# proprio `<link rel="icon">`. E a unica folga, e ela e pedida por nome.
#
# O `font-src 'self' data:` que esta constante tinha era sobra: o projeto nao
# tem `@font-face` nem nenhum arquivo de fonte. Saiu.
CONTENT_SECURITY_POLICY = montar_csp(imagens_data_uri=True)

# Ao contrario da versao anterior, o dicionario agora traz tambem os tres
# cabecalhos que o Django ja emite por configuracao (`SECURE_CONTENT_TYPE_
# NOSNIFF`, `X_FRAME_OPTIONS`, `SECURE_REFERRER_POLICY`). Nao ha conflito --
# os valores sao os mesmos e os dois lados usam `setdefault` --, e o ganho e
# que os valores passam a ter um dono so nos quatro projetos.
# `tests/test_security_headers.py` afirma que as settings do Django e o
# dicionario nao podem discordar.


class ContentSecurityPolicyMiddleware:
    """Define ``Content-Security-Policy`` e os defensivos em toda resposta.

    Implementado como middleware simples (sem dependencia nova) porque a
    politica e fixa para toda a aplicacao: nao ha necessidade de nonces
    por requisicao nem de configuracao por view.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        for header, value in SECURITY_HEADERS.items():
            response.setdefault(header, value)
        return response
