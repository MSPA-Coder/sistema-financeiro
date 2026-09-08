import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_raw_reconciliation_amounts_have_privacy_markers():
    """Todo valor do extrato entra como `td.amount`, que é o que a máscara vê.

    O modo discreto é puramente CSS: `html[data-values-hidden="true"]
    td.amount` e `td.amount:not([data-sensitive-value="true"])::after`, em
    `core/application.css`. Uma célula de valor que nascesse com outra classe
    continuaria legível com o modo discreto ligado, e nada avisaria.

    Esta guarda comparava a LINHA INTEIRA com um literal
    (`<td class="amount">{{ line.amount }}</td>`), o que a amarrava ao texto em
    vez de à propriedade. Em 08/09/2026 a coluna passou a formatar o valor —
    faltava `|money_signed`, e por isso a tela mostrava "-1175,30" em vez de
    "- R$ 1.175,30" — e ganhou `|amount_class` para colorir o sinal. A máscara
    seguiu funcionando, porque `td.amount` casa com `class="amount
    amount-negative"`; só o literal reprovou. Agora a guarda lê a classe.
    """
    template = (ROOT / "templates" / "banking" / "_reconciliation_tables.html").read_text(encoding="utf-8")

    celulas = re.findall(r"<td[^>]*>\s*\{\{ line\.amount", template)
    assert celulas, (
        "Nenhuma célula com `line.amount` encontrada: o template mudou de "
        "forma e esta guarda deixou de olhar para alguma coisa."
    )

    fora_da_mascara = [
        celula for celula in celulas if not re.match(r'<td class="amount[ "]', celula)
    ]
    assert not fora_da_mascara, (
        f"Valores do extrato fora de `td.amount`: {fora_da_mascara}. O modo "
        "discreto mascara por essa classe; sem ela o valor fica visível com a "
        "privacidade ligada."
    )

    assert 'data-sensitive-value="true"' in template


def test_privacy_reapplies_to_dynamic_content_and_masks_toasts():
    script = (ROOT / "static" / "js" / "core" / "application.js").read_text(encoding="utf-8")
    assert "htmx:afterSwap" in script
    assert script.count("document.addEventListener('htmx:afterSwap'") == 1
    assert "_maskServerAvisos" in script
    assert "_maskServerAvisos(document);\n        _initContentArea(document);" in script


def test_discreet_mode_is_explicitly_visual_and_hides_charts():
    script = (ROOT / "static" / "js" / "core" / "application.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "static" / "css" / "core" / "application.css").read_text(encoding="utf-8")
    navigation = (ROOT / "templates" / "components" / "navigation.html").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "MODO DISCRETO (PREFERÊNCIA VISUAL)" in script
    assert "não remove os dados do DOM" in script
    assert "Ativar modo discreto" in navigation
    assert "Modo discreto</span>" in navigation
    assert "visibility: hidden;" in stylesheet
    assert 'content: "Valores ocultos (****)";' in stylesheet
    assert "preferência visual local" in architecture


def test_todo_container_financeiro_marcado_exibe_placeholder_e_nao_fica_so_transparente():
    stylesheet = (ROOT / "static" / "css" / "core" / "application.css").read_text(encoding="utf-8")
    annual_stylesheet = (ROOT / "static" / "css" / "pages" / "annual-planning.css").read_text(encoding="utf-8")

    assert '[data-sensitive-value="true"]:not(.sensitive-value):not(option):not(input)::after' in stylesheet
    assert 'html[data-values-hidden="true"] td.amount' in stylesheet
    assert 'html[data-values-hidden="true"] .card-value' in stylesheet
    assert 'td.amount:not([data-sensitive-value="true"])::after' in stylesheet
    assert '.card-value:not([data-sensitive-value="true"])::after' in stylesheet
    assert 'data-values-privacy-ready="true"' in stylesheet
    assert 'data-values-privacy-pending="true"' in stylesheet
    assert 'content: "****";' in stylesheet
    assert 'annual-planning-table .annual-value-column { color: transparent' not in annual_stylesheet


def test_discreet_mode_prevents_first_paint_and_htmx_flash():
    script = (ROOT / "static" / "js" / "core" / "application.js").read_text(encoding="utf-8")

    assert "data-values-privacy-ready" in script
    assert "htmx:beforeSwap" in script
    assert "data-values-privacy-pending" in script


def test_edit_toggles_are_delegated_for_ajax_replaced_content():
    core_script = (ROOT / "static" / "js" / "core" / "application.js").read_text(encoding="utf-8")
    transactions_script = (ROOT / "static" / "js" / "transactions.js").read_text(encoding="utf-8")

    assert "document.addEventListener('click', function (event)" in core_script
    assert "event.target.closest('[data-toggle-edit]')" in core_script
    assert "O handler delegado de core/application.js" in transactions_script
