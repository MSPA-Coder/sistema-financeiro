"""Excluir um lançamento conciliado não pode travar sem feedback.

O DEFEITO (02/10/2026)

`BankStatementLine.matched_entry` usa `on_delete=PROTECT`: apagar um
`CashFlowEntry` ainda referenciado por uma linha de extrato conciliada
levanta `django.db.models.deletion.ProtectedError`. `delete_transaction_or_
operation` não tratava essa exceção, que subia crua até a view como erro
500 -- o modal "Confirmar exclusão" ficava sem nenhum retorno visível.

O QUE ESTE ARQUIVO FIXA

`ProtectedError` vira `ValueError` com mensagem explicando o motivo, igual
já acontecia para categoria com lançamentos (`delete_category`).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements.models import BankStatementImport, BankStatementLine
from bank_statements.reconciliation import reconcile_line_with_entry
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, OPERATION_SCOPE_ALL, STATUS_PROJECTED
from transactions import services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(username="operador-exclusao", password="senha-segura")
    owner = AccountOwner.objects.create(name="Titular")
    banco = FinancialInstitution.objects.create(
        institution_name="Banco dos testes", institution_type="Banco"
    )
    conta = FinancialAccount.objects.create(owner=owner, institution=banco, account_name="Corrente")
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    categoria = CashFlowCategory.objects.create(category_name="Mercado")
    return user, conta, categoria


def test_excluir_lancamento_conciliado_recusa_com_mensagem_clara(cenario):
    user, conta, categoria = cenario
    lancamento = services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description="Mercado",
            entry_amount=Decimal("100.00"),
            installments=1,
            due_date=date(2026, 9, 10),
            status=STATUS_PROJECTED,
        ),
        user=user,
    )[0]
    lote = BankStatementImport.objects.create(
        account=conta, source_filename="extrato.ofx", row_count=1
    )
    linha = BankStatementLine.objects.create(
        import_batch=lote, account=conta, statement_date=date(2026, 9, 10),
        description="Mercado", amount=Decimal("-100.00"), line_hash="hash-exclusao-teste",
    )
    reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id)

    with pytest.raises(ValueError, match="conciliado"):
        services.delete_transaction_or_operation(lancamento, OPERATION_SCOPE_ALL, user=user)

    # Nem a linha de extrato nem o lançamento sumiram: a recusa é limpa.
    assert CashFlowEntry.objects.filter(id=lancamento.id).exists()
    linha.refresh_from_db()
    assert linha.matched_entry_id == lancamento.id


def test_excluir_lancamento_nao_conciliado_continua_funcionando(cenario):
    user, conta, categoria = cenario
    lancamento = services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description="Mercado",
            entry_amount=Decimal("100.00"),
            installments=1,
            due_date=date(2026, 9, 10),
            status=STATUS_PROJECTED,
        ),
        user=user,
    )[0]

    services.delete_transaction_or_operation(lancamento, OPERATION_SCOPE_ALL, user=user)

    assert not CashFlowEntry.objects.filter(id=lancamento.id).exists()
