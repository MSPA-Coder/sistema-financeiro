"""Menu lateral recolhido por padrão, só ícones.

Risco que protege: a página nascer com o menu completo (o salto visual que o
padrão recolhido evita), o botão de alternar sumir ou perder o estado
acessível, o menu recolhido deixar um item sem nome (só ícone), ou o estado
passar a ser guardado, o que a decisão de produto descartou: toda carga
nasce recolhida.
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
def test_toda_pagina_nasce_com_o_menu_recolhido(pagina):
    html = pagina.content.decode()

    assert re.search(r'<html[^>]*data-sidebar="collapsed"', html)


@pytest.mark.django_db
def test_o_botao_informa_que_o_menu_esta_recolhido(pagina):
    html = pagina.content.decode()
    botao = re.search(r"<button[^>]*data-sidebar-collapse[^>]*>", html)

    assert botao is not None
    assert 'aria-expanded="false"' in botao.group(0)
    assert 'aria-controls="appSidebar"' in botao.group(0)
    assert 'id="appSidebar"' in html


@pytest.mark.django_db
def test_cada_item_tem_nome_acessivel_e_dica_com_o_menu_recolhido(pagina):
    html = pagina.content.decode()
    itens = re.findall(r'<(?:a|button)\b[^>]*class="sidebar-link[^"]*"[^>]*>(.*?)</(?:a|button)>', html, re.S)
    titulos = re.findall(r'<(?:a|button)\b[^>]*title="([^"]+)"[^>]*class="sidebar-link', html) + re.findall(
        r'<(?:a|button)\b[^>]*class="sidebar-link[^"]*"[^>]*title="([^"]+)"', html
    )

    assert itens
    assert len(titulos) == len(itens)
    for item in itens:
        texto = re.sub(r"<[^>]+>", " ", item)
        assert re.search(r"[A-Za-zÀ-ú]{3,}", texto), "item sem texto: ficaria sem nome acessivel"


@pytest.mark.django_db
def test_o_estado_nao_e_guardado_em_cookie(pagina):
    assert not [nome for nome in pagina.cookies if "sidebar" in nome.lower() or "menu" in nome.lower()]


@pytest.mark.django_db
def test_cada_item_tem_um_icone_so_dele(pagina):
    """Recolhido, o ícone é tudo o que identifica o item: repetido, dois itens se confundem."""
    html = pagina.content.decode()
    icones = re.findall(r'<span class="sidebar-icon"[^>]*>([^<]+)</span>', html)

    assert len(icones) > 20
    repetidos = sorted({i for i in icones if icones.count(i) > 1})
    assert not repetidos, f"ícones repetidos no menu: {repetidos}"
