"""Conversão dos pares próprios (receita e despesa entre contas do mesmo titular)
em transferência.

O que fica fixado: a conversão muda só a natureza (categoria, operação interna e
vínculo entre as pontas), nunca valor, data, conta nem a conciliação com o
extrato, e por isso nenhum saldo; mês fechado só é atravessado com autorização,
permissão e saldo de fechamento idêntico; par ambíguo, sem indício, de moedas
diferentes ou já transferência não é tocado; e a simulação não grava nada.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements import pares_proprios
from bank_statements.models import BankStatementImport, BankStatementLine
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    STATUS_REALIZED,
    VIEW_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR, USER_TYPE_USER
from core.models import AuditLog
from reports.services import decimal_balance_before
from transactions import services
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def mundo():
    admin = AppUser.objects.create_user(
        username="admin-pares", password="senha-segura", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Mariano")
    UserOwnerAccess.objects.create(
        user=admin, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    genial = FinancialInstitution.objects.create(institution_name="Genial", institution_type="Corretora")
    c6 = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    contas = {
        "genial": FinancialAccount.objects.create(owner=titular, institution=genial, account_name="Conta 03"),
        "c6": FinancialAccount.objects.create(owner=titular, institution=c6, account_name="Conta 02"),
    }
    save_transfer_destination_accesses(admin, {conta.id for conta in contas.values()})
    categorias = {
        "outros": CashFlowCategory.objects.create(category_name="Outros"),
        "transferencia": CashFlowCategory.objects.create(
            category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER
        ),
    }
    return admin, contas, categorias


def _lancar(user, conta, categoria, descricao, valor, dia, tipo):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=tipo, description=descricao,
            entry_amount=Decimal(valor), installments=1, due_date=dia, status=STATUS_REALIZED,
            realized_date=dia, realized_amount=Decimal(valor),
        ),
        user=user,
    )[0]


def _par(mundo, dia=date(2026, 9, 24), valor="200.00"):
    admin, contas, cat = mundo
    saida = _lancar(admin, contas["genial"], cat["outros"], "Pix - Para Mariano Sergio Pacheco de Angelo",
                    valor, dia, ENTRY_TYPE_EXPENSE)
    entrada = _lancar(admin, contas["c6"], cat["outros"], "Pix recebido de MARIANO SERGIO PACHECO DE ANGELO",
                      valor, dia, ENTRY_TYPE_INCOME)
    return saida, entrada


def _saldos(contas):
    return {nome: decimal_balance_before([conta.id], date(2027, 1, 1), VIEW_REALIZED) for nome, conta in contas.items()}


def test_par_proprio_vira_transferencia_sem_mudar_saldo_valor_data_nem_conciliacao(mundo):
    admin, contas, cat = mundo
    saida, entrada = _par(mundo)
    lote = BankStatementImport.objects.create(account=contas["genial"], source_filename="g.pdf", row_count=1)
    linha = BankStatementLine.objects.create(
        import_batch=lote, account=contas["genial"], statement_date=date(2026, 9, 24),
        description="Pix - Para Mariano", amount=Decimal("-200.00"), line_hash="h1",
        status="conciliado", matched_entry=saida,
    )
    saldos_antes = _saldos(contas)

    pares = pares_proprios.encontrar_pares(admin)
    assert [(p.saida.id, p.entrada.id) for p in pares] == [(saida.id, entrada.id)]
    assert pares_proprios.aplicar(admin, pares) == 1

    saida.refresh_from_db()
    entrada.refresh_from_db()
    linha.refresh_from_db()
    for ponta in (saida, entrada):
        assert ponta.category == cat["transferencia"]
        assert ponta.operation_type == OPERATION_INTERNAL_TRANSFER
        assert ponta.entry_amount == Decimal("200.00") and ponta.realized_date == date(2026, 9, 24)
    assert entrada.source_entry_id == saida.id
    assert saida.bank_operation_id == entrada.bank_operation_id
    assert saida.description.startswith("Conta Destino:") and entrada.description.startswith("Conta Origem:")
    assert linha.matched_entry_id == saida.id and linha.status == "conciliado"
    assert _saldos(contas) == saldos_antes
    assert AuditLog.objects.filter(entity_id=saida.id, summary__contains="convertido em transferência").exists()
    # Convertido, não aparece mais como par.
    assert pares_proprios.encontrar_pares(admin) == []


def test_simulacao_pelo_comando_nao_grava_e_aplicar_grava(mundo):
    admin, contas, cat = mundo
    saida, _ = _par(mundo)
    saida_texto = StringIO()
    call_command("converter_pares_proprios", "--usuario", admin.username, stdout=saida_texto)
    assert "1 par(es)" in saida_texto.getvalue() and "Simulação" in saida_texto.getvalue()
    saida.refresh_from_db()
    assert saida.category == cat["outros"]

    call_command("converter_pares_proprios", "--usuario", admin.username, "--aplicar", stdout=StringIO())
    saida.refresh_from_db()
    assert saida.category == cat["transferencia"]


def test_sem_indicio_ambiguo_moeda_diferente_ou_ja_transferencia_nao_e_par(mundo):
    admin, contas, cat = mundo
    # Mesmo valor e dia, mas sem nome de titular nem cara de transferência.
    _lancar(admin, contas["genial"], cat["outros"], "Mercado Alfa", "28.00", date(2026, 9, 1), ENTRY_TYPE_EXPENSE)
    _lancar(admin, contas["c6"], cat["outros"], "Estorno Beta", "28.00", date(2026, 9, 1), ENTRY_TYPE_INCOME)
    assert pares_proprios.encontrar_pares(admin) == []

    # Duas entradas candidatas para uma saída: ambíguo.
    _lancar(admin, contas["genial"], cat["outros"], "Pix para Mariano", "50.00", date(2026, 9, 2), ENTRY_TYPE_EXPENSE)
    _lancar(admin, contas["c6"], cat["outros"], "Pix de Mariano", "50.00", date(2026, 9, 2), ENTRY_TYPE_INCOME)
    outra = FinancialAccount.objects.create(
        owner=contas["c6"].owner, institution=contas["c6"].institution, account_name="Conta 09"
    )
    save_transfer_destination_accesses(admin, {c.id for c in FinancialAccount.objects.all()})
    _lancar(admin, outra, cat["outros"], "Pix de Mariano", "50.00", date(2026, 9, 2), ENTRY_TYPE_INCOME)
    assert pares_proprios.encontrar_pares(admin) == []

    # Moeda diferente.
    dolar = FinancialAccount.objects.create(
        owner=contas["c6"].owner, institution=contas["c6"].institution, account_name="Dólar", currency="USD"
    )
    save_transfer_destination_accesses(admin, {c.id for c in FinancialAccount.objects.all()})
    _lancar(admin, contas["genial"], cat["outros"], "Pix para Mariano", "70.00", date(2026, 9, 3), ENTRY_TYPE_EXPENSE)
    _lancar(admin, dolar, cat["outros"], "Pix de Mariano", "70.00", date(2026, 9, 3), ENTRY_TYPE_INCOME)
    assert pares_proprios.encontrar_pares(admin) == []


def test_mes_fechado_exige_autorizacao_e_mantem_o_saldo_de_fechamento(mundo):
    admin, contas, cat = mundo
    saida, entrada = _par(mundo, dia=date(2026, 7, 10))
    services.close_month(contas["genial"], 2026, 7, None, admin)
    services.close_month(contas["c6"], 2026, 7, None, admin)
    fechamentos = {
        c.id: AccountMonthClose.objects.get(account=c, year=2026, month=7, active=True).closing_balance
        for c in contas.values()
    }
    pares = pares_proprios.encontrar_pares(admin)
    assert len(pares) == 1

    with pytest.raises(ValueError, match="meses fechados"):
        pares_proprios.aplicar(admin, pares)
    saida.refresh_from_db()
    assert saida.category == cat["outros"]

    assert pares_proprios.aplicar(admin, pares, autorizar_meses=True) == 1
    for conta in contas.values():
        fechamento = AccountMonthClose.objects.get(account=conta, year=2026, month=7, active=True)
        assert fechamento.closing_balance == fechamentos[conta.id]
    saida.refresh_from_db()
    assert saida.category == cat["transferencia"]


def test_sem_permissao_de_fechamento_nao_reabre_mes(mundo):
    admin, contas, cat = mundo
    # O padrão do modelo é administrador, que tem toda permissão.
    comum = AppUser.objects.create_user(username="comum-pares", password="senha-segura", user_type=USER_TYPE_USER)
    for dono in {c.owner for c in contas.values()}:
        UserOwnerAccess.objects.create(
            user=comum, owner=dono, can_view=True, can_create=True, can_update=True, can_delete=True
        )
    save_transfer_destination_accesses(comum, {c.id for c in contas.values()})
    _par(mundo, dia=date(2026, 7, 10))
    services.close_month(contas["genial"], 2026, 7, None, admin)
    pares = pares_proprios.encontrar_pares(comum)
    assert len(pares) == 1
    with pytest.raises(ValueError, match="permissão de fechamento"):
        pares_proprios.aplicar(comum, pares, autorizar_meses=True)
    assert CashFlowEntry.objects.filter(category=cat["transferencia"]).count() == 0
