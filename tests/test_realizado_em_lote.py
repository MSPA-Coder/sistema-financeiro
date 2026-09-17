"""Realizar é um fato de uma ocorrência, não de um grupo.

O DEFEITO (17/09/2026)

`create_transaction_batch` resolvia o status de cada ocorrência com o status
pedido. Criar um recorrente ou um parcelado como "realizado" gravava TODAS as
ocorrências realizadas -- inclusive meses que ainda não tinham chegado -- e
todas na mesma data. Como o saldo realizado agrupa pela data de realização, um
recorrente de R$ 100 criado como realizado em 03/06 tirava R$ 1.000 do saldo
daquele dia. A edição de grupo ("todos" ou "este e os próximos") fazia o
mesmo, e fazia também o contrário: editar o grupo a partir de uma ocorrência
em aberto apagava a realização das que já tinham sido pagas.

A cópia local do banco de produção não tinha nenhum caso quando isto foi
medido. O defeito era latente.

A REGRA (decisão do mantenedor, 17/09/2026)

- na criação, só a primeira ocorrência nasce realizada, com a data informada;
  as demais seguem o vencimento -- vencidas se já passou, a vencer se não --,
  como a projeção mensal já fazia com molde realizado;
- na edição em grupo, status, data e valor realizados do formulário valem só
  para a linha editada (e a outra ponta dela, numa transferência). As demais
  mantêm a realização que têm; as que estão em aberto só acompanham o
  vencimento.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from accounts.models import AccountOwner
from accounts.services import save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SCOPE_ALL,
    OPERATION_SCOPE_CURRENT_FUTURE,
    OPERATION_SCOPE_SINGLE,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from reports.services import add_months, decimal_balance_before
from transactions import services
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.recurring_projection import ensure_recurring_projection_horizon

pytestmark = pytest.mark.django_db

# Relativo a hoje de propósito: "já venceu" só significa algo em relação ao dia
# em que a suíte roda. Três meses atrás garante vencimentos passados; o
# horizonte da recorrência e as seis parcelas garantem vencimentos futuros.
HOJE = date.today()
INICIO = add_months(date(HOJE.year, HOJE.month, 3), -3)
SEGUNDO = add_months(INICIO, 1)
VALOR = Decimal("100.00")

RECORRENTE = pytest.param({"is_recurring": True}, id="recorrente")
PARCELADO = pytest.param({"installments": 6}, id="parcelado")
ESCOPOS_DE_GRUPO = pytest.mark.parametrize(
    "escopo", [OPERATION_SCOPE_ALL, OPERATION_SCOPE_CURRENT_FUTURE]
)


@pytest.fixture
def cenario():
    usuario = get_user_model().objects.create_user(
        username="teste-realizado-em-lote",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )
    titular = AccountOwner.objects.create(name="Titular de teste")
    banco = FinancialInstitution.objects.create(
        institution_name="Banco de teste", institution_type="Banco"
    )
    conta = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta de teste"
    )
    destino = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta destino"
    )
    save_transfer_destination_accesses(usuario, {destino.id})
    return SimpleNamespace(
        usuario=usuario,
        conta=conta,
        destino=destino,
        despesa=CashFlowCategory.objects.create(category_name="Mensalidade"),
        transferencia=CashFlowCategory.objects.create(
            category_name="Transferência", kind=CATEGORY_KIND_TRANSFER
        ),
    )


def _requisicao(cenario, **campos):
    """O pedido da tela: realizado, com a data do primeiro vencimento."""
    padrao = {
        "account_id": cenario.conta.id,
        "category_id": cenario.despesa.id,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Mensalidade",
        "entry_amount": VALOR,
        "installments": 1,
        "due_date": INICIO,
        "status": STATUS_REALIZED,
        "realized_date": INICIO,
    }
    padrao.update(campos)
    return services.TransactionRequest(**padrao)


def _transferencia(cenario, **campos):
    return _requisicao(
        cenario,
        category_id=cenario.transferencia.id,
        counterparty_account_id=cenario.destino.id,
        **campos,
    )


def _em_aberto(cenario, req_de=_requisicao, **extras):
    return _criar(cenario, req_de(cenario, status=STATUS_PENDING, realized_date=None, **extras))


def _criar(cenario, req):
    return services.create_transaction_batch(req, user=cenario.usuario)


def _editar(cenario, entry, req, escopo):
    token = None
    if escopo == OPERATION_SCOPE_CURRENT_FUTURE:
        token = services.current_future_confirmation_token(entry.id)
    return services.update_transaction_operation(entry, req, escopo, token, user=cenario.usuario)


def _pagar_a_segunda(cenario, entries):
    """Realiza a segunda ocorrência (e a outra ponta, se houver) no vencimento."""
    segunda = next(e for e in entries if e.due_date == SEGUNDO)
    services.realize_transaction(segunda, realized_date=SEGUNDO, user=cenario.usuario)


def _da_operacao(bank_operation_id):
    """Sempre do banco: "este e os próximos" apaga e recria as linhas."""
    return list(
        CashFlowEntry.objects.filter(bank_operation_id=bank_operation_id).order_by(
            "account_id", "due_date", "id"
        )
    )


def _descrever(entries):
    return "\n".join(
        f"  conta {e.account_id}  venc. {e.due_date:%d/%m/%Y}  {e.status}"
        + (f" em {e.realized_date:%d/%m/%Y} por {e.realized_amount}" if e.realized_date else "")
        for e in entries
    )


def _em_aberto_pelo_vencimento(vencimento):
    return STATUS_PENDING if vencimento < HOJE else STATUS_PROJECTED


def _afirmar_realizadas(bank_operation_id, esperadas):
    """Só as ocorrências esperadas estão realizadas, cada uma na sua data.

    `esperadas` é um conjunto de (conta, vencimento, data de realização). Toda
    ocorrência fora dele precisa estar em aberto, com o status que o vencimento
    dá e sem data nem valor de realização.
    """
    entries = _da_operacao(bank_operation_id)
    realizadas = {
        (e.account_id, e.due_date, e.realized_date)
        for e in entries
        if e.status == STATUS_REALIZED
    }
    abertas_erradas = [
        e for e in entries
        if e.status != STATUS_REALIZED
        and (
            e.status != _em_aberto_pelo_vencimento(e.due_date)
            or e.realized_date is not None
            or e.realized_amount is not None
        )
    ]
    assert realizadas == esperadas and not abertas_erradas, (
        f"hoje é {HOJE:%d/%m/%Y}\n{_descrever(entries)}"
    )
    return entries


# --- Criação -----------------------------------------------------------------


@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_criar_como_realizado_realiza_so_a_primeira(cenario, extras):
    entries = _criar(cenario, _requisicao(cenario, **extras))

    _afirmar_realizadas(entries[0].bank_operation_id, {(cenario.conta.id, INICIO, INICIO)})


@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_criar_transferencia_como_realizada_realiza_so_a_primeira_dupla(cenario, extras):
    entries = _criar(cenario, _transferencia(cenario, **extras))

    _afirmar_realizadas(
        entries[0].bank_operation_id,
        {(cenario.conta.id, INICIO, INICIO), (cenario.destino.id, INICIO, INICIO)},
    )


@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_saldo_realizado_do_dia_perde_uma_ocorrencia_e_nao_todas(cenario, extras):
    """O dano, medido onde ele aparecia: no saldo realizado do dia escolhido."""
    conta = [cenario.conta.id]
    antes = decimal_balance_before(conta, INICIO, VIEW_REALIZED)

    entries = _criar(cenario, _requisicao(cenario, realized_amount=VALOR, **extras))

    depois = decimal_balance_before(conta, INICIO + timedelta(days=1), VIEW_REALIZED)
    assert depois - antes == -VALOR, (
        f"o saldo realizado de {INICIO:%d/%m/%Y} mudou {depois - antes}, e não -{VALOR}:\n"
        f"{_descrever(_da_operacao(entries[0].bank_operation_id))}"
    )


@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_operacao_criada_como_realizada_reflete_as_ocorrencias(cenario, extras):
    """A operação nascia com o status pedido; agora ela resume as ocorrências.

    Com a primeira realizada, parcelas vencidas e parcelas a vencer, é a regra
    de `_sync_bank_operation_status`: havendo vencida, a operação é vencida.
    """
    entries = _criar(cenario, _requisicao(cenario, **extras))

    operacao = BankOperation.objects.get(id=entries[0].bank_operation_id)
    assert operacao.status == STATUS_PENDING


# --- Edição em grupo: o formulário vale para a linha editada ------------------


@ESCOPOS_DE_GRUPO
@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_editar_grupo_para_realizado_realiza_so_a_linha_editada(cenario, extras, escopo):
    entries = _em_aberto(cenario, **extras)

    _editar(cenario, entries[0], _requisicao(cenario, **extras), escopo)

    _afirmar_realizadas(entries[0].bank_operation_id, {(cenario.conta.id, INICIO, INICIO)})


@ESCOPOS_DE_GRUPO
@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_editar_transferencia_para_realizada_realiza_so_a_dupla_editada(cenario, extras, escopo):
    entries = _em_aberto(cenario, _transferencia, **extras)

    _editar(cenario, entries[0], _transferencia(cenario, **extras), escopo)

    _afirmar_realizadas(
        entries[0].bank_operation_id,
        {(cenario.conta.id, INICIO, INICIO), (cenario.destino.id, INICIO, INICIO)},
    )


# --- Edição em grupo: a realização das outras é fato --------------------------


@pytest.mark.parametrize("status_do_formulario", [STATUS_PENDING, STATUS_REALIZED])
@ESCOPOS_DE_GRUPO
@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_editar_grupo_mantem_a_realizacao_das_outras(
    cenario, extras, escopo, status_do_formulario
):
    """Corrigir o grupo a partir da primeira ocorrência não mexe na segunda, paga.

    A descrição e o valor previsto mudam para todas; a data e o valor
    realizados da paga continuam os dela. "Este e os próximos" recria as
    linhas, então a paga é achada pelo vencimento, não pelo id.
    """
    entries = _em_aberto(cenario, **extras)
    _pagar_a_segunda(cenario, entries)
    realizada_pelo_formulario = status_do_formulario == STATUS_REALIZED

    _editar(
        cenario,
        entries[0],
        _requisicao(
            cenario,
            status=status_do_formulario,
            realized_date=INICIO if realizada_pelo_formulario else None,
            description="Mensalidade corrigida",
            entry_amount=Decimal("120.00"),
            **extras,
        ),
        escopo,
    )

    esperadas = {(cenario.conta.id, SEGUNDO, SEGUNDO)}
    if realizada_pelo_formulario:
        esperadas.add((cenario.conta.id, INICIO, INICIO))
    todas = _afirmar_realizadas(entries[0].bank_operation_id, esperadas)
    paga = next(e for e in todas if e.due_date == SEGUNDO)
    assert (paga.description, paga.entry_amount, paga.realized_amount) == (
        "Mensalidade corrigida",
        Decimal("120.00"),
        VALOR,
    )


@ESCOPOS_DE_GRUPO
@pytest.mark.parametrize("extras", [RECORRENTE, PARCELADO])
def test_editar_transferencia_mantem_a_realizacao_das_outras_duplas(cenario, extras, escopo):
    entries = _em_aberto(cenario, _transferencia, **extras)
    _pagar_a_segunda(cenario, entries)

    _editar(
        cenario,
        entries[0],
        _transferencia(cenario, status=STATUS_PENDING, realized_date=None, **extras),
        escopo,
    )

    _afirmar_realizadas(
        entries[0].bank_operation_id,
        {(cenario.conta.id, SEGUNDO, SEGUNDO), (cenario.destino.id, SEGUNDO, SEGUNDO)},
    )


# --- Controles: o que já estava certo -----------------------------------------


def test_controle_escopo_somente_este_realiza_so_ele(cenario):
    entries = _em_aberto(cenario, installments=6)

    _editar(cenario, entries[0], _requisicao(cenario, installments=6), OPERATION_SCOPE_SINGLE)

    _afirmar_realizadas(entries[0].bank_operation_id, {(cenario.conta.id, INICIO, INICIO)})


def test_controle_projecao_mensal_nao_propaga_o_realizado(cenario):
    """A projeção automática já tratava molde realizado como não realizado.

    `_projected_status` faz as ocorrências novas nascerem "a vencer" mesmo
    quando a última ocorrência existente está realizada -- é a regra que a
    criação passou a seguir. O molde é montado pelo ORM para o controle não
    depender do caminho corrigido.
    """
    molde = _em_aberto(cenario, is_recurring=True)[0]
    CashFlowEntry.objects.filter(bank_operation_id=molde.bank_operation_id).exclude(
        id=molde.id
    ).delete()
    CashFlowEntry.objects.filter(id=molde.id).update(
        status=STATUS_REALIZED, realized_date=INICIO, realized_amount=VALOR
    )

    ensure_recurring_projection_horizon(today=HOJE, horizon_months=2, update_last_run=False)

    novas = [e for e in _da_operacao(molde.bank_operation_id) if e.id != molde.id]
    assert novas, "a projeção não gerou ocorrência nenhuma; o controle não mediu nada"
    assert {e.status for e in novas} <= {STATUS_PENDING, STATUS_PROJECTED}
    assert all(e.realized_date is None for e in novas)
