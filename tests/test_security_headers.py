"""A Content-Security-Policy e os cabecalhos defensivos chegam ao cliente.

Este arquivo existe com o mesmo nome nos quatro projetos do mantenedor. Uma
politica que afrouxa nao quebra nada visivelmente -- a pagina continua
carregando --, entao so um teste percebe.
"""

from __future__ import annotations

import pytest
from django.conf import settings

from core.security import CONTENT_SECURITY_POLICY, SECURITY_HEADERS

# `/health/` responde sem sessao e sem consultar o banco: e o alvo certo para
# medir cabecalho, que e aplicado por middleware em toda resposta.
ROTA = "/health/"


def test_csp_fechada_na_propria_origem(client):
    csp = client.get(ROTA).headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_nao_admite_inline_nem_origem_externa(client):
    csp = client.get(ROTA).headers.get("Content-Security-Policy", "")
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "http://" not in csp
    assert "https://" not in csp


@pytest.mark.parametrize(("cabecalho", "valor"), sorted(SECURITY_HEADERS.items()))
def test_cabecalho_do_middleware_presente(client, cabecalho, valor):
    assert client.get(ROTA).headers.get(cabecalho) == valor


def test_permissions_policy_restringe_dispositivos(client):
    politica = client.get(ROTA).headers.get("Permissions-Policy", "")
    for recurso in ("camera=()", "microphone=()", "geolocation=()"):
        assert recurso in politica


@pytest.mark.parametrize(
    ("cabecalho", "valor"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "same-origin"),
    ],
)
def test_cabecalho_do_django_presente(client, cabecalho, valor):
    # Estes vem da configuracao do proprio Django, nao do middleware da
    # aplicacao; o conjunto entregue ao navegador precisa ser o mesmo dos
    # outros tres projetos, independentemente de quem escreve cada um.
    assert client.get(ROTA).headers.get(cabecalho) == valor


def test_configuracao_defensiva_do_django():
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.X_FRAME_OPTIONS == "DENY"
    # `same-origin`, nao `no-referrer`: ver o comentario em settings.py. Sob
    # `no-referrer` o navegador manda `Origin: null` em POST de mesma origem e
    # o CSRF do Django recusa a requisicao com o token correto.
    assert settings.SECURE_REFERRER_POLICY == "same-origin"


def test_csp_do_middleware_e_a_declarada():
    assert CONTENT_SECURITY_POLICY.startswith("default-src 'self'")
