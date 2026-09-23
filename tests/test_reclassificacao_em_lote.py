"""Reclassificar categorias em lote (Banking > Reclassificação).

O que os testes fixam: a chave que decide "mesma loja"; que iguais entram e
parecidas só são sugeridas; que parcelado muda inteiro; que transferência fica
de fora; que mês fechado só é atravessado com autorização e sem mudar o saldo
de fechamento; e que a escolha vira a sugestão das próximas faturas.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements import fatura, reclassificacao
from bank_statements.reclassificacao import chave_da_descricao, primeira_palavra
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from core.models import AuditLog
from transactions.models import AccountMonthClose, CashFlowCategory
from transactions.services import TransactionRequest, close_month, create_transaction_batch

# --- Chave (sem banco) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("descricao", "chave"),
    [
        ("MERCADOLIVRE*2PRODUTOS · Mariano", "MERCADOLIVRE"),
        ("MERCADOLIVRE*5PRODUTOS · Claudia", "MERCADOLIVRE"),
        ("AMAZON US (US$ 10.00) · Mariano", "AMAZON US"),
        ("Farmácia São João 123", "FARMACIA SAO JOAO"),
        ("UBER* TRIP", "UBER TRIP"),
        ("PARC 02/10 LOJA X", "LOJA X"),
    ],
)
def test_chave_tira_o_que_muda_de_uma_compra_para_outra(descricao, chave):
    assert chave_da_descricao(descricao) == chave


def test_primeira_palavra_ignora_termos_curtos_e_intermediadores():
    assert primeira_palavra("UBER TRIP") == "UBER"
    assert primeira_palavra("MP LOJA") is None
    assert primeira_palavra("PAGSEGURO JOSE") is None


# --- Com banco ------------------------------------------------------------------


@pytest.fixture
def cenario():
    usuario = AppUser.objects.create_user(
        username="reclass", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Maridito")
    UserOwnerAccess.objects.create(
        user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco reclass", institution_type="Banco")
    corrente = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Corrente")
    cartao = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Cartão", account_kind=ACCOUNT_KIND_CREDIT_CARD,
        card_closing_day=19, card_due_day=25, initial_balance_date=date(2026, 1, 1),
    )
    save_transfer_destination_accesses(usuario, {cartao.id})
    outros, _ = CashFlowCategory.objects.get_or_create(
        category_name="Outros", defaults={"kind": CATEGORY_KIND_MANAGERIAL}
    )
    return {
        "usuario": usuario, "corrente": corrente, "cartao": cartao, "outros": outros,
        "saude": CashFlowCategory.objects.create(category_name="Saúde r", kind=CATEGORY_KIND_MANAGERIAL),
        "transf": CashFlowCategory.objects.create(category_name="Transf r", kind=CATEGORY_KIND_TRANSFER),
    }


def _lancar(c, descricao, dia, valor="10.00", parcelas=1, conta=None, categoria=None):
    return create_transaction_batch(
        TransactionRequest(
            account_id=(conta or c["cartao"]).id, category_id=(categoria or c["outros"]).id,
            entry_type=ENTRY_TYPE_EXPENSE, description=descricao, entry_amount=Decimal(valor),
            installments=parcelas, due_date=dia, status=STATUS_REALIZED, realized_date=dia,
        ),
        user=c["usuario"],
    )


def _categoria(entrada):
    entrada.refresh_from_db()
    return entrada.category


@pytest.mark.django_db
def test_iguais_entram_e_parecidas_so_sao_sugeridas(cenario):
    escolhida = _lancar(cenario, "DROGASIL*123 · Mariano", date(2026, 3, 1))[0]
    igual = _lancar(cenario, "DROGASIL*456 · Claudia", date(2026, 4, 1), conta=cenario["corrente"])[0]
    parecida = _lancar(cenario, "DROGASIL ONLINE", date(2026, 5, 1))[0]
    outra = _lancar(cenario, "PADARIA", date(2026, 5, 1))[0]

    plano = reclassificacao.planejar(
        cenario["usuario"], entry_ids=[escolhida.id], categoria_id=cenario["saude"].id
    )

    assert {e.id for e in plano.alvo} == {escolhida.id, igual.id}
    assert plano.iguais == 1
    assert [e.id for e in plano.parecidas] == [parecida.id]
    assert outra.id not in {e.id for e in plano.alvo}


@pytest.mark.django_db
def test_parecida_marcada_entra_e_parcelado_muda_inteiro(cenario):
    escolhida = _lancar(cenario, "DROGASIL", date(2026, 3, 1))[0]
    parcelas = _lancar(cenario, "DROGASIL ONLINE", date(2026, 3, 5), parcelas=3)

    reclassificacao.aplicar(
        cenario["usuario"], entry_ids=[escolhida.id], categoria_id=cenario["saude"].id,
        parecidas_ids=[parcelas[0].id],
    )

    assert all(_categoria(p) == cenario["saude"] for p in parcelas)


@pytest.mark.django_db
def test_transferencia_fica_de_fora(cenario):
    transferencia = create_transaction_batch(
        TransactionRequest(
            account_id=cenario["corrente"].id, category_id=cenario["transf"].id, entry_type=ENTRY_TYPE_EXPENSE,
            description="DROGASIL", entry_amount=Decimal("5.00"), installments=1, due_date=date(2026, 3, 1),
            counterparty_account_id=cenario["cartao"].id,
        ),
        user=cenario["usuario"],
    )[0]

    with pytest.raises(ValueError, match="Selecione"):
        reclassificacao.planejar(cenario["usuario"], entry_ids=[transferencia.id], categoria_id=cenario["saude"].id)


@pytest.mark.django_db
def test_mes_fechado_exige_autorizacao_e_mantem_o_saldo_de_fechamento(cenario):
    entrada = _lancar(cenario, "DROGASIL", date(2026, 3, 10), conta=cenario["corrente"])[0]
    fechado = close_month(cenario["corrente"], 2026, 3, Decimal("-10.00"), cenario["usuario"])

    with pytest.raises(ValueError, match="autorize"):
        reclassificacao.aplicar(cenario["usuario"], entry_ids=[entrada.id], categoria_id=cenario["saude"].id)
    assert _categoria(entrada) == cenario["outros"]

    reclassificacao.aplicar(
        cenario["usuario"], entry_ids=[entrada.id], categoria_id=cenario["saude"].id, autorizar_meses=True
    )

    assert _categoria(entrada) == cenario["saude"]
    fechado.refresh_from_db()
    assert fechado.active and fechado.closing_balance == Decimal("-10.00")
    assert AuditLog.objects.filter(entity_name="account_month_close", action="reopen").exists()
    assert AuditLog.objects.filter(entity_name="cash_flow_entry", entity_id=str(entrada.id), action="update").exists()


@pytest.mark.django_db
def test_saldo_de_fechamento_divergente_nao_grava_nada(cenario):
    entrada = _lancar(cenario, "DROGASIL", date(2026, 3, 10), conta=cenario["corrente"])[0]
    # Um fechamento gravado com saldo errado: reclassificar não o conserta em silêncio.
    close_month(cenario["corrente"], 2026, 3, Decimal("999.00"), cenario["usuario"])

    with pytest.raises(ValueError, match="nada foi gravado"):
        reclassificacao.aplicar(
            cenario["usuario"], entry_ids=[entrada.id], categoria_id=cenario["saude"].id, autorizar_meses=True
        )

    assert _categoria(entrada) == cenario["outros"]
    assert AccountMonthClose.objects.get(account=cenario["corrente"], year=2026, month=3).active


@pytest.mark.django_db
def test_reclassificacao_vira_a_sugestao_da_proxima_fatura(cenario):
    antiga = _lancar(cenario, "DROGASIL*123 · Mariano", date(2026, 3, 1), conta=cenario["corrente"])[0]
    reclassificacao.aplicar(cenario["usuario"], entry_ids=[antiga.id], categoria_id=cenario["saude"].id)

    from bank_statements.models import BankStatementImport, BankStatementLine

    lote = BankStatementImport.objects.create(account=cenario["cartao"], source_filename="f.csv")
    linha = BankStatementLine.objects.create(
        import_batch=lote, account=cenario["cartao"], statement_date=date(2026, 4, 2),
        description="DROGASIL*999 · Claudia", amount=Decimal("-20.00"), line_hash="h1", card_holder="Claudia",
    )

    plano = fatura.planejar(cenario["cartao"], [linha])[0]
    assert plano.categoria == cenario["saude"]


@pytest.mark.django_db
def test_tela_lista_previa_e_aplica(cenario):
    entrada = _lancar(cenario, "DROGASIL", date(2026, 3, 10))[0]
    client = Client()
    client.force_login(cenario["usuario"])

    lista = client.get("/banking/reclassification/", {"texto": "DROGA"})
    assert lista.status_code == 200
    assert "DROGASIL" in lista.content.decode()

    previa = client.post("/banking/reclassification/", {
        "acao": "previa", "entry_ids": [entrada.id], "categoria": cenario["saude"].id, "iguais": "1",
        "voltar": "texto=DROGA",
    })
    assert "Prévia: 1 lançamento(s) passam para Saúde r" in previa.content.decode()

    aplicada = client.post("/banking/reclassification/", {
        "acao": "aplicar", "entry_ids": [entrada.id], "categoria": cenario["saude"].id, "iguais": "1",
        "voltar": "texto=DROGA",
    })
    assert aplicada.status_code == 302
    assert aplicada["Location"] == "/banking/reclassification/?texto=DROGA"
    assert _categoria(entrada) == cenario["saude"]
