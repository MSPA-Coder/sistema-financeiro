"""A Content-Security-Policy e os cabecalhos defensivos chegam ao cliente.

Este arquivo existe com o mesmo nome nos quatro projetos do mantenedor. Uma
politica que afrouxa nao quebra nada visivelmente -- a pagina continua
carregando --, entao so um teste percebe.
"""

from __future__ import annotations

import pytest
from django.conf import settings

from core.security import CONTENT_SECURITY_POLICY, SECURITY_HEADERS

# `/health/` responde sem sessao: e o alvo certo para medir cabecalho, que e
# aplicado por middleware em toda resposta. Como a rota consulta o banco, os
# testes daqui pedem `banco_sondavel` -- sem isso mediriam
# o cabecalho de um 503, que passaria igual e esconderia a intencao.
ROTA = "/health/"


def test_csp_fechada_na_propria_origem(client, banco_sondavel):
    csp = client.get(ROTA).headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_nao_admite_inline_nem_origem_externa(client, banco_sondavel):
    csp = client.get(ROTA).headers.get("Content-Security-Policy", "")
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "http://" not in csp
    assert "https://" not in csp


@pytest.mark.parametrize(("cabecalho", "valor"), sorted(SECURITY_HEADERS.items()))
def test_cabecalho_do_middleware_presente(client, cabecalho, valor):
    assert client.get(ROTA).headers.get(cabecalho) == valor


def test_permissions_policy_restringe_dispositivos(client, banco_sondavel):
    # `browsing-topics` entrou no conjunto comum vindo do ControleRendaVariavel,
    # onde o Flask-Talisman o escrevia sozinho: recusar a Topics API e
    # estritamente mais restritivo que nao declarar nada.
    politica = client.get(ROTA).headers.get("Permissions-Policy", "")
    for recurso in (
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "browsing-topics=()",
    ):
        assert recurso in politica


def test_csp_nao_libera_data_uri_para_nada(client, banco_sondavel):
    """A politica e a fechada da biblioteca, sem excecao nenhuma.

    A folga de `img-src ... data:` existia por um motivo unico -- o favicon
    era um SVG embutido no proprio `<link rel="icon">`. Ele virou
    `static/favicon.svg` e a folga saiu junto. Este teste passa a guardar a
    ausencia: se `data:` reaparecer na politica, alguem reabriu a excecao e
    precisa justificar por que.
    """
    csp = client.get(ROTA).headers.get("Content-Security-Policy", "")
    assert "img-src 'self';" in csp
    assert "font-src 'self';" in csp
    assert "data:" not in csp


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


def test_settings_do_django_nao_discordam_do_conjunto_comum():
    # Tres destes cabecalhos tem dois escritores: a configuracao do Django e o
    # middleware, que agora le os valores de `sharedauth.security`. Os dois
    # usam `setdefault`, entao uma divergencia nao quebraria nada visivelmente
    # -- o navegador receberia o valor de quem escrevesse primeiro. Este teste
    # e o que transforma "manter igual a mao" em algo verificado.
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert SECURITY_HEADERS["X-Frame-Options"] == settings.X_FRAME_OPTIONS
    assert SECURITY_HEADERS["Referrer-Policy"] == settings.SECURE_REFERRER_POLICY


def test_csp_do_middleware_e_a_declarada():
    assert CONTENT_SECURITY_POLICY.startswith("default-src 'self'")

# ---------------------------------------------------------------------------
# Nonce de estilo
# ---------------------------------------------------------------------------
#
# A CSP fecha `style-src` em 'self', o que bloqueia TAMBEM o estilo aplicado
# por CSSOM (`el.style.x = ...`) -- e o bloqueio e silencioso: violacao no
# console, nenhum erro de JavaScript, nenhum teste vermelho, e o estilo nao
# aparece. Foi assim que o teto de rolagem das tabelas nunca funcionou.


def test_style_src_continua_fechado_sem_excecao(client):
    """Nem `unsafe-inline`, nem nonce, nem hash.

    Cheguei a introduzir um nonce para o unico estilo que varia por usuario.
    Nonce exige `<style>` embutido; `<style>` no `<head>` faz o HTMX tentar
    reinjeta-lo a cada troca de tela, perdendo o nonce; e a correcao
    documentada seria expor o nonce num `<meta>` -- entregando ao DOM
    justamente o segredo que ele e. A preferencia virou folha de estilo servida
    da propria origem, e `style-src 'self'` voltou a bastar.
    """
    politica = client.get(ROTA).headers["Content-Security-Policy"]

    assert "style-src 'self';" in politica
    assert "unsafe-inline" not in politica
    assert "nonce-" not in politica


def test_a_casca_nao_traz_estilo_embutido() -> None:
    """Um `<style>` no `base.html` seria bloqueado -- em silencio."""
    from django.template.loader import render_to_string

    rendered = render_to_string("base.html", {"app_menu_items": []})

    assert "<style" not in rendered
    assert 'href="/core/preferencias.css"' in rendered or "preferencias.css" in rendered


def test_a_folha_de_preferencias_carrega_a_escolha_do_usuario() -> None:
    """Servida como CSS de verdade, autorizada por `style-src 'self'`."""
    from types import SimpleNamespace

    from django.test import RequestFactory

    from core.views import preferencias_css

    pedido = RequestFactory().get("/core/preferencias.css")
    pedido.user = SimpleNamespace(table_scroll_rows=42)
    resposta = preferencias_css(pedido)

    assert resposta["Content-Type"].startswith("text/css")
    assert "--table-scroll-rows: 42" in resposta.content.decode()
    assert "private" in resposta["Cache-Control"]


def test_a_folha_de_preferencias_sanea_valor_absurdo() -> None:
    """O limite do banco e 5..200; a folha nao pode confiar no que recebe."""
    from types import SimpleNamespace

    from django.test import RequestFactory

    from core.views import preferencias_css

    pedido = RequestFactory().get("/core/preferencias.css")
    pedido.user = SimpleNamespace(table_scroll_rows="nao-e-numero")

    assert "--table-scroll-rows: 15" in preferencias_css(pedido).content.decode()


def test_nenhum_javascript_do_projeto_aplica_estilo_inline():
    """A CSP bloquearia, e o sintoma seria invisivel.

    Todo estado alternado por JavaScript mora em classe CSS. Um `el.style.x`
    novo voltaria a falhar em silencio -- por isso a varredura.
    """
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "static" / "js"
    sobras = [
        f"{caminho.relative_to(raiz).as_posix()}:{numero}"
        for caminho in raiz.rglob("*.js")
        if "vendor" not in caminho.parts
        for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), 1)
        if ".style." in linha
    ]

    assert sobras == [], sobras
