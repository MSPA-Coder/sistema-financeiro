"""Grupos de categoria: o nível acima da categoria, em exatamente dois níveis.

O lançamento continua apontando para a categoria; o grupo só agrupa para ler o
resultado. O que fica fixado aqui: o cadastro (e a recusa de apagar grupo com
categoria), a semeadura que nunca troca um grupo já escolhido, a rosca do
Dashboard por grupo (sem o grupo "Sistema"), o filtro por grupo em Lançamentos e
o subtotal por grupo no Planejamento anual.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_MOVEMENT,
    ENTRY_TYPE_EXPENSE,
    STATUS_REALIZED,
    VIEW_REALIZED,
)
from dashboard.views import SEM_GRUPO, _categorias
from reports import services as report_services
from transactions import services
from transactions.models import CashFlowCategory, CashFlowCategoryGroup, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def mundo():
    user = AppUser.objects.create_user(username="operador-grupos", password="senha-segura")
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    conta = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Conta 02")
    saude = CashFlowCategoryGroup.objects.create(group_name="Saúde", position=80)
    sistema = CashFlowCategoryGroup.objects.create(group_name="Sistema", position=900, in_charts=False)
    categorias = {
        "plano": CashFlowCategory.objects.create(category_name="Plano de saúde", group=saude),
        "farmacia": CashFlowCategory.objects.create(category_name="Farmácia", group=saude),
        "ajuste": CashFlowCategory.objects.create(category_name="Ajustes de Saldo", group=sistema),
        "solta": CashFlowCategory.objects.create(category_name="Sem lar"),
    }
    return user, conta, {"saude": saude, "sistema": sistema}, categorias


def _gasto(user, conta, categoria, valor, dia=date(2026, 9, 10), descricao="Gasto"):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_EXPENSE,
            description=descricao, entry_amount=Decimal(valor), installments=1, due_date=dia,
            status=STATUS_REALIZED, realized_date=dia, realized_amount=Decimal(valor),
        ),
        user=user,
    )[0]


# --- cadastro ------------------------------------------------------------------


def test_cadastro_e_edicao_do_grupo():
    grupo = services.create_category_group("  Moradia ", "60")
    assert (grupo.group_name, grupo.position, grupo.in_charts) == ("Moradia", 60, True)
    services.update_category_group(grupo, "Casa e contas", "5", in_charts=False)
    grupo.refresh_from_db()
    assert (grupo.group_name, grupo.position, grupo.in_charts) == ("Casa e contas", 5, False)


@pytest.mark.parametrize(("nome", "posicao", "erro"), [
    ("", "10", "obrigatório"), ("X", "abc", "Posição"), ("X", "40000", "entre 0 e 32000"),
])
def test_grupo_invalido_e_recusado(nome, posicao, erro):
    with pytest.raises(ValueError, match=erro):
        services.create_category_group(nome, posicao)


def test_nome_de_grupo_repetido_e_recusado():
    services.create_category_group("Moradia")
    with pytest.raises(ValueError, match="Já existe"):
        services.create_category_group("Moradia")


def test_nao_apaga_grupo_com_categoria_mas_apaga_o_vazio(mundo):
    _, _, grupos, _ = mundo
    with pytest.raises(ValueError, match="ainda há categorias"):
        services.delete_category_group(grupos["saude"])
    vazio = services.create_category_group("Vazio")
    services.delete_category_group(vazio)
    assert not CashFlowCategoryGroup.objects.filter(id=vazio.id).exists()


def test_categoria_escolhe_grupo_e_atualizar_sem_o_argumento_preserva(mundo):
    _, _, grupos, categorias = mundo
    categoria = services.create_category("Dentista", "gerencial", grupos["saude"].id)
    assert categoria.group == grupos["saude"]
    services.update_category(categoria, "Dentista e ortodontia", "gerencial")
    categoria.refresh_from_db()
    assert categoria.group == grupos["saude"]
    services.update_category(categoria, "Dentista e ortodontia", "gerencial", "")
    categoria.refresh_from_db()
    assert categoria.group is None
    with pytest.raises(ValueError, match="Grupo de categoria inválido"):
        services.update_category(categoria, "Dentista", "gerencial", "99999")


def test_renomear_o_grupo_toca_as_categorias_dele(mundo):
    _, _, grupos, categorias = mundo
    antes = CashFlowCategory.objects.get(id=categorias["plano"].id).updated_at
    services.update_category_group(grupos["saude"], "Saúde e bem-estar", "80")
    assert CashFlowCategory.objects.get(id=categorias["plano"].id).updated_at > antes


# --- semeadura -----------------------------------------------------------------


def test_semeadura_cria_os_grupos_liga_as_categorias_e_nunca_troca_a_escolha():
    gerencial = CashFlowCategory.objects.create(category_name="Saúde")
    escolhida = CashFlowCategory.objects.create(category_name="Lazer")
    outro = CashFlowCategoryGroup.objects.create(group_name="Meu grupo")
    escolhida.group = outro
    escolhida.save(update_fields=["group"])
    CashFlowCategory.objects.create(category_name="Operações em Bolsa", kind=CATEGORY_KIND_MOVEMENT)

    call_command("grupos_de_categoria", "semear")
    gerencial.refresh_from_db()
    escolhida.refresh_from_db()
    assert gerencial.group.group_name == "Saúde"
    assert escolhida.group == outro
    assert CashFlowCategory.objects.get(category_name="Operações em Bolsa").group.in_charts is False

    antes = CashFlowCategoryGroup.objects.count()
    call_command("grupos_de_categoria", "semear")
    assert CashFlowCategoryGroup.objects.count() == antes
    assert CashFlowCategoryGroup.objects.get(group_name="Sistema").in_charts is False


# --- Dashboard por grupo --------------------------------------------------------


def _entradas():
    return list(CashFlowEntry.objects.select_related("category__group"))


def test_dashboard_por_grupo_soma_as_categorias_do_grupo_e_omite_o_sistema(mundo):
    user, conta, _, cat = mundo
    _gasto(user, conta, cat["plano"], "300.00")
    _gasto(user, conta, cat["farmacia"], "50.00")
    _gasto(user, conta, cat["solta"], "20.00")
    _gasto(user, conta, cat["ajuste"], "999.00")

    por_categoria = dict(_categorias(_entradas(), VIEW_REALIZED, ENTRY_TYPE_EXPENSE))
    assert por_categoria["Plano de saúde"] == Decimal("300.00")
    assert por_categoria["Ajustes de Saldo"] == Decimal("999.00")

    por_grupo = dict(_categorias(_entradas(), VIEW_REALIZED, ENTRY_TYPE_EXPENSE, por_grupo=True))
    assert por_grupo == {"Saúde": Decimal("350.00"), SEM_GRUPO: Decimal("20.00")}


# --- Lançamentos filtrado pelo grupo --------------------------------------------


def test_lancamentos_filtra_pelo_grupo(mundo):
    user, conta, _, cat = mundo
    plano = _gasto(user, conta, cat["plano"], "300.00")
    farmacia = _gasto(user, conta, cat["farmacia"], "50.00")
    _gasto(user, conta, cat["solta"], "20.00")

    achados = services.list_transactions_for_view(
        account_ids=[conta.id], view_mode=VIEW_REALIZED,
        start_selected=date(2026, 9, 1), end_selected=date(2026, 9, 30), filter_group="Saúde",
    )
    assert {entrada.id for entrada in achados} == {plano.id, farmacia.id}
    assert "filter_group" in services.TRANSACTIONS_QUERY_PARAMS


# --- Planejamento anual com subtotal por grupo ----------------------------------


def _linhas(user, por_grupo):
    relatorio = report_services.annual_planning_presentation(
        user, date(2026, 9, 1), view_mode=VIEW_REALIZED, by_group=por_grupo
    )
    return [(linha["kind"], linha["label"], linha["level"]) for linha in relatorio["rows"]], relatorio


def test_planejamento_anual_agrupa_com_subtotal_e_sem_agrupar_fica_como_sempre(mundo):
    user, conta, _, cat = mundo
    _gasto(user, conta, cat["plano"], "300.00")
    _gasto(user, conta, cat["farmacia"], "50.00")
    _gasto(user, conta, cat["solta"], "20.00")

    simples, _ = _linhas(user, False)
    assert simples == [
        ("total", "Despesas não recorrentes", 0),
        ("category", "Farmácia", 1),
        ("category", "Plano de saúde", 1),
        ("category", "Sem lar", 1),
    ]

    agrupado, relatorio = _linhas(user, True)
    assert agrupado == [
        ("total", "Despesas não recorrentes", 0),
        ("group", "Saúde", 1),
        ("category", "Farmácia", 2),
        ("category", "Plano de saúde", 2),
        ("group", "Sem grupo", 1),
        ("category", "Sem lar", 2),
    ]
    subtotal = next(linha for linha in relatorio["rows"] if linha["kind"] == "group" and linha["label"] == "Saúde")
    setembro = next(mes["value"] for mes in subtotal["months"] if mes["is_current"])
    assert setembro == Decimal("-350.00")
    # O total geral não muda com o agrupamento.
    assert relatorio["totals"]["months"] == _linhas(user, False)[1]["totals"]["months"]
