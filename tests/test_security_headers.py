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
    """Nem `unsafe-inline`, nem nonce, nem hash."""
    politica = client.get(ROTA).headers["Content-Security-Policy"]

    assert "style-src 'self';" in politica
    assert "unsafe-inline" not in politica
    assert "nonce-" not in politica


def test_a_casca_nao_traz_estilo_embutido() -> None:
    """`<style>` sem nonce e o que a CSP realmente bloqueia (`style-src-elem`).

    Escrita por CSSOM (`el.style.x = ...`) NAO e bloqueada -- a politica governa
    o ATRIBUTO `style` do HTML e o elemento `<style>`, nao a propriedade `style`
    de um objeto. A distincao custou uma correcao errada; fica registrada aqui.
    """
    from django.template.loader import render_to_string

    rendered = render_to_string("base.html", {"app_menu_items": []})

    assert "<style" not in rendered


def test_htmx_nao_reaplica_o_atributo_style_ao_trocar_de_tela() -> None:
    """`attributesToSettle` inclui `style` por padrao -- e isso a CSP bloqueia.

    O HTMX copiava o atributo `style` dos `<canvas>` do Chart.js a cada troca,
    e cada copia virava uma violacao (`style-src-attr`): seis por troca no
    painel. Nenhum elemento deste projeto depende de `style` sobreviver a um
    swap; todo estado alternado por JavaScript mora em classe.
    """
    import json
    import re

    from django.template.loader import render_to_string

    rendered = render_to_string("base.html", {"app_menu_items": []})
    bruto = re.search("content='([^']+)'", rendered).group(1)
    config = json.loads(bruto)

    assert "style" not in config["attributesToSettle"]
    assert config["includeIndicatorStyles"] is False


def test_o_indicador_do_htmx_tem_estilo_proprio() -> None:
    """Com `includeIndicatorStyles` desligado, quem estiliza e o projeto.

    Sem estas regras o "Importando..." fica visivel o tempo todo -- que era o
    comportamento real, porque o `<style>` que o HTMX injetava era bloqueado.
    """
    from pathlib import Path

    css = (
        Path(__file__).resolve().parent.parent
        / "static" / "css" / "core" / "application.css"
    ).read_text(encoding="utf-8")

    assert ".htmx-indicator" in css
    assert ".htmx-request .htmx-indicator" in css


def test_nenhum_javascript_do_projeto_escreve_o_atributo_style() -> None:
    """`setAttribute('style', ...)` e bloqueado; `el.style.x = ...` nao.

    A varredura mira o primeiro, que e o que a CSP recusa. A propriedade
    continua permitida e e usada num lugar so, com justificativa:
    `_initTableScrollWrappers` mede a altura real do cabecalho e da primeira
    linha, que nenhuma regra CSS conhece.
    """
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "static" / "js"
    sobras = [
        f"{caminho.relative_to(raiz).as_posix()}:{numero}"
        for caminho in raiz.rglob("*.js")
        if "vendor" not in caminho.parts
        for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), 1)
        if "setAttribute('style'" in linha or 'setAttribute("style"' in linha
    ]

    assert sobras == [], sobras
