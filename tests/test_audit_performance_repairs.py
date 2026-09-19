"""Regressões dos limites e dos caminhos de leitura auditados."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core import patrimonio
from reports import services
from transactions.models import CashFlowCategory, CashFlowEntry


def test_projection_period_limit_is_explicit_and_does_not_truncate():
    with (
        override_settings(REPORT_MAX_PROJECTION_RANGE_MONTHS=2),
        pytest.raises(services.InvalidMonthPeriodError, match="2 meses"),
    ):
        services.bounded_projection_month_range(date(2026, 1, 1), date(2026, 4, 1))


def test_upcoming_period_limit_has_a_clear_message():
    with (
        override_settings(REPORT_MAX_UPCOMING_PERIOD_DAYS=2),
        pytest.raises(services.InvalidMonthPeriodError, match="2 dias"),
    ):
        services.bounded_upcoming_period(date(2026, 1, 1), date(2026, 1, 3))


@pytest.mark.django_db
def test_patrimony_summary_reads_balances_in_batch():
    owner = AccountOwner.objects.create(name="Auditoria")
    institution = FinancialInstitution.objects.create(
        institution_name="Banco de auditoria", institution_type="Banco"
    )
    category = CashFlowCategory.objects.create(category_name="Auditoria")
    accounts = [
        FinancialAccount.objects.create(
            owner=owner,
            institution=institution,
            account_name=f"Conta {index}",
            initial_balance=Decimal("100.00"),
            currency="BRL",
            initial_balance_date=date(2025, 12, 31),
        )
        for index in range(3)
    ]
    for account in accounts:
        CashFlowEntry.objects.create(
            account=account,
            category=category,
            entry_type="receita",
            description="Entrada",
            entry_amount=Decimal("10.00"),
            due_date=date(2026, 1, 10),
            realized_date=date(2026, 1, 10),
            realized_amount=Decimal("10.00"),
            status="realizado",
        )

    with CaptureQueriesContext(connection) as queries:
        resumo = patrimonio.montar_resumo(date(2026, 1, 31))

    assert {linha["saldo"] for linha in resumo["contas"]} == {"110.00"}
    assert len(queries) <= 5
