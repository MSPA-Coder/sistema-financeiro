"""Rota que muda estado sem tela que a acione nao volta sem querer.

Cobre tambem o admin do Django, removido em 01/09/2026: ele nao registrava
modelo nenhum deste projeto, mas `django.contrib.auth.admin` registra o modelo
de usuario sozinho -- e `/admin/login/` autenticava senha sem passar por
`AppLoginView`, portanto sem o `LoginLockout`.

`cancel_entry`, `close_month` e `reopen_month` nao fazem parte das URLs de
`transactions`; uma rota POST autenticada sem tela correspondente ampliaria a
superficie mutante sem ampliar o contrato da interface.

Fechar e reabrir mes continuam existindo pelos caminhos de `core:`, e o teste
exige isso como controle positivo.
"""

from __future__ import annotations

import pytest
from django.urls import NoReverseMatch, reverse

REMOVIDAS = [
    "transactions:cancel_entry",
    "transactions:close_month",
    "transactions:reopen_month",
]

# Os caminhos vivos recebem os dados pelo POST e resolvem sem argumentos. As
# assinaturas abaixo tambem garantem que os nomes ausentes nao resolvam com os
# antigos formatos posicionais.
MANTIDAS = [
    "core:settings_close_month",
    "core:settings_reopen_month",
    "core:settings_monthly_close",
]


@pytest.mark.parametrize("nome", REMOVIDAS)
def test_rota_orfa_nao_resolve(nome):
    with pytest.raises(NoReverseMatch):
        reverse(nome, args=[1, 2026, 1])

    with pytest.raises(NoReverseMatch):
        reverse(nome, args=[1])


@pytest.mark.parametrize("nome", MANTIDAS)
def test_caminho_vivo_de_fechamento_continua_existindo(nome):
    assert reverse(nome)


# --- Admin do Django, removido ----------------------------------------------


def test_admin_do_django_nao_esta_instalado():
    from django.conf import settings

    assert "django.contrib.admin" not in settings.INSTALLED_APPS
    # As duas continuam necessarias e nao dependem do admin.
    assert "django.contrib.auth" in settings.INSTALLED_APPS
    assert "django.contrib.messages" in settings.INSTALLED_APPS


@pytest.mark.parametrize("caminho", ["/admin/", "/admin/login/"])
def test_rota_do_admin_nao_responde(client, caminho):
    """404, e nao 302 para o login: a rota deixou de existir.

    Importa ser 404: um 302 significaria que a URLconf ainda casa o caminho e
    so o acesso foi negado -- e a superficie de autenticacao paralela
    continuaria la.
    """
    assert client.get(caminho).status_code == 404
