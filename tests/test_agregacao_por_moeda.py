"""Somar contas de moedas diferentes é erro, não arredondamento.

Este é o teste que mais importa da mudança de moeda. O defeito que ele impede
não aparece na tela: um total que soma real com dólar sai formatado, alinhado e
com duas casas decimais, e ninguém desconfia dele até conferir à mão. Por isso
a regra é levantar `MixedCurrencyError`, e por isso existe um teste que provoca
a soma de propósito.

Converter não é alternativa: este sistema não guarda taxa de câmbio, e inventar
uma na hora de somar produziria um número que ninguém consegue auditar depois.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from banking.services import (
    account_ids_by_currency,
    currencies_of_accounts,
    currency_blocks,
    currency_of_accounts,
)
from core.domain.finance import (
    BASE_CURRENCY,
    CURRENCY_BRL,
    CURRENCY_USD,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_REALIZED,
    MixedCurrencyError,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from reports import services as reports_services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def usuario():
    return get_user_model().objects.create_user(
        username="teste-agregacao",
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


def _conta(titular, instituicao, nome, moeda, saldo="1000.00"):
    return FinancialAccount.objects.create(
        owner=titular,
        institution=instituicao,
        account_name=nome,
        currency=moeda,
        initial_balance=Decimal(saldo),
        initial_balance_date=date(2025, 12, 31),
    )


def _lancamento_em(conta, valor, vencimento):
    """Despesa em aberto no mês corrente.

    Aberta, e não realizada, de propósito: na visão "a vencer" -- a padrão da
    tela -- o que já foi realizado entra no saldo inicial do período, e aí o
    saldo não se moveria dentro da listagem, que é justamente o que este teste
    mede.
    """
    categoria, _ = CashFlowCategory.objects.get_or_create(category_name="Categoria de teste")
    return CashFlowEntry.objects.create(
        account=conta,
        category=categoria,
        entry_type=ENTRY_TYPE_EXPENSE,
        description="Lançamento de teste",
        entry_amount=Decimal(valor),
        due_date=vencimento,
        status=STATUS_PROJECTED,
        operation_type=OPERATION_SINGLE,
    )


def _lancamento(conta, valor="100.00"):
    categoria, _ = CashFlowCategory.objects.get_or_create(category_name="Categoria de teste")
    return CashFlowEntry.objects.create(
        account=conta,
        category=categoria,
        entry_type=ENTRY_TYPE_EXPENSE,
        description="Lançamento de teste",
        entry_amount=Decimal(valor),
        due_date=date(2026, 6, 10),
        realized_date=date(2026, 6, 10),
        status=STATUS_REALIZED,
        operation_type=OPERATION_SINGLE,
    )


# --- A porta única --------------------------------------------------------


def test_conjunto_vazio_vale_a_moeda_base(usuario):
    assert currency_of_accounts([]) == BASE_CURRENCY


def test_contas_da_mesma_moeda_devolvem_a_moeda(titular, instituicao):
    contas = [
        _conta(titular, instituicao, "Conta 1", CURRENCY_BRL),
        _conta(titular, instituicao, "Conta 2", CURRENCY_BRL),
    ]

    assert currency_of_accounts([c.id for c in contas]) == CURRENCY_BRL


def test_moedas_diferentes_levantam_erro(titular, instituicao):
    """O teste que provoca a soma de propósito."""
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)

    with pytest.raises(MixedCurrencyError) as erro:
        currency_of_accounts([real.id, dolar.id])

    assert erro.value.currencies == (CURRENCY_BRL, CURRENCY_USD)
    assert "não converte moeda" in str(erro.value)


def test_erro_diz_quais_moedas_estao_envolvidas(titular, instituicao):
    """A mensagem tem que bastar para a pessoa saber o que filtrar."""
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)

    with pytest.raises(MixedCurrencyError, match="BRL, USD"):
        currency_of_accounts([real.id, dolar.id])

    assert currencies_of_accounts([real.id, dolar.id]) == (CURRENCY_BRL, CURRENCY_USD)


# --- A porta plural: um bloco por moeda -----------------------------------


def test_reparticao_devolve_um_grupo_por_moeda(titular, instituicao):
    """Cada grupo sai com uma moeda só -- é o que os agregados sempre exigiram."""
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    outra_real = _conta(titular, instituicao, "Outra em real", CURRENCY_BRL)
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)

    grupos = account_ids_by_currency([real.id, dolar.id, outra_real.id])

    assert grupos == {CURRENCY_BRL: [real.id, outra_real.id], CURRENCY_USD: [dolar.id]}
    for moeda, ids in grupos.items():
        assert currency_of_accounts(ids) == moeda


def test_moeda_base_vem_primeiro(titular, instituicao):
    """A ordem dos blocos na tela não pode depender da ordem do banco."""
    dolar = _conta(titular, instituicao, "AAA em dólar", CURRENCY_USD)
    real = _conta(titular, instituicao, "ZZZ em real", CURRENCY_BRL)

    assert list(account_ids_by_currency([dolar.id, real.id])) == [CURRENCY_BRL, CURRENCY_USD]


def test_reparticao_de_selecao_vazia_nao_tem_bloco(usuario):
    assert account_ids_by_currency([]) == {}


# --- Os agregados que atravessam contas -----------------------------------


def test_saldo_base_recusa_moedas_misturadas(titular, instituicao):
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL, "1000.00")
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD, "200.00")

    with pytest.raises(MixedCurrencyError):
        reports_services.decimal_base_balance([real.id, dolar.id])

    # E continua funcionando quando a seleção é de uma moeda só.
    assert reports_services.decimal_base_balance([real.id]) == Decimal("1000.00")


def test_saldo_do_periodo_recusa_moedas_misturadas(titular, instituicao):
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)
    _lancamento(real)
    _lancamento(dolar)

    with pytest.raises(MixedCurrencyError):
        reports_services.decimal_balance_before(
            [real.id, dolar.id], date(2026, 12, 31), VIEW_REALIZED
        )


def test_relatorio_por_conta_nao_mistura_porque_nao_soma(titular, instituicao):
    """Linha por conta é sempre de uma moeda só: o relatório detalhado continua
    de pé mesmo com contas de moedas diferentes. O que não existe é o TOTAL
    delas -- e é a view que o pede, por `ContextOptions.currency`."""
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL, "1000.00")
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD, "200.00")

    linhas = reports_services.account_cash_report_rows(
        [real.id, dolar.id], date(2026, 6, 1), date(2026, 6, 1), VIEW_REALIZED
    )

    por_moeda = {linha.currency: linha.start_balance for linha in linhas}
    assert por_moeda == {CURRENCY_BRL: Decimal("1000.00"), CURRENCY_USD: Decimal("200.00")}


def test_selecao_de_uma_moeda_rende_um_bloco_so(usuario, titular, instituicao):
    """Com uma moeda só, a tela tem um bloco -- e sai idêntica ao que sempre foi."""
    _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    ctx = reports_services.FinancialContext(owner_id=None, institution_id=None, account_id=None)

    options = reports_services.context_options(usuario, ctx)
    blocos = currency_blocks(options.account_ids)

    assert [moeda for moeda, _ids in blocos] == [CURRENCY_BRL]


def test_selecao_com_duas_moedas_rende_dois_blocos(usuario, titular, instituicao):
    """A seleção que antes era recusada agora é respondida -- sem somar as duas."""
    _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)
    ctx = reports_services.FinancialContext(owner_id=None, institution_id=None, account_id=None)

    options = reports_services.context_options(usuario, ctx)
    blocos = currency_blocks(options.account_ids)

    assert [moeda for moeda, _ids in blocos] == [CURRENCY_BRL, CURRENCY_USD]
    # E cada bloco continua sendo o que os agregados exigem: uma moeda só.
    for moeda, ids in blocos:
        assert reports_services.decimal_base_balance(ids) > 0
        assert currency_of_accounts(ids) == moeda


def test_filtrar_uma_conta_continua_resolvendo(usuario, titular, instituicao):
    """Escolher a conta em dólar traz a moeda junto, sem segundo clique."""
    _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)
    ctx = reports_services.FinancialContext(owner_id=None, institution_id=None, account_id=dolar.id)

    options = reports_services.context_options(usuario, ctx)

    assert [moeda for moeda, _ids in currency_blocks(options.account_ids)] == [CURRENCY_USD]


# --- A tela: um bloco por moeda, e nenhum total entre elas -----------------


def test_extrato_monta_um_bloco_por_moeda(usuario, titular, instituicao):
    """O saldo corrente corre dentro de um bloco e nunca atravessa para o outro.

    Este é o teste da etapa que substituiu a recusa: antes, esta mesma seleção
    respondia 400. Agora ela responde -- sem que uma linha em dólar entre no
    saldo em real.
    """
    from transactions.services import build_transactions_view_context

    hoje = date.today()
    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL, saldo="1000.00")
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD, saldo="500.00")
    _lancamento_em(real, "100.00", hoje)
    _lancamento_em(dolar, "50.00", hoje)

    contexto = build_transactions_view_context(usuario, {}, {})
    blocos = {bloco["currency"]: bloco for bloco in contexto["blocos"]}

    assert list(blocos) == [CURRENCY_BRL, CURRENCY_USD]
    assert blocos[CURRENCY_BRL]["saldo_inicial"] == Decimal("1000.00")
    assert blocos[CURRENCY_BRL]["saldo_final"] == Decimal("900.00")
    assert blocos[CURRENCY_USD]["saldo_inicial"] == Decimal("500.00")
    assert blocos[CURRENCY_USD]["saldo_final"] == Decimal("450.00")
    # O saldo corrente de cada linha é o da moeda dela: 900 e 450, nunca 1350.
    assert sorted(tx.running_balance for tx in contexto["txs"]) == [
        Decimal("450.00"), Decimal("900.00")
    ]


def test_painel_oferece_o_seletor_de_moeda_antes_do_primeiro_lancamento(
    client, usuario, titular, instituicao
):
    """Uma conta em dólar nasce com saldo e sem nenhum movimento.

    A primeira versão deste seletor lia as moedas dos LANÇAMENTOS, e por isso
    ele não aparecia justamente para quem tinha acabado de abrir a conta em
    dólar: sem movimento, a única moeda "existente" era o real. As opções vêm
    das contas no escopo.
    """
    _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)
    assert CashFlowEntry.objects.count() == 0
    client.force_login(usuario)

    resposta = client.get("/dashboard/")

    assert resposta.status_code == 200
    assert resposta.context["show_currency_filter"] is True
    assert [moeda for moeda, _simbolo in resposta.context["currency_options"]] == [
        CURRENCY_BRL, CURRENCY_USD
    ]


def test_painel_de_quem_so_tem_real_nao_ganha_seletor(client, usuario, titular, instituicao):
    """Controle que nunca muda nada é ruído: só aparece com duas moedas."""
    _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    client.force_login(usuario)

    resposta = client.get("/dashboard/")

    assert resposta.context["show_currency_filter"] is False


def test_uma_moeda_so_continua_rendendo_um_bloco_no_extrato(usuario, titular, instituicao):
    """Com os dados de hoje a tela tem de continuar exatamente como era."""
    from transactions.services import build_transactions_view_context

    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL, saldo="1000.00")
    _lancamento_em(real, "100.00", date.today())

    contexto = build_transactions_view_context(usuario, {}, {})

    assert len(contexto["blocos"]) == 1
    assert contexto["blocos"][0]["currency"] == CURRENCY_BRL
    assert contexto["blocos"][0]["saldo_final"] == Decimal("900.00")


# --- Orçamento: moeda base, e só ------------------------------------------


def test_orcamento_compara_so_o_realizado_em_moeda_base(titular, instituicao):
    """`MonthlyBudget.planned_amount` é em `BRL` porque orçamento não tem conta.

    Então o realizado que ele compara também tem que ser: um gasto em dólar
    entrando nessa soma faria a diferença orçada virar um número sem
    significado -- e, pior, plausível.
    """
    from management.services import actual_amount_for_budget

    real = _conta(titular, instituicao, "Conta em real", CURRENCY_BRL)
    dolar = _conta(titular, instituicao, "Conta em dólar", CURRENCY_USD)
    _lancamento(real, "100.00")
    _lancamento(dolar, "900.00")
    categoria = CashFlowCategory.objects.get(category_name="Categoria de teste")

    total = actual_amount_for_budget(titular.id, categoria.id, 2026, 6)

    assert total == Decimal("100.00")
