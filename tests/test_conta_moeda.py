"""A moeda da conta: escolhida na criação, imóvel depois do primeiro lançamento.

A `CheckConstraint` no PostgreSQL (`test_invariantes_persistidos.py`) garante
que a sigla é uma das válidas. Ela não tem como garantir a outra metade: que a
moeda de uma conta com histórico não muda. Isso depende de contar lançamentos,
e por isso é regra de serviço -- medida aqui, com banco de verdade.

Por que a regra existe: trocar a moeda de uma conta com lançamentos não converte
nada. Os mesmos números continuariam gravados, valendo outra coisa, e nenhum
relatório teria como perceber. A saída, quando a moeda estiver errada, é outra
conta.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from banking.services import create_account, update_account
from core.domain.finance import (
    CURRENCY_BRL,
    CURRENCY_USD,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def usuario():
    return get_user_model().objects.create_user(
        username="teste-moeda",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )


@pytest.fixture
def titular():
    return AccountOwner.objects.create(name="Titular de teste")


@pytest.fixture
def instituicao():
    return FinancialInstitution.objects.create(
        institution_name="Corretora de teste",
        institution_type="Corretora",
    )


def _campos(titular, instituicao, **extras):
    padrao = {
        "owner_id": str(titular.id),
        "institution_id": str(instituicao.id),
        "account_name": "Conta de teste",
        "initial_balance": "1000,00",
        "currency": CURRENCY_BRL,
    }
    padrao.update(extras)
    return padrao


def _lancamento(conta):
    categoria = CashFlowCategory.objects.create(category_name="Categoria de teste")
    return CashFlowEntry.objects.create(
        account=conta,
        category=categoria,
        entry_type=ENTRY_TYPE_EXPENSE,
        description="Lançamento de teste",
        entry_amount=Decimal("100.00"),
        due_date=date(2026, 6, 10),
        status=STATUS_PROJECTED,
        operation_type=OPERATION_SINGLE,
    )


# --- Criação ---------------------------------------------------------------


def test_conta_em_dolar_e_criada_pelo_servico(usuario, titular, instituicao):
    conta = create_account(
        usuario,
        **_campos(titular, instituicao, currency=CURRENCY_USD, initial_balance_date="2025-12-31"),
    )

    assert conta.currency == CURRENCY_USD
    assert conta.initial_balance_date == date(2025, 12, 31)
    assert conta.initial_balance == Decimal("1000.00")


def test_criacao_sem_moeda_e_recusada(usuario, titular, instituicao):
    """Moeda em branco não vira BRL calado: o formulário tem que dizer qual é."""
    with pytest.raises(ValueError, match="Moeda é obrigatória"):
        create_account(usuario, **_campos(titular, instituicao, currency=""))

    assert FinancialAccount.objects.count() == 0


def test_criacao_com_moeda_desconhecida_e_recusada(usuario, titular, instituicao):
    with pytest.raises(ValueError, match="Moeda inválida"):
        create_account(usuario, **_campos(titular, instituicao, currency="EUR"))


def test_data_do_saldo_invalida_e_recusada(usuario, titular, instituicao):
    with pytest.raises(ValueError, match="Data do saldo inicial inválida"):
        create_account(usuario, **_campos(titular, instituicao, initial_balance_date="31/12/2025"))


def test_data_do_saldo_em_branco_vale_hoje(usuario, titular, instituicao):
    from django.utils.timezone import localdate

    conta = create_account(usuario, **_campos(titular, instituicao, initial_balance_date=""))

    assert conta.initial_balance_date == localdate()


# --- Troca de moeda --------------------------------------------------------


def test_conta_sem_lancamento_pode_trocar_de_moeda(usuario, titular, instituicao):
    """Sem histórico não há o que reinterpretar: a correção é legítima."""
    conta = create_account(usuario, **_campos(titular, instituicao))

    atualizada = update_account(
        usuario, conta, **_campos(titular, instituicao, currency=CURRENCY_USD)
    )

    assert atualizada.currency == CURRENCY_USD


def test_conta_com_lancamento_recusa_troca_de_moeda(usuario, titular, instituicao):
    conta = create_account(usuario, **_campos(titular, instituicao))
    _lancamento(conta)

    with pytest.raises(ValueError, match="não pode mudar de moeda"):
        update_account(usuario, conta, **_campos(titular, instituicao, currency=CURRENCY_USD))

    conta.refresh_from_db()
    assert conta.currency == CURRENCY_BRL


def test_conta_com_lancamento_ainda_aceita_as_demais_edicoes(usuario, titular, instituicao):
    """A trava é da moeda, não da conta: nome e saldo continuam editáveis."""
    conta = create_account(usuario, **_campos(titular, instituicao))
    _lancamento(conta)

    atualizada = update_account(
        usuario,
        conta,
        **_campos(titular, instituicao, account_name="Nome novo", initial_balance="2500,00"),
    )

    assert atualizada.account_name == "Nome novo"
    assert atualizada.initial_balance == Decimal("2500.00")


# --- A tela sabe o que o serviço vai recusar -------------------------------


def test_listagem_marca_conta_com_lancamento(usuario, titular, instituicao):
    """`has_entries` é o que trava o seletor de moeda na tela de Contas."""
    from banking.services import list_accounts_for_user

    sem_lancamento = create_account(usuario, **_campos(titular, instituicao))
    com_lancamento = create_account(
        usuario, **_campos(titular, instituicao, account_name="Conta movimentada")
    )
    _lancamento(com_lancamento)

    marcas = {conta.id: conta.has_entries for conta in list_accounts_for_user(usuario)}

    assert marcas[sem_lancamento.id] is False
    assert marcas[com_lancamento.id] is True
