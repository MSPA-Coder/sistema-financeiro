"""Regressões dos limites e dos caminhos de leitura auditados."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
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


@pytest.mark.django_db(transaction=True)
def test_patrimony_snapshot_reads_balances_in_batch(monkeypatch, tmp_path):
    """O custo de consulta do snapshot v4 não cresce com o número de contas."""
    token = "token-de-integracao-v4-com-mais-de-trinta-e-dois-caracteres"
    arquivo = tmp_path / "patrimonio_integration_token"
    arquivo.write_text(token, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO_V4, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(arquivo))

    owner = AccountOwner.objects.create(name="Auditoria")
    institution = FinancialInstitution.objects.create(
        institution_name="Banco de auditoria", institution_type="Banco"
    )
    category = CashFlowCategory.objects.create(category_name="Auditoria")

    def criar_conta(indice):
        conta = FinancialAccount.objects.create(
            owner=owner,
            institution=institution,
            account_name=f"Conta {indice}",
            initial_balance=Decimal("100.00"),
            currency="BRL",
            initial_balance_date=date(2025, 12, 31),
        )
        CashFlowEntry.objects.create(
            account=conta,
            category=category,
            entry_type="receita",
            description="Entrada",
            entry_amount=Decimal("10.00"),
            due_date=date(2026, 1, 10),
            realized_date=date(2026, 1, 10),
            realized_amount=Decimal("10.00"),
            status="realizado",
        )

    def pedir_snapshot():
        with CaptureQueriesContext(connection) as queries:
            resposta = Client().get("/patrimonio/v4/snapshot", HTTP_AUTHORIZATION=f"Bearer {token}")
        assert resposta.status_code == 200
        return resposta.json(), len(queries)

    criar_conta(0)
    _, consultas_com_uma = pedir_snapshot()
    for indice in range(1, 4):
        criar_conta(indice)
    corpo, consultas_com_quatro = pedir_snapshot()

    saldos = [item["payload"]["balance"] for item in corpo["items"] if item["resource"] == "account"]
    assert saldos == ["110.00"] * 4
    assert consultas_com_quatro == consultas_com_uma
