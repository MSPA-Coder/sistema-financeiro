"""Cobertura da página individual de conta usada pelo contrato patrimonial."""
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from accounts.models import AccountOwner, UserOwnerAccess
from accounts.services import save_function_permissions
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, ENTRY_TYPE_INCOME, STATUS_REALIZED
from core.domain.identity import USER_TYPE_USER
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def account_detail_setup():
    user = get_user_model().objects.create_user(
        username="leitor", password="senha-segura", user_type=USER_TYPE_USER
    )
    save_function_permissions(user, {"transactions.view"})
    owner = AccountOwner.objects.create(name="Titular da conta")
    UserOwnerAccess.objects.create(user=user, owner=owner, can_view=True)
    institution = FinancialInstitution.objects.create(
        institution_name="Banco da conta", institution_type="Banco"
    )
    account = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Conta principal",
        initial_balance=Decimal("100.00"), initial_balance_date=date(2026, 1, 1),
    )
    category = CashFlowCategory.objects.create(category_name="Categoria da conta")
    CashFlowEntry.objects.create(
        account=account, category=category, entry_type=ENTRY_TYPE_INCOME,
        description="Receita", entry_amount=Decimal("40.00"), due_date=date(2026, 3, 4),
        realized_date=date(2026, 3, 4), realized_amount=Decimal("40.00"), status=STATUS_REALIZED,
    )
    CashFlowEntry.objects.create(
        account=account, category=category, entry_type=ENTRY_TYPE_EXPENSE,
        description="Despesa", entry_amount=Decimal("15.00"), due_date=date(2026, 3, 5),
        realized_date=date(2026, 3, 5), realized_amount=Decimal("15.00"), status=STATUS_REALIZED,
    )
    AccountMonthClose.objects.create(
        account=account, year=2026, month=2, closing_balance=Decimal("100.00"),
        closed_at=timezone.now(), closed_by_user=user,
    )
    return user, account


def test_account_detail_reuses_realized_context_and_reference_date(account_detail_setup):
    user, account = account_detail_setup
    client = Client()
    client.force_login(user)

    response = client.get(f"/banking/accounts/{account.id}/?data=2026-03-05")

    assert response.status_code == 200
    assert response.context["account"] == account
    assert response.context["reference_date"] == date(2026, 3, 5)
    assert response.context["saldo_atual"] == Decimal("125.00")
    assert response.context["realizado"]["total_receitas"] == Decimal("40.00")
    assert response.context["realizado"]["total_despesas"] == Decimal("15.00")
    assert [tx.description for tx in response.context["txs"]] == ["Receita", "Despesa"]
    assert response.context["ultimo_fechamento"].month == 2


def test_account_detail_uses_the_end_of_a_selected_closed_month(account_detail_setup):
    user, account = account_detail_setup
    client = Client()
    client.force_login(user)

    response = client.get(f"/banking/accounts/{account.id}/?period=2026-03")

    assert response.status_code == 200
    assert response.context["reference_date"] == date(2026, 3, 31)


def test_account_detail_does_not_reveal_a_foreign_account(account_detail_setup):
    user, _account = account_detail_setup
    other_owner = AccountOwner.objects.create(name="Outro titular")
    institution = FinancialInstitution.objects.get(institution_name="Banco da conta")
    foreign_account = FinancialAccount.objects.create(
        owner=other_owner, institution=institution, account_name="Conta alheia",
    )
    client = Client()
    client.force_login(user)

    assert client.get(f"/banking/accounts/{foreign_account.id}/").status_code == 404
