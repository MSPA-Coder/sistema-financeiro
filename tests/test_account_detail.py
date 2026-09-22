"""Cobertura da página individual de conta usada pelo contrato patrimonial."""
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import AccountOwner, UserOwnerAccess
from accounts.services import save_function_permissions
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_PROJECTED,
    VIEW_REALIZED,
)
from core.domain.identity import USER_TYPE_USER
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry
from transactions.services import build_transactions_view_context

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


@pytest.mark.parametrize("mode, chave", [(VIEW_REALIZED, "realizado"), (VIEW_PROJECTED, "previsto")])
def test_account_detail_totals_match_the_transactions_screen(account_detail_setup, mode, chave):
    """Detalhe e Lançamentos calculam o mesmo extrato; os totais não divergem.

    O detalhe resolve o recorte uma vez e calcula os dois modos sobre ele. Este
    teste é o que garante que o atalho não mudou o resultado.
    """
    user, account = account_detail_setup
    hoje = date.today()
    # Previsto só lista o que vence de hoje em diante; um lançamento no mês
    # corrente é o que faz o bloco previsto diferir do realizado.
    CashFlowEntry.objects.create(
        account=account, category=CashFlowCategory.objects.get(), entry_type=ENTRY_TYPE_EXPENSE,
        description="Conta futura", entry_amount=Decimal("30.00"), due_date=hoje, status=STATUS_PROJECTED,
    )
    periodo = hoje.strftime("%Y-%m")
    client = Client()
    client.force_login(user)

    detalhe = client.get(f"/banking/accounts/{account.id}/?period={periodo}").context[chave]
    lancamentos = build_transactions_view_context(
        user, {"account_id": str(account.id), "period": periodo, "mode": mode}, {}
    )["blocos"][0]

    for campo in ("saldo_inicial", "saldo_final", "total_receitas", "total_despesas"):
        assert detalhe[campo] == lancamentos[campo], campo


def test_account_detail_query_budget(account_detail_setup):
    """Um teto para o custo da página, que chegou a 89 consultas.

    Eram dois contextos inteiros da tela Lançamentos -- seletores, contas de
    formulário e edição inline incluídos --, uma consulta por `has_perm` no
    menu, a data inicial do sistema relida a cada serviço e uma gravação de
    sessão. Subir este número é uma decisão, não um acidente.
    """
    user, account = account_detail_setup
    client = Client()
    client.force_login(user)
    url = f"/banking/accounts/{account.id}/?data=2026-03-05"
    client.get(url)

    with CaptureQueriesContext(connection) as queries:
        assert client.get(url).status_code == 200

    # 34 quando o teto foi fixado.
    assert len(queries) <= 36


def test_account_detail_keeps_the_month_remembered_by_the_transactions_screen(account_detail_setup):
    user, account = account_detail_setup
    client = Client()
    client.force_login(user)
    client.get("/transactions/?period=2026-05")
    lembrado = (client.session["tx_sel_year"], client.session["tx_sel_month"])

    assert client.get(f"/banking/accounts/{account.id}/?period=2026-03").status_code == 200

    assert (client.session["tx_sel_year"], client.session["tx_sel_month"]) == lembrado == (2026, 5)


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
