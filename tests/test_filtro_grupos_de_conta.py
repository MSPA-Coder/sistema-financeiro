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
    DEFAULT_ACCOUNT_GROUPS,
    GROUP_ADMINISTERED,
    GROUP_BANKS,
    GROUP_BROKERS,
    GROUP_CARDS,
    GROUP_INVESTMENTS,
    account_group,
    account_group_q,
    is_filtering,
    parse_account_groups,
)
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    ACCOUNT_KIND_REGULAR,
    ACCOUNT_PURPOSE_ADMINISTERED,
    ACCOUNT_PURPOSE_PERSONAL,
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


def test_ausente_ou_vazio_vale_o_padrao_que_nao_inclui_administradas():
    # Decisão de 02/10/2026: o dinheiro de terceiros que o titular só gere não
    # entra no fluxo pessoal sem que ele peça.
    assert parse_account_groups({}) == DEFAULT_ACCOUNT_GROUPS
    assert parse_account_groups({"grupos": ""}) == DEFAULT_ACCOUNT_GROUPS
    assert ALL_ACCOUNT_GROUPS - {GROUP_ADMINISTERED} == DEFAULT_ACCOUNT_GROUPS
    assert not is_filtering(DEFAULT_ACCOUNT_GROUPS)
    assert is_filtering(ALL_ACCOUNT_GROUPS)


@pytest.mark.parametrize(
    ("kind", "institution_type"),
    [
        (ACCOUNT_KIND_REGULAR, "Banco"),
        (ACCOUNT_KIND_CREDIT_CARD, "Banco"),
        (ACCOUNT_KIND_INVESTMENT, "Corretora"),
    ],
)
def test_a_finalidade_administrada_vence_o_tipo_da_conta(kind, institution_type):
    assert account_group(kind, institution_type, ACCOUNT_PURPOSE_ADMINISTERED) == GROUP_ADMINISTERED
    assert account_group(kind, institution_type, ACCOUNT_PURPOSE_PERSONAL) != GROUP_ADMINISTERED


@pytest.mark.django_db
def test_grupos_continuam_uma_particao_com_a_conta_administrada(contas):
    administrada = FinancialAccount.objects.create(
        owner=contas[GROUP_BANKS].owner, institution=contas[GROUP_BANKS].institution,
        account_name="Conta administrada", purpose=ACCOUNT_PURPOSE_ADMINISTERED,
    )
    administrada_aplicacao = FinancialAccount.objects.create(
        owner=contas[GROUP_BANKS].owner, institution=contas[GROUP_BANKS].institution,
        account_name="Caixinha administrada", account_kind=ACCOUNT_KIND_INVESTMENT,
        purpose=ACCOUNT_PURPOSE_ADMINISTERED,
    )
    todas = set(FinancialAccount.objects.values_list("id", flat=True))
    por_grupo = {
        grupo: set(FinancialAccount.objects.filter(account_group_q([grupo])).values_list("id", flat=True))
        for grupo in ALL_ACCOUNT_GROUPS
    }
    # Cada conta em um grupo só, e todas em algum.
    assert set().union(*por_grupo.values()) == todas
    assert sum(len(ids) for ids in por_grupo.values()) == len(todas)
    assert por_grupo[GROUP_ADMINISTERED] == {administrada.id, administrada_aplicacao.id}
    assert administrada_aplicacao.id not in por_grupo[GROUP_INVESTMENTS]
    # O padrão deixa as administradas de fora; marcar todas as traz de volta.
    padrao = set(FinancialAccount.objects.filter(account_group_q(DEFAULT_ACCOUNT_GROUPS)).values_list("id", flat=True))
    assert padrao == todas - por_grupo[GROUP_ADMINISTERED]
    assert set(FinancialAccount.objects.filter(account_group_q(ALL_ACCOUNT_GROUPS)).values_list("id", flat=True)) == todas


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
    # Os seletores de Conta e Instituição também respeitam o filtro: o cartão e
    # o CDB são do Banco G, e a corretora não tem nenhum dos dois.
    assert {conta.id for conta in options.accounts} == {contas[GROUP_CARDS].id, contas[GROUP_INVESTMENTS].id}
    assert [instituicao.institution_name for instituicao in options.institutions] == ["Banco G"]


@pytest.mark.django_db
def test_seletor_de_contas_combina_titular_e_instituicao(usuario, contas):
    """Conta exibida precisa ser compatível com todos os filtros de contexto."""
    conta_do_banco = contas[GROUP_BANKS]
    outro_titular = AccountOwner.objects.create(name="Outro titular")
    conta_de_outro_titular = FinancialAccount.objects.create(
        owner=outro_titular,
        institution=conta_do_banco.institution,
        account_name="Cofrinho de outro titular",
        initial_balance=Decimal("0.00"),
        initial_balance_date=date(2025, 12, 31),
    )

    ctx = reports_services.selected_context(
        usuario,
        {
            "owner_id": str(conta_do_banco.owner_id),
            "institution_id": str(conta_do_banco.institution_id),
            "currency": "BRL",
        },
    )
    options = reports_services.context_options(usuario, ctx)

    assert {conta.id for conta in options.accounts} == {
        contas[GROUP_BANKS].id,
        contas[GROUP_CARDS].id,
        contas[GROUP_INVESTMENTS].id,
    }
    assert conta_de_outro_titular.id not in {conta.id for conta in options.accounts}


@pytest.mark.django_db
def test_conta_escolhida_vence_o_filtro_de_grupos(usuario, contas):
    """Sem isso, a tela ficaria vazia sem dizer por quê."""
    corrente = contas[GROUP_BANKS]
    ctx = reports_services.selected_context(usuario, {"grupos": "cartoes", "account_id": str(corrente.id)})
    options = reports_services.context_options(usuario, ctx)

    assert options.account_ids == [corrente.id]
    # E o seletor mostra a conta que está valendo, mesmo fora do filtro.
    assert corrente.id in {conta.id for conta in options.accounts}


@pytest.mark.django_db
def test_seletores_respeitam_a_moeda(usuario, contas):
    corretora = contas[GROUP_BROKERS].institution
    dolar = FinancialAccount.objects.create(
        owner=contas[GROUP_BANKS].owner, institution=FinancialInstitution.objects.create(
            institution_name="Corretora em dólar", institution_type="Corretora",
        ),
        account_name="Conta em dólar", currency="USD",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2025, 12, 31),
    )

    def seletores(params):
        options = reports_services.context_options(usuario, reports_services.selected_context(usuario, params))
        return {conta.id for conta in options.accounts}, {inst.id for inst in options.institutions}

    contas_brl, instituicoes_brl = seletores({})
    assert dolar.id not in contas_brl and dolar.institution_id not in instituicoes_brl
    assert corretora.id in instituicoes_brl

    contas_usd, instituicoes_usd = seletores({"currency": "USD"})
    assert contas_usd == {dolar.id} and instituicoes_usd == {dolar.institution_id}

    contas_todas, _ = seletores({"currency": "BRL,USD"})
    assert contas_todas == {conta.id for conta in contas.values()} | {dolar.id}


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
def test_planejamento_anual_oferece_so_as_contas_da_moeda(client, usuario, contas):
    dolar = FinancialAccount.objects.create(
        owner=contas[GROUP_BANKS].owner, institution=contas[GROUP_BROKERS].institution,
        account_name="Conta em dólar", currency="USD",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2025, 12, 31),
    )
    client.force_login(usuario)

    resposta = client.get("/reports/annual-planning/?currency=USD")

    assert {conta.id for conta in resposta.context["accounts"]} == {dolar.id}


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
    # O cenário não tem conta administrada: o grupo existe no menu, com zero.
    assert {codigo: opcao["count"] for codigo, opcao in opcoes.items()} == {
        **dict.fromkeys(ALL_ACCOUNT_GROUPS, 1), GROUP_ADMINISTERED: 0,
    }
    assert [codigo for codigo, opcao in opcoes.items() if opcao["selected"]] == [GROUP_CARDS]


@pytest.mark.django_db
def test_menu_manda_ao_script_a_selecao_padrao_dos_grupos(client, usuario, contas):
    """Marcar todos os grupos (Administradas incluída) não é o padrão: o padrão
    deixa Administradas de fora. O script só pode omitir `grupos` da URL quando
    a escolha é a padrão; antes ele omitia com todas marcadas, e a tela voltava
    a esconder as administradas (a C6 da Esposita sumia do filtro)."""
    client.force_login(usuario)

    pagina = client.get("/dashboard/").content.decode()

    assert 'data-default-value="bancos,corretoras,cartoes,aplicacoes"' in pagina
    todos = client.get("/transactions/?grupos=bancos,corretoras,cartoes,aplicacoes,administradas")
    assert todos.context["global_account_groups_active"] is True


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
