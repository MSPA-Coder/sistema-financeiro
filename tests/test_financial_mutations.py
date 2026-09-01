"""Contratos de autorização, validação financeira e confirmação HTMX."""

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.http import HttpResponseRedirect
from django.test import RequestFactory, override_settings

from core.domain.finance import (
    OPERATION_SCOPE_ALL,
    OPERATION_SCOPE_CURRENT_FUTURE,
    OPERATION_SCOPE_SINGLE,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_ALL,
    VIEW_PROJECTED,
)
from core.htmx import HtmxAuthenticationMiddleware
from core.services import audit_request_context, log_audit_event
from transactions import services, views


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


def _group_entry(entry_id, installment):
    return SimpleNamespace(
        id=entry_id,
        operation_type="installment",
        is_recurring=False,
        installments=3,
        current_installment=installment,
        due_date=date(2026, 8, installment),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (OPERATION_SCOPE_SINGLE, OPERATION_SCOPE_SINGLE),
        (OPERATION_SCOPE_ALL, OPERATION_SCOPE_ALL),
        (OPERATION_SCOPE_CURRENT_FUTURE, OPERATION_SCOPE_CURRENT_FUTURE),
    ],
)
def test_grouped_operation_scope_accepts_only_explicit_valid_values(raw, expected):
    assert views._operation_scope_from_request(_group_entry(1, 1), raw) == expected


@pytest.mark.parametrize("raw", [None, "", "unexpected"])
def test_grouped_operation_scope_rejects_missing_or_invalid_value(raw):
    with pytest.raises(ValueError, match="escopo|Escopo"):
        views._operation_scope_from_request(_group_entry(1, 1), raw)


@pytest.mark.parametrize("payload", [{}, {"operation_scope": "unexpected"}])
def test_delete_view_returns_400_for_missing_or_invalid_group_scope(payload):
    request = RequestFactory().post("/transaction/delete/1/", data=payload)
    request.user = SimpleNamespace()
    entry = _group_entry(1, 1)
    queryset = Mock()
    queryset.select_related.return_value.first.return_value = entry

    with (
        patch("transactions.views.CashFlowEntry.objects.filter", return_value=queryset),
        patch("transactions.views.access.can_access_entry", return_value=True),
        patch("transactions.views._log_failed_transaction_action"),
    ):
        response = inspect.unwrap(views.transaction_delete)(request, 1)

    assert response.status_code == 400


def test_simple_operation_without_scope_is_explicitly_single():
    entry = SimpleNamespace(operation_type="single", is_recurring=False, installments=1)

    assert views._operation_scope_from_request(entry, None) == OPERATION_SCOPE_SINGLE


def test_scoped_entries_honors_single_all_and_current_future():
    current = _group_entry(2, 2)
    entries = [_group_entry(1, 1), current, _group_entry(3, 3)]

    assert [entry.id for entry in services.scoped_entries(current, entries, OPERATION_SCOPE_SINGLE)] == [2]
    assert [entry.id for entry in services.scoped_entries(current, entries, OPERATION_SCOPE_ALL)] == [1, 2, 3]
    assert [entry.id for entry in services.scoped_entries(current, entries, OPERATION_SCOPE_CURRENT_FUTURE)] == [2, 3]


def test_scoped_entries_rejects_unknown_scope_in_service_layer():
    entry = _group_entry(1, 1)

    with pytest.raises(ValueError, match="Escopo"):
        services.scoped_entries(entry, [entry], "unexpected")


def test_divided_installments_absorb_rounding_remainder_in_last_entry():
    amounts = [
        services.installment_amount_for(Decimal("100.00"), 3, "divide", installment)
        for installment in range(1, 4)
    ]

    assert amounts == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
    assert sum(amounts) == Decimal("100.00")


def test_projection_balance_is_canonical_regardless_of_status_filter():
    from reports import services as report_services

    category = SimpleNamespace(is_internal=False)
    realized_income = SimpleNamespace(
        status=STATUS_REALIZED, entry_type="receita", entry_amount=Decimal("100.00"),
        realized_amount=Decimal("100.00"), due_date=date(2026, 8, 1),
        realized_date=date(2026, 8, 1), category=category,
    )
    planned_expense = SimpleNamespace(
        status=STATUS_PROJECTED, entry_type="despesa", entry_amount=Decimal("20.00"),
        realized_amount=None, due_date=date(2026, 8, 2), realized_date=None, category=category,
    )
    planned_income = SimpleNamespace(
        status=STATUS_PROJECTED, entry_type="receita", entry_amount=Decimal("10.00"),
        realized_amount=None, due_date=date(2026, 9, 1), realized_date=None, category=category,
    )

    def entries(_account_ids, _start, _end, mode):
        return [realized_income, planned_expense, planned_income] if mode == VIEW_ALL else [planned_expense, planned_income]

    with (
        patch("reports.services.entries_for_period", side_effect=entries),
        patch("reports.services.decimal_balance_before", return_value=Decimal("0.00")),
    ):
        projected = report_services.projection_months_between(
            [1], date(2026, 8, 1), date(2026, 9, 1), VIEW_PROJECTED
        )
        all_statuses = report_services.projection_months_between(
            [1], date(2026, 8, 1), date(2026, 9, 1), VIEW_ALL
        )

    assert [month["saldo"] for month in projected] == [Decimal("80.00"), Decimal("90.00")]
    assert [month["saldo"] for month in all_statuses] == [Decimal("80.00"), Decimal("90.00")]


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


@override_settings(AUDIT_TRUSTED_PROXY_CIDRS=())
def test_audit_context_ignores_forwarded_header_from_untrusted_source():
    request = RequestFactory().post(
        "/transactions/",
        HTTP_X_FORWARDED_FOR="198.51.100.7",
        REMOTE_ADDR="203.0.113.9",
    )

    context = audit_request_context(request)

    assert context.client_ip == "203.0.113.9"
    assert context.proxy_ip is None


@override_settings(AUDIT_TRUSTED_PROXY_CIDRS=("10.0.0.0/8",))
def test_audit_context_accepts_forwarded_header_only_from_trusted_proxy():
    request = RequestFactory().post(
        "/transactions/",
        HTTP_X_FORWARDED_FOR="198.51.100.7, 10.0.0.2",
        HTTP_X_REQUEST_ID="req-123",
        REMOTE_ADDR="10.0.0.2",
    )

    context = audit_request_context(request)

    assert context.client_ip == "198.51.100.7"
    assert context.proxy_ip == "10.0.0.2"
    assert context.request_id == "req-123"


def test_log_audit_event_persists_request_context_and_minimal_summary():
    request = RequestFactory().post("/transactions/", REMOTE_ADDR="127.0.0.1")
    request.user = SimpleNamespace(id=12, is_authenticated=True, get_username=lambda: "operador")

    with patch("core.models.AuditLog.objects.create") as create:
        log_audit_event(
            "cash_flow_entry",
            99,
            "delete",
            request_context=audit_request_context(request),
            result="failure",
            summary="Operação recusada pela validação do servidor.",
        )

    kwargs = create.call_args.kwargs
    assert kwargs["actor_id"] == 12
    assert kwargs["actor_name"] == "operador"
    assert kwargs["client_ip"] == "127.0.0.1"
    assert kwargs["proxy_ip"] is None
    assert kwargs["result"] == "failure"
    assert kwargs["summary"] == "Operação recusada pela validação do servidor."
