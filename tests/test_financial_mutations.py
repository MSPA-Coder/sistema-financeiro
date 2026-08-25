"""Contratos de autorização, validação financeira e confirmação HTMX."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.http import HttpResponseRedirect
from django.test import RequestFactory, override_settings

from core.domain.finance import OPERATION_SCOPE_CURRENT_FUTURE, STATUS_PENDING
from core.htmx import HtmxAuthenticationMiddleware
from transactions import services


def test_close_month_cross_owner_does_not_create_close():
    account = SimpleNamespace(id=41)

    with (
        patch("transactions.services.can_access_account", return_value=False),
        patch("transactions.services.AccountMonthClose.objects.create") as create,
        pytest.raises(ValueError, match="Acesso negado"),
    ):
        services.close_month.__wrapped__(account, 2026, 8, Decimal("10.00"), object())

    create.assert_not_called()


def test_reopen_month_cross_owner_does_not_load_or_change_close():
    account = SimpleNamespace(id=42)

    with (
        patch("transactions.services.can_access_account", return_value=False),
        patch("transactions.services.AccountMonthClose.objects.select_for_update") as lock,
        pytest.raises(ValueError, match="Acesso negado"),
    ):
        services.reopen_month.__wrapped__(account, 2026, 8, "ajuste", object())

    lock.assert_not_called()


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1.00")])
def test_realized_amount_zero_or_negative_is_rejected(amount):
    entry = SimpleNamespace(
        status=STATUS_PENDING,
        due_date=date(2026, 8, 1),
        account=SimpleNamespace(id=1),
        entry_amount=Decimal("10.00"),
        bank_operation_id=None,
        save=Mock(),
    )

    with (
        patch("transactions.services.validate_month_not_closed"),
        patch("transactions.services.transfer_counterparty", return_value=None),
        pytest.raises(ValueError, match="positivo"),
    ):
        services.realize_transaction.__wrapped__(entry, realized_amount=amount)

    entry.save.assert_not_called()


def test_realized_amount_none_uses_planned_amount_without_truthiness_fallback():
    entry = SimpleNamespace(
        status=STATUS_PENDING,
        due_date=date(2026, 8, 1),
        account=SimpleNamespace(id=1),
        entry_amount=Decimal("10.00"),
        bank_operation_id=None,
        save=Mock(),
    )

    with (
        patch("transactions.services.validate_month_not_closed"),
        patch("transactions.services.transfer_counterparty", return_value=None),
    ):
        services.realize_transaction.__wrapped__(entry, realized_amount=None)

    assert entry.realized_amount == Decimal("10.00")
    entry.save.assert_called_once()


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1.00")])
def test_transaction_payload_rejects_invalid_realized_amount(amount):
    request = SimpleNamespace(description="válido", realized_amount=amount)

    with pytest.raises(ValueError, match="positivo"):
        services._validate_common_payload(request, Decimal("10.00"), 1)


def test_current_future_mutation_requires_signed_confirmation():
    entry = SimpleNamespace(id=77)
    request = SimpleNamespace(
        account_id=1,
        category_id=1,
        entry_type="despesa",
        description="teste",
        entry_amount=Decimal("10.00"),
        installments=1,
        due_date=date(2026, 8, 1),
        calc_mode="repeat",
        is_recurring=False,
        status=STATUS_PENDING,
        realized_date=None,
        realized_amount=None,
        counterparty_account_id=None,
    )

    with (
        patch("transactions.services.operation_entries", return_value=[]),
        pytest.raises(ValueError, match="Confirme explicitamente"),
    ):
        services.update_transaction_operation.__wrapped__(
            entry, request, OPERATION_SCOPE_CURRENT_FUTURE, None
        )


def test_htmx_expired_session_redirects_to_login_page():
    request = RequestFactory().get("/transactions/", HTTP_HX_REQUEST="true")
    request.htmx = True

    with override_settings(LOGIN_URL="/login"):
        middleware = HtmxAuthenticationMiddleware(
            lambda _request: HttpResponseRedirect("/login?next=%2Ftransactions%2F")
        )
        response = middleware(request)

    assert response.status_code == 200
    assert response["HX-Redirect"] == "/login?next=%2Ftransactions%2F"


def test_persist_buttons_have_visual_confirmation():
    root = Path(__file__).resolve().parents[1]
    reconciliation = (root / "templates/banking/_reconciliation_tables.html").read_text()
    monthly_close = (root / "templates/settings/monthly_close.html").read_text()

    assert reconciliation.count("data-sa-confirmar=") >= 5
    assert monthly_close.count("data-sa-confirmar=") == 2
    assert 'name="reason"' in monthly_close and "required" in monthly_close


def test_transaction_scope_confirmation_covers_submit_and_fail_closed_path():
    script = (Path(__file__).resolve().parents[1] / "static/js/transactions.js").read_text()
    assert "form.addEventListener('submit'" in script
    assert "form.checkValidity()" in script
    assert "confirmationBypassed" in script
    assert "A confirmação visual é necessária" in (
        Path(__file__).resolve().parents[1] / "templates/transactions/_fields.html"
    ).read_text()
