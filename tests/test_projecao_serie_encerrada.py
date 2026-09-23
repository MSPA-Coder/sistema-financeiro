"""Excluir "este e os próximos" encerra a série: a projeção não a retoma.

O DEFEITO (23/09/2026)

`_extend_operation` estende cada operação recorrente a partir da MAIOR data que
restou. Excluir uma série "deste registro em diante" apaga a cauda, mas as
ocorrências anteriores -- em geral já realizadas -- continuam com
`is_recurring=True`. Na execução seguinte da projeção (a mensal do middleware
ou o botão em Parâmetros), a maior data restante fica antes do horizonte e a
cauda apagada volta inteira. Em produção, as operações 162 e 164 foram
contornadas à mão em 23/09/2026, desligando `is_recurring` nas linhas que
restaram.

O `test_projecao_recorrente_idempotente.py` prova que uma lacuna NO MEIO não
ressuscita; a cauda apagada é o caso que ele não cobre.

O QUE ESTE ARQUIVO FIXA

- a exclusão "este e os próximos" grava o encerramento na `BankOperation`, e a
  projeção deixa de estender a operação -- recorrente simples e transferência
  interna recorrente (as duas pernas);
- o histórico que restou mantém `is_recurring=True`: o Planejamento anual
  separa recorrente de não recorrente por esse booleano, e desligá-lo
  reclassificaria meses já realizados;
- as linhas desligadas à mão (o contorno de produção) seguem fora da projeção;
- controles positivos: uma série não encerrada continua sendo estendida.

Toca o banco de propósito: o defeito só aparece na sequência criar, excluir e
projetar, contando linhas no fim.
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
    OPERATION_SCOPE_CURRENT_FUTURE,
    OPERATION_SCOPE_SINGLE,
    STATUS_PROJECTED,
)
from reports.services import add_months
from transactions import services
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.recurring_projection import ensure_recurring_projection_horizon

pytestmark = pytest.mark.django_db

INICIO = date(2026, 1, 10)
CORTE = date(2026, 3, 10)


def _hoje_da_projecao() -> date:
    """Um mês bem depois de qualquer horizonte que a criação tenha alcançado.

    A criação projeta até `date.today()` mais o horizonte (no máximo 36
    meses); passando disso, toda série viva tem o que gerar, e a série
    encerrada teria também -- é esse o contraste que os testes medem.
    """
    return add_months(date.today(), 40)


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(
        username="operador-serie-encerrada", password="senha-segura", user_type="administrator"
    )
    owner = AccountOwner.objects.create(name="Titular")
    banco = FinancialInstitution.objects.create(
        institution_name="Banco dos testes", institution_type="Banco"
    )
    corrente = FinancialAccount.objects.create(owner=owner, institution=banco, account_name="Corrente")
    poupanca = FinancialAccount.objects.create(owner=owner, institution=banco, account_name="Poupança")
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    save_transfer_destination_accesses(user, {poupanca.id})
    mercado = CashFlowCategory.objects.create(category_name="Mercado")
    transferencia = CashFlowCategory.objects.create(
        category_name="Transferência", kind=CATEGORY_KIND_TRANSFER
    )
    return user, corrente, poupanca, mercado, transferencia


def _criar_recorrente(user, conta, categoria, destino=None):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description="Recorrente",
            entry_amount=Decimal("100.00"),
            installments=1,
            due_date=INICIO,
            is_recurring=True,
            status=STATUS_PROJECTED,
            counterparty_account_id=destino.id if destino else None,
        ),
        user=user,
    )


def _excluir(user, linha, escopo=OPERATION_SCOPE_CURRENT_FUTURE):
    token = (
        services.current_future_confirmation_token(linha.id)
        if escopo == OPERATION_SCOPE_CURRENT_FUTURE
        else None
    )
    return services.delete_transaction_or_operation(linha, escopo, token, user=user)


def _origem_em(linhas, vencimento):
    return next(e for e in linhas if e.due_date == vencimento and e.source_entry_id is None)


def _datas(operacao_id):
    return sorted(
        CashFlowEntry.objects.filter(bank_operation_id=operacao_id).values_list("account_id", "due_date")
    )


def _projetar():
    return ensure_recurring_projection_horizon(
        today=_hoje_da_projecao(), horizon_months=2, update_last_run=False
    )


# --- Recorrente simples --------------------------------------------------------


def test_recorrente_excluida_deste_em_diante_nao_volta_na_projecao(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    encerrada = _criar_recorrente(user, corrente, mercado)
    viva = _criar_recorrente(user, corrente, mercado)
    operacao_encerrada = encerrada[0].bank_operation_id
    operacao_viva = viva[0].bank_operation_id

    _excluir(user, _origem_em(encerrada, CORTE))
    antes = _datas(operacao_encerrada)
    assert [d for _c, d in antes] == [date(2026, 1, 10), date(2026, 2, 10)]
    linhas_vivas_antes = CashFlowEntry.objects.filter(bank_operation_id=operacao_viva).count()

    _projetar()

    assert _datas(operacao_encerrada) == antes
    # Controle positivo: a mesma execução estende a série não encerrada.
    assert CashFlowEntry.objects.filter(bank_operation_id=operacao_viva).count() > linhas_vivas_antes


def test_encerramento_fica_registrado_e_historico_continua_recorrente(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    linhas = _criar_recorrente(user, corrente, mercado)
    operacao_id = linhas[0].bank_operation_id

    _excluir(user, _origem_em(linhas, CORTE))

    assert BankOperation.objects.get(id=operacao_id).recurrence_ended_on == CORTE
    assert all(CashFlowEntry.objects.filter(bank_operation_id=operacao_id).values_list("is_recurring", flat=True))


def test_excluir_so_uma_ocorrencia_nao_encerra_a_serie(cenario):
    # "Somente este" abre uma lacuna, não encerra. A série continua viva.
    user, corrente, _poupanca, mercado, _transferencia = cenario
    linhas = _criar_recorrente(user, corrente, mercado)
    operacao_id = linhas[0].bank_operation_id

    _excluir(user, _origem_em(linhas, CORTE), escopo=OPERATION_SCOPE_SINGLE)
    antes = CashFlowEntry.objects.filter(bank_operation_id=operacao_id).count()
    _projetar()

    assert BankOperation.objects.get(id=operacao_id).recurrence_ended_on is None
    assert CashFlowEntry.objects.filter(bank_operation_id=operacao_id).count() > antes


# --- Transferência interna recorrente --------------------------------------------


def test_transferencia_recorrente_excluida_deste_em_diante_nao_volta(cenario):
    user, corrente, poupanca, _mercado, transferencia = cenario
    encerrada = _criar_recorrente(user, corrente, transferencia, destino=poupanca)
    viva = _criar_recorrente(user, corrente, transferencia, destino=poupanca)
    operacao_encerrada = encerrada[0].bank_operation_id
    operacao_viva = viva[0].bank_operation_id

    _excluir(user, _origem_em(encerrada, CORTE))
    antes = _datas(operacao_encerrada)
    # As duas pernas de janeiro e fevereiro.
    assert len(antes) == 4
    linhas_vivas_antes = CashFlowEntry.objects.filter(bank_operation_id=operacao_viva).count()

    _projetar()

    assert _datas(operacao_encerrada) == antes
    assert BankOperation.objects.get(id=operacao_encerrada).recurrence_ended_on == CORTE
    # Controle positivo: a transferência não encerrada ganha pernas novas.
    assert CashFlowEntry.objects.filter(bank_operation_id=operacao_viva).count() > linhas_vivas_antes


# --- O contorno de produção (operações 162 e 164) -------------------------------


def test_contorno_manual_de_desligar_is_recurring_continua_valendo(cenario):
    # Em 23/09/2026 as linhas que restaram das operações 162 e 164 tiveram
    # `is_recurring` desligado à mão, sem data de encerramento. Esse estado
    # precisa continuar fora da projeção, sem migração de dados.
    user, corrente, _poupanca, mercado, _transferencia = cenario
    linhas = _criar_recorrente(user, corrente, mercado)
    operacao_id = linhas[0].bank_operation_id
    CashFlowEntry.objects.filter(bank_operation_id=operacao_id, due_date__gte=CORTE).delete()
    CashFlowEntry.objects.filter(bank_operation_id=operacao_id).update(is_recurring=False)
    antes = _datas(operacao_id)

    _projetar()

    assert BankOperation.objects.get(id=operacao_id).recurrence_ended_on is None
    assert _datas(operacao_id) == antes


# --- Editar "este e os próximos" desmarcando "recorrente" -----------------------
#
# O mesmo mecanismo por outro caminho: a edição grava `is_recurring=False` no
# bloco, as linhas anteriores continuam recorrentes e a projeção -- que só lê
# as recorrentes -- partia da última delas e recriava, por cima do bloco, uma
# ocorrência recorrente em cada mês que já tinha a linha editada.


def _desmarcar_recorrente(user, linha, destino=None):
    requisicao = services.TransactionRequest(
        account_id=linha.account_id,
        category_id=linha.category_id,
        entry_type=linha.entry_type,
        description=linha.description,
        entry_amount=linha.entry_amount,
        installments=1,
        due_date=linha.due_date,
        is_recurring=False,
        status=linha.status,
        counterparty_account_id=destino.id if destino else None,
    )
    token = services.current_future_confirmation_token(linha.id)
    return services.update_transaction_operation(
        linha, requisicao, OPERATION_SCOPE_CURRENT_FUTURE, token, user=user
    )


def _datas_repetidas(operacao_id):
    datas = _datas(operacao_id)
    return sorted({d for d in datas if datas.count(d) > 1})


def test_recorrente_desmarcada_deste_em_diante_nao_duplica(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    linhas = _criar_recorrente(user, corrente, mercado)
    operacao_id = linhas[0].bank_operation_id

    _desmarcar_recorrente(user, _origem_em(linhas, CORTE))
    antes = _datas(operacao_id)
    _projetar()

    assert _datas_repetidas(operacao_id) == []
    assert _datas(operacao_id) == antes
    assert BankOperation.objects.get(id=operacao_id).recurrence_ended_on == CORTE


def test_transferencia_desmarcada_deste_em_diante_nao_duplica(cenario):
    user, corrente, poupanca, _mercado, transferencia = cenario
    linhas = _criar_recorrente(user, corrente, transferencia, destino=poupanca)
    operacao_id = linhas[0].bank_operation_id

    _desmarcar_recorrente(user, _origem_em(linhas, CORTE), destino=poupanca)
    antes = _datas(operacao_id)
    _projetar()

    assert _datas_repetidas(operacao_id) == []
    assert _datas(operacao_id) == antes
    assert BankOperation.objects.get(id=operacao_id).recurrence_ended_on == CORTE
