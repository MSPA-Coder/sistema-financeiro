"""Menu lateral: o estado expandido vale só pela sessão do navegador.

Risco que protege: o estado do menu passar a ser guardado no servidor (banco ou
cookie), e dois itens do menu ficarem com o mesmo ícone (recolhido, o ícone é
tudo o que identifica o item).
"""

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse


@pytest.fixture
def pagina(client):
    user = get_user_model().objects.create_user(
        username="menu-recolhivel", password="senha-segura", user_type="administrator",
    )
    client.force_login(user)
    resposta = client.get(reverse("core:settings_profile"))
    assert resposta.status_code == 200
    return resposta


@pytest.mark.django_db
def test_o_estado_nao_e_guardado_em_cookie(pagina):
    assert not [nome for nome in pagina.cookies if "sidebar" in nome.lower() or "menu" in nome.lower()]


@pytest.mark.django_db
def test_cada_item_tem_um_icone_so_dele(pagina):
    html = pagina.content.decode()
    icones = re.findall(r'<span class="sidebar-icon"[^>]*>([^<]+)</span>', html)

    assert len(icones) > 20
    repetidos = sorted({i for i in icones if icones.count(i) > 1})
    assert not repetidos, f"ícones repetidos no menu: {repetidos}"
