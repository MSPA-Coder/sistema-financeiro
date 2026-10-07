"""Aplicação sem extrato próprio, movimentada por uma conta corrente.

Cofrinho, CDB e Tesouro Direto não têm extrato: o dinheiro entra e sai pela
conta corrente (`FinancialAccount.movement_account`), cujo extrato traz as
transferências, e o rendimento é a diferença para o saldo informado. O que este
arquivo fixa:

- a conta de movimento só vale para aplicação, e tem de ser conta comum do mesmo
  titular e moeda;
- a regra "aplicação vinculada" leva a linha para a única aplicação da conta;
- Saldo Aplicações recusa o saldo enquanto a conta de movimento não foi
  importada no mês e conciliada até a data (a transferência que falta viraria
  rendimento);
- o mês da aplicação é conciliado quando a conta de movimento está e o saldo do
  último dia foi informado (ou ela terminou zerada e parada);
- o fechamento em lote fecha só os meses conciliados;
- os ajustes de dados criam a aplicação e convertem um avulso em transferência.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from accounts.models import AccountOwner, AppUser, UserOwnerAccess, UserTransferDestinationAccess
from bank_statements import ajustes, extrato, saldo, situacao
from bank_statements.models import (
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    RULE_ACTION_LINKED,
    RULE_SIGN_DEBIT,
    BankStatementImport,
    BankStatementLine,
    StatementRule,
)
from banking.models import FinancialAccount, FinancialInstitution
from banking.services import create_account, update_account
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    OPERATION_INTERNAL_TRANSFER,
    STATUS_REALIZED,
    VIEW_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from reports.services import decimal_balances_before_by_account
from transactions import services
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

INICIO = date(2025, 12, 31)


@pytest.fixture
def c():
    user = AppUser.objects.create_user(username="aplicacoes", password="senha-segura", user_type=USER_TYPE_ADMINISTRATOR)
    titular = AccountOwner.objects.create(name="Titular")
    outro = AccountOwner.objects.create(name="Outro")
    for dono in (titular, outro):
        UserOwnerAccess.objects.create(
            user=user, owner=dono, can_view=True, can_create=True, can_update=True, can_delete=True
        )
    banco = FinancialInstitution.objects.create(institution_name="Banco teste", institution_type="Banco")
    corrente = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Corrente", initial_balance=Decimal("1000.00"),
        initial_balance_date=INICIO,
    )
    cofrinho = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Cofrinho", account_kind=ACCOUNT_KIND_INVESTMENT,
        initial_balance=Decimal("500.00"), initial_balance_date=INICIO, movement_account=corrente,
    )
    for destino in (corrente, cofrinho):
        UserTransferDestinationAccess.objects.create(user=user, destination_account=destino)
    CashFlowCategory.objects.create(category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER)
    gerencial = CashFlowCategory.objects.create(category_name="Outros")
    CashFlowCategory.objects.create(category_name="Rendimentos")
    return SimpleNamespace(user=user, titular=titular, outro=outro, banco=banco, corrente=corrente, cofrinho=cofrinho, gerencial=gerencial)


def _linha(conta, dia, valor, descricao="PIX", status=LINE_STATUS_NEW):
    lote = BankStatementImport.objects.create(account=conta, source_filename="extrato.ofx", row_count=1)
    return BankStatementLine.objects.create(
        import_batch=lote, account=conta, statement_date=dia, description=descricao, amount=Decimal(valor),
        line_hash=f"{conta.id}-{dia}-{descricao}-{valor}-{status}", status=status,
    )


def _campos(conta, **muda):
    return {
        "owner_id": str(conta.owner_id), "institution_id": str(conta.institution_id),
        "account_name": conta.account_name, "initial_balance": str(conta.initial_balance),
        "currency": conta.currency, "initial_balance_date": conta.initial_balance_date.isoformat(),
        "account_kind": conta.account_kind, "movement_account_id": str(conta.movement_account_id or ""),
        **muda,
    }


# --- Conta de movimento -------------------------------------------------------------


def test_conta_de_movimento_so_para_aplicacao_e_conta_comum_do_mesmo_titular(c):
    cdb = create_account(c.user, **_campos(c.cofrinho, account_name="CDB", movement_account_id=str(c.corrente.id)))
    assert cdb.movement_account_id == c.corrente.id

    comum = create_account(c.user, **_campos(c.corrente, account_name="Outra corrente", movement_account_id=str(c.corrente.id)))
    assert comum.movement_account_id is None  # conta comum não tem conta de movimento

    with pytest.raises(ValueError, match="conta comum"):
        create_account(c.user, **_campos(c.cofrinho, account_name="X", movement_account_id=str(c.cofrinho.id)))
    alheia = FinancialAccount.objects.create(owner=c.outro, institution=c.banco, account_name="Dele")
    with pytest.raises(ValueError, match="mesmo titular"):
        create_account(c.user, **_campos(c.cofrinho, account_name="Y", movement_account_id=str(alheia.id)))


def test_conta_de_movimento_nao_vira_cartao_nem_aplicacao(c):
    with pytest.raises(ValueError, match="conta de movimento de uma aplicação"):
        update_account(c.user, c.corrente, **_campos(c.corrente, account_kind=ACCOUNT_KIND_CREDIT_CARD, card_closing_day="1", card_due_day="10"))


# --- Regra para a aplicação vinculada -----------------------------------------------


def _regra():
    return StatementRule.objects.create(
        name="Guardar no cofrinho", pattern="DINHEIRO RESERVADO", institution=None,
        sign=RULE_SIGN_DEBIT, action=RULE_ACTION_LINKED,
    )


def test_regra_vinculada_transfere_para_a_unica_aplicacao_da_conta(c):
    _regra()
    linha = _linha(c.corrente, date(2026, 9, 10), "-200.00", "Dinheiro reservado")

    [plano] = extrato.planejar(c.user, [linha])
    assert plano.acao == extrato.TRANSFERENCIA_REGRA and plano.conta_destino == c.cofrinho

    feitas, erros = extrato.aplicar(c.user, [linha])
    assert (dict(feitas), erros) == ({extrato.TRANSFERENCIA_REGRA: 1}, [])
    entrada = CashFlowEntry.objects.get(account=c.cofrinho)
    assert entrada.operation_type == OPERATION_INTERNAL_TRANSFER and entrada.entry_amount == Decimal("200.00")


def test_regra_vinculada_sem_aplicacao_unica_fica_manual(c):
    _regra()
    FinancialAccount.objects.create(
        owner=c.titular, institution=c.banco, account_name="CDB", account_kind=ACCOUNT_KIND_INVESTMENT,
        initial_balance_date=INICIO, movement_account=c.corrente,
    )
    linha = _linha(c.corrente, date(2026, 9, 10), "-200.00", "Dinheiro reservado")

    [plano] = extrato.planejar(c.user, [linha])

    assert plano.acao == extrato.MANUAL
    assert "aplicação vinculada" in plano.rotulo


# --- Saldo Aplicações ---------------------------------------------------------------


def test_saldo_da_aplicacao_espera_o_extrato_do_mes_da_conta_de_movimento(c):
    assert "ainda não foi importado" in saldo.pendencia_da_movimentacao(c.cofrinho, date(2026, 9, 30))

    pendente = _linha(c.corrente, date(2026, 9, 10), "-50.00")
    assert "1 linha(s) de extrato pendente(s)" in saldo.pendencia_da_movimentacao(c.cofrinho, date(2026, 9, 30))
    with pytest.raises(ValueError, match="pendente"):
        saldo.aplicar(
            c.user, account_id=c.cofrinho.id, data=date(2026, 9, 30), saldo_informado="510,00",
            diferenca_esperada="10.00", destino=saldo.DESTINO_RENDIMENTOS,
        )

    pendente.status = LINE_STATUS_RECONCILED
    pendente.save()
    assert saldo.pendencia_da_movimentacao(c.cofrinho, date(2026, 9, 30)) == ""
    lancamento = saldo.aplicar(
        c.user, account_id=c.cofrinho.id, data=date(2026, 9, 30), saldo_informado="510,00",
        diferenca_esperada="10.00", destino=saldo.DESTINO_RENDIMENTOS,
    )
    assert lancamento.entry_amount == Decimal("10.00")


def test_conta_sem_conta_de_movimento_nao_tem_trava(c):
    assert saldo.pendencia_da_movimentacao(c.corrente, date(2026, 9, 30)) == ""


# --- Situação das Contas e fechamento -----------------------------------------------


def _informar_saldo(c, dia, valor):
    saldo.aplicar(
        c.user, account_id=c.cofrinho.id, data=dia, saldo_informado=valor,
        diferenca_esperada=str(Decimal(valor.replace(",", ".")) - Decimal("500.00")), destino=saldo.DESTINO_RENDIMENTOS,
    )


def test_mes_da_aplicacao_conciliado_com_movimento_conciliado_e_saldo_do_ultimo_dia(c):
    setembro, outubro = date(2026, 9, 1), date(2026, 10, 1)
    _linha(c.corrente, date(2026, 9, 10), "-50.00", status=LINE_STATUS_RECONCILED)
    _linha(c.corrente, date(2026, 10, 10), "-50.00")  # outubro pendente

    celulas = situacao.estados([c.cofrinho], [setembro, outubro])
    assert celulas[(c.cofrinho.id, setembro)].estado == situacao.COM_PENDENCIAS
    assert "falta informar o saldo de 30/09" in celulas[(c.cofrinho.id, setembro)].descricao
    assert "falta conciliar" in celulas[(c.cofrinho.id, outubro)].descricao

    _informar_saldo(c, date(2026, 9, 30), "512,00")
    celulas = situacao.estados([c.cofrinho], [setembro, outubro])
    assert celulas[(c.cofrinho.id, setembro)].estado == situacao.CONCILIADO
    assert celulas[(c.cofrinho.id, outubro)].estado == situacao.COM_PENDENCIAS


def test_aplicacao_zerada_e_parada_nao_precisa_de_saldo(c):
    c.cofrinho.initial_balance = Decimal("0.00")
    c.cofrinho.save()
    setembro = date(2026, 9, 1)
    _linha(c.corrente, date(2026, 9, 10), "-50.00", status=LINE_STATUS_RECONCILED)

    celula = situacao.estados([c.cofrinho], [setembro])[(c.cofrinho.id, setembro)]

    assert celula.estado == situacao.CONCILIADO
    assert "zerada" in celula.descricao


def test_fechamento_em_lote_fecha_so_os_meses_conciliados(c, capsys):
    _linha(c.corrente, date(2026, 8, 10), "-50.00", status=LINE_STATUS_RECONCILED)
    _linha(c.corrente, date(2026, 9, 10), "-50.00")  # setembro pendente
    _informar_saldo(c, date(2026, 8, 31), "505,00")

    call_command("fechar_meses_conciliados", "--usuario", c.user.username)
    assert not AccountMonthClose.objects.exists()  # simulação

    call_command("fechar_meses_conciliados", "--usuario", c.user.username, "--aplicar")
    fechados = set(AccountMonthClose.objects.filter(active=True).values_list("account_id", "year", "month"))
    assert fechados == {(c.corrente.id, 2026, 8), (c.cofrinho.id, 2026, 8)}
    assert "2 mês(es) fechado(s)" in capsys.readouterr().out


def test_fechamento_em_lote_pula_mes_com_saldo_de_extrato_divergente(c, capsys):
    lote = BankStatementImport.objects.create(
        account=c.corrente, source_filename="x.ofx", row_count=1,
        statement_balance=Decimal("1.00"), statement_balance_date=date(2026, 8, 31),
    )
    BankStatementLine.objects.create(
        import_batch=lote, account=c.corrente, statement_date=date(2026, 8, 5), description="X",
        amount=Decimal("-1.00"), line_hash="divergente", status=LINE_STATUS_RECONCILED,
    )

    call_command("fechar_meses_conciliados", "--usuario", c.user.username, "--aplicar")

    assert not AccountMonthClose.objects.filter(account=c.corrente).exists()
    assert "PULADO 08/2026" in capsys.readouterr().out


# --- Ajustes de dados: criar a aplicação e converter o avulso -----------------------


def test_ajustes_criam_aplicacao_e_convertem_avulso_em_transferencia_com_mes_fechado(c):
    avulso = services.create_transaction_batch(
        services.TransactionRequest(
            account_id=c.corrente.id, category_id=c.gerencial.id, entry_type=ENTRY_TYPE_EXPENSE,
            description="Rende Facil", entry_amount=Decimal("64.00"), installments=1, due_date=date(2026, 8, 3),
            status=STATUS_REALIZED, realized_date=date(2026, 8, 3), realized_amount=Decimal("64.00"),
        ),
        user=c.user,
    )[0]
    services.close_month(c.corrente, 2026, 8, None, c.user)
    saldo_antes = AccountMonthClose.objects.get(account=c.corrente, year=2026, month=8).closing_balance
    plano = {
        "criar_contas": [{
            "titular": "Titular", "instituicao": "Banco teste", "nome": "Rende Fácil", "tipo": "aplicacao",
            "saldo_inicial": "0", "data_do_saldo_inicial": "2025-12-31",
            "conta_de_movimento": {"titular": "Titular", "instituicao": "Banco teste", "nome": "Corrente"},
            "liberar_destino": True,
        }],
        "converter_em_transferencia": [{
            "lancamento": {"conta": {"titular": "Titular", "instituicao": "Banco teste", "nome": "Corrente"},
                           "data": "2026-08-03", "tipo": "despesa", "valor": "64.00"},
            "outra_conta": {"titular": "Titular", "instituicao": "Banco teste", "nome": "Rende Fácil"},
        }],
    }

    simulado = ajustes.executar(c.user, plano)
    assert simulado.erros == [] and not FinancialAccount.objects.filter(account_name="Rende Fácil").exists()

    relatorio = ajustes.executar(c.user, plano, aplicar=True)

    assert relatorio.erros == []
    rende = FinancialAccount.objects.get(account_name="Rende Fácil")
    assert rende.movement_account_id == c.corrente.id
    avulso.refresh_from_db()
    assert avulso.operation_type == OPERATION_INTERNAL_TRANSFER
    ponta = CashFlowEntry.objects.get(account=rende)
    assert (ponta.entry_amount, ponta.source_entry_id) == (Decimal("64.00"), avulso.id)
    fechamento = AccountMonthClose.objects.get(account=c.corrente, year=2026, month=8)
    assert fechamento.active and fechamento.closing_balance == saldo_antes
    saldos = decimal_balances_before_by_account([rende.id], date(2026, 9, 1), VIEW_REALIZED)
    assert saldos[rende.id] == Decimal("64.00")


def test_fim_do_periodo_do_extrato_cobre_dias_sem_movimento(c):
    """O OFX vai até hoje, mas o último movimento foi em setembro: o saldo de
    hoje da aplicação vale, porque o arquivo diz cobrir a conta até hoje."""
    lote = BankStatementImport.objects.create(
        account=c.corrente, source_filename="c6.ofx", row_count=1, statement_period_end=date(2026, 10, 7),
    )
    BankStatementLine.objects.create(
        import_batch=lote, account=c.corrente, statement_date=date(2026, 9, 24), description="PIX",
        amount=Decimal("-10.00"), line_hash="fim-do-periodo", status=LINE_STATUS_RECONCILED,
    )
    assert saldo.pendencia_da_movimentacao(c.cofrinho, date(2026, 10, 7)) == ""
    lote.statement_period_end = None
    lote.save()
    assert "ainda não foi importado" in saldo.pendencia_da_movimentacao(c.cofrinho, date(2026, 10, 7))


def test_fim_do_periodo_vem_do_dtend_do_ofx():
    from django.core.files.uploadedfile import SimpleUploadedFile

    from bank_statements.adapters import extract_statement_period_end

    ofx = b"<OFX><BANKTRANLIST><DTSTART>20260101<DTEND>20261007074917[-3:BRT]</BANKTRANLIST></OFX>"
    assert extract_statement_period_end(SimpleUploadedFile("x.ofx", ofx)) == date(2026, 10, 7)
    assert extract_statement_period_end(SimpleUploadedFile("x.pdf", b"%PDF")) is None
