import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_edit_toggle_uses_the_css_visibility_state():
    script = (ROOT / "static" / "js" / "core" / "application.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "css" / "core" / "application.css").read_text(encoding="utf-8")

    assert "!row.classList.contains('is-editing')" in script
    assert "row.classList.toggle('is-editing', show);" in script
    assert ".edit-row { display: none; }" in stylesheet
    assert ".edit-row.is-editing { display: table-row; }" in stylesheet


def test_painel_reconstroi_os_graficos_pelo_mesmo_caminho():
    """A primeira carga e a troca de filtro chamam o MESMO construtor.

    Havia dois: um no `load` e outro no `app:contentLoaded`. Eles divergiram --
    a cópia da troca de filtro perdeu os plugins que desenham os rótulos --, e
    o sintoma era um gráfico que "mudava de forma sozinho" ao mexer num filtro.
    Duas construções do mesmo gráfico é o defeito; uma só é o contrato.
    """
    script = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")

    assert "window.addEventListener('load', renderizar);" in script
    assert "document.addEventListener('app:contentLoaded', renderizar);" in script
    assert script.count("function construirGraficos") == 1


def test_painel_destroi_os_graficos_da_troca_anterior():
    """Quem destrói tem de ser uma lista, não uma busca por `id` no documento.

    Cada troca de filtro cria seis gráficos. Os seis anteriores saem do
    documento junto com o HTML antigo, então procurá-los por `id` acha os
    NOVOS -- e os velhos ficam vivos, presos a canvas que não existem mais,
    cada um segurando o seu bitmap e o seu observador de tamanho. Medido: 6, 12,
    18, 24... a cada troca. Passado o teto de memória de canvas do navegador, os
    canvas novos param de conseguir contexto e os gráficos somem.
    """
    script = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")

    assert "var graficos = [];" in script
    assert "graficos.forEach" in script
    assert script.count("guardar(new Chart(") == script.count("new Chart(")


def test_page_styles_do_not_override_the_global_edit_row_visibility_contract():
    page_styles = [
        ROOT / "static" / "css" / "transactions.css",
        ROOT / "static" / "css" / "pages" / "owners.css",
        ROOT / "static" / "css" / "pages" / "accounts.css",
        ROOT / "static" / "css" / "pages" / "banks.css",
        ROOT / "static" / "css" / "pages" / "categories.css",
        ROOT / "static" / "css" / "pages" / "permissions.css",
    ]

    for stylesheet in page_styles:
        assert not re.search(
            r"\.edit-row\s*\{[^}]*display\s*:\s*none",
            stylesheet.read_text(encoding="utf-8"),
        )
