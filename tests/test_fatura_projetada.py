"""Fatura projetada: parcelas contratadas mais o gasto novo estimado.

O cenário tem três faturas importadas de um cartão que fecha dia 19 e vence
dia 25, todas com uma compra à vista de 1.000 no começo do ciclo. A de agosto
traz ainda uma TV em 3x de 300 (1/3); a de setembro, a 2/3. A 3/3 já é um
lançamento futuro do cartão, em 05/10. Com isso:

- gasto novo a 1 fechamento: 1.000, 1.300 e 1.000 -> mediana 1.000
  (a 2/3 de setembro já era conhecida desde agosto);
- a 2 e a 3 fechamentos: 1.000, 1.300 e 1.300 -> mediana 1.300
  (visto de dois meses antes, a TV ainda não tinha sido comprada).

As próximas faturas vencem em 25/10 (parcela de 300 + 1.000), 25/11 (1.300) e
25/12 (1.300); a de 25/01 passa do horizonte de 3 meses a partir de 23/09.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import Client

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements import fatura_projetada
from bank_statements.fatura import PREFIXO_ESTIMATIVA, PREFIXO_PAGAMENTO
from bank_statements.models import BankStatementImport, BankStatementLine
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.recurring_projection import (
    ensure_recurring_projection_horizon,
    recurring_projection_horizon_end,
)
from transactions.services import TransactionRequest, create_transaction_batch

HOJE = date(2026, 9, 23)
FIM = recurring_projection_horizon_end(HOJE, 3)


def _linha(dia, descricao, valor, parcela=None):
    return SimpleNamespace(
        statement_date=dia, description=descricao, amount=Decimal(valor),
        installment_current=parcela[0] if parcela else None,
        installment_total=parcela[1] if parcela else None,
    )


# --- Gasto novo (sem banco) ----------------------------------------------------


def test_gasto_novo_cresce_com_a_distancia_e_ignora_pagamento_e_estorno():
    linhas = [
        _linha(date(2026, 9, 1), "MERCADO", "-1000.00"),
        _linha(date(2026, 9, 5), "TV", "-300.00", (2, 3)),
        _linha(date(2026, 8, 25), "Pagamento fatura", "900.00"),
        _linha(date(2026, 9, 6), "Estorno", "50.00"),
    ]
    fechamento = date(2026, 9, 19)
    assert fatura_projetada.gasto_novo(linhas, fechamento, 1) == Decimal("1000.00")
    assert fatura_projetada.gasto_novo(linhas, fechamento, 2) == Decimal("1300.00")


def test_parcela_com_a_data_do_lancamento_conta_como_antiga():
    # A anuidade da C6 vem com a data do próprio lançamento: a 4/12 lançada
    # em 13/09 é conhecida desde junho, e não é gasto novo.
    linhas = [_linha(date(2026, 9, 13), "Anuidade", "-98.00", (4, 12))]
    assert fatura_projetada.gasto_novo(linhas, date(2026, 9, 19), 2) == Decimal("0.00")


def test_vencimento_depois_do_fechamento():
    assert fatura_projetada.vencimento_da_fatura(date(2026, 10, 19), 25) == date(2026, 10, 25)
    assert fatura_projetada.vencimento_da_fatura(date(2026, 10, 1), 10) == date(2026, 10, 10)
    assert fatura_projetada.vencimento_da_fatura(date(2026, 10, 29), 5) == date(2026, 11, 5)


# --- Com banco -------------------------------------------------------------------


@pytest.fixture
def cenario(db):
    usuario = AppUser.objects.create_user(
        username="projecao-cartao", password="troca-esta-senha", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco teste", institution_type="Banco")
    corrente = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Corrente")
    cartao = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Carbon",
        account_kind=ACCOUNT_KIND_CREDIT_CARD, card_closing_day=19, card_due_day=25,
        card_payment_account=corrente, initial_balance_date=date(2026, 6, 1),
    )
    save_transfer_destination_accesses(usuario, {cartao.id})
    transferencia = CashFlowCategory.objects.create(category_name="Transf. teste", kind=CATEGORY_KIND_TRANSFER)
    tv = CashFlowCategory.objects.create(category_name="Eletrônicos teste", kind=CATEGORY_KIND_MANAGERIAL)
    CashFlowEntry.objects.create(
        account=cartao, category=tv, entry_type=ENTRY_TYPE_EXPENSE, description="TV",
        entry_amount=Decimal("300.00"), installments=3, current_installment=3,
        due_date=date(2026, 10, 5), status=STATUS_PROJECTED,
    )
    return SimpleNamespace(usuario=usuario, corrente=corrente, cartao=cartao, transferencia=transferencia)


def _importar(cartao, nome, linhas):
    lote = BankStatementImport.objects.create(account=cartao, source_filename=nome)
    for indice, linha in enumerate(linhas):
        BankStatementLine.objects.create(
            import_batch=lote, account=cartao, statement_date=linha.statement_date,
            description=linha.description, amount=linha.amount, line_hash=f"{nome}-{indice}",
            installment_current=linha.installment_current, installment_total=linha.installment_total,
        )


def _tres_faturas(cartao, quantas=3):
    faturas = [
        ("julho", [_linha(date(2026, 7, 1), "MERCADO", "-1000.00"),
                   _linha(date(2026, 6, 25), "Pagamento", "800.00")]),
        ("agosto", [_linha(date(2026, 8, 1), "MERCADO", "-1000.00"),
                    _linha(date(2026, 8, 5), "TV", "-300.00", (1, 3)),
                    _linha(date(2026, 7, 25), "Pagamento", "1000.00")]),
        ("setembro", [_linha(date(2026, 9, 1), "MERCADO", "-1000.00"),
                      _linha(date(2026, 9, 5), "TV", "-300.00", (2, 3)),
                      _linha(date(2026, 8, 25), "Pagamento", "1300.00")]),
    ]
    for nome, linhas in faturas[-quantas:]:
        _importar(cartao, nome, linhas)


def _estimativas(cartao):
    return list(
        CashFlowEntry.objects.filter(
            account=cartao, bank_operation__operation_key__startswith=f"{PREFIXO_ESTIMATIVA}:{cartao.id}:"
        ).order_by("due_date").values_list("due_date", "entry_amount")
    )


def _pagamentos(corrente, cartao):
    return list(
        CashFlowEntry.objects.filter(
            account=corrente, bank_operation__operation_key__startswith=f"{PREFIXO_PAGAMENTO}:{cartao.id}:"
        ).order_by("due_date").values_list("due_date", "entry_amount")
    )


@pytest.mark.django_db
def test_plano_soma_parcelas_e_mediana_por_distancia(cenario):
    _tres_faturas(cenario.cartao)

    planos = fatura_projetada.planejar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert [(p.vencimento, p.lancado, p.estimativa.valor, p.total) for p in planos] == [
        (date(2026, 10, 25), Decimal("300.00"), Decimal("1000.00"), Decimal("1300.00")),
        (date(2026, 11, 25), Decimal("0.00"), Decimal("1300.00"), Decimal("1300.00")),
        (date(2026, 12, 25), Decimal("0.00"), Decimal("1300.00"), Decimal("1300.00")),
    ]
    assert not _estimativas(cenario.cartao)


@pytest.mark.django_db
def test_atualizar_cria_estimativa_e_pagamento_e_refazer_nao_duplica(cenario):
    _tres_faturas(cenario.cartao)

    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)
    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert _estimativas(cenario.cartao) == [
        (date(2026, 10, 19), Decimal("1000.00")),
        (date(2026, 11, 19), Decimal("1300.00")),
        (date(2026, 12, 19), Decimal("1300.00")),
    ]
    assert _pagamentos(cenario.corrente, cenario.cartao) == [
        (date(2026, 10, 25), Decimal("1300.00")),
        (date(2026, 11, 25), Decimal("1300.00")),
        (date(2026, 12, 25), Decimal("1300.00")),
    ]
    chegada = CashFlowEntry.objects.get(
        account=cenario.cartao, entry_type=ENTRY_TYPE_INCOME, due_date=date(2026, 10, 25)
    )
    assert chegada.source_entry.account_id == cenario.corrente.id
    assert chegada.category_id == cenario.transferencia.id


@pytest.mark.django_db
def test_pagamento_ja_agendado_e_respeitado(cenario):
    _tres_faturas(cenario.cartao)
    create_transaction_batch(
        TransactionRequest(
            account_id=cenario.corrente.id, category_id=cenario.transferencia.id,
            entry_type=ENTRY_TYPE_EXPENSE, description="Pagamento combinado",
            entry_amount=Decimal("999.00"), installments=1, due_date=date(2026, 10, 25),
            is_recurring=False, status=STATUS_PROJECTED, counterparty_account_id=cenario.cartao.id,
        ),
        user=cenario.usuario,
    )

    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert [dia for dia, _ in _pagamentos(cenario.corrente, cenario.cartao)] == [
        date(2026, 11, 25), date(2026, 12, 25),
    ]


@pytest.mark.django_db
def test_pagamento_realizado_vira_fato_e_nao_e_refeito(cenario):
    _tres_faturas(cenario.cartao)
    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)
    outubro = CashFlowEntry.objects.filter(
        bank_operation__operation_key=f"{PREFIXO_PAGAMENTO}:{cenario.cartao.id}:2026-10-25"
    )
    outubro.update(status=STATUS_REALIZED, realized_date=date(2026, 10, 25), realized_amount=Decimal("1300.00"))

    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert [dia for dia, _ in _pagamentos(cenario.corrente, cenario.cartao)] == [
        date(2026, 11, 25), date(2026, 12, 25),
    ]
    pagas = CashFlowEntry.objects.filter(due_date=date(2026, 10, 25), status=STATUS_REALIZED)
    assert pagas.count() == 2
    assert pagas.first().bank_operation.operation_key.startswith("realizado-")


@pytest.mark.django_db
def test_sem_conta_de_pagamento_so_estima(cenario):
    _tres_faturas(cenario.cartao)
    FinancialAccount.objects.filter(id=cenario.cartao.id).update(card_payment_account=None)

    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert len(_estimativas(cenario.cartao)) == 3
    assert not _pagamentos(cenario.corrente, cenario.cartao)


@pytest.mark.django_db
def test_valor_fixado_substitui_a_mediana(cenario):
    _tres_faturas(cenario.cartao)
    FinancialAccount.objects.filter(id=cenario.cartao.id).update(card_estimated_spend=Decimal("500.00"))

    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert [valor for _, valor in _estimativas(cenario.cartao)] == [Decimal("500.00")] * 3
    assert _pagamentos(cenario.corrente, cenario.cartao)[0] == (date(2026, 10, 25), Decimal("800.00"))


@pytest.mark.django_db
def test_poucas_faturas_nao_estimam(cenario):
    _tres_faturas(cenario.cartao, quantas=2)

    fatura_projetada.atualizar(cenario.cartao, hoje=HOJE, fim=FIM)

    assert not _estimativas(cenario.cartao)
    assert _pagamentos(cenario.corrente, cenario.cartao) == [(date(2026, 10, 25), Decimal("300.00"))]


@pytest.mark.django_db
def test_execucao_da_projecao_atualiza_os_cartoes(cenario):
    _tres_faturas(cenario.cartao)

    ensure_recurring_projection_horizon(today=HOJE, horizon_months=3, update_last_run=False)

    assert len(_estimativas(cenario.cartao)) == 3
    assert BankOperation.objects.filter(operation_key__startswith=f"{PREFIXO_PAGAMENTO}:").count() == 3


@pytest.mark.django_db
def test_menu_faturas_mostra_o_cartao_as_faturas_e_a_projecao(cenario):
    _tres_faturas(cenario.cartao)
    client = Client()
    client.force_login(cenario.usuario)

    resposta = client.get("/banking/cards/")

    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert 'href="/banking/cards/"' in html
    assert "Carbon" in html
    assert "setembro" in html  # a fatura importada, com link para ela
    assert "Próximas faturas (projeção)" in html
