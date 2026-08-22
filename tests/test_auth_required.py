"""A aplicacao nega acesso anonimo.

Uma rota que deixa de exigir sessao continua respondendo 200 e parecendo
correta: a falha e silenciosa e so aparece quando alguem de fora ja entrou.
"""

from __future__ import annotations

import pytest
from django.conf import settings

ROTAS_PROTEGIDAS = [
    "/dashboard/",
    "/transactions/",
    "/operations/",
    "/reports/projections/",
    "/tables/banks/",
    "/management/",
    "/permissions/",
    "/settings/",
]

ROTAS_PUBLICAS = ["/health/", "/login"]


@pytest.mark.parametrize("rota", ROTAS_PROTEGIDAS)
def test_rota_protegida_recusa_acesso_anonimo(client, rota):
    resposta = client.get(rota)
    assert resposta.status_code in (301, 302), (
        f"{rota} respondeu {resposta.status_code} sem sessao"
    )
    assert "login" in resposta.headers.get("Location", "").lower()


@pytest.mark.parametrize("rota", ROTAS_PUBLICAS)
def test_rota_publica_nao_redireciona_para_login(client, rota):
    resposta = client.get(rota)
    destino = resposta.headers.get("Location", "")
    assert not (resposta.status_code in (301, 302) and "login" in destino.lower()), (
        f"{rota} deveria ser alcancavel sem sessao"
    )


def test_health_responde_sem_sessao(client, banco_sondavel):
    # E o que o Docker consulta para decidir se o contêiner esta saudavel; um
    # health atras do login deixaria o orquestrador lendo o redirecionamento
    # como "doente".
    resposta = client.get("/health/")
    assert resposta.status_code == 200
    assert resposta.json() == {"servico": "controle-bancario", "status": "ok"}


def test_health_sem_barra_tambem_responde(client, banco_sondavel):
    # Os tres apps Flask servem `/health`; aqui o `APPEND_SLASH` respondia 301
    # e um vigia que nao segue redirecionamento marcava o projeto como fora.
    assert client.get("/health").status_code == 200


def test_health_reporta_503_com_banco_fora(client, banco_fora):
    # A sonda precisa refletir a dependencia do PostgreSQL para que o Compose
    # nao marque o conteiner como saudavel quando o banco esta indisponivel.
    resposta = client.get("/health/")
    assert resposta.status_code == 503
    assert resposta.json()["status"] == "erro"


def test_login_url_configurada():
    assert settings.LOGIN_URL == "/login"


def test_sessao_endurecida():
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_HTTPONLY is True


def test_debug_desligado_por_padrao():
    # `DEBUG` so liga quando pedido explicitamente pelo ambiente; ligado em
    # producao ele entrega stack trace e configuracao a quem errar uma URL.
    assert settings.DEBUG is False
