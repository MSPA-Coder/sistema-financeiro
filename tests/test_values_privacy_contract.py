from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_raw_reconciliation_amounts_have_privacy_markers():
    template = (ROOT / "templates" / "banking" / "_reconciliation_tables.html").read_text()
    assert '<td class="amount">{{ line.amount }}</td>' in template
    assert 'data-sensitive-value="true"' in template


def test_privacy_reapplies_to_dynamic_content_and_masks_toasts():
    script = (ROOT / "static" / "js" / "core" / "application.js").read_text()
    assert "htmx:afterSwap" in script
    assert "htmx:load" in script
    assert "_maskServerAvisos" in script
    assert "_maskServerAvisos(document);\n        _initContentArea(document);" in script


def test_edit_toggles_are_delegated_for_ajax_replaced_content():
    core_script = (ROOT / "static" / "js" / "core" / "application.js").read_text()
    transactions_script = (ROOT / "static" / "js" / "transactions.js").read_text()

    assert "document.addEventListener('click', function (event)" in core_script
    assert "event.target.closest('[data-toggle-edit]')" in core_script
    assert "O handler delegado de core/application.js" in transactions_script
