"""A projeção de caixa que o consolidador soma aos investimentos.

Como o resumo, isto é um contrato lido por outro deployable: número errado
aqui vira patrimônio projetado errado do outro lado, sem erro nenhum. Os testes
fixam as três decisões que mudam o número -- ponto de partida realizado,
vencidos à parte e aporte que não é despesa -- e a forma do envelope.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core import patrimonio
from core.domain.finance import (
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_RECURRING,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

TOKEN = "token-de-teste-com-mais-de-trinta-e-dois-caracteres"
ROTA = "/patrimonio/v3/projection"
HOJE = date(2026, 3, 15)
FIM = date(2026, 6, 30)


@pytest.fixture
def com_token(monkeypatch, tmp_path):
    arquivo = tmp_path / "patrimonio_token"
    arquivo.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo))
    return TOKEN


@pytest.fixture
def cenario():
    """Conta corrente com 950 realizados em HOJE, uma reserva e uma conta em dólar.

    Depois de HOJE: despesa de 400 (20/03), salário recorrente de 900 (05/04),
    aporte de 300 na bolsa (10/04) e transferência de 200 para a reserva
    (12/04). Antes de HOJE: uma conta de 100 vencida e não paga. Depois de FIM:
    uma despesa que não pode aparecer.
    """
    titular = AccountOwner.objects.create(name="Mariano")
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(institution_name="Avenue", institution_type="Corretora")

    def conta(nome, instituicao, saldo, moeda):
        return FinancialAccount.objects.create(
            owner=titular, institution=instituicao, account_name=nome,
            initial_balance=Decimal(saldo), currency=moeda,
            initial_balance_date=date(2025, 12, 31),
        )

    corrente = conta("Conta corrente", banco, "100.00", "BRL")
    reserva = conta("Reserva", banco, "0.00", "BRL")
    dolar = conta("Conta em dólar", corretora, "1250.00", "USD")

    gerencial = CashFlowCategory.objects.create(category_name="Salário")
    bolsa = CashFlowCategory.objects.create(category_name="Operações em Bolsa", kind=CATEGORY_KIND_MOVEMENT)
    transferencia = CashFlowCategory.objects.create(
        category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER
    )

    def lancar(conta_, categoria, tipo, valor, vencimento, status=STATUS_PROJECTED, **extra):
        realizado = status == STATUS_REALIZED
        return CashFlowEntry.objects.create(
            account=conta_, category=categoria, entry_type=tipo,
            description=f"{categoria.category_name} {vencimento}", entry_amount=Decimal(valor),
            due_date=vencimento, status=status,
            realized_date=vencimento if realizado else None,
            realized_amount=Decimal(valor) if realizado else None,
            **extra,
        )

    lancar(corrente, gerencial, ENTRY_TYPE_INCOME, "900.00", date(2026, 1, 10), STATUS_REALIZED)
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "50.00", date(2026, 2, 10), STATUS_REALIZED)
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "100.00", date(2026, 3, 1), STATUS_PENDING)
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "400.00", date(2026, 3, 20))
    lancar(
        corrente, gerencial, ENTRY_TYPE_INCOME, "900.00", date(2026, 4, 5),
        operation_type=OPERATION_RECURRING,
    )
    lancar(corrente, bolsa, ENTRY_TYPE_EXPENSE, "300.00", date(2026, 4, 10))
    lancar(corrente, transferencia, ENTRY_TYPE_EXPENSE, "200.00", date(2026, 4, 12))
    lancar(reserva, transferencia, ENTRY_TYPE_INCOME, "200.00", date(2026, 4, 12))
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "5000.00", date(2026, 12, 1))
    return {"corrente": corrente, "reserva": reserva, "dolar": dolar}


def _por_moeda(itens, moeda):
    return [item for item in itens if item["moeda"] == moeda]


def _conta(corpo, nome):
    return next(item for item in corpo["contas"] if item["nome"] == nome)


def test_parte_do_saldo_realizado_e_publica_vencidos_a_parte(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    assert corpo["recurso"] == "projecao"
    assert corpo["data_base"] == "2026-03-15"
    assert {"moeda": "BRL", "saldo": "950.00"} in corpo["saldos_iniciais"]
    assert corpo["vencidos"] == [{"moeda": "BRL", "entradas": "0.00", "saidas": "100.00", "quantidade": 1}]
    assert corpo["vencidos_incluidos"] is True


def test_serie_diaria_por_moeda_inclui_vencidos_no_dia_base(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    serie = [(item["data"], item["saldo"]) for item in _por_moeda(corpo["serie"], "BRL")]
    # A transferência de 12/04 troca dinheiro de conta sem mudar o total da
    # moeda, então não gera um ponto novo na série.
    assert serie == [
        ("2026-03-15", "850.00"),
        ("2026-03-20", "450.00"),
        ("2026-04-05", "1350.00"),
        ("2026-04-10", "1050.00"),
    ]
    assert _por_moeda(corpo["serie"], "USD") == [{"moeda": "USD", "data": "2026-03-15", "saldo": "1250.00"}]


def test_excluir_vencidos_muda_so_o_ponto_de_partida(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM, incluir_vencidos=False)

    serie = [item["saldo"] for item in _por_moeda(corpo["serie"], "BRL")]
    assert serie[0] == "950.00"
    assert serie[-1] == "1150.00"
    # O bloco continua publicado: o consumidor precisa poder avisar que existe.
    assert corpo["vencidos"][0]["quantidade"] == 1


def test_aporte_na_bolsa_nao_e_despesa(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    abril = next(item for item in corpo["meses"] if item["mes"] == "2026-04" and item["moeda"] == "BRL")
    assert abril["saidas"] == "0.00"
    assert abril["entradas"] == "900.00"
    assert abril["investimentos_saida"] == "300.00"
    assert abril["transferencias_saida"] == "200.00"
    assert abril["transferencias_entrada"] == "200.00"
    assert abril["saldo_final"] == "1050.00"


def test_meses_cobrem_o_periodo_inteiro_mesmo_sem_lancamento(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    brl = [(item["mes"], item["saldo_final"]) for item in _por_moeda(corpo["meses"], "BRL")]
    assert brl == [("2026-03", "450.00"), ("2026-04", "1050.00"), ("2026-05", "1050.00"), ("2026-06", "1050.00")]
    marco = _por_moeda(corpo["meses"], "BRL")[0]
    # O vencido entra no saldo, não no fluxo do mês: ele não é deste mês.
    assert marco["saidas"] == "400.00"
    assert marco["deep_link"].startswith("/reports/projections/?")


def test_lancamento_depois_do_fim_fica_de_fora(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    assert _conta(corpo, "Conta corrente")["saldo_final"] == "850.00"


def test_menor_saldo_por_conta_e_por_moeda(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    assert _conta(corpo, "Conta corrente")["menor_saldo"] == {"valor": "450.00", "data": "2026-03-20"}
    assert _conta(corpo, "Reserva")["menor_saldo"] == {"valor": "0.00", "data": "2026-03-15"}
    assert _conta(corpo, "Reserva")["saldo_final"] == "200.00"
    assert {"moeda": "BRL", "data": "2026-03-20", "valor": "450.00"} in corpo["menor_saldo"]


def test_horizonte_publica_a_ultima_recorrencia(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    assert corpo["horizonte"]["ultima_recorrencia"] == "2026-04-05"
    assert corpo["horizonte"]["meses_configurados"] >= 1


def test_moedas_nunca_sao_somadas(cenario):
    corpo = patrimonio.montar_projecao(HOJE, FIM)

    assert {item["moeda"] for item in corpo["saldos_iniciais"]} == {"BRL", "USD"}
    assert _conta(corpo, "Conta em dólar")["saldo_final"] == "1250.00"


# --- A rota -----------------------------------------------------------------


def pedir(token: str | None = TOKEN, **parametros):
    cabecalhos = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token is not None else {}
    return Client().get(ROTA, parametros, **cabecalhos)


@pytest.mark.django_db(transaction=True)
def test_rota_exige_bearer_e_so_responde_a_get(cenario, com_token):
    assert pedir(token=None).status_code == 401
    assert pedir(token="outro-token-com-mais-de-trinta-e-dois-caracteres").status_code == 401
    resposta = Client().post(ROTA, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert resposta.status_code == 405


@pytest.mark.django_db(transaction=True)
def test_rota_publica_valores_como_texto_e_nao_guarda_cache(cenario, com_token):
    resposta = pedir()

    assert resposta.status_code == 200
    assert resposta["Cache-Control"] == "no-store"
    corpo = resposta.json()
    assert corpo["data_base"] == timezone.localdate().isoformat()
    assert corpo["fim"] == (timezone.localdate() + timedelta(days=patrimonio.PROJECAO_DIAS_PADRAO)).isoformat()

    def numeros(valor):
        if isinstance(valor, dict):
            return [n for item in valor.values() for n in numeros(item)]
        if isinstance(valor, list):
            return [n for item in valor for n in numeros(item)]
        return [valor] if isinstance(valor, float) else []

    assert numeros(json.loads(resposta.content)) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("parametros", "trecho"),
    [
        ({"fim": "2020-01-01"}, "anterior a hoje"),
        ({"fim": (date.today() + timedelta(days=2000)).isoformat()}, "limite"),
        ({"fim": "amanha"}, "fim"),
        ({"vencidos": "talvez"}, "vencidos"),
    ],
)
def test_rota_recusa_parametros_invalidos(cenario, com_token, parametros, trecho):
    resposta = pedir(**parametros)

    assert resposta.status_code == 400
    assert trecho in resposta.json()["erro"]


@pytest.mark.django_db(transaction=True)
def test_metadata_declara_a_capacidade_de_projecao(cenario, com_token):
    resposta = Client().get("/patrimonio/v3/metadata", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    assert resposta.json()["capacidades"]["projecao"] is True
