"""Recolher as recorrências que passaram do horizonte de projeção.

A projeção só estende. Uma série criada quando o horizonte era de 12 meses
continua com 12 meses de ocorrências depois que o horizonte cai para 6, e os
meses além do novo fim ficam com só uma parte das séries. Em produção, em
23/09/2026, eram 34 ocorrências até 07/2027 com o horizonte terminando em
03/2027. `recolher_alem_do_horizonte` devolve as séries ao horizonte atual,
sem apagar o que recebeu atenção própria.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SCOPE_ALL,
    OPERATION_SCOPE_SINGLE,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from core.services import update_recurring_projection_settings
from reports.services import add_months
from transactions import services
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.recurring_projection import (
    ensure_recurring_projection_horizon,
    recolher_alem_do_horizonte,
    recurring_projection_horizon_end,
)

pytestmark = pytest.mark.django_db

HOJE = date.today()
INICIO = date(HOJE.year, HOJE.month, 10)
FIM_CURTO = recurring_projection_horizon_end(HOJE, 3)


@pytest.fixture
def cenario():
    update_recurring_projection_settings(horizon_months=12, run_day=28)
    user = AppUser.objects.create_user(
        username="operador-recolhe", password="senha-segura", user_type="administrator"
    )
    owner = AccountOwner.objects.create(name="Titular")
    banco = FinancialInstitution.objects.create(institution_name="Banco", institution_type="Banco")
    corrente = FinancialAccount.objects.create(owner=owner, institution=banco, account_name="Corrente")
    poupanca = FinancialAccount.objects.create(owner=owner, institution=banco, account_name="Poupança")
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    save_transfer_destination_accesses(user, {poupanca.id})
    mercado = CashFlowCategory.objects.create(category_name="Mercado")
    transferencia = CashFlowCategory.objects.create(category_name="Transferência", kind=CATEGORY_KIND_TRANSFER)
    return user, corrente, poupanca, mercado, transferencia


def _criar(user, conta, categoria, *, destino=None, recorrente=True, parcelas=1):
    linhas = services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description="Série",
            entry_amount=Decimal("100.00"),
            installments=parcelas,
            due_date=INICIO,
            is_recurring=recorrente,
            status=STATUS_PROJECTED,
            counterparty_account_id=destino.id if destino else None,
        ),
        user=user,
    )
    return linhas[0].bank_operation_id


def _datas(operacao_id):
    return sorted(CashFlowEntry.objects.filter(bank_operation_id=operacao_id).values_list("due_date", flat=True))


def _recolher():
    return recolher_alem_do_horizonte(today=HOJE, horizon_months=3)


def _editar(user, linha, escopo, valor):
    requisicao = services.TransactionRequest(
        account_id=linha.account_id,
        category_id=linha.category_id,
        entry_type=linha.entry_type,
        description=linha.description,
        entry_amount=valor,
        installments=1,
        due_date=linha.due_date,
        is_recurring=True,
        status=linha.status,
    )
    return services.update_transaction_operation(linha, requisicao, escopo, None, user=user)


def test_recorrente_volta_ao_horizonte_e_a_operacao_se_atualiza(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    operacao = _criar(user, corrente, mercado)
    assert max(_datas(operacao)) > FIM_CURTO

    resultado = _recolher()

    datas = _datas(operacao)
    assert max(datas) <= FIM_CURTO
    assert max(datas) > add_months(FIM_CURTO, -1)
    assert resultado.removed_count == 12 - 3
    # A operação continua e só perde a cauda; quantidade e datas vêm dos
    # próprios lançamentos (conferidos acima), não de colunas copiadas.
    registro = BankOperation.objects.get(id=operacao)
    assert registro.recurrence_ended_on is None


def test_recolher_de_novo_nao_remove_nada(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    _criar(user, corrente, mercado)
    _recolher()
    assert _recolher().removed_count == 0


def test_serie_recolhida_volta_a_crescer_quando_o_horizonte_aumenta(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    operacao = _criar(user, corrente, mercado)
    antes = _datas(operacao)
    _recolher()

    ensure_recurring_projection_horizon(today=HOJE, horizon_months=12, update_last_run=False)

    assert _datas(operacao) == antes


def test_transferencia_perde_as_duas_pontas(cenario):
    user, corrente, poupanca, _mercado, transferencia = cenario
    operacao = _criar(user, corrente, transferencia, destino=poupanca)

    _recolher()

    linhas = CashFlowEntry.objects.filter(bank_operation_id=operacao)
    por_conta = {
        conta: sorted(linhas.filter(account_id=conta).values_list("due_date", flat=True))
        for conta in (corrente.id, poupanca.id)
    }
    assert por_conta[corrente.id] == por_conta[poupanca.id]
    assert max(por_conta[corrente.id]) <= FIM_CURTO


def test_parcelado_fica_inteiro(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    operacao = _criar(user, corrente, mercado, recorrente=False, parcelas=10)

    assert _recolher().removed_count == 0
    assert len(_datas(operacao)) == 10


def test_realizada_alem_do_horizonte_fica(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    operacao = _criar(user, corrente, mercado)
    ultima = CashFlowEntry.objects.filter(bank_operation_id=operacao).order_by("-due_date").first()
    CashFlowEntry.objects.filter(id=ultima.id).update(
        status=STATUS_REALIZED, realized_date=HOJE, realized_amount=ultima.entry_amount
    )

    _recolher()

    assert CashFlowEntry.objects.filter(id=ultima.id).exists()


def test_editada_so_ela_protege_a_serie_ate_ela(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    operacao = _criar(user, corrente, mercado)
    alvo_data = add_months(INICIO, 6)
    alvo = CashFlowEntry.objects.get(bank_operation_id=operacao, due_date=alvo_data)
    _editar(user, alvo, OPERATION_SCOPE_SINGLE, Decimal("250.00"))

    _recolher()

    datas = _datas(operacao)
    # Até a editada fica tudo, sem buraco; depois dela, sai.
    assert max(datas) == alvo_data
    assert len(datas) == 7


def test_edicao_da_serie_inteira_nao_protege(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    operacao = _criar(user, corrente, mercado)
    primeira = CashFlowEntry.objects.filter(bank_operation_id=operacao).order_by("due_date").first()
    _editar(user, primeira, OPERATION_SCOPE_ALL, Decimal("150.00"))

    _recolher()

    assert max(_datas(operacao)) <= FIM_CURTO
