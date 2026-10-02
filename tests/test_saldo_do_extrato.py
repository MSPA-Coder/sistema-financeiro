"""Saldo que o próprio extrato informa, para conferir com o saldo do CB.

Cobre a leitura (OFX `LEDGERBAL`, "Saldo final" do PDF da Genial e do Mercado
Pago) e a conferência contra o saldo realizado da conta. Um arquivo sem saldo
(o OFX do C6, o CSV) continua importando, só sem conferência.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements.adapters import _saldo_do_ofx, _saldo_do_pdf, extract_statement_balance
from bank_statements.services import conferencia_de_saldo, import_statement_file
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_INCOME, STATUS_REALIZED
from transactions import services
from transactions.models import CashFlowCategory

OFX_SGML = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260901000000[-3:BRT]<TRNAMT>212.30<FITID>1<MEMO>Pix</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>0.00<DTASOF>20260930000000[-3:BRT]</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

OFX_XML_NEGATIVO = """<?xml version="1.0"?><OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260105</DTPOSTED><TRNAMT>-10.00</TRNAMT>
<FITID>9</FITID><MEMO>Tarifa</MEMO></STMTTRN></BANKTRANLIST>
<LEDGERBAL><BALAMT>-1234.56</BALAMT><DTASOF>20260131120000</DTASOF></LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

OFX_SEM_SALDO = OFX_SGML.replace("<LEDGERBAL><BALAMT>0.00<DTASOF>20260930000000[-3:BRT]</LEDGERBAL>", "")

TEXTO_GENIAL = """Extrato de conta corrente
De 01 set 2026 a 30 set 2026
R$ 1.506,80
Saldo final do período
Balanço do período Saldo inicial R$ 2.311,85
GENIAL INVESTIMENTOS CORRETORA DE VALORES MOBILIÁRIOS S.A.
"""

TEXTO_MERCADO_PAGO = """EXTRATO DE CONTA
Periodo: De 01-09-2026 al 30-09-2026
Entradas: R$ 24,68
Saldo inicial: R$ 4,60 Saldo final: R$ 29,28
Saidas: R$ 0,00
Mercado Pago Instituição de Pagamento Ltda.
"""


def test_ofx_sgml_traz_saldo_e_data():
    assert _saldo_do_ofx(OFX_SGML) == (Decimal("0.00"), date(2026, 9, 30))


def test_ofx_xml_aceita_saldo_negativo():
    assert _saldo_do_ofx(OFX_XML_NEGATIVO) == (Decimal("-1234.56"), date(2026, 1, 31))


def test_ofx_sem_ledgerbal_nao_informa_saldo():
    assert _saldo_do_ofx(OFX_SEM_SALDO) is None


def test_pdf_da_genial_traz_saldo_final_e_fim_do_periodo():
    assert _saldo_do_pdf(TEXTO_GENIAL) == (Decimal("1506.80"), date(2026, 9, 30))


def test_pdf_do_mercado_pago_traz_saldo_final_e_fim_do_periodo():
    assert _saldo_do_pdf(TEXTO_MERCADO_PAGO) == (Decimal("29.28"), date(2026, 9, 30))


def test_saldo_negativo_no_pdf():
    texto = TEXTO_GENIAL.replace("R$ 1.506,80", "-R$ 1.506,80")
    assert _saldo_do_pdf(texto)[0] == Decimal("-1506.80")


def test_pdf_desconhecido_nao_informa_saldo():
    assert _saldo_do_pdf("Extrato de outro banco\nSaldo final: R$ 10,00") is None


def test_arquivo_ilegivel_nao_derruba_a_importacao():
    arquivo = SimpleUploadedFile("extrato.ofx", b"isto nao e um ofx", content_type="application/x-ofx")
    assert extract_statement_balance(arquivo) is None
    vazio = SimpleUploadedFile("extrato.pdf", b"", content_type="application/pdf")
    assert extract_statement_balance(vazio) is None


@pytest.mark.django_db
def test_importacao_guarda_o_saldo_e_a_conferencia_compara_com_o_cb():
    user = AppUser.objects.create_user(username="operador-saldo", password="senha-segura")
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco do Brasil", institution_type="Banco")
    conta = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Conta 06")
    categoria = CashFlowCategory.objects.create(category_name="Outros")

    arquivo = SimpleUploadedFile("set.ofx", OFX_SGML.encode(), content_type="application/x-ofx")
    lote, inseridas, _ = import_statement_file(user, account_id=conta.id, uploaded_file=arquivo)
    assert inseridas == 1
    assert (lote.statement_balance, lote.statement_balance_date) == (Decimal("0.00"), date(2026, 9, 30))

    # O CB ainda não tem o Pix de 212,30: o saldo dele (0,00) bate com o extrato (0,00)
    # só por coincidência de zero; com a entrada lançada, passa a divergir.
    assert conferencia_de_saldo(lote).bate
    services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_INCOME,
            description="Pix", entry_amount=Decimal("212.30"), installments=1,
            due_date=date(2026, 9, 1), status=STATUS_REALIZED,
            realized_date=date(2026, 9, 1), realized_amount=Decimal("212.30"),
        ),
        user=user,
    )
    conferencia = conferencia_de_saldo(lote)
    assert not conferencia.bate
    assert conferencia.diferenca == Decimal("212.30")
    # Lançamento depois da data do extrato não entra na conferência.
    services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_INCOME,
            description="Outro", entry_amount=Decimal("5.00"), installments=1,
            due_date=date(2026, 10, 2), status=STATUS_REALIZED,
            realized_date=date(2026, 10, 2), realized_amount=Decimal("5.00"),
        ),
        user=user,
    )
    assert conferencia_de_saldo(lote).diferenca == Decimal("212.30")


@pytest.mark.django_db
def test_arquivo_sem_saldo_importa_e_nao_tem_conferencia():
    user = AppUser.objects.create_user(username="operador-sem-saldo", password="senha-segura")
    titular = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    conta = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Conta 02")
    arquivo = SimpleUploadedFile("c6.ofx", OFX_SEM_SALDO.encode(), content_type="application/x-ofx")
    lote, _, _ = import_statement_file(user, account_id=conta.id, uploaded_file=arquivo)
    assert lote.statement_balance is None
    assert conferencia_de_saldo(lote) is None
