"""A navegação por filtro, depois de sair do JavaScript próprio.

O que estes testes protegem: trocar uma tela por filtro continua trocando as
DUAS regiões (`#appMain` e `#appPageHeader`), continua empurrando o endereço
para o histórico, e o endereço que chega à barra não carrega campo vazio nem
estado de interface.

Nada aqui toca o banco: o contrato é de template e de middleware, decidido
antes de qualquer consulta.
"""

from __future__ import annotations

import re

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory

from core.navegacao import ESTADO_DE_INTERFACE, UrlCanonicaMiddleware, url_canonica

# ---------------------------------------------------------------------------
# O endereço canônico
# ---------------------------------------------------------------------------


class _RequisicaoHtmx:
    """`request.htmx` do django-htmx é um objeto verdadeiro; aqui basta o bool."""

    def __init__(self, verdadeiro: bool = True) -> None:
        self.verdadeiro = verdadeiro

    def __bool__(self) -> bool:
        return self.verdadeiro


def _pedido(consulta: str, caminho: str = "/lancamentos/"):
    pedido = RequestFactory().get(f"{caminho}?{consulta}" if consulta else caminho)
    pedido.htmx = _RequisicaoHtmx()
    return pedido


def test_campo_vazio_sai_do_endereco() -> None:
    """O formulário serializa todo campo, inclusive o que ninguém escolheu."""
    assert url_canonica(_pedido("period=2026-03&owner_id=&account_id=")) == (
        "/lancamentos/?period=2026-03"
    )


def test_filtro_escolhido_permanece() -> None:
    assert url_canonica(_pedido("owner_id=3&account_id=")) == "/lancamentos/?owner_id=3"


def test_sem_nada_a_tirar_nao_mexe_na_barra() -> None:
    """Sem `None` aqui, o middleware mandaria um cabeçalho a cada requisição.

    O `hx-push-url` do elemento já resolve o caso comum; o cabeçalho existe
    apenas para corrigir o endereço quando há ruído.
    """
    assert url_canonica(_pedido("owner_id=3&period=2026-03")) is None


def test_todos_os_campos_vazios_deixam_so_o_caminho() -> None:
    assert url_canonica(_pedido("owner_id=&account_id=")) == "/lancamentos/"


def test_estado_de_interface_nunca_chega_a_barra() -> None:
    """`filters_open` diz se o painel está aberto, não o que a tela mostra."""
    assert url_canonica(
        _pedido("filters_open=1&layout=calendar", caminho="/reports/annual-planning/")
    ) == "/reports/annual-planning/?layout=calendar"


def test_show_descriptions_permanece() -> None:
    """Muda o que o relatório exibe, não como — some dele quebraria o link.

    É a fronteira que separa este parâmetro de `filters_open`: um é escolha de
    conteúdo, o outro é desenho de tela.
    """
    assert "show_descriptions" not in ESTADO_DE_INTERFACE
    assert url_canonica(
        _pedido("show_descriptions=1&reference_month=", caminho="/reports/annual-planning/")
    ) == "/reports/annual-planning/?show_descriptions=1"


def test_selecao_multipla_preserva_todas_as_escolhas() -> None:
    """`QueryDict.items()` devolveria só a última — e o filtro perderia o resto."""
    assert url_canonica(_pedido("tag=casa&tag=carro&owner_id=")) == (
        "/lancamentos/?tag=casa&tag=carro"
    )


def test_valor_com_caractere_especial_e_escapado() -> None:
    destino = url_canonica(_pedido("q=a%26b%3Dc&owner_id="))
    assert destino == "/lancamentos/?q=a%26b%3Dc"


def test_parametro_desconhecido_e_preservado() -> None:
    """Falha visível em vez de silenciosa.

    Um filtro novo que alguém acrescente sem lembrar deste módulo continua
    funcionando. Descartá-lo o faria sumir do endereço em silêncio, quebrando
    favorito e link sem nada apontar para a causa.
    """
    assert url_canonica(_pedido("filtro_novo=42&owner_id=")) == (
        "/lancamentos/?filtro_novo=42"
    )


# ---------------------------------------------------------------------------
# O middleware
# ---------------------------------------------------------------------------


def _resposta(**cabecalhos):
    from django.http import HttpResponse

    resposta = HttpResponse("<html></html>", content_type="text/html")
    for nome, valor in cabecalhos.items():
        resposta[nome.replace("_", "-")] = valor
    return resposta


def _passar(pedido, resposta=None):
    resposta = resposta if resposta is not None else _resposta()
    return UrlCanonicaMiddleware(lambda _: resposta)(pedido)


def test_middleware_limpa_a_barra_em_requisicao_htmx() -> None:
    assert _passar(_pedido("owner_id=3&account_id="))["HX-Replace-Url"] == (
        "/lancamentos/?owner_id=3"
    )


def test_navegacao_comum_nao_recebe_cabecalho() -> None:
    """Sem HTMX não há barra a corrigir: o navegador já está no endereço certo."""
    pedido = _pedido("owner_id=3&account_id=")
    pedido.htmx = _RequisicaoHtmx(False)
    assert not _passar(pedido).has_header("HX-Replace-Url")


@pytest.mark.parametrize("cabecalho", ["HX-Replace-Url", "HX-Push-Url", "HX-Redirect"])
def test_view_que_ja_decidiu_o_endereco_continua_mandando(cabecalho: str) -> None:
    resposta = _passar(
        _pedido("owner_id=3&account_id="),
        _resposta(**{cabecalho.replace("-", "_"): "/decidido-pela-view/"}),
    )
    assert resposta[cabecalho] == "/decidido-pela-view/"


def test_resposta_de_erro_nao_muda_a_barra() -> None:
    """Num 4xx/5xx a barra não deve passar a apontar para o que não foi servido."""
    from django.http import HttpResponse

    erro = HttpResponse("nao encontrado", status=404, content_type="text/html")
    assert not _passar(_pedido("owner_id=3&account_id="), erro).has_header(
        "HX-Replace-Url"
    )


# ---------------------------------------------------------------------------
# O contrato nos templates
# ---------------------------------------------------------------------------

#: Todo elemento que troca a tela por filtro precisa dos cinco. Faltar um dá
#: sintoma diferente e igualmente confuso: sem `hx-select` a página inteira
#: entra dentro do `main`; sem `hx-select-oob` os seletores do cabeçalho ficam
#: com as opções antigas; sem `hx-push-url` o endereço não acompanha a tela.
ATRIBUTOS_DE_NAVEGACAO = (
    'hx-target="#appMain"',
    'hx-swap="innerHTML"',
    'hx-select="#appMain"',
    'hx-select-oob="#appPageHeader:innerHTML"',
    'hx-push-url="true"',
)


def test_tag_emite_o_contrato_inteiro() -> None:
    from django.template import Context, Template

    emitido = Template("{% load navegacao %}{% nav_filtro %}").render(Context({}))

    for atributo in ATRIBUTOS_DE_NAVEGACAO:
        assert atributo in emitido, atributo


def test_filtros_da_tabela_de_lancamentos_navegam_por_htmx() -> None:
    """Os três seletores do cabeçalho da tabela e o botão de limpar."""
    rendered = render_to_string(
        "transactions/_table_body.html",
        {
            "txs": [],
            "available_dates": ["2026-03-10"],
            "available_types": ["despesa"],
            "available_categories": ["Casa"],
            "view_mode": "a_vencer",
            "selected_period": "2026-03",
            "filter_actions_colspan": 3,
            "request": RequestFactory().get("/lancamentos/"),
        },
    )

    seletores = re.findall(r"<select[^>]*data-table-filter[^>]*>", rendered)
    assert len(seletores) == 3, seletores
    for seletor in seletores:
        for atributo in ATRIBUTOS_DE_NAVEGACAO:
            assert atributo in seletor, (atributo, seletor)
        assert 'hx-include="#contextForm"' in seletor

    assert "data-filter-form" not in rendered, (
        "sobrou gancho do application.js na linha de filtros"
    )
    assert "data-clear-filters" not in rendered


def test_nenhum_template_ainda_depende_do_application_js() -> None:
    """Os ganchos do mecanismo antigo não podem sobreviver à migração.

    Um template esquecido não quebra visivelmente: o elemento simplesmente
    para de reagir, e só se descobre usando aquela tela.
    """
    from pathlib import Path

    ganchos = ("data-auto-submit", "data-ajax-nav", "data-filter-form", "data-clear-filters")
    raiz = Path(__file__).resolve().parent.parent / "templates"

    sobras = [
        f"{caminho.relative_to(raiz)}: {gancho}"
        for caminho in raiz.rglob("*.html")
        for gancho in ganchos
        if gancho in caminho.read_text(encoding="utf-8")
    ]

    assert sobras == [], sobras
