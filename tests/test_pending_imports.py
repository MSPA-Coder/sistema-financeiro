"""Importação em lote de extratos (upload de vários arquivos sem escolher a
conta antes) - `bank_statements/pending_imports.py`.

Cobre a detecção de conta por conteúdo (`detect_account`), o estágio
(`stage_uploaded_files`) e a confirmação (`resolve_pending_uploads`), além das
três views que compõem o fluxo (`stage_imports`, `confirm_imports`,
`process_imports`). Nenhum dado real de extrato entra aqui - os arquivos são
sintéticos, no layout que os adapters já esperam.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements.models import BankStatementLine, PendingStatementUpload
from bank_statements.pending_imports import (
    detect_account,
    pending_uploads_for_user,
    resolve_pending_uploads,
    stage_uploaded_files,
)
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.identity import USER_TYPE_ADMINISTRATOR, USER_TYPE_USER

GENIAL_TEXT = """
Extrato de conta corrente
De 01 mai 2026 a 31 mai 2026
Qui 21 mai 2026 Corretagem - R$ 0,20
Corretagem Executor - Btc
Nome: FULANO DE TAL Extrato gerado em
Conta: 1234567-8 15 ago 2026 - 13:32
Genial Investimentos CCTVM S.A.
"""

MERCADOPAGO_TEXT = """
EXTRATO DE CONTA
Fulano de Tal
CPF/CNPJ: 00000000000 Agência: 1 Conta: 11111111111
Saldo inicial: R$ 100,00 Saldo final: R$ 150,00
DETALHE DOS MOVIMENTOS
Data Descrição ID da operação Valor Saldo
05-09-2026 Pix recebido de Fulano 111 R$ 50,00 R$ 150,00
Data de geração: 01-10-2026
Mercado Pago Instituição de Pagamento Ltda.
"""

CABECALHO_FATURA_C6 = (
    "Data de Compra;Nome no Cartão;Final do Cartão;Categoria;Descrição;Parcela;"
    "Valor (em US$);Cotação (em R$);Valor (em R$)"
)


def _ofx(acctid: str | None, *, trnamt: str = "-10.00", dtposted: str = "20260911") -> bytes:
    header = f"<BANKACCTFROM><BANKID>001<ACCTID>{acctid}</BANKACCTFROM>" if acctid else ""
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
{header}
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>{dtposted}
<TRNAMT>{trnamt}
<FITID>abc123
<MEMO>Teste
</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
""".encode()


def _stub_pdf_text(monkeypatch, text: str) -> None:
    """`extract_pdf_text` é importado em dois módulos; sem pdfplumber de
    verdade nos testes, as duas ligações precisam ser substituídas."""
    monkeypatch.setattr("bank_statements.pending_imports.extract_pdf_text", lambda raw: text)
    monkeypatch.setattr("bank_statements.adapters.extract_pdf_text", lambda raw: text)


@pytest.fixture
def cenario(settings, tmp_path):
    # Em `quality`, `MEDIA_ROOT` (`BASE_DIR / 'media'`) não é gravável - a
    # fronteira de escrita do runtime é deliberada (AGENTS.md). Mesmo padrão
    # de `tests/test_audit_io_repairs.py`: redireciona para um tmp_path.
    settings.MEDIA_ROOT = tmp_path
    usuario = AppUser.objects.create_user(
        username="lote", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Maridito")
    UserOwnerAccess.objects.create(
        user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco Teste", institution_type="Banco")
    genial = FinancialInstitution.objects.create(
        institution_name="Genial", institution_type="Corretora", homologada=True
    )
    conta_ofx = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta corrente",
        initial_balance=Decimal("1000.00"), initial_balance_date=date(2026, 1, 1),
        statement_identifier="41861-7",
    )
    conta_genial = FinancialAccount.objects.create(
        owner=titular, institution=genial, account_name="Conta Genial",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2026, 1, 1),
        statement_identifier="1234567-8",
    )
    return {"usuario": usuario, "titular": titular, "banco": banco, "conta_ofx": conta_ofx, "conta_genial": conta_genial}


# --- detect_account -----------------------------------------------------------


@pytest.mark.django_db
def test_detect_account_matches_ofx_hint_to_configured_account(cenario):
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.ofx", content_type="application/x-ofx", raw=_ofx("41861-7")
    )
    assert conta == cenario["conta_ofx"]
    assert "41861-7" in rotulo
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_ofx_hint_without_matching_account_stays_unidentified(cenario):
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.ofx", content_type="application/x-ofx", raw=_ofx("999999")
    )
    assert conta is None
    assert "999999" in rotulo
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_ofx_without_header_has_no_hint_but_still_validates(cenario):
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.ofx", content_type="application/x-ofx", raw=_ofx(None)
    )
    assert conta is None
    assert rotulo == "OFX"
    assert erro == ""


def _ofx_sem_movimento(acctid: str, *, com_saldo: bool) -> bytes:
    saldo = "<LEDGERBAL><BALAMT>0.00<DTASOF>20260131</LEDGERBAL>" if com_saldo else ""
    return f"""OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKACCTFROM><BANKID>001<ACCTID>{acctid}</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20260131</BANKTRANLIST>
{saldo}
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
""".encode()


@pytest.mark.django_db
def test_detect_account_aceita_ofx_sem_movimento_que_traz_saldo(cenario):
    conta, _rotulo, erro = detect_account(
        cenario["usuario"], filename="jan.ofx", content_type="application/x-ofx",
        raw=_ofx_sem_movimento("41861-7", com_saldo=True),
    )
    assert conta == cenario["conta_ofx"]
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_recusa_ofx_sem_movimento_nem_saldo(cenario):
    conta, _rotulo, erro = detect_account(
        cenario["usuario"], filename="jan.ofx", content_type="application/x-ofx",
        raw=_ofx_sem_movimento("41861-7", com_saldo=False),
    )
    assert conta is None
    assert "nem saldo" in erro


@pytest.mark.django_db
def test_detect_account_reports_error_for_unreadable_ofx(cenario):
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.ofx", content_type="application/x-ofx",
        raw=b"isto nao e um ofx valido",
    )
    assert conta is None
    assert erro != ""


@pytest.mark.django_db
def test_detect_account_matches_pdf_conta_label_to_configured_account(cenario, monkeypatch):
    _stub_pdf_text(monkeypatch, GENIAL_TEXT)
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.pdf", content_type="application/pdf", raw=b"%PDF-fake"
    )
    assert conta == cenario["conta_genial"]
    assert "Genial" in rotulo
    assert "1234567-8" in rotulo
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_pdf_institution_recognized_without_matching_account(cenario, monkeypatch):
    FinancialAccount.objects.filter(id=cenario["conta_genial"].id).update(statement_identifier="")
    _stub_pdf_text(monkeypatch, GENIAL_TEXT)
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.pdf", content_type="application/pdf", raw=b"%PDF-fake"
    )
    assert conta is None
    assert "Genial" in rotulo
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_pdf_institution_breaks_tie_between_accounts_with_same_number(cenario, monkeypatch):
    # A conta digital e a de investimento da XP têm o mesmo número: no PDF, a
    # instituição reconhecida no arquivo decide.
    FinancialAccount.objects.filter(id=cenario["conta_ofx"].id).update(statement_identifier="1234567-8")
    _stub_pdf_text(monkeypatch, GENIAL_TEXT)
    conta, _rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.pdf", content_type="application/pdf", raw=b"%PDF-fake"
    )
    assert conta == cenario["conta_genial"]
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_matches_mercadopago_pdf(cenario, monkeypatch):
    mercadopago = FinancialInstitution.objects.create(
        institution_name="Mercado Pago", institution_type="Corretora", homologada=True
    )
    conta_mp = FinancialAccount.objects.create(
        owner=cenario["titular"], institution=mercadopago, account_name="Conta MP",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2026, 1, 1),
        statement_identifier="11111111111",
    )
    _stub_pdf_text(monkeypatch, MERCADOPAGO_TEXT)
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.pdf", content_type="application/pdf", raw=b"%PDF-fake"
    )
    assert conta == conta_mp
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_csv_recognizes_fatura_c6_but_never_auto_matches(cenario):
    raw = (CABECALHO_FATURA_C6 + "\r\n20/02/2026;MARIANO;1111;Padaria;PADARIA;Única;0;0;50.00").encode("utf-8")
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="fatura.csv", content_type="text/csv", raw=raw
    )
    assert conta is None
    assert rotulo == "Fatura C6"
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_generic_csv_has_no_hint(cenario):
    raw = b"data;descricao;valor\n01/09/2026;Teste;10,00"
    conta, rotulo, erro = detect_account(
        cenario["usuario"], filename="extrato.csv", content_type="text/csv", raw=raw
    )
    assert conta is None
    assert rotulo == "CSV de extrato"
    assert erro == ""


@pytest.mark.django_db
def test_detect_account_never_matches_an_account_outside_user_access(cenario):
    # `cenario["usuario"]` é administrator e enxerga todo mundo
    # (`_has_broad_owner_access`), então este caso precisa de um usuário comum,
    # só com acesso ao titular do cenário - não ao titular criado aqui.
    usuario_comum = AppUser.objects.create_user(
        username="comum", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_USER
    )
    UserOwnerAccess.objects.create(
        user=usuario_comum, owner=cenario["titular"],
        can_view=True, can_create=True, can_update=True, can_delete=True,
    )
    outro_titular = AccountOwner.objects.create(name="Outra Pessoa")
    FinancialAccount.objects.create(
        owner=outro_titular, institution=cenario["banco"], account_name="Conta de outra pessoa",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2026, 1, 1),
        statement_identifier="555555",
    )
    # Única conta com esse identificador pertence a um titular que
    # `usuario_comum` não acessa (sem `UserOwnerAccess` para ele) -
    # `accounts_for_import_form` já a exclui antes da comparação, então a
    # detecção não pode selecioná-la.
    conta, _, _ = detect_account(
        usuario_comum, filename="extrato.ofx", content_type="application/x-ofx", raw=_ofx("555555")
    )
    assert conta is None


# --- stage_uploaded_files / resolve_pending_uploads ----------------------------


@pytest.mark.django_db
def test_stage_uploaded_files_creates_one_pending_row_per_file_and_detects(cenario):
    files = [
        SimpleUploadedFile("a.ofx", _ofx("41861-7"), content_type="application/x-ofx"),
        SimpleUploadedFile("b.ofx", _ofx("999999"), content_type="application/x-ofx"),
    ]
    staged = stage_uploaded_files(cenario["usuario"], files)
    assert len(staged) == 2
    assert {upload.original_filename for upload in staged} == {"a.ofx", "b.ofx"}
    primeiro = next(u for u in staged if u.original_filename == "a.ofx")
    assert primeiro.detected_account_id == cenario["conta_ofx"].id
    assert primeiro.stored_path
    assert pending_uploads_for_user(cenario["usuario"]).count() == 2


@pytest.mark.django_db
def test_stage_uploaded_files_records_error_for_disallowed_extension(cenario):
    files = [SimpleUploadedFile("foto.png", b"\x89PNG\r\n", content_type="image/png")]
    staged = stage_uploaded_files(cenario["usuario"], files)
    assert len(staged) == 1
    assert staged[0].detection_error == "Tipo de arquivo não permitido para importação."
    assert staged[0].stored_path == ""


@pytest.mark.django_db(transaction=True)
def test_resolve_pending_uploads_imports_the_chosen_account(cenario):
    # `_delete_pending` agenda o unlink com `on_commit`: só roda com um commit
    # de verdade, por isso `transaction=True` em vez do rollback padrão da
    # marca `django_db`.
    from bank_statements.pending_imports import pending_storage_dir

    path = pending_storage_dir() / "teste_resolve.ofx"
    path.write_bytes(_ofx("41861-7"))
    upload = PendingStatementUpload.objects.create(
        uploaded_by=cenario["usuario"], original_filename="a.ofx", stored_filename="teste_resolve.ofx",
        stored_path="pending_imports/teste_resolve.ofx", detected_account=cenario["conta_ofx"],
    )

    resultados, erros, lotes = resolve_pending_uploads(
        cenario["usuario"], {upload.id: str(cenario["conta_ofx"].id)}
    )
    assert resultados == [("a.ofx", 1, 0)]
    assert erros == []
    assert BankStatementLine.objects.filter(account=cenario["conta_ofx"]).count() == 1
    assert not PendingStatementUpload.objects.filter(id=upload.id).exists()
    assert not path.exists()


@pytest.mark.django_db
def test_resolve_pending_uploads_discards_when_not_chosen(cenario):
    upload = PendingStatementUpload.objects.create(
        uploaded_by=cenario["usuario"], original_filename="a.ofx", stored_filename="x", stored_path="",
    )
    resultados, erros, lotes = resolve_pending_uploads(cenario["usuario"], {upload.id: ""})
    assert resultados == []
    assert erros == []
    assert not PendingStatementUpload.objects.filter(id=upload.id).exists()


@pytest.mark.django_db
def test_resolve_pending_uploads_reports_detection_error_and_discards(cenario):
    upload = PendingStatementUpload.objects.create(
        uploaded_by=cenario["usuario"], original_filename="ruim.ofx", stored_filename="x", stored_path="",
        detection_error="Arquivo OFX não contém transações válidas.",
    )
    resultados, erros, lotes = resolve_pending_uploads(
        cenario["usuario"], {upload.id: str(cenario["conta_ofx"].id)}
    )
    assert resultados == []
    assert erros == [("ruim.ofx", "Arquivo OFX não contém transações válidas.")]
    assert not PendingStatementUpload.objects.filter(id=upload.id).exists()


@pytest.mark.django_db
def test_resolve_pending_uploads_ignores_rows_from_another_user(cenario):
    outro = AppUser.objects.create_user(
        username="outro", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_ADMINISTRATOR
    )
    upload = PendingStatementUpload.objects.create(
        uploaded_by=outro, original_filename="a.ofx", stored_filename="x", stored_path="",
    )
    resultados, erros, lotes = resolve_pending_uploads(
        cenario["usuario"], {upload.id: str(cenario["conta_ofx"].id)}
    )
    assert resultados == [] and erros == []
    assert PendingStatementUpload.objects.filter(id=upload.id).exists()


# --- Views ----------------------------------------------------------------------


@pytest.mark.django_db
def test_stage_imports_view_accepts_several_files_and_redirects_to_confirm(cenario):
    client = Client()
    client.force_login(cenario["usuario"])
    resposta = client.post(
        "/banking/imports/stage/",
        {
            "statement_files": [
                SimpleUploadedFile("a.ofx", _ofx("41861-7"), content_type="application/x-ofx"),
                SimpleUploadedFile("b.ofx", _ofx("999999"), content_type="application/x-ofx"),
            ]
        },
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == "/banking/imports/confirm/"
    assert pending_uploads_for_user(cenario["usuario"]).count() == 2


@pytest.mark.django_db
def test_confirm_imports_view_lists_pending_uploads(cenario):
    PendingStatementUpload.objects.create(
        uploaded_by=cenario["usuario"], original_filename="a.ofx", stored_filename="x", stored_path="",
        detected_account=cenario["conta_ofx"], detected_label="OFX · conta 41861-7",
    )
    client = Client()
    client.force_login(cenario["usuario"])
    resposta = client.get("/banking/imports/confirm/")
    assert resposta.status_code == 200
    conteudo = resposta.content.decode()
    assert "a.ofx" in conteudo
    assert "41861-7" in conteudo


@pytest.mark.django_db
def test_confirm_imports_view_redirects_when_nothing_pending(cenario):
    client = Client()
    client.force_login(cenario["usuario"])
    resposta = client.get("/banking/imports/confirm/")
    assert resposta.status_code == 302
    assert resposta["Location"] == "/banking/imports/"


@pytest.mark.django_db
def test_process_imports_view_commits_confirmed_account_and_reports_summary(cenario):
    client = Client()
    client.force_login(cenario["usuario"])
    client.post(
        "/banking/imports/stage/",
        {"statement_files": SimpleUploadedFile("a.ofx", _ofx("41861-7"), content_type="application/x-ofx")},
    )
    pendente = PendingStatementUpload.objects.get(uploaded_by=cenario["usuario"])

    resposta = client.post(
        "/banking/imports/confirm/process/", {f"account_{pendente.id}": cenario["conta_ofx"].id}
    )
    assert resposta.status_code == 302
    assert resposta["Location"] == "/banking/imports/"
    assert BankStatementLine.objects.filter(account=cenario["conta_ofx"]).count() == 1
    assert not PendingStatementUpload.objects.filter(id=pendente.id).exists()
