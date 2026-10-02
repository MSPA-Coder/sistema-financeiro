"""Finalidade da conta: pessoal ou administrada (dinheiro de terceiros).

A conta administrada nasce pessoal, só muda por escolha explícita e valor
inválido é recusado. O efeito no filtro global de grupos está em
`test_filtro_grupos_de_conta.py`.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from banking.models import FinancialAccount, FinancialInstitution
from banking.services import create_account, update_account
from core.domain.finance import ACCOUNT_PURPOSE_ADMINISTERED, ACCOUNT_PURPOSE_PERSONAL

pytestmark = pytest.mark.django_db


@pytest.fixture
def base():
    user = AppUser.objects.create_user(username="operador-finalidade", password="senha-segura")
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    return user, titular, banco


def _criar(user, titular, banco, **extra):
    return create_account(
        user, owner_id=str(titular.id), institution_id=str(banco.id), account_name="Conta 02",
        initial_balance="0", currency="BRL", **extra,
    )


def test_conta_nasce_pessoal(base):
    conta = _criar(*base)
    assert conta.purpose == ACCOUNT_PURPOSE_PERSONAL


def test_conta_pode_ser_criada_e_alterada_para_administrada(base):
    user, titular, banco = base
    conta = _criar(user, titular, banco, purpose=ACCOUNT_PURPOSE_ADMINISTERED)
    assert conta.purpose == ACCOUNT_PURPOSE_ADMINISTERED
    update_account(
        user, conta, owner_id=str(titular.id), institution_id=str(banco.id), account_name="Conta 02",
        initial_balance="0", currency="BRL", purpose=ACCOUNT_PURPOSE_PERSONAL,
    )
    conta.refresh_from_db()
    assert conta.purpose == ACCOUNT_PURPOSE_PERSONAL


def test_finalidade_invalida_e_recusada_no_servico_e_no_banco(base):
    user, titular, banco = base
    with pytest.raises(ValueError, match="Finalidade"):
        _criar(user, titular, banco, purpose="terceiros")
    conta = _criar(user, titular, banco)
    FinancialAccount.objects.filter(id=conta.id).update(purpose="pessoal")
    with pytest.raises(IntegrityError), transaction.atomic():
        FinancialAccount.objects.filter(id=conta.id).update(purpose="terceiros")
