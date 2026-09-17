"""A categoria diz o que ela é, e são três coisas, não duas.

`is_internal` respondia sim ou não, e escondia um terceiro caso: dinheiro que
sai da conta sem mudar de dono e **sem conta de destino neste sistema** -- a
liquidação de bolsa, que vira ação e é o Renda Variável quem avalia.

Chamar isso de despesa infla a despesa do mês com dinheiro que continua sendo
seu. Chamar de transferência exigiria uma contraparte que não existe aqui, e era
exatamente essa exigência que impedia a classificação certa.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
)
from transactions import services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(username="operador-tipo", password="senha-segura")
    owner = AccountOwner.objects.create(name="Titular")
    instituicao = FinancialInstitution.objects.create(
        institution_name="Corretora dos testes", institution_type="Corretora"
    )
    conta = FinancialAccount.objects.create(
        owner=owner, institution=instituicao, account_name="Conta", initial_balance=Decimal("1000.00")
    )
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    return user, conta


def _requisicao(conta, categoria, **extras):
    campos = {
        "account_id": conta.id,
        "category_id": categoria.id,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Liquidação de bolsa",
        "entry_amount": Decimal("5000.00"),
        "installments": 1,
        "due_date": date(2026, 9, 10),
        "status": STATUS_PROJECTED,
    }
    campos.update(extras)
    return services.TransactionRequest(**campos)


# --- O terceiro caso -------------------------------------------------------


def test_movimentacao_nao_exige_conta_destino(cenario):
    """O teste que justifica o tipo novo existir.

    Com o booleano antigo, classificar a liquidação como "interna" (para tirá-la
    da despesa) obrigaria a informar uma conta destino que não existe -- o
    dinheiro virou ação, e ação não é conta deste sistema.
    """
    user, conta = cenario
    categoria = CashFlowCategory.objects.create(
        category_name="Operações em Bolsa", kind=CATEGORY_KIND_MOVEMENT
    )

    lancamentos = services.create_transaction_batch(_requisicao(conta, categoria), user=user)

    assert len(lancamentos) == 1
    assert lancamentos[0].operation_type == OPERATION_SINGLE
    assert lancamentos[0].entry_amount == Decimal("5000.00")


def test_movimentacao_nao_entra_em_receita_nem_despesa(cenario):
    """Sai do resultado, como a transferência -- mas sem contraparte."""
    user, conta = cenario
    movimentacao = CashFlowCategory.objects.create(
        category_name="Operações em Bolsa", kind=CATEGORY_KIND_MOVEMENT
    )
    gerencial = CashFlowCategory.objects.create(
        category_name="Supermercado", kind=CATEGORY_KIND_MANAGERIAL
    )
    hoje = date.today()
    services.create_transaction_batch(
        _requisicao(conta, movimentacao, due_date=hoje), user=user
    )
    services.create_transaction_batch(
        _requisicao(conta, gerencial, due_date=hoje, entry_amount=Decimal("300.00")), user=user
    )

    contexto = services.build_transactions_view_context(user, {}, {})
    bloco = contexto["blocos"][0]

    assert bloco["total_despesas"] == Decimal("300.00")
    assert bloco["total_movimentacoes_internas"] == Decimal("-5000.00")


def test_transferencia_continua_exigindo_conta_destino(cenario):
    """A trava que protege o par não pode ter sido afrouxada pelo tipo novo."""
    user, conta = cenario
    categoria = CashFlowCategory.objects.create(
        category_name="Transferência", kind=CATEGORY_KIND_TRANSFER
    )

    with pytest.raises(ValueError, match="Conta destino é obrigatória"):
        services.create_transaction_batch(_requisicao(conta, categoria), user=user)

    assert CashFlowEntry.objects.count() == 0


# --- Cadastro --------------------------------------------------------------


def test_tipo_e_obrigatorio_e_validado():
    with pytest.raises(ValueError, match="Tipo da categoria é obrigatório"):
        services.create_category("Nova", "")
    with pytest.raises(ValueError, match="Tipo de categoria inválido"):
        services.create_category("Nova", "qualquer")
    assert CashFlowCategory.objects.count() == 0


def test_categoria_com_lancamento_nao_muda_de_tipo(cenario):
    """Mudar o tipo reclassifica o passado inteiro de uma vez, inclusive meses
    fechados. Isso é reclassificação, e reclassificação tem relatório."""
    user, conta = cenario
    categoria = services.create_category("Aplicações", CATEGORY_KIND_MANAGERIAL)
    services.create_transaction_batch(_requisicao(conta, categoria), user=user)

    with pytest.raises(ValueError, match="não pode mudar de tipo"):
        services.update_category(categoria, "Aplicações", CATEGORY_KIND_MOVEMENT)

    categoria.refresh_from_db()
    assert categoria.kind == CATEGORY_KIND_MANAGERIAL


def test_categoria_sem_lancamento_pode_mudar_de_tipo():
    categoria = services.create_category("Aplicações", CATEGORY_KIND_MANAGERIAL)

    atualizada = services.update_category(categoria, "Aplicações", CATEGORY_KIND_MOVEMENT)

    assert atualizada.kind == CATEGORY_KIND_MOVEMENT


def test_banco_recusa_tipo_desconhecido():
    """A `CheckConstraint` é a rede: nem toda escrita passa pelo serviço."""
    with pytest.raises(IntegrityError), transaction.atomic():
        CashFlowCategory.objects.create(category_name="Torta", kind="inventado")


# --- O que `is_internal` virou ---------------------------------------------


@pytest.mark.parametrize(
    ("kind", "fora_do_resultado", "tem_contraparte"),
    [
        (CATEGORY_KIND_MANAGERIAL, False, False),
        (CATEGORY_KIND_TRANSFER, True, True),
        (CATEGORY_KIND_MOVEMENT, True, False),
    ],
)
def test_as_duas_perguntas_que_o_booleano_misturava(kind, fora_do_resultado, tem_contraparte):
    """`is_internal` respondia as duas de uma vez, e elas são diferentes."""
    categoria = CashFlowCategory(category_name="X", kind=kind)

    assert categoria.is_internal is fora_do_resultado
    assert categoria.requires_counterparty is tem_contraparte
