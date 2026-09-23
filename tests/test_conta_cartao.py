"""O cartão de crédito como conta.

O saldo de um cartão é dívida: fica negativo por natureza. Os dias de
fechamento e de vencimento guiam a fatura projetada, e a conta de pagamento
padrão só diz de onde ela sai -- o pagamento real pode ser dividido entre
contas, com uma transferência de cada. Os testes fixam essas regras no serviço,
no banco e nos contratos que o NetWorth lê.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from banking.services import create_account, update_account
from core import patrimonio
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_REGULAR,
    CURRENCY_BRL,
    CURRENCY_USD,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR

pytestmark = pytest.mark.django_db

TOKEN = "token-de-teste-com-mais-de-trinta-e-dois-caracteres"


@pytest.fixture
def usuario():
    return get_user_model().objects.create_user(
        username="teste-cartao",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )


@pytest.fixture
def titular():
    return AccountOwner.objects.create(name="Mariano")


@pytest.fixture
def banco():
    return FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")


@pytest.fixture
def corrente(usuario, titular, banco):
    return create_account(usuario, **_campos(titular, banco, account_name="Conta corrente"))


def _campos(titular, banco, **extras):
    padrao = {
        "owner_id": str(titular.id),
        "institution_id": str(banco.id),
        "account_name": "Cartão C6",
        "initial_balance": "0",
        "currency": CURRENCY_BRL,
    }
    padrao.update(extras)
    return padrao


def _cartao(**extras):
    padrao = {
        "account_kind": ACCOUNT_KIND_CREDIT_CARD,
        "card_closing_day": "29",
        "card_due_day": "25",
    }
    padrao.update(extras)
    return padrao


# --- Serviço -----------------------------------------------------------------


def test_cartao_e_criado_com_os_dias_e_a_conta_de_pagamento(usuario, titular, banco, corrente):
    cartao = create_account(
        usuario, **_campos(titular, banco), **_cartao(card_payment_account_id=str(corrente.id))
    )

    cartao.refresh_from_db()
    assert cartao.is_credit_card
    assert (cartao.card_closing_day, cartao.card_due_day) == (29, 25)
    assert cartao.card_payment_account == corrente


def test_conta_de_pagamento_e_opcional(usuario, titular, banco):
    cartao = create_account(usuario, **_campos(titular, banco), **_cartao())

    assert cartao.card_payment_account is None


@pytest.mark.parametrize(
    ("dias", "trecho"),
    [
        ({"card_closing_day": ""}, "fechamento"),
        ({"card_due_day": "32"}, "entre 1 e 31"),
        ({"card_closing_day": "0"}, "entre 1 e 31"),
        ({"card_due_day": "dez"}, "vencimento"),
    ],
)
def test_cartao_exige_dias_validos(usuario, titular, banco, dias, trecho):
    with pytest.raises(ValueError, match=trecho):
        create_account(usuario, **_campos(titular, banco), **_cartao(**dias))


def test_conta_comum_descarta_os_campos_de_cartao(usuario, titular, banco, corrente):
    conta = create_account(
        usuario,
        **_campos(titular, banco, account_name="Poupança"),
        account_kind=ACCOUNT_KIND_REGULAR,
        card_closing_day="10",
        card_due_day="20",
        card_payment_account_id=str(corrente.id),
    )

    assert conta.account_kind == ACCOUNT_KIND_REGULAR
    assert (conta.card_closing_day, conta.card_due_day, conta.card_payment_account) == (None, None, None)


def test_tipo_desconhecido_e_recusado(usuario, titular, banco):
    with pytest.raises(ValueError, match="Tipo de conta"):
        create_account(usuario, **_campos(titular, banco), account_kind="poupanca")


def test_conta_de_pagamento_em_outra_moeda_e_recusada(usuario, titular, banco):
    em_dolar = create_account(usuario, **_campos(titular, banco, account_name="Dólar", currency=CURRENCY_USD))

    with pytest.raises(ValueError, match="mesma moeda"):
        create_account(usuario, **_campos(titular, banco), **_cartao(card_payment_account_id=str(em_dolar.id)))


def test_cartao_nao_paga_outro_cartao(usuario, titular, banco):
    primeiro = create_account(usuario, **_campos(titular, banco), **_cartao())

    with pytest.raises(ValueError, match="não outro cartão"):
        create_account(
            usuario,
            **_campos(titular, banco, account_name="Cartão XP"),
            **_cartao(card_payment_account_id=str(primeiro.id)),
        )


def test_cartao_nao_paga_a_propria_fatura(usuario, titular, banco):
    conta = create_account(usuario, **_campos(titular, banco))

    with pytest.raises(ValueError, match="própria fatura"):
        update_account(usuario, conta, **_campos(titular, banco), **_cartao(card_payment_account_id=str(conta.id)))


def test_conta_que_paga_um_cartao_nao_vira_cartao(usuario, titular, banco, corrente):
    create_account(usuario, **_campos(titular, banco), **_cartao(card_payment_account_id=str(corrente.id)))

    with pytest.raises(ValueError, match="conta de pagamento de um cartão"):
        update_account(usuario, corrente, **_campos(titular, banco, account_name="Conta corrente"), **_cartao())


def test_conta_existente_vira_cartao_e_volta(usuario, titular, banco):
    conta = create_account(usuario, **_campos(titular, banco))

    update_account(usuario, conta, **_campos(titular, banco), **_cartao())
    conta.refresh_from_db()
    assert conta.is_credit_card

    update_account(usuario, conta, **_campos(titular, banco))
    conta.refresh_from_db()
    assert not conta.is_credit_card
    assert conta.card_closing_day is None


# --- Banco -------------------------------------------------------------------


def test_banco_recusa_cartao_sem_dias(titular, banco):
    with pytest.raises(IntegrityError), transaction.atomic():
        FinancialAccount.objects.create(
            owner=titular, institution=banco, account_name="Sem dias", account_kind=ACCOUNT_KIND_CREDIT_CARD
        )


def test_banco_recusa_conta_comum_com_dados_de_cartao(titular, banco):
    with pytest.raises(IntegrityError), transaction.atomic():
        FinancialAccount.objects.create(
            owner=titular, institution=banco, account_name="Mista", card_closing_day=5, card_due_day=10
        )


# --- Contratos ---------------------------------------------------------------


@pytest.fixture
def com_token(monkeypatch, tmp_path):
    arquivo = tmp_path / "patrimonio_token"
    arquivo.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo))
    return TOKEN


@pytest.fixture
def contas(usuario, titular, banco, corrente):
    cartao = create_account(
        usuario,
        **_campos(titular, banco, initial_balance="-500,00"),
        initial_balance_date=date(2025, 12, 31).isoformat(),
        **_cartao(),
    )
    return {"corrente": corrente, "cartao": cartao}


def _tipos(itens):
    return {item["nome"]: item["tipo"] for item in itens}


@pytest.mark.django_db(transaction=True)
def test_resumo_v1_publica_o_tipo_da_conta(contas, com_token):
    resposta = Client().get(
        "/patrimonio/v1/resumo", {"data": timezone.localdate().isoformat()}, HTTP_AUTHORIZATION=f"Bearer {TOKEN}"
    )

    assert resposta.status_code == 200
    assert _tipos(resposta.json()["contas"]) == {"Conta corrente": "conta", "Cartão C6": "cartao_credito"}


@pytest.mark.django_db(transaction=True)
def test_metadata_v3_publica_o_tipo_da_conta(contas, com_token):
    resposta = Client().get("/patrimonio/v3/metadata", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    assert _tipos(resposta.json()["contas"])["Cartão C6"] == "cartao_credito"


def test_projecao_publica_o_tipo_e_o_saldo_negativo_do_cartao(contas):
    corpo = patrimonio.montar_projecao(timezone.localdate(), timezone.localdate())

    cartao = next(item for item in corpo["contas"] if item["nome"] == "Cartão C6")
    assert cartao["tipo"] == "cartao_credito"
    assert Decimal(cartao["saldo_inicial"]) == Decimal("-500.00")


# --- Tela --------------------------------------------------------------------


def test_tela_de_contas_mostra_o_cartao_e_os_campos(usuario, contas):
    client = Client()
    client.force_login(usuario)

    resposta = client.get("/tables/accounts/")

    conteudo = resposta.content.decode()
    assert resposta.status_code == 200
    assert "Cartão de crédito · fecha dia 29 · vence dia 25" in conteudo
    assert 'name="card_closing_day"' in conteudo
    assert 'name="card_payment_account_id"' in conteudo
