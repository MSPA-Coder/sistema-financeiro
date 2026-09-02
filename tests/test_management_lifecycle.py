"""Contratos de exclusão e arquivamento da camada gerencial."""

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

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


def _budget(owner_id: int = 1):
    budget = Mock(
        id=7,
        owner_id=owner_id,
        category_id=2,
        year=2026,
        month=8,
        planned_amount=Decimal("1500.00"),
    )
    budget.owner = SimpleNamespace(name="Titular A")
    budget.category = SimpleNamespace(category_name="Moradia")
    return budget


@contextmanager
def _cenario_orcamento(budget, *, tem_realizado: bool, titulares_permitidos: list[int]):
    """Monta o entorno de `retire_budget` sem tocar no banco.

    `log_audit_event` entra na lista porque o registro passou a ser feito
    dentro do serviço -- sem o patch, o teste tentaria gravar `AuditLog`.
    """
    manager = Mock()
    manager.select_related.return_value.get.return_value = budget
    entry_query = Mock()
    entry_query.exists.return_value = tem_realizado

    with (
        patch("management.services.MonthlyBudget.objects", manager),
        patch("management.services.CashFlowEntry.objects.filter", return_value=entry_query),
        patch("management.services.accessible_owner_ids", return_value=titulares_permitidos),
        patch("core.services.log_audit_event") as auditoria,
    ):
        yield auditoria


def test_budget_with_realized_entry_is_archived_instead_of_deleted():
    budget = _budget()

    with _cenario_orcamento(budget, tem_realizado=True, titulares_permitidos=[1]):
        result, action = services.retire_budget(Mock(), 1)

    assert result is budget and action == "archived"
    assert budget.active is False
    budget.delete.assert_not_called()


def test_budget_without_realized_entry_is_deleted_and_audited():
    budget = _budget()

    with _cenario_orcamento(budget, tem_realizado=False, titulares_permitidos=[1]) as auditoria:
        _result, action = services.retire_budget(Mock(), 1)

    assert action == "deleted"
    budget.delete.assert_called_once()
    # O id vai para a auditoria porque foi lido ANTES do `delete()`, que zera a
    # chave primária da instância.
    assert auditoria.call_args.args[:3] == ("monthly_budget", 7, "deleted")


def test_budget_de_titular_fora_do_escopo_nao_e_removido():
    """O caso que a permissão funcional `management.manage` deixava passar.

    O usuário enxerga o titular 1; o orçamento é do titular 99. Antes desta
    correção, o POST direto removia assim mesmo -- a listagem da tela filtra,
    a rota não filtrava.
    """
    budget = _budget(owner_id=99)

    with (
        _cenario_orcamento(budget, tem_realizado=False, titulares_permitidos=[1]) as auditoria,
        pytest.raises(ValueError) as recusa,
    ):
        services.retire_budget(Mock(), 1)

    budget.delete.assert_not_called()
    budget.save.assert_not_called()
    auditoria.assert_not_called()
    # Mesma mensagem do inexistente: a recusa não confirma que o id existe.
    assert str(recusa.value) == "Orçamento não encontrado."


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
