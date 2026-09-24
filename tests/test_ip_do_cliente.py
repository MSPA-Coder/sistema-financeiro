"""De onde veio a requisição: trilha de auditoria e trava de login.

No VPS, o nginx recebe a conexão e a repassa pela rede do Docker, então
`REMOTE_ADDR` é o gateway dessa rede para todo mundo. O endereço real vem no
`X-Forwarded-For`, que o nginx completa acrescentando o cliente no FIM da
lista; o que está à esquerda foi escrito pelo próprio cliente. Até 24/09/2026
a auditoria lia o primeiro item (falsificável) e a trava de login usava
`REMOTE_ADDR` (em produção, só gateways `172.x` na tabela).
"""

from __future__ import annotations

import pytest
from django.test import Client, RequestFactory, override_settings

from core.services import audit_request_context

PROXY = ("172.16.0.0/12",)
GATEWAY = "172.19.0.1"


def _requisicao(encaminhado: str, remoto: str = GATEWAY):
    return RequestFactory().post("/x/", REMOTE_ADDR=remoto, HTTP_X_FORWARDED_FOR=encaminhado)


@override_settings(AUDIT_TRUSTED_PROXY_CIDRS=PROXY)
def test_o_endereco_que_o_cliente_escreve_nao_vira_a_origem():
    # O cliente mandou `X-Forwarded-For: 6.6.6.6`; o nginx acrescentou o real.
    contexto = audit_request_context(_requisicao("6.6.6.6, 198.51.100.7"))

    assert contexto.client_ip == "198.51.100.7"
    assert contexto.proxy_ip == GATEWAY


@override_settings(AUDIT_TRUSTED_PROXY_CIDRS=PROXY)
def test_proxies_conhecidos_na_cadeia_sao_pulados():
    contexto = audit_request_context(_requisicao("198.51.100.7, 172.20.0.5"))

    assert contexto.client_ip == "198.51.100.7"


@override_settings(AUDIT_TRUSTED_PROXY_CIDRS=PROXY)
def test_cadeia_com_lixo_nao_produz_endereco_inventado():
    contexto = audit_request_context(_requisicao("198.51.100.7, nao-e-ip"))

    assert contexto.client_ip == GATEWAY
    assert contexto.proxy_ip is None


def _entrar(cliente: Client, origem: str, senha: str):
    return cliente.post(
        "/login",
        {"username": "travada", "password": senha},
        REMOTE_ADDR=GATEWAY,
        HTTP_X_FORWARDED_FOR=origem,
    )


@pytest.mark.django_db
@override_settings(AUDIT_TRUSTED_PROXY_CIDRS=PROXY)
def test_quem_erra_a_senha_nao_tranca_o_dono_que_vem_de_outro_lugar():
    """A trava é por usuário e endereço. Com o endereço errado (o gateway),
    ela vira só por usuário: qualquer pessoa na internet tranca a conta alheia
    errando a senha cinco vezes."""
    from accounts.models import AppUser

    AppUser.objects.create_user(username="travada", password="Senha-Certa-Longa-1")
    atacante, dono = "203.0.113.10", "198.51.100.20"

    for _ in range(5):
        _entrar(Client(), atacante, "errada")

    resposta_do_atacante = _entrar(Client(), atacante, "Senha-Certa-Longa-1")
    resposta_do_dono = _entrar(Client(), dono, "Senha-Certa-Longa-1")

    assert resposta_do_atacante.status_code == 200, "o endereço que errou fica travado"
    assert resposta_do_dono.status_code == 302, "o dono, de outro endereço, entra"
