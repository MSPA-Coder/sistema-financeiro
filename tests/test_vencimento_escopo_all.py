"""Editar o grupo inteiro não pode mover os vencimentos que ninguém mudou.

O DEFEITO (17/09/2026)

No escopo "Todos os registros do grupo", `_update_installment_or_recurring` e
`_update_internal_transfer` recalculam o vencimento de cada ocorrência como
`add_months(req.due_date, offset)`, com `offset` contado a partir da PRIMEIRA
ocorrência do grupo. Só que o formulário de edição envia o vencimento da linha
EDITADA. Editar a 2ª parcela de um parcelado de 6 (03/06..03/11) só para trocar
a descrição gravava 03/07..03/12: o grupo inteiro andava um mês.

O mesmo recálculo por posição fecha as lacunas: uma ocorrência excluída no
meio some do calendário, e as seguintes recuam um mês -- mesmo editando a
primeira.

O QUE ESTE ARQUIVO FIXA

Só o que independe da regra a decidir para quando o vencimento da linha
editada MUDA: se ele não mudou, nenhum vencimento do grupo muda. Status e
realização ficam de fora de propósito -- são outra correção.
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
    OPERATION_SCOPE_CURRENT_FUTURE,
    OPERATION_SCOPE_SINGLE,
    STATUS_PROJECTED,
)
from transactions import services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(username="operador-vencimento", password="senha-segura")
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


def _criar(user, conta, categoria, *, vencimento, parcelas=1, recorrente=False, destino=None):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description="Original",
            entry_amount=Decimal("100.00"),
            installments=parcelas,
            due_date=vencimento,
            is_recurring=recorrente,
            status=STATUS_PROJECTED,
            counterparty_account_id=destino.id if destino else None,
        ),
        user=user,
    )


def _editar_so_a_descricao(user, linha, destino=None, escopo=OPERATION_SCOPE_ALL, vencimento=None):
    """O que o formulário envia: os campos da própria linha, descrição trocada."""
    requisicao = services.TransactionRequest(
        account_id=linha.account_id,
        category_id=linha.category_id,
        entry_type=linha.entry_type,
        description="Descrição corrigida",
        entry_amount=linha.entry_amount,
        installments=linha.installments,
        due_date=vencimento or linha.due_date,
        is_recurring=linha.is_recurring,
        status=linha.status,
        realized_date=linha.realized_date,
        realized_amount=linha.realized_amount,
        counterparty_account_id=destino.id if destino else None,
    )
    token = (
        services.current_future_confirmation_token(linha.id)
        if escopo == OPERATION_SCOPE_CURRENT_FUTURE
        else None
    )
    return services.update_transaction_operation(linha, requisicao, escopo, token, user=user)


def _vencimentos(operacao_id):
    """(id, conta, vencimento) de cada linha, para comparar antes e depois."""
    return list(
        CashFlowEntry.objects.filter(bank_operation_id=operacao_id)
        .order_by("id")
        .values_list("id", "account_id", "due_date")
    )


def _datas(operacao_id):
    """Só as datas, por conta: "este e os próximos" recria as linhas com ids novos."""
    return sorted(
        CashFlowEntry.objects.filter(bank_operation_id=operacao_id).values_list("account_id", "due_date")
    )


def _linhas_da_origem(entradas):
    return sorted((e for e in entradas if e.source_entry_id is None), key=lambda e: e.due_date)


# --- Editar uma ocorrência que não é a primeira ------------------------------


def test_parcelado_editado_pela_segunda_parcela_mantem_os_vencimentos(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), parcelas=6)
    antes = _vencimentos(parcelas[0].bank_operation_id)
    assert [v for _i, _c, v in antes] == [date(2026, m, 3) for m in range(6, 12)]

    _editar_so_a_descricao(user, parcelas[1])

    assert _vencimentos(parcelas[0].bank_operation_id) == antes
    # A edição chegou ao grupo inteiro: o escopo foi mesmo "todos".
    assert set(
        CashFlowEntry.objects.filter(bank_operation_id=parcelas[0].bank_operation_id)
        .values_list("description", flat=True)
    ) == {"Descrição corrigida"}


def test_recorrente_editado_pela_terceira_ocorrencia_mantem_os_vencimentos(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    assert len(ocorrencias) >= 4
    antes = _vencimentos(ocorrencias[0].bank_operation_id)

    _editar_so_a_descricao(user, ocorrencias[2])

    assert _vencimentos(ocorrencias[0].bank_operation_id) == antes


def test_transferencia_parcelada_editada_pela_segunda_dupla_mantem_os_vencimentos(cenario):
    user, corrente, poupanca, _mercado, transferencia = cenario
    entradas = _criar(
        user, corrente, transferencia, vencimento=date(2026, 6, 3), parcelas=6, destino=poupanca
    )
    origens = _linhas_da_origem(entradas)
    antes = _vencimentos(entradas[0].bank_operation_id)

    _editar_so_a_descricao(user, origens[1], destino=poupanca)

    assert _vencimentos(entradas[0].bank_operation_id) == antes


def test_transferencia_recorrente_editada_pela_terceira_dupla_mantem_os_vencimentos(cenario):
    user, corrente, poupanca, _mercado, transferencia = cenario
    entradas = _criar(
        user, corrente, transferencia, vencimento=date(2026, 6, 3), recorrente=True, destino=poupanca
    )
    origens = _linhas_da_origem(entradas)
    assert len(origens) >= 4
    antes = _vencimentos(entradas[0].bank_operation_id)

    _editar_so_a_descricao(user, origens[2], destino=poupanca)

    assert _vencimentos(entradas[0].bank_operation_id) == antes


def test_parcelado_no_dia_31_editado_pela_parcela_de_fevereiro_mantem_os_vencimentos(cenario):
    """Fevereiro é o 28 de um grupo do dia 31: ancorar o grupo na linha editada
    e voltar um mês daria 28/01, e o grupo inteiro passaria para o dia 28."""
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 1, 31), parcelas=4)
    antes = _vencimentos(parcelas[0].bank_operation_id)
    assert [v for _i, _c, v in antes] == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)
    ]

    _editar_so_a_descricao(user, parcelas[1])

    assert _vencimentos(parcelas[0].bank_operation_id) == antes


# --- Lacuna no meio do grupo ---------------------------------------------------


def test_recorrente_com_ocorrencia_excluida_nao_fecha_a_lacuna_ao_editar_o_grupo(cenario):
    """A projeção não recria a ocorrência removida; a edição também não pode
    puxar as seguintes para o lugar dela -- nem editando a primeira."""
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    services.delete_transaction_or_operation(ocorrencias[1], OPERATION_SCOPE_SINGLE, user=user)
    antes = _vencimentos(ocorrencias[0].bank_operation_id)
    assert date(2026, 7, 3) not in [v for _i, _c, v in antes]

    _editar_so_a_descricao(user, CashFlowEntry.objects.get(id=ocorrencias[0].id))

    assert _vencimentos(ocorrencias[0].bank_operation_id) == antes


def test_ajuste_de_dia_em_uma_ocorrencia_sobrevive_a_edicao_do_grupo(cenario):
    """Vencimento que caiu em fim de semana é adiantado ou adiado a mão, uma
    ocorrência de cada vez. Recalcular o grupo por posição uniformiza o dia e
    apaga esses ajustes -- mesmo editando a primeira ocorrência."""
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    segunda = CashFlowEntry.objects.get(id=ocorrencias[1].id)
    segunda.due_date = date(2026, 7, 6)
    segunda.save(update_fields=["due_date", "updated_at"])
    antes = _vencimentos(ocorrencias[0].bank_operation_id)

    _editar_so_a_descricao(user, CashFlowEntry.objects.get(id=ocorrencias[0].id))

    assert _vencimentos(ocorrencias[0].bank_operation_id) == antes


def test_parcelado_com_parcela_excluida_nao_fecha_a_lacuna_ao_editar_o_grupo(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), parcelas=6)
    services.delete_transaction_or_operation(parcelas[2], OPERATION_SCOPE_SINGLE, user=user)
    antes = _vencimentos(parcelas[0].bank_operation_id)
    assert [v for _i, _c, v in antes] == [
        date(2026, 6, 3), date(2026, 7, 3), date(2026, 9, 3), date(2026, 10, 3), date(2026, 11, 3)
    ]

    _editar_so_a_descricao(user, CashFlowEntry.objects.get(id=parcelas[0].id))

    assert _vencimentos(parcelas[0].bank_operation_id) == antes


# --- "Este registro e os próximos" -------------------------------------------
#
# O bloco é apagado e recriado, para renumerar as parcelas de uma vez. Os ids
# mudam, por isso a comparação aqui é só de datas. O que não pode mudar é o
# mesmo: o que ninguém mexeu.


def test_este_e_os_proximos_sem_mudar_a_data_nao_move_o_bloco(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), parcelas=6)
    antes = _datas(parcelas[0].bank_operation_id)

    _editar_so_a_descricao(user, parcelas[2], escopo=OPERATION_SCOPE_CURRENT_FUTURE)

    assert _datas(parcelas[0].bank_operation_id) == antes


def test_este_e_os_proximos_preserva_ajuste_de_dia_dentro_do_bloco(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    quarta = CashFlowEntry.objects.get(id=ocorrencias[3].id)
    quarta.due_date = date(2026, 9, 6)
    quarta.save(update_fields=["due_date", "updated_at"])
    antes = _datas(ocorrencias[0].bank_operation_id)

    _editar_so_a_descricao(
        user, CashFlowEntry.objects.get(id=ocorrencias[2].id), escopo=OPERATION_SCOPE_CURRENT_FUTURE
    )

    assert _datas(ocorrencias[0].bank_operation_id) == antes


def test_este_e_os_proximos_preserva_lacuna_dentro_do_bloco(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    services.delete_transaction_or_operation(ocorrencias[3], OPERATION_SCOPE_SINGLE, user=user)
    antes = _datas(ocorrencias[0].bank_operation_id)
    assert (corrente.id, date(2026, 9, 3)) not in antes

    _editar_so_a_descricao(
        user, CashFlowEntry.objects.get(id=ocorrencias[2].id), escopo=OPERATION_SCOPE_CURRENT_FUTURE
    )

    assert _datas(ocorrencias[0].bank_operation_id) == antes


def test_este_e_os_proximos_nao_move_a_transferencia_recorrente(cenario):
    """A transferência não passa pela reconstrução do bloco: é o outro caminho."""
    user, corrente, poupanca, _mercado, transferencia = cenario
    entradas = _criar(
        user, corrente, transferencia, vencimento=date(2026, 6, 3), recorrente=True, destino=poupanca
    )
    origens = _linhas_da_origem(entradas)
    antes = _vencimentos(entradas[0].bank_operation_id)

    _editar_so_a_descricao(
        user, origens[2], destino=poupanca, escopo=OPERATION_SCOPE_CURRENT_FUTURE
    )

    assert _vencimentos(entradas[0].bank_operation_id) == antes


# --- Quando o vencimento muda mesmo -------------------------------------------
#
# A regra decidida em 17/09/2026: o grupo anda a diferença entre o vencimento
# novo e o antigo DA LINHA EDITADA, aplicada à data de cada ocorrência.


def test_adiar_um_mes_pela_segunda_parcela_adia_o_grupo_um_mes(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), parcelas=6)

    _editar_so_a_descricao(user, parcelas[1], vencimento=date(2026, 8, 3))

    assert [v for _i, _c, v in _vencimentos(parcelas[0].bank_operation_id)] == [
        date(2026, m, 3) for m in range(7, 13)
    ]


def test_mudar_so_o_dia_leva_o_grupo_ao_novo_dia_sem_mudar_de_mes(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), parcelas=6)

    _editar_so_a_descricao(user, parcelas[1], vencimento=date(2026, 7, 10))

    assert [v for _i, _c, v in _vencimentos(parcelas[0].bank_operation_id)] == [
        date(2026, m, 10) for m in range(6, 12)
    ]


def test_adiar_o_grupo_preserva_o_dia_ajustado_das_outras_ocorrencias(cenario):
    """Adiar a série é mexer no mês; o dia de cada ocorrência continua sendo dela."""
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    segunda = CashFlowEntry.objects.get(id=ocorrencias[1].id)
    segunda.due_date = date(2026, 7, 6)
    segunda.save(update_fields=["due_date", "updated_at"])

    _editar_so_a_descricao(
        user, CashFlowEntry.objects.get(id=ocorrencias[2].id), vencimento=date(2026, 9, 3)
    )

    datas = [v for _c, v in _datas(ocorrencias[0].bank_operation_id)]
    assert datas[:3] == [date(2026, 7, 3), date(2026, 8, 6), date(2026, 9, 3)]


def test_adiar_o_grupo_preserva_a_lacuna(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    ocorrencias = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), recorrente=True)
    services.delete_transaction_or_operation(ocorrencias[1], OPERATION_SCOPE_SINGLE, user=user)

    _editar_so_a_descricao(
        user, CashFlowEntry.objects.get(id=ocorrencias[2].id), vencimento=date(2026, 9, 3)
    )

    datas = [v for _c, v in _datas(ocorrencias[0].bank_operation_id)]
    # Junho virou julho, agosto continua sem ocorrência, e o resto seguiu junto.
    assert datas[:3] == [date(2026, 7, 3), date(2026, 9, 3), date(2026, 10, 3)]


def test_adiar_a_transferencia_parcelada_move_as_duas_pontas(cenario):
    user, corrente, poupanca, _mercado, transferencia = cenario
    entradas = _criar(
        user, corrente, transferencia, vencimento=date(2026, 6, 3), parcelas=6, destino=poupanca
    )
    origens = _linhas_da_origem(entradas)

    _editar_so_a_descricao(user, origens[1], destino=poupanca, vencimento=date(2026, 8, 3))

    for conta in (corrente.id, poupanca.id):
        assert [v for c, v in _datas(entradas[0].bank_operation_id) if c == conta] == [
            date(2026, m, 3) for m in range(7, 13)
        ]


def test_este_e_os_proximos_move_so_o_bloco(cenario):
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 6, 3), parcelas=6)

    _editar_so_a_descricao(
        user, parcelas[2], escopo=OPERATION_SCOPE_CURRENT_FUTURE, vencimento=date(2026, 9, 10)
    )

    datas = [v for _c, v in _datas(parcelas[0].bank_operation_id)]
    assert datas[:2] == [date(2026, 6, 3), date(2026, 7, 3)]
    assert datas[2:] == [date(2026, m, 10) for m in range(9, 13)]


def test_adiar_um_grupo_do_dia_31_respeita_o_tamanho_de_cada_mes(cenario):
    """Fevereiro já entrou aparado em 28, e é o 28 que viaja: o sistema desloca
    a data que existe, não a intenção de "todo dia 31" que ela não guarda."""
    user, corrente, _poupanca, mercado, _transferencia = cenario
    parcelas = _criar(user, corrente, mercado, vencimento=date(2026, 1, 31), parcelas=4)

    _editar_so_a_descricao(user, parcelas[0], vencimento=date(2026, 3, 31))

    assert [v for _i, _c, v in _vencimentos(parcelas[0].bank_operation_id)] == [
        date(2026, 3, 31), date(2026, 4, 28), date(2026, 5, 31), date(2026, 6, 30)
    ]
