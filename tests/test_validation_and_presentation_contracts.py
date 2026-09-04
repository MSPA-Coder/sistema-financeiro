"""Regressões dos achados CB-03, CB-04, CB-12 e CB-13 sem banco real."""

from pathlib import Path
from unittest.mock import patch

import pytest

from accounts import services as account_services
from banking import services as banking_services


def test_owner_duplicate_is_rejected_case_insensitively_before_insert():
    with patch("accounts.services.AccountOwner.objects.filter") as lookup:
        lookup.return_value.exists.return_value = True

        with pytest.raises(ValueError, match="Já existe um titular"):
            account_services.create_owner("Ana")


def test_institution_duplicate_is_rejected_case_insensitively_before_insert():
    with patch("banking.services.FinancialInstitution.objects.filter") as lookup:
        lookup.return_value.exists.return_value = True

        with pytest.raises(ValueError, match="Já existe uma instituição"):
            banking_services.create_institution("Banco Azul", "Banco")


def test_name_duplicates_also_have_database_constraints():
    from accounts.models import AccountOwner
    from banking.models import FinancialInstitution

    assert any(constraint.name == "uq_account_owner_name_ci" for constraint in AccountOwner._meta.constraints)
    assert any(
        constraint.name == "uq_financial_institution_name_ci"
        for constraint in FinancialInstitution._meta.constraints
    )


def test_management_template_displays_expense_with_negative_sign():
    template = (Path(__file__).resolve().parents[1] / "templates/management/partials/management_content.html").read_text(encoding="utf-8")

    assert "entry.entry_type == 'despesa'" in template
    assert "entry.entry_amount|neg|money_signed" in template


def test_password_minimum_input_uses_domain_floor_not_current_value():
    template = (Path(__file__).resolve().parents[1] / "templates/settings/index.html").read_text(encoding="utf-8")

    assert 'name="min_length"' in template
    assert 'min="8"' in template
    assert 'min="15"' not in template


def test_native_constraint_messages_are_overridden_in_portuguese():
    script = (Path(__file__).resolve().parents[1] / "static/js/core/application.js").read_text(encoding="utf-8")

    assert "setCustomValidity" in script
    assert "Preencha este campo." in script
    assert "mínimo permitido" in script


def test_audit_template_handles_legacy_rows_without_a_user():
    template = (Path(__file__).resolve().parents[1] / "templates/settings/audit_log.html").read_text(encoding="utf-8")

    assert "{% elif log.user %}{{ log.user.username }}" in template
    assert "default:log.user.username" not in template
