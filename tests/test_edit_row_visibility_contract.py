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
