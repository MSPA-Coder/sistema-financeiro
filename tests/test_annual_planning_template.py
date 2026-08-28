"""Contratos pequenos da apresentação do planejamento anual."""

from decimal import Decimal
from types import SimpleNamespace

from django.template.loader import render_to_string

from core.context_processors import _build_menu_items
from core.domain.finance import VIEW_MODE_OPTIONS


def _find_menu_item(items, label):
    for item in items:
        if item.label == label:
            return item
        found = _find_menu_item(item.children, label)
        if found:
            return found
    return None


def test_annual_planning_is_reachable_from_reports_menu():
    item = _find_menu_item(_build_menu_items(), "Planejamento anual")

    assert item is not None
    assert item.url == "/reports/annual-planning/"


def test_annual_planning_partial_uses_summary_headers_without_transfer_card():
    report = {
        "reference_month_label": "mar/2026",
        "owner_columns": [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Bia"}],
        "months": [
            {"key": "2026-03", "label": "Mar/26", "is_current": True},
            {"key": "2026-04", "label": "Abr/26", "is_current": False},
        ],
        "rows": [
            {
                "kind": "description",
                "label": "Moradia",
                "description": "Custos fixos",
                "level": 0,
                "owner_values": [Decimal("120.00"), Decimal("80.00")],
                "months": [
                    {"value": Decimal("200.00"), "is_current": True},
                    {"value": Decimal("210.00"), "is_current": False},
                ],
            }
        ],
        "summary_rows": [
            {
                "label": "Movimentações Internas",
                "owner_values": [Decimal("120.00"), Decimal("80.00")],
                "months": [
                    {"value": Decimal("200.00"), "is_current": True},
                    {"value": Decimal("210.00"), "is_current": False},
                ],
            }
        ],
        "totals": {
            "owner_values": [Decimal("120.00"), Decimal("80.00")],
            "months": [
                {"value": Decimal("200.00"), "is_current": True},
                {"value": Decimal("210.00"), "is_current": False},
            ],
        },
    }

    rendered = render_to_string(
        "reports/partials/annual_planning_content.html",
        {
            "report": report,
            "layout": "calendar",
            "view_mode": "todos",
            "status_options": VIEW_MODE_OPTIONS,
            "filter_panel_open": True,
            "owners": [SimpleNamespace(id=1, name="Ana"), SimpleNamespace(id=2, name="Bia")],
            "accounts": [
                SimpleNamespace(id=10, owner=SimpleNamespace(name="Ana"), account_name="Conta A"),
                SimpleNamespace(id=20, owner=SimpleNamespace(name="Bia"), account_name="Conta B"),
            ],
            "selected_owner_ids": [1, 2],
            "selected_account_ids": [10, 20],
            "show_descriptions": True,
        },
    )

    assert rendered.count("Ana") == 1 and rendered.count("Bia") == 1
    assert "Custos fixos" in rendered
    assert "Transferências internas" not in rendered
    assert "Mês de referência · mar/2026" in rendered
    assert "Todos os modos" in rendered
    assert "200,00" in rendered
    assert '<details class="annual-filter-details" open>' in rendered
    assert 'name="filters_open" value="1"' in rendered
    assert rendered.count('name="owner_ids"') == 1
    assert rendered.count('name="account_ids"') == 1
    assert rendered.count(" selected") >= 5
