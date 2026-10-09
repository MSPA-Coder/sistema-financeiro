"""Formato regional (Brasil/EUA) por usuário: só apresentação.

Risco que protege: o usuário escolher EUA e ver número ou data em formato
trocado pela metade (a mistura que motivou a mudança), ou a escolha de um
usuário vazar para a requisição seguinte. O que é gravado, importado e
exportado não passa por esta camada e não deve mudar.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.urls import reverse

from core import regional
from core.domain.settings import REGIONAL_FORMAT_BR, REGIONAL_FORMAT_US, normalize_regional_format

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture
def eua():
    token = regional.ativar(REGIONAL_FORMAT_US)
    yield
    regional.desativar(token)


def _renderizar(texto: str, **contexto) -> str:
    return Template("{% load money_filters regional_filters %}" + texto).render(Context(contexto))


def test_sem_requisicao_vale_o_formato_do_brasil():
    assert regional.formato_ativo() == REGIONAL_FORMAT_BR
    assert regional.formatar_data(date(2026, 12, 31)) == "31/12/2026"
    assert regional.formatar_data_hora(datetime(2026, 12, 31, 8, 5, 9)) == "31/12/2026 08:05"


def test_formato_desconhecido_cai_no_brasil():
    assert normalize_regional_format("xx") == REGIONAL_FORMAT_BR
    assert normalize_regional_format(None) == REGIONAL_FORMAT_BR


def test_datas_no_formato_dos_eua(eua):
    assert regional.formatar_data(date(2026, 12, 31)) == "12/31/2026"
    assert regional.formatar_data(date(2026, 3, 4)) == "03/04/2026"
    assert regional.formatar_data_hora(datetime(2026, 12, 31, 8, 5, 9), segundos=True) == "12/31/2026 08:05:09"
    assert regional.formatar_dia_mes(date(2026, 12, 31)) == "12/31"
    assert regional.formatar_mes(date(2026, 12, 31)) == "12/2026"


def test_data_ausente_nao_vira_texto():
    assert regional.formatar_data(None) == ""
    assert regional.formatar_data_hora(None) == ""


def test_dinheiro_troca_so_os_separadores_e_mantem_o_simbolo(eua):
    saida = _renderizar("{{ v|money }}|{{ v|money:'USD' }}|{{ v|money_signed }}", v=Decimal("-1234567.89"))
    assert saida == "R$ -1,234,567.89|US$ -1,234,567.89|- R$ 1,234,567.89"


def test_dinheiro_no_brasil_continua_igual():
    assert _renderizar("{{ v|money }}", v=Decimal("1234.56")) == "R$ 1.234,56"


def test_filtros_de_data_seguem_o_formato_ativo(eua):
    saida = _renderizar("{{ d|udate }}|{{ t|udatetime }}|{{ t|udatetime_s }}|{{ d|umonth }}",
                        d=date(2026, 12, 31), t=datetime(2026, 12, 31, 23, 59, 58))
    assert saida == "12/31/2026|12/31/2026 23:59|12/31/2026 23:59:58|12/2026"


def test_filtro_de_data_aceita_vazio():
    assert _renderizar("[{{ d|udate }}]", d=None) == "[]"


def test_nenhum_template_formata_data_fora_dos_filtros_regionais():
    """Data legível por pessoa passa por `udate`, nunca por `date:"d/m/Y"` fixo.

    Valores de `<input>` seguem ISO (`Y-m-d`), que é o que o servidor recebe.
    """
    fixos = []
    for caminho in (RAIZ / "templates").rglob("*.html"):
        texto = caminho.read_text(encoding="utf-8")
        for padrao in ('date:"d/m/Y', "date:'d/m/Y", 'date:"m/Y', "date:'m/Y"):
            if padrao in texto:
                fixos.append(f"{caminho.relative_to(RAIZ).as_posix()}: {padrao}")
    assert not fixos, fixos


@pytest.mark.django_db
class TestPreferenciaDoUsuario:
    @pytest.fixture
    def usuario(self, client):
        user = get_user_model().objects.create_user(
            username="formato-regional", password="senha-segura", user_type="administrator",
        )
        client.force_login(user)
        return user

    def test_padrao_e_brasil_para_quem_ja_existia(self, usuario):
        assert usuario.regional_format == REGIONAL_FORMAT_BR

    def test_perfil_grava_a_escolha_e_recusa_valor_estranho(self, client, usuario):
        resposta = client.post(reverse("core:settings_update_regional_format"), {"regional_format": "us"})
        assert resposta.status_code == 302
        usuario.refresh_from_db()
        assert usuario.regional_format == REGIONAL_FORMAT_US

        client.post(reverse("core:settings_update_regional_format"), {"regional_format": "../x"})
        usuario.refresh_from_db()
        assert usuario.regional_format == REGIONAL_FORMAT_BR

    def test_perfil_mostra_as_duas_opcoes_e_a_escolhida(self, client, usuario):
        usuario.regional_format = REGIONAL_FORMAT_US
        usuario.save()
        html = client.get(reverse("core:settings_profile")).content.decode()
        assert 'name="regional_format" value="br"' in html
        assert 'name="regional_format" value="us" aria-label="Formato EUA" checked' in html
        assert 'data-regional="us"' in html

    def test_cada_requisicao_usa_o_formato_do_seu_usuario(self, client, usuario):
        from django.test import Client

        usuario.regional_format = REGIONAL_FORMAT_US
        usuario.save()
        assert 'data-regional="us"' in client.get(reverse("core:settings_profile")).content.decode()

        outro = get_user_model().objects.create_user(
            username="formato-br", password="senha-segura", user_type="administrator",
        )
        cliente_br = Client()
        cliente_br.force_login(outro)
        assert 'data-regional="br"' in cliente_br.get(reverse("core:settings_profile")).content.decode()
        assert regional.formato_ativo() == REGIONAL_FORMAT_BR


@pytest.mark.sentinela_front
def test_o_javascript_regional_ignora_o_auxiliar_do_calendario():
    """O campo auxiliar do calendario nunca vira campo regional.

    Risco que protege: o auxiliar e um `<input type="date">` dentro do wrapper.
    Sem a guarda, conteudo inserido depois do carregamento (troca de HTMX, campo
    criado por JS) faz o observador tratar o auxiliar como campo novo e criar
    wrapper dentro de wrapper sem fim, travando a aba.
    """
    texto = (RAIZ / "static/js/core/regional.js").read_text(encoding="utf-8")
    assert "classList.contains('regional-picker-proxy')" in texto
    assert ":not(.regional-picker-proxy)" in texto
