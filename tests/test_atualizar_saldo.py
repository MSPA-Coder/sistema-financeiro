"""Atualizar saldo: a diferença entre o saldo real e o do CB vira um lançamento
realizado com destino explícito.

O que fica fixado: a prévia compara o saldo realizado até a data (inclusive); o
destino tem de servir ao sentido da diferença; "perda" e "ajuste assumido"
exigem motivo e aparecem na lista de assunções; se o saldo do CB mudou entre a
prévia e o lançamento, nada é gravado.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements import saldo
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, ENTRY_TYPE_INCOME, STATUS_REALIZED
from transactions import services
from transactions.models import CashFlowCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(username="operador-saldo-real", password="senha-segura")
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    conta = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="CDB", account_kind="aplicacao",
        initial_balance=Decimal("100.00"),
    )
    categorias = {
        nome: CashFlowCategory.objects.create(category_name=nome)
        for nome in ("Rendimentos", "Impostos e Tributos", "Ajustes de Saldo", "Outros")
    }
    services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categorias["Outros"].id, entry_type=ENTRY_TYPE_INCOME,
            description="Aporte", entry_amount=Decimal("50.00"), installments=1,
            due_date=date(2026, 9, 10), status=STATUS_REALIZED,
            realized_date=date(2026, 9, 10), realized_amount=Decimal("50.00"),
        ),
        user=user,
    )
    return user, conta, categorias


def _previa(user, conta, informado, dia=date(2026, 9, 30)):
    return saldo.previa(user, account_id=conta.id, data=dia, saldo_informado=informado)


def test_previa_compara_o_saldo_realizado_ate_a_data_inclusive(cenario):
    user, conta, _ = cenario
    previa = _previa(user, conta, "160,00")
    assert previa.saldo_no_cb == Decimal("150.00")
    assert previa.diferenca == Decimal("10.00")
    assert previa.tipo == ENTRY_TYPE_INCOME
    assert [chave for chave, _ in previa.destinos] == [saldo.DESTINO_RENDIMENTOS, saldo.DESTINO_AJUSTE]
    # No próprio dia do aporte o saldo já o inclui; um dia antes, não.
    assert _previa(user, conta, "150", dia=date(2026, 9, 10)).saldo_no_cb == Decimal("150.00")
    assert _previa(user, conta, "100", dia=date(2026, 9, 9)).saldo_no_cb == Decimal("100.00")


def test_aceita_virgula_brasileira_e_ponto_decimal(cenario):
    user, conta, _ = cenario
    assert _previa(user, conta, "1.160,50").saldo_informado == Decimal("1160.50")
    assert _previa(user, conta, "160.50").saldo_informado == Decimal("160.50")
    with pytest.raises(ValueError, match="inválido"):
        _previa(user, conta, "abc")


def test_rendimento_vira_receita_realizada_e_o_saldo_passa_a_bater(cenario):
    user, conta, cat = cenario
    entrada = saldo.aplicar(
        user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="160,00",
        diferenca_esperada="10,00", destino=saldo.DESTINO_RENDIMENTOS,
    )
    assert entrada.category == cat["Rendimentos"]
    assert entrada.entry_type == ENTRY_TYPE_INCOME and entrada.status == STATUS_REALIZED
    assert entrada.realized_date == date(2026, 9, 30) and entrada.entry_amount == Decimal("10.00")
    assert _previa(user, conta, "160,00").diferenca == Decimal("0.00")
    # Aparece na lista das atualizações de saldo da tela, mas não é assunção.
    assert [item.id for item in saldo.atualizacoes(user)] == [entrada.id]
    assert saldo.assuncoes(user) == []


def test_saida_vai_para_ir_iof_e_o_destino_de_entrada_nao_serve(cenario):
    user, conta, cat = cenario
    previa = _previa(user, conta, "140,00")
    assert previa.tipo == ENTRY_TYPE_EXPENSE and previa.diferenca == Decimal("-10.00")
    with pytest.raises(ValueError, match="não serve"):
        saldo.aplicar(
            user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="140",
            diferenca_esperada="-10", destino=saldo.DESTINO_RENDIMENTOS,
        )
    entrada = saldo.aplicar(
        user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="140",
        diferenca_esperada="-10", destino=saldo.DESTINO_IR_IOF,
    )
    assert entrada.category == cat["Impostos e Tributos"] and entrada.entry_type == ENTRY_TYPE_EXPENSE


def test_ajuste_assumido_exige_motivo_e_entra_na_lista_de_assuncoes(cenario):
    user, conta, cat = cenario
    with pytest.raises(ValueError, match="exige um motivo"):
        saldo.aplicar(
            user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="155",
            diferenca_esperada="5", destino=saldo.DESTINO_AJUSTE,
        )
    entrada = saldo.aplicar(
        user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="155",
        diferenca_esperada="5", destino=saldo.DESTINO_AJUSTE, motivo="extrato do CDB ainda não importado",
    )
    assert entrada.category == cat["Ajustes de Saldo"]
    assert "extrato do CDB ainda não importado" in entrada.description
    assert [item.id for item in saldo.assuncoes(user)] == [entrada.id]


def test_saldo_que_mudou_desde_a_previa_nao_grava(cenario):
    user, conta, _ = cenario
    with pytest.raises(ValueError, match="mudou desde a prévia"):
        saldo.aplicar(
            user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="160",
            diferenca_esperada="7", destino=saldo.DESTINO_RENDIMENTOS,
        )
    assert saldo.assuncoes(user) == []


def test_saldo_que_ja_bate_nao_lanca_nada(cenario):
    user, conta, _ = cenario
    with pytest.raises(ValueError, match="já bate"):
        saldo.aplicar(
            user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="150",
            diferenca_esperada="0", destino=saldo.DESTINO_RENDIMENTOS,
        )


def test_mes_fechado_recusa_o_lancamento(cenario):
    user, conta, _ = cenario
    services.close_month(conta, 2026, 9, None, user)
    with pytest.raises(ValueError, match="fechado"):
        saldo.aplicar(
            user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="160",
            diferenca_esperada="10", destino=saldo.DESTINO_RENDIMENTOS,
        )


def test_categoria_ausente_pede_cadastro(cenario):
    user, conta, cat = cenario
    cat["Rendimentos"].delete()
    with pytest.raises(ValueError, match="Cadastre a categoria"):
        saldo.aplicar(
            user, account_id=conta.id, data=date(2026, 9, 30), saldo_informado="160",
            diferenca_esperada="10", destino=saldo.DESTINO_RENDIMENTOS,
        )


def test_cartao_de_credito_nao_tem_saldo_para_atualizar(cenario):
    user, conta, _ = cenario
    cartao = FinancialAccount.objects.create(
        owner=conta.owner, institution=conta.institution, account_name="Carbon",
        account_kind="cartao_credito", card_closing_day=10, card_due_day=17,
    )
    with pytest.raises(ValueError, match="fatura"):
        _previa(user, cartao, "0")
