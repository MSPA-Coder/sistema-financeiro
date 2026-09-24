"""Filtro global de grupos de conta: bancos, corretoras, cartões e aplicações.

O filtro só é legível se os grupos forem uma partição: cada conta em um grupo,
e em um só. Um cartão emitido por um banco não pode aparecer ao marcar
"Bancos" -- era essa a confusão que o filtro veio desfazer. Por isso o tipo da
conta vence o tipo da instituição, e é isso que estes testes medem.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import SuspiciousOperation
from django.db import IntegrityError, transaction

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from banking.services import update_account
from core.account_group_filter import (
    ALL_ACCOUNT_GROUPS,
    GROUP_BANKS,
    GROUP_BROKERS,
    GROUP_CARDS,
    GROUP_INVESTMENTS,
    account_group,
    account_group_q,
    parse_account_groups,
)
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    ACCOUNT_KIND_REGULAR,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from reports import services as reports_services
from transactions.models import CashFlowCategory, CashFlowEntry

# --- A regra, sem banco ----------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "institution_type", "grupo"),
    [
        (ACCOUNT_KIND_REGULAR, "Banco", GROUP_BANKS),
        (ACCOUNT_KIND_REGULAR, "Corretora", GROUP_BROKERS),
        # O tipo da conta vence o da instituição.
        (ACCOUNT_KIND_CREDIT_CARD, "Banco", GROUP_CARDS),
        (ACCOUNT_KIND_CREDIT_CARD, "Corretora", GROUP_CARDS),
        (ACCOUNT_KIND_INVESTMENT, "Banco", GROUP_INVESTMENTS),
        (ACCOUNT_KIND_INVESTMENT, "Corretora", GROUP_INVESTMENTS),
    ],
)
def test_cada_conta_cai_em_um_grupo_so(kind, institution_type, grupo):
    assert account_group(kind, institution_type) == grupo


def test_ausente_ou_vazio_vale_todos_os_grupos():
    assert parse_account_groups({}) == ALL_ACCOUNT_GROUPS
    assert parse_account_groups({"grupos": ""}) == ALL_ACCOUNT_GROUPS


def test_lista_de_grupos_e_normalizada():
    assert parse_account_groups({"grupos": " Cartoes, bancos "}) == {GROUP_CARDS, GROUP_BANKS}


@pytest.mark.parametrize("valor", ["poupanca", "bancos,xyz", ","])
def test_grupo_desconhecido_e_recusado(valor):
    with pytest.raises(SuspiciousOperation):
        parse_account_groups({"grupos": valor})


# --- Com banco ---------------------------------------------------------------


@pytest.fixture
def usuario(db):
    return get_user_model().objects.create_user(
        username="teste-grupos",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )


@pytest.fixture
def contas(db):
    """Uma conta de cada grupo; banco e corretora têm cartão e aplicação."""
    titular = AccountOwner.objects.create(name="Titular dos grupos")
    banco = FinancialInstitution.objects.create(institution_name="Banco G", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(institution_name="Corretora G", institution_type="Corretora")

    def conta(nome, instituicao, kind=ACCOUNT_KIND_REGULAR):
        campos = {"card_closing_day": 5, "card_due_day": 12} if kind == ACCOUNT_KIND_CREDIT_CARD else {}
        return FinancialAccount.objects.create(
            owner=titular, institution=instituicao, account_name=nome, account_kind=kind,
            initial_balance=Decimal("0.00"), initial_balance_date=date(2025, 12, 31), **campos,
        )

    return {
        GROUP_BANKS: conta("Corrente", banco),
        GROUP_BROKERS: conta("Caixa da corretora", corretora),
        GROUP_CARDS: conta("Cartão do banco", banco, ACCOUNT_KIND_CREDIT_CARD),
        GROUP_INVESTMENTS: conta("CDB", banco, ACCOUNT_KIND_INVESTMENT),
    }


def _ids(grupos):
    return set(FinancialAccount.objects.filter(account_group_q(grupos)).values_list("id", flat=True))


@pytest.mark.django_db
def test_consulta_de_cada_grupo_devolve_so_a_conta_dele(contas):
    for grupo, conta in contas.items():
        assert _ids({grupo}) == {conta.id}, grupo
    assert _ids(ALL_ACCOUNT_GROUPS) == {conta.id for conta in contas.values()}


@pytest.mark.django_db
def test_contexto_das_telas_aplica_os_grupos(usuario, contas):
    ctx = reports_services.selected_context(usuario, {"grupos": "cartoes,aplicacoes"})
    options = reports_services.context_options(usuario, ctx)

    assert set(options.account_ids) == {contas[GROUP_CARDS].id, contas[GROUP_INVESTMENTS].id}
    # O seletor continua inteiro: é por ele que se escolhe uma conta fora do filtro.
    assert len(options.accounts) == 4


@pytest.mark.django_db
def test_conta_escolhida_vence_o_filtro_de_grupos(usuario, contas):
    """Sem isso, a tela ficaria vazia sem dizer por quê."""
    corrente = contas[GROUP_BANKS]
    ctx = reports_services.selected_context(usuario, {"grupos": "cartoes", "account_id": str(corrente.id)})

    assert reports_services.context_options(usuario, ctx).account_ids == [corrente.id]


def _despesa(conta, valor):
    categoria, _ = CashFlowCategory.objects.get_or_create(category_name="Categoria dos grupos")
    return CashFlowEntry.objects.create(
        account=conta, category=categoria, entry_type=ENTRY_TYPE_EXPENSE,
        description="Despesa", entry_amount=Decimal(valor), due_date=date.today(),
        status=STATUS_PROJECTED, operation_type=OPERATION_SINGLE,
    )


@pytest.mark.django_db
def test_lancamentos_mostram_so_os_grupos_pedidos(usuario, contas):
    from transactions.services import build_transactions_view_context

    for valor, conta in zip(("10.00", "20.00", "30.00", "40.00"), contas.values(), strict=True):
        _despesa(conta, valor)

    contexto = build_transactions_view_context(usuario, {"grupos": "cartoes"}, {})

    assert {tx.account_id for tx in contexto["txs"]} == {contas[GROUP_CARDS].id}
    assert contexto["blocos"][0]["total_despesas"] == Decimal("30.00")


@pytest.mark.django_db
def test_planejamento_anual_oferece_so_as_contas_dos_grupos(client, usuario, contas):
    client.force_login(usuario)

    resposta = client.get("/reports/annual-planning/?grupos=bancos,corretoras")

    assert resposta.status_code == 200
    assert {conta.id for conta in resposta.context["accounts"]} == {
        contas[GROUP_BANKS].id, contas[GROUP_BROKERS].id,
    }


@pytest.mark.django_db
def test_grupo_invalido_na_url_responde_400(client, usuario):
    client.force_login(usuario)

    assert client.get("/transactions/?grupos=poupanca").status_code == 400


@pytest.mark.django_db
def test_menu_conta_as_contas_de_cada_grupo_e_avisa_o_filtro_ativo(client, usuario, contas):
    client.force_login(usuario)

    resposta = client.get("/reports/projections/?grupos=cartoes")

    assert resposta.context["global_account_groups_active"] is True
    assert resposta.context["global_account_groups_labels"] == ["Cartões"]
    opcoes = {opcao["code"]: opcao for opcao in resposta.context["global_account_group_options"]()}
    assert {codigo: opcao["count"] for codigo, opcao in opcoes.items()} == dict.fromkeys(ALL_ACCOUNT_GROUPS, 1)
    assert [codigo for codigo, opcao in opcoes.items() if opcao["selected"]] == [GROUP_CARDS]


# --- O tipo novo -------------------------------------------------------------


@pytest.mark.django_db
def test_aplicacao_nao_guarda_dado_de_cartao(contas):
    aplicacao = contas[GROUP_INVESTMENTS]
    aplicacao.card_closing_day = 5
    aplicacao.card_due_day = 12

    with pytest.raises(IntegrityError), transaction.atomic():
        aplicacao.save()


@pytest.mark.django_db
def test_conta_vira_aplicacao_pelo_cadastro(usuario, contas):
    corrente = contas[GROUP_BANKS]

    update_account(
        usuario, corrente,
        owner_id=str(corrente.owner_id), institution_id=str(corrente.institution_id),
        account_name="Corrente", initial_balance="0", currency="BRL",
        account_kind=ACCOUNT_KIND_INVESTMENT,
        # Dias de cartão vindos do formulário são descartados, não gravados.
        card_closing_day="5", card_due_day="12",
    )

    corrente.refresh_from_db()
    assert corrente.account_kind == ACCOUNT_KIND_INVESTMENT
    assert corrente.card_closing_day is None
    assert account_group(corrente.account_kind, corrente.institution.institution_type) == GROUP_INVESTMENTS
