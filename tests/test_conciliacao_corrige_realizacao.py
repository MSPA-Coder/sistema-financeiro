"""Conciliar com lançamento já realizado em outra data ou por outro valor.

O extrato é o fato: escolher o lançamento e conciliar troca a data e o valor
realizados pelos da linha. Antes era recusado ("Movimento já realizado com data
ou valor diferente da linha de extrato"), e o usuário ficava sem saída a não ser
desfazer a realização à mão. Mês fechado só pelo botão da linha, e só se o saldo
de fechamento não mudar.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements import extrato
from bank_statements.models import (
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    BankStatementImport,
    BankStatementLine,
)
from bank_statements.reconciliation import bulk_reconcile_lines, reconcile_line_with_entry
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, STATUS_REALIZED
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions import services
from transactions.models import AccountMonthClose, CashFlowCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(
        username="operador-correcao", password="senha-segura", user_type=USER_TYPE_ADMINISTRATOR
    )
    owner = AccountOwner.objects.create(name="Titular")
    banco = FinancialInstitution.objects.create(institution_name="Banco dos testes", institution_type="Banco")
    conta = FinancialAccount.objects.create(owner=owner, institution=banco, account_name="Corrente")
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    categoria = CashFlowCategory.objects.create(category_name="Contas")
    return user, conta, categoria


def _despesa_realizada(user, conta, categoria, *, vencimento, realizado_em, valor="100.00", realizado_por=None):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_EXPENSE,
            description="Conta de luz", entry_amount=Decimal(valor), installments=1, due_date=vencimento,
            status=STATUS_REALIZED, realized_date=realizado_em,
            realized_amount=Decimal(realizado_por or valor),
        ),
        user=user,
    )[0]


def _linha(conta, *, data, valor):
    lote = BankStatementImport.objects.create(account=conta, source_filename="extrato.ofx", row_count=1)
    return BankStatementLine.objects.create(
        import_batch=lote, account=conta, statement_date=data, description="PAGTO CONTA",
        amount=valor, line_hash=f"hash-{conta.id}-{data.isoformat()}-{valor}",
    )


def test_corrige_a_data_realizada_pela_do_extrato(cenario):
    user, conta, categoria = cenario
    lancamento = _despesa_realizada(user, conta, categoria, vencimento=date(2026, 9, 4), realizado_em=date(2026, 9, 4))
    linha = _linha(conta, data=date(2026, 9, 1), valor=Decimal("-100.00"))

    resultado = reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id)

    lancamento.refresh_from_db()
    assert lancamento.realized_date == date(2026, 9, 1)
    assert lancamento.status == STATUS_REALIZED
    assert resultado.status == LINE_STATUS_RECONCILED
    assert "04/09/2026 para 01/09/2026" in resultado.correcao


def test_corrige_o_valor_realizado_e_mantem_o_previsto(cenario):
    user, conta, categoria = cenario
    lancamento = _despesa_realizada(
        user, conta, categoria, vencimento=date(2026, 9, 4), realizado_em=date(2026, 9, 4), realizado_por="98.00"
    )
    linha = _linha(conta, data=date(2026, 9, 4), valor=Decimal("-100.00"))

    reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id)

    lancamento.refresh_from_db()
    assert lancamento.realized_amount == Decimal("100.00")
    assert lancamento.entry_amount == Decimal("100.00")


def test_mes_fechado_so_pelo_botao_da_linha_e_sem_mudar_o_saldo(cenario):
    user, conta, categoria = cenario
    lancamento = _despesa_realizada(user, conta, categoria, vencimento=date(2026, 9, 4), realizado_em=date(2026, 9, 4))
    services.close_month(conta, 2026, 9, None, user)
    saldo_antes = AccountMonthClose.objects.get(account=conta, year=2026, month=9).closing_balance
    linha = _linha(conta, data=date(2026, 9, 1), valor=Decimal("-100.00"))

    with pytest.raises(ValueError, match="botão Conciliar"):
        reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id)

    reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id, autorizar_meses=True)

    lancamento.refresh_from_db()
    fechamento = AccountMonthClose.objects.get(account=conta, year=2026, month=9)
    assert lancamento.realized_date == date(2026, 9, 1)
    assert fechamento.active is True
    assert fechamento.closing_balance == saldo_antes


def test_mes_fechado_recusa_quando_o_saldo_de_fechamento_mudaria(cenario):
    user, conta, categoria = cenario
    lancamento = _despesa_realizada(user, conta, categoria, vencimento=date(2026, 9, 30), realizado_em=date(2026, 10, 1))
    services.close_month(conta, 2026, 9, None, user)
    linha = _linha(conta, data=date(2026, 9, 30), valor=Decimal("-100.00"))

    with pytest.raises(ValueError, match="saldo de fechamento"):
        reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id, autorizar_meses=True)

    lancamento.refresh_from_db()
    linha.refresh_from_db()
    assert lancamento.realized_date == date(2026, 10, 1)
    assert linha.status == LINE_STATUS_NEW
    assert AccountMonthClose.objects.get(account=conta, year=2026, month=9).active is True


def test_conciliar_selecionadas_tambem_corrige_fora_de_mes_fechado(cenario):
    user, conta, categoria = cenario
    lancamento = _despesa_realizada(user, conta, categoria, vencimento=date(2026, 9, 4), realizado_em=date(2026, 9, 4))
    linha = _linha(conta, data=date(2026, 9, 2), valor=Decimal("-100.00"))

    conciliadas, erros = bulk_reconcile_lines(user, line_ids=[linha.id])

    lancamento.refresh_from_db()
    assert (conciliadas, erros) == (1, [])
    assert lancamento.realized_date == date(2026, 9, 2)


def test_sugestao_avisa_que_corrige_a_realizacao(cenario):
    user, conta, categoria = cenario
    _despesa_realizada(user, conta, categoria, vencimento=date(2026, 9, 4), realizado_em=date(2026, 9, 4))
    linha = _linha(conta, data=date(2026, 9, 1), valor=Decimal("-100.00"))

    [plano] = extrato.planejar(user, [linha])

    assert plano.acao == extrato.CONCILIA
    assert "corrige a realização: data de 04/09/2026 para 01/09/2026" in plano.rotulo


def test_realizado_por_outro_valor_e_candidato_pelo_valor_realizado(cenario):
    user, conta, categoria = cenario
    lancamento = _despesa_realizada(
        user, conta, categoria, vencimento=date(2026, 4, 6), realizado_em=date(2026, 4, 6),
        valor="495.00", realizado_por="495.34",
    )
    linha = _linha(conta, data=date(2026, 4, 6), valor=Decimal("-495.34"))

    [plano] = extrato.planejar(user, [linha])
    assert plano.acao == extrato.CONCILIA and plano.lancamento.id == lancamento.id

    reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id)
    lancamento.refresh_from_db()
    linha.refresh_from_db()
    assert linha.status == LINE_STATUS_RECONCILED
    assert (lancamento.entry_amount, lancamento.realized_amount) == (Decimal("495.00"), Decimal("495.34"))


def test_dois_candidatos_iguais_no_mes_desempatam_pela_data_da_linha(cenario):
    user, conta, categoria = cenario
    dia_19 = _despesa_realizada(user, conta, categoria, vencimento=date(2026, 3, 19), realizado_em=date(2026, 3, 19))
    dia_23 = _despesa_realizada(user, conta, categoria, vencimento=date(2026, 3, 23), realizado_em=date(2026, 3, 23))
    linha_19 = _linha(conta, data=date(2026, 3, 19), valor=Decimal("-100.00"))
    linha_23 = _linha(conta, data=date(2026, 3, 23), valor=Decimal("-100.00"))
    linha_25 = _linha(conta, data=date(2026, 3, 25), valor=Decimal("-100.00"))

    planos = {plano.linha.id: plano for plano in extrato.planejar(user, [linha_19, linha_23, linha_25])}

    assert planos[linha_19.id].lancamento == dia_19
    assert planos[linha_23.id].lancamento == dia_23
    assert planos[linha_25.id].acao == extrato.AMBIGUA  # nenhum no dia: continua manual
