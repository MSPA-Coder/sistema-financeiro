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

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


# Conjunto defensivo comum aos quatro projetos do mantenedor. Manter igual em
# todos e o que permite auditar um e confiar nos demais.
#
# Nao repete o que o Django ja aplica por configuracao: `X-Content-Type-Options`
# vem de SECURE_CONTENT_TYPE_NOSNIFF, `X-Frame-Options` de X_FRAME_OPTIONS,
# `Referrer-Policy` de SECURE_REFERRER_POLICY e o HSTS de SECURE_HSTS_SECONDS.
SECURITY_HEADERS = {
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}


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
