"""O saldo de fechamento é calculado depois de trancar a conta.

Até 24/09/2026 a tela calculava o saldo e só então chamava `close_month`, que
trancava a conta. Uma realização que entrasse nesse intervalo ficava de fora:
o mês fechava com um saldo que já não era o dele, e o bloqueio de mês fechado
impedia de corrigir sem reabrir.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    ENTRY_TYPE_INCOME,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions import services
from transactions.models import CashFlowCategory
from transactions.services import TransactionRequest, create_transaction_batch


@pytest.fixture
def cenario():
    usuario = AppUser.objects.create_user(
        username="fechamento", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Titular fechamento")
    UserOwnerAccess.objects.create(
        user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco fechamento", institution_type="Banco")
    conta = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Corrente fechamento",
        initial_balance=Decimal("100.00"), initial_balance_date=date(2026, 1, 1),
    )
    categoria = CashFlowCategory.objects.create(category_name="Salário fechamento", kind=CATEGORY_KIND_MANAGERIAL)
    return usuario, conta, categoria


def _receita(usuario, conta, categoria, valor: str) -> None:
    create_transaction_batch(
        TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_INCOME,
            description="Receita", entry_amount=Decimal(valor), installments=1,
            due_date=date(2026, 8, 10), status=STATUS_REALIZED, realized_date=date(2026, 8, 10),
        ),
        user=usuario,
    )


@pytest.mark.django_db
def test_sem_valor_o_fechamento_calcula_o_saldo_realizado(cenario):
    usuario, conta, categoria = cenario
    _receita(usuario, conta, categoria, "50.00")

    fechado = services.close_month(conta, 2026, 8, None, usuario)

    assert fechado.closing_balance == Decimal("150.00")


@pytest.mark.django_db
def test_realizacao_que_chega_antes_do_lock_entra_no_saldo(cenario, monkeypatch):
    """Simula a concorrência: a realização comita enquanto o fechamento espera o lock."""
    usuario, conta, categoria = cenario
    trancar = services._lock_accounts
    pendente = [True]

    def trancar_depois_de_uma_realizacao(ids):
        # Só na primeira chamada: a própria realização também tranca a conta.
        if pendente:
            pendente.clear()
            _receita(usuario, conta, categoria, "70.00")
        return trancar(ids)

    monkeypatch.setattr(services, "_lock_accounts", trancar_depois_de_uma_realizacao)
    fechado = services.close_month(conta, 2026, 8, None, usuario)

    assert fechado.closing_balance == Decimal("170.00")


@pytest.mark.django_db
def test_a_tela_de_fechamento_delega_o_calculo(cenario, client):
    usuario, conta, categoria = cenario
    _receita(usuario, conta, categoria, "30.00")
    client.force_login(usuario)

    client.post(
        reverse("core:settings_close_month"),
        {"account_id": conta.id, "year": 2026, "month": 8},
        secure=True,
    )

    fechado = conta.month_closes.get(year=2026, month=8)
    assert fechado.closing_balance == Decimal("130.00")
