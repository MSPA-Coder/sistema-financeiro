"""Contratos de exclusão e arquivamento da camada gerencial."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from management import services


def _item_with_links(has_links: bool):
    item = Mock()
    item.entry_links.exists.return_value = has_links
    return item


def test_tag_with_entry_history_is_archived_instead_of_deleted():
    tag = _item_with_links(True)

    with patch("management.services._resolve_tag", return_value=tag):
        result, action = services.retire_tag(1)

    assert result is tag and action == "archived"
    assert tag.active is False
    tag.delete.assert_not_called()


def test_project_without_history_is_deleted():
    project = _item_with_links(False)

    with patch("management.services._resolve_project", return_value=project):
        result, action = services.retire_project(1)

    assert result is project and action == "deleted"
    project.delete.assert_called_once()


def test_budget_with_realized_entry_is_archived_instead_of_deleted():
    budget = Mock(owner_id=1, category_id=2, year=2026, month=8)
    budget.owner = SimpleNamespace()
    budget.category = SimpleNamespace()
    manager = Mock()
    manager.select_related.return_value.get.return_value = budget
    entry_query = Mock()
    entry_query.exists.return_value = True

    with (
        patch("management.services.MonthlyBudget.objects", manager),
        patch("management.services.CashFlowEntry.objects.filter", return_value=entry_query),
    ):
        result, action = services.retire_budget(1)

    assert result is budget and action == "archived"
    assert budget.active is False
    budget.delete.assert_not_called()


def test_archived_tag_cannot_receive_new_link():
    tag = Mock(active=False)
    tag.id = 1

    with patch("management.services.ManagementTag.objects.filter") as filtered:
        filtered.return_value.first.return_value = tag
        try:
            services._resolve_tag(1)
        except ValueError as exc:
            assert "arquivada" in str(exc)
        else:
            raise AssertionError("A tag arquivada não deveria aceitar novo vínculo.")
