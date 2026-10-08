"""Situação das Contas e Extratos importados.

A função que classifica a célula e o cálculo dos meses não tocam o banco e
carregam as fronteiras (virada de ano, mês anterior ao saldo inicial, conta sem
linhas). O que depende de dados -- linha pendente, conciliada, fatura de cartão,
escopo de titular, teto de consultas -- usa o banco do `quality`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_function_permissions
from bank_statements import painel_de_extratos, situacao
from bank_statements.models import (
    LINE_STATUS_IGNORED,
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    BankStatementImport,
    BankStatementLine,
)
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ENTRY_TYPE_INCOME,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR, USER_TYPE_USER
from transactions.models import CashFlowCategory, CashFlowEntry

# --- Classificação e meses (sem banco) ------------------------------------------------


@pytest.mark.parametrize(
    ("linhas", "novas", "saldo", "antes", "esperado"),
    [
        (10, 0, False, False, situacao.CONCILIADO),
        (10, 3, False, False, situacao.COM_PENDENCIAS),
        (0, 0, False, False, situacao.SEM_IMPORTACAO),
        (0, 0, True, False, situacao.SALDO_INFORMADO),
        (0, 0, False, True, situacao.NAO_SE_APLICA),
        # Antes do saldo inicial, mas com linhas: vale o que as linhas dizem
        # (a fatura anterior ao saldo inicial é importada e ignorada).
        (4, 0, False, True, situacao.CONCILIADO),
        (4, 1, False, True, situacao.COM_PENDENCIAS),
        # Linhas pendentes vencem o saldo informado.
        (4, 1, True, False, situacao.COM_PENDENCIAS),
    ],
)
def test_classificar(linhas, novas, saldo, antes, esperado):
    assert situacao.classificar(
        linhas=linhas, novas=novas, saldo_informado=saldo, antes_do_saldo_inicial=antes
    ) == esperado


@pytest.mark.parametrize(
    ("linhas", "novas", "esperado"),
    [
        # Mês sem movimento cujo extrato traz o saldo: importado e nada pendente.
        (0, 0, situacao.CONCILIADO),
        # Com linhas, valem as linhas.
        (3, 1, situacao.COM_PENDENCIAS),
    ],
)
def test_classificar_mes_com_saldo_de_extrato(linhas, novas, esperado):
    assert situacao.classificar(
        linhas=linhas, novas=novas, saldo_informado=False, antes_do_saldo_inicial=False, saldo_do_extrato=True
    ) == esperado


def test_meses_atravessam_a_virada_do_ano():
    assert situacao.meses_ate(date(2026, 2, 17), 4) == [
        date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1),
    ]
    assert situacao.primeiro_dia_do_mes_seguinte(date(2025, 12, 31)) == date(2026, 1, 1)


# --- Com banco -----------------------------------------------------------------------

HOJE = date(2026, 10, 3)


@pytest.fixture
def cenario(db):
    usuario = AppUser.objects.create_user(
        username="situacao", password="troca-esta-senha", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(user=usuario, owner=titular, can_view=True)
    banco = FinancialInstitution.objects.create(institution_name="Banco teste", institution_type="Banco")
    inicio = date(2026, 1, 1)
    conta = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Corrente", initial_balance_date=inicio
    )
    cdb = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="CDB", initial_balance_date=inicio
    )
    nova = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta nova", initial_balance_date=date(2026, 9, 15)
    )
    cartao = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Cartão", account_kind=ACCOUNT_KIND_CREDIT_CARD,
        card_closing_day=19, card_due_day=25, initial_balance_date=inicio,
    )
    client = Client()
    client.force_login(usuario)
    # O administrador enxerga todos os titulares; o escopo se prova com um usuário comum.
    comum = AppUser.objects.create_user(username="comum", password="troca-esta-senha", user_type=USER_TYPE_USER)
    save_function_permissions(comum, {"banking.view"})
    UserOwnerAccess.objects.create(user=comum, owner=titular, can_view=True)
    client_comum = Client()
    client_comum.force_login(comum)
    return SimpleNamespace(
        usuario=usuario, comum=comum, client_comum=client_comum, titular=titular, banco=banco, conta=conta, cdb=cdb, nova=nova, cartao=cartao,
        client=client,
    )


def _importar(conta, nome, linhas, *, saldo=None, data_do_saldo=None):
    lote = BankStatementImport.objects.create(
        account=conta, source_filename=nome, row_count=len(linhas),
        statement_balance=saldo, statement_balance_date=data_do_saldo,
    )
    for indice, (dia, status) in enumerate(linhas):
        BankStatementLine.objects.create(
            import_batch=lote, account=conta, statement_date=dia, description=f"Linha {indice}",
            amount=Decimal("10.00"), line_hash=f"{nome}-{indice}", status=status,
        )
    return lote


def _celulas(matriz, conta):
    linha = next(linha for linha in matriz.linhas if linha.conta.id == conta.id)
    return {celula.mes: celula for celula in linha.celulas}


@pytest.mark.django_db
def test_matriz_mostra_cada_estado_e_o_resumo_do_mes_anterior(cenario):
    c = cenario
    _importar(c.conta, "ago", [(date(2026, 8, 5), LINE_STATUS_RECONCILED)])
    _importar(c.conta, "set", [
        (date(2026, 9, 2), LINE_STATUS_RECONCILED), (date(2026, 9, 9), LINE_STATUS_NEW),
        (date(2026, 9, 12), LINE_STATUS_IGNORED),
    ])
    _importar(c.cartao, "fatura-set", [(date(2026, 9, 5), LINE_STATUS_RECONCILED)])
    categoria = CashFlowCategory.objects.create(category_name="Rendimentos teste")
    CashFlowEntry.objects.create(
        account=c.cdb, category=categoria, entry_type=ENTRY_TYPE_INCOME, entry_amount=Decimal("5.00"),
        description="Atualização de saldo: rendimentos", due_date=date(2026, 9, 30),
        status=STATUS_REALIZED, realized_date=date(2026, 9, 30), realized_amount=Decimal("5.00"),
    )

    matriz = situacao.montar(c.usuario, quantos=3, hoje=HOJE)

    assert matriz.meses == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert matriz.referencia == date(2026, 9, 1)
    corrente = _celulas(matriz, c.conta)
    assert corrente[date(2026, 8, 1)].estado == situacao.CONCILIADO
    assert corrente[date(2026, 9, 1)].estado == situacao.COM_PENDENCIAS
    assert (corrente[date(2026, 9, 1)].linhas, corrente[date(2026, 9, 1)].novas) == (3, 1)
    assert corrente[date(2026, 10, 1)].estado == situacao.SEM_IMPORTACAO
    assert _celulas(matriz, c.cdb)[date(2026, 9, 1)].estado == situacao.SALDO_INFORMADO
    assert _celulas(matriz, c.cdb)[date(2026, 8, 1)].estado == situacao.SEM_IMPORTACAO
    assert _celulas(matriz, c.nova)[date(2026, 8, 1)].estado == situacao.NAO_SE_APLICA
    assert _celulas(matriz, c.nova)[date(2026, 9, 1)].estado == situacao.SEM_IMPORTACAO
    assert _celulas(matriz, c.cartao)[date(2026, 9, 1)].estado == situacao.CONCILIADO
    # Setembro: cartão e CDB em dia, corrente com pendência, conta nova sem importação.
    assert matriz.resumo.por_estado == {
        situacao.CONCILIADO: 1, situacao.SALDO_INFORMADO: 1,
        situacao.COM_PENDENCIAS: 1, situacao.SEM_IMPORTACAO: 1,
    }
    assert (matriz.resumo.em_dia, matriz.resumo.aplicaveis, matriz.resumo.percentual) == (2, 4, 50)


@pytest.mark.django_db
def test_mes_sem_movimento_com_saldo_do_extrato_fica_conciliado(cenario):
    c = cenario
    lote = _importar(c.conta, "ago-sem-movimento", [], saldo=Decimal("0.00"), data_do_saldo=date(2026, 8, 31))
    # Lote sem saldo não cobre mês nenhum.
    _importar(c.cdb, "sem-nada", [], saldo=None, data_do_saldo=None)

    matriz = situacao.montar(c.usuario, quantos=3, hoje=HOJE)

    agosto = _celulas(matriz, c.conta)[date(2026, 8, 1)]
    assert agosto.estado == situacao.CONCILIADO
    assert agosto.lote_id == lote.id
    assert agosto.descricao == "Conciliado: extrato sem movimento; saldo de 31/08"
    assert _celulas(matriz, c.conta)[date(2026, 9, 1)].estado == situacao.SEM_IMPORTACAO
    assert _celulas(matriz, c.cdb)[date(2026, 8, 1)].estado == situacao.SEM_IMPORTACAO


@pytest.mark.django_db
def test_resumo_nao_conta_o_mes_anterior_ao_saldo_inicial(cenario):
    matriz = situacao.montar(cenario.usuario, quantos=3, referencia=date(2026, 8, 20), hoje=HOJE)

    assert matriz.referencia == date(2026, 8, 1)
    assert matriz.resumo.por_estado[situacao.NAO_SE_APLICA] == 1  # "Conta nova" ainda não existia
    assert matriz.resumo.aplicaveis == 3


@pytest.mark.django_db
def test_matriz_so_traz_contas_do_titular_acessivel(cenario):
    outro = AccountOwner.objects.create(name="Outro titular")
    FinancialAccount.objects.create(owner=outro, institution=cenario.banco, account_name="Conta alheia")

    matriz = situacao.montar(cenario.comum, hoje=HOJE)

    nomes = {linha.conta.account_name for linha in matriz.linhas}
    assert "Conta alheia" not in nomes
    assert {"Corrente", "CDB", "Conta nova", "Cartão"} <= nomes


@pytest.mark.django_db
def test_tela_leva_cada_celula_para_onde_resolve(cenario):
    c = cenario
    _importar(c.conta, "set", [(date(2026, 9, 2), LINE_STATUS_NEW)])
    lote_cartao = _importar(c.cartao, "fatura", [(date(2026, 9, 5), LINE_STATUS_NEW)])
    lote_cdb = _importar(c.cdb, "cdb", [(date(2026, 9, 5), LINE_STATUS_RECONCILED)])

    resposta = c.client.get("/banking/status/")

    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert 'href="/banking/reconciliation/"' in html  # pendência de conta: Conciliação
    assert f'href="/banking/import/{lote_cartao.id}/fatura/"' in html  # pendência de cartão: a fatura
    assert f'href="/banking/import/{lote_cdb.id}/extrato/"' in html  # conciliado: o extrato
    assert 'href="/banking/imports/"' in html  # sem importação: Importação de Dados
    assert "Situação das Contas" in html


@pytest.mark.django_db
def test_tela_aceita_periodo_e_mes_de_referencia_invalidos(cenario):
    assert cenario.client.get("/banking/status/?meses=abc&ref=lixo").status_code == 200
    assert cenario.client.get("/banking/status/?meses=12&ref=2019-01").status_code == 200


@pytest.mark.django_db
def test_tela_tem_teto_de_consultas_que_nao_cresce_com_as_contas(cenario):
    def consultas() -> int:
        cenario.client.get("/banking/status/")  # aquece sessão e ContentType
        with CaptureQueriesContext(connection) as capturadas:
            assert cenario.client.get("/banking/status/").status_code == 200
        return len(capturadas)

    antes = consultas()
    for i in range(5):
        conta = FinancialAccount.objects.create(
            owner=cenario.titular, institution=cenario.banco, account_name=f"Extra {i}"
        )
        _importar(conta, f"extra-{i}", [(date(2026, 9, 3), LINE_STATUS_NEW)])

    assert consultas() == antes


# --- Extratos --------------------------------------------------------------------------


@pytest.mark.django_db
def test_extratos_listam_so_contas_que_nao_sao_cartao_com_conferencia_de_saldo(cenario):
    c = cenario
    _importar(
        c.conta, "set.ofx", [(date(2026, 9, 2), LINE_STATUS_RECONCILED), (date(2026, 9, 9), LINE_STATUS_NEW)],
        saldo=Decimal("123.00"), data_do_saldo=date(2026, 9, 30),
    )
    _importar(c.cartao, "fatura.csv", [(date(2026, 9, 5), LINE_STATUS_NEW)])

    paineis = painel_de_extratos.paineis(c.usuario, hoje=HOJE)

    assert {p.conta.account_name for p in paineis} == {"Corrente", "CDB", "Conta nova"}
    corrente = next(p for p in paineis if p.conta.account_name == "Corrente")
    extrato = corrente.extratos[0]
    assert (extrato.total, extrato.conciliadas, extrato.novas) == (2, 1, 1)
    assert not extrato.em_dia
    assert not extrato.conferencia.bate  # o CB está em zero e o arquivo diz 123,00
    assert extrato.conferencia.diferenca == Decimal("-123.00")


@pytest.mark.django_db
def test_tela_de_extratos_e_detalhe(cenario):
    c = cenario
    lote = _importar(c.conta, "set.ofx", [(date(2026, 9, 2), LINE_STATUS_NEW)])

    lista = c.client.get("/banking/statements/")
    detalhe = c.client.get(f"/banking/import/{lote.id}/extrato/")

    assert lista.status_code == 200
    assert "set.ofx" in lista.content.decode()
    assert detalhe.status_code == 200
    assert "Linha 0" in detalhe.content.decode()


@pytest.mark.django_db
def test_detalhe_do_extrato_recusa_cartao_e_conta_alheia(cenario):
    c = cenario
    lote_cartao = _importar(c.cartao, "fatura.csv", [(date(2026, 9, 5), LINE_STATUS_NEW)])
    outro = AccountOwner.objects.create(name="Outro titular")
    alheia = FinancialAccount.objects.create(owner=outro, institution=c.banco, account_name="Alheia")
    lote_alheio = _importar(alheia, "alheio.ofx", [(date(2026, 9, 5), LINE_STATUS_NEW)])

    for lote in (lote_cartao, lote_alheio):
        resposta = c.client_comum.get(f"/banking/import/{lote.id}/extrato/")
        assert resposta.status_code == 302
        assert resposta.url == "/banking/statements/"


def test_matriz_nao_mostra_mes_anterior_a_data_inicial_do_sistema(cenario):
    from core.services import update_system_start_date

    update_system_start_date("2026-08-15")
    matriz = situacao.montar(cenario.usuario, quantos=6, hoje=date(2026, 10, 6))

    assert matriz.meses == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert all(celula.mes >= date(2026, 8, 1) for linha in matriz.linhas for celula in linha.celulas)
