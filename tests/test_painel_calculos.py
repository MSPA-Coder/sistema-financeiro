"""Os números do painel são os dos relatórios, com os mesmos filtros.

O painel tinha cálculo próprio, e ele divergia do resto do sistema: somava
transferência como receita e despesa, usava o valor previsto e o vencimento de
lançamento já realizado, chamava de "saldo" uma soma que começava em zero,
somava real com dólar com a moeda "Todas", comparava na tendência dois
trimestres futuros e contava mês vazio como mês que "fechou com sobra". Em
junho/2026, só as transferências internas puseram R$ 130 mil a mais de despesa
no gráfico. Cada teste aqui descreve a regra certa e falhava no cálculo antigo.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    ACCOUNT_KIND_REGULAR,
    CATEGORY_KIND_TRANSFER,
    CURRENCY_BRL,
    CURRENCY_USD,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_ALL,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from reports import services as reports_services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

PERIODO = "2026-06"


@pytest.fixture
def usuario():
    return get_user_model().objects.create_user(
        username="teste-painel",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )


@pytest.fixture
def painel(client, usuario):
    client.force_login(usuario)

    def abrir(**params):
        query = {"period": PERIODO, **params}
        resposta = client.get("/dashboard/", query)
        assert resposta.status_code == 200
        return resposta.context

    return abrir


def _categoria(nome, kind="gerencial"):
    return CashFlowCategory.objects.get_or_create(category_name=nome, defaults={"kind": kind})[0]


def _realizado(conta, tipo, categoria, valor, dia, *, previsto=None, vencimento=None):
    """Lançamento realizado; `previsto` e `vencimento` diferem do real quando dados."""
    return CashFlowEntry.objects.create(
        account=conta, category=categoria, entry_type=tipo, description="Lançamento",
        entry_amount=Decimal(previsto or valor), due_date=vencimento or dia,
        realized_amount=Decimal(valor), realized_date=dia,
        status=STATUS_REALIZED, operation_type=OPERATION_SINGLE,
    )


@pytest.fixture
def cenario():
    """Junho/2026 de uma família com conta, cartão, aplicação e conta em dólar."""
    titular = AccountOwner.objects.create(name="Titular do painel")
    banco = FinancialInstitution.objects.create(institution_name="Banco P", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(institution_name="Corretora P", institution_type="Corretora")

    def conta(nome, instituicao, kind=ACCOUNT_KIND_REGULAR, moeda=CURRENCY_BRL, saldo="0.00"):
        campos = {"card_closing_day": 5, "card_due_day": 12} if kind == ACCOUNT_KIND_CREDIT_CARD else {}
        return FinancialAccount.objects.create(
            owner=titular, institution=instituicao, account_name=nome, account_kind=kind,
            currency=moeda, initial_balance=Decimal(saldo), initial_balance_date=date(2025, 12, 31), **campos,
        )

    corrente = conta("Corrente", banco, saldo="1000.00")
    cartao = conta("Cartão", banco, ACCOUNT_KIND_CREDIT_CARD)
    cdb = conta("CDB", banco, ACCOUNT_KIND_INVESTMENT)
    dolar = conta("Conta em dólar", corretora, moeda=CURRENCY_USD, saldo="500.00")

    salario = _categoria("Salário")
    mercado = _categoria("Mercado")
    transferencia = _categoria("Transferência", CATEGORY_KIND_TRANSFER)

    _realizado(corrente, ENTRY_TYPE_INCOME, salario, "3000.00", date(2026, 6, 5))
    # Previsto 200, pago 250: vale o pago.
    _realizado(corrente, ENTRY_TYPE_EXPENSE, mercado, "250.00", date(2026, 6, 10), previsto="200.00")
    # Venceu em maio, foi pago em junho: é de junho.
    _realizado(corrente, ENTRY_TYPE_EXPENSE, mercado, "100.00", date(2026, 6, 2), vencimento=date(2026, 5, 28))
    # Aplicação: muda o saldo das duas contas, não é despesa nem receita.
    _realizado(corrente, ENTRY_TYPE_EXPENSE, transferencia, "500.00", date(2026, 6, 15))
    _realizado(cdb, ENTRY_TYPE_INCOME, transferencia, "500.00", date(2026, 6, 15))
    _realizado(cartao, ENTRY_TYPE_EXPENSE, mercado, "300.00", date(2026, 6, 20))
    _realizado(dolar, ENTRY_TYPE_EXPENSE, mercado, "40.00", date(2026, 6, 12))
    return {"corrente": corrente, "cartao": cartao, "cdb": cdb, "dolar": dolar}


def _mes(contexto, chave, periodo=PERIODO):
    dados = contexto["chart_data"]
    return dados[chave][dados["chartPeriods"].index(periodo)]


def test_transferencia_nao_e_receita_nem_despesa(cenario, painel):
    contexto = painel()

    assert _mes(contexto, "chartIncome") == 3000.0
    assert _mes(contexto, "chartExpense") == 650.0  # 250 + 100 + 300
    assert contexto["chart_data"]["catLabels"] == ["Mercado"]
    assert contexto["chart_data"]["catValues"] == [650.0]


def test_realizado_vale_pela_data_e_pelo_valor_da_realizacao(cenario, painel):
    contexto = painel()

    assert _mes(contexto, "chartExpense", "2026-05") == 0.0
    assert _mes(contexto, "chartExpense") == 650.0


def test_saldo_e_o_saldo_real_das_contas(cenario, painel, usuario):
    """1000 + 3000 - 250 - 100 - 500 + 500 - 300, e não uma soma a partir de zero."""
    contexto = painel()

    assert _mes(contexto, "projSaldo") == 3350.0
    assert contexto["chart_data"]["dailyBal"][-1] == 3350.0
    # O mesmo número da tela Projeções.
    ids = [cenario["corrente"].id, cenario["cartao"].id, cenario["cdb"].id]
    meses = reports_services.projection_months_between(ids, date(2026, 6, 1), date(2026, 6, 1), VIEW_ALL)
    assert meses[0]["saldo"] == Decimal("3350.00")


def test_filtro_de_grupos_vale_para_todos_os_graficos(cenario, painel):
    so_bancos = painel(grupos="bancos")
    assert _mes(so_bancos, "chartExpense") == 350.0
    # A aplicação saiu da seleção, então o dinheiro que foi para ela saiu do saldo.
    assert _mes(so_bancos, "projSaldo") == 3150.0

    so_cartoes = painel(grupos="cartoes")
    assert _mes(so_cartoes, "chartIncome") == 0.0
    assert _mes(so_cartoes, "chartExpense") == 300.0
    assert _mes(so_cartoes, "projSaldo") == -300.0
    assert so_cartoes["chart_data"]["accountGroups"] == "cartoes"


def test_moeda_todas_mostra_uma_moeda_e_avisa(cenario, painel):
    contexto = painel(currency="ALL")

    assert contexto["currency"] == CURRENCY_BRL
    assert contexto["currency_notice"] is True
    assert _mes(contexto, "chartExpense") == 650.0  # sem os 40 dólares


def test_conta_em_dolar_escolhida_mostra_dolar_com_o_filtro_em_real(cenario, painel):
    contexto = painel(account_id=str(cenario["dolar"].id))

    assert contexto["currency"] == CURRENCY_USD
    assert contexto["currency_notice"] is False
    assert _mes(contexto, "chartExpense") == 40.0


def test_tendencia_compara_os_tres_meses_ate_o_escolhido_com_os_tres_anteriores(usuario, painel):
    titular = AccountOwner.objects.create(name="Titular da tendência")
    banco = FinancialInstitution.objects.create(institution_name="Banco T", institution_type="Banco")
    conta = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Corrente",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2025, 12, 31),
    )
    salario = _categoria("Salário")
    _realizado(conta, ENTRY_TYPE_INCOME, salario, "100.00", date(2026, 3, 10))
    _realizado(conta, ENTRY_TYPE_INCOME, salario, "300.00", date(2026, 6, 10))

    saude = painel()["financial_health"]

    # jan-mar geraram 100; abr-jun, 300.
    assert saude["trend_percent"] == "+200.0%"
    # Só os dois meses com movimento contam, e os dois fecharam com sobra.
    assert (saude["positive_months"], saude["total_months"]) == (2, 2)


def test_lancamentos_em_real_nao_listam_nem_somam_a_conta_em_dolar(usuario):
    """Com o filtro em real (o padrão), a conta em dólar aparecia na lista e,
    sem bloco próprio, tinha a despesa somada no bloco do real."""
    from transactions.services import build_transactions_view_context

    titular = AccountOwner.objects.create(name="Titular do extrato")
    corretora = FinancialInstitution.objects.create(institution_name="Corretora E", institution_type="Corretora")
    real, dolar = (
        FinancialAccount.objects.create(
            owner=titular, institution=corretora, account_name=nome, currency=moeda,
            initial_balance=Decimal("1000.00"), initial_balance_date=date(2025, 12, 31),
        )
        for nome, moeda in (("Real", CURRENCY_BRL), ("Dólar", CURRENCY_USD))
    )
    for conta, valor in ((real, "100.00"), (dolar, "50.00")):
        CashFlowEntry.objects.create(
            account=conta, category=_categoria("Mercado"), entry_type=ENTRY_TYPE_EXPENSE,
            description="Despesa", entry_amount=Decimal(valor), due_date=date.today(),
            status=STATUS_PROJECTED, operation_type=OPERATION_SINGLE,
        )

    contexto = build_transactions_view_context(usuario, {}, {})

    assert {tx.account_id for tx in contexto["txs"]} == {real.id}
    assert [bloco["currency"] for bloco in contexto["blocos"]] == [CURRENCY_BRL]
    assert contexto["blocos"][0]["total_despesas"] == Decimal("100.00")
