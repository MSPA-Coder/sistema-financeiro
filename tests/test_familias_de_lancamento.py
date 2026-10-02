"""Reclassificação das famílias determinísticas (IRRF, VA/VR, plano de saúde...).

Fixa: o texto casa por palavra inteira; só muda lançamento que está na categoria
de origem da família (a escolha do usuário em outra categoria nunca é tocada);
a categoria e o grupo de destino são criados só na aplicação; mês fechado exige
autorização; a simulação não grava nada.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements import familias
from bank_statements.familias import FAMILIAS, Familia, casa
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, ENTRY_TYPE_INCOME, STATUS_REALIZED
from transactions import services
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowCategoryGroup

PLANO = next(f for f in FAMILIAS if f.nome == "Plano de saúde")
FARMACIA = next(f for f in FAMILIAS if f.nome == "Farmácia")
SALARIO = next(f for f in FAMILIAS if f.nome == "Salário")
IRRF = next(f for f in FAMILIAS if f.nome.startswith("IRRF"))
VA_SAIDA = next(f for f in FAMILIAS if f.nome == "VA/VR: saída no mercado")


# --- O casamento do texto (sem banco) ---------------------------------------------


@pytest.mark.parametrize(("familia", "descricao", "esperado"), [
    (PLANO, "Prevent Mariano", True),
    (PLANO, "PREVENT ESTHER", True),
    (PLANO, "Prevention Plus", False),
    (FARMACIA, "Raia · Claudia", True),
    (FARMACIA, "Drogaria Sao Paulo · Claudia", True),
    (FARMACIA, "Praia Grande Turismo", False),
    (SALARIO, "Salário", True),
    (SALARIO, "Salário - adiantamento", False),
    (IRRF, "Irrf S/Rendimentos Empréstimos de Ações Brraizacnpr6", True),
    (IRRF, "Irrf S/ Day Trade Bmf 11/09/2026", True),
    (IRRF, "IR Cláudia", False),
    (VA_SAIDA, "Vale alimentação + vale refeição", True),
    (VA_SAIDA, "Vale alimentação", False),
])
def test_casamento_por_palavra_inteira(familia, descricao, esperado):
    assert casa(familia, descricao) is esperado


# --- Com banco ---------------------------------------------------------------------


@pytest.fixture
def mundo(db):
    user = AppUser.objects.create_user(username="operador-familias", password="senha-segura")
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    conta = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Cartão base")
    categorias = {
        nome: CashFlowCategory.objects.create(category_name=nome)
        for nome in ("Saúde", "Outros", "Lazer", "Impostos e Tributos", "Supermercado", "DBR")
    }
    return user, conta, categorias


def _lancar(user, conta, categoria, descricao, valor, dia=date(2026, 9, 10), tipo=ENTRY_TYPE_EXPENSE):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=tipo, description=descricao,
            entry_amount=Decimal(valor), installments=1, due_date=dia, status=STATUS_REALIZED,
            realized_date=dia, realized_amount=Decimal(valor),
        ),
        user=user,
    )[0]


@pytest.mark.django_db
def test_simulacao_mostra_o_efeito_e_nao_grava(mundo):
    user, conta, cat = mundo
    _lancar(user, conta, cat["Saúde"], "Prevent Mariano", "1000.00")
    _lancar(user, conta, cat["Saúde"], "Prevent Esther", "800.00")
    _lancar(user, conta, cat["Saúde"], "Raia · Claudia", "50.00")

    efeitos = {e.familia.nome: e for e in familias.simular(user) if e.entradas}
    assert set(efeitos) == {"Plano de saúde", "Farmácia"}
    assert len(efeitos["Plano de saúde"].entradas) == 2 and efeitos["Plano de saúde"].total == Decimal("1800.00")
    assert efeitos["Plano de saúde"].categoria_nova and efeitos["Plano de saúde"].grupo_novo
    assert not CashFlowCategory.objects.filter(category_name="Plano de saúde").exists()
    assert not CashFlowCategoryGroup.objects.filter(group_name="Saúde").exists()

    saida = StringIO()
    call_command("reclassificar_familias", "--usuario", user.username, stdout=saida)
    assert "Plano de saúde: 2 lançamento(s)" in saida.getvalue() and "Simulação" in saida.getvalue()
    assert not CashFlowCategory.objects.filter(category_name="Plano de saúde").exists()


@pytest.mark.django_db
def test_aplicar_cria_a_categoria_no_grupo_e_so_toca_a_categoria_de_origem(mundo):
    user, conta, cat = mundo
    plano = _lancar(user, conta, cat["Saúde"], "Prevent Mariano", "1000.00")
    # O usuário pôs este em Lazer de propósito: a família não o toca.
    escolhido = _lancar(user, conta, cat["Lazer"], "Prevent Esther", "800.00")
    # E o que já está na categoria de destino também fica como está.
    destino = CashFlowCategory.objects.create(category_name="Plano de saúde")
    ja_no_destino = _lancar(user, conta, destino, "Prevent Mariano", "900.00")

    resultado = familias.aplicar(user)
    assert resultado == {"Plano de saúde": 1}
    plano.refresh_from_db()
    escolhido.refresh_from_db()
    ja_no_destino.refresh_from_db()
    assert plano.category.category_name == "Plano de saúde"
    assert plano.category.group.group_name == "Saúde"
    assert escolhido.category == cat["Lazer"]
    assert ja_no_destino.category == destino
    # Rodar de novo não faz nada.
    assert familias.aplicar(user) == {}


@pytest.mark.django_db
def test_tipo_do_lancamento_tem_de_ser_o_da_familia(mundo):
    user, conta, cat = mundo
    _lancar(user, conta, cat["DBR"], "Salário", "9000.00", tipo=ENTRY_TYPE_INCOME)
    _lancar(user, conta, cat["DBR"], "Salário", "100.00", tipo=ENTRY_TYPE_EXPENSE)
    resultado = familias.aplicar(user)
    assert resultado == {"Salário": 1}
    assert CashFlowCategory.objects.get(category_name="Salário").transactions.get().entry_type == ENTRY_TYPE_INCOME


@pytest.mark.django_db
def test_mes_fechado_exige_autorizacao_e_o_saldo_de_fechamento_nao_muda(mundo):
    user, conta, cat = mundo
    entrada = _lancar(user, conta, cat["Saúde"], "Prevent Mariano", "1000.00", dia=date(2026, 7, 10))
    services.close_month(conta, 2026, 7, None, user)
    fechamento = AccountMonthClose.objects.get(account=conta, year=2026, month=7, active=True).closing_balance

    efeito = next(e for e in familias.simular(user) if e.familia.nome == "Plano de saúde")
    assert efeito.meses_fechados == 1

    with pytest.raises(ValueError, match="meses fechados"):
        familias.aplicar(user)
    entrada.refresh_from_db()
    assert entrada.category == cat["Saúde"]

    assert familias.aplicar(user, autorizar_meses=True) == {"Plano de saúde": 1}
    entrada.refresh_from_db()
    assert entrada.category.category_name == "Plano de saúde"
    assert AccountMonthClose.objects.get(
        account=conta, year=2026, month=7, active=True
    ).closing_balance == fechamento


def test_familia_e_tupla_de_dados_imutaveis():
    assert all(isinstance(f, Familia) for f in FAMILIAS)
    assert len({f.nome for f in FAMILIAS}) == len(FAMILIAS)
