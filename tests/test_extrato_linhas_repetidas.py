"""Linhas idênticas num extrato são movimentos distintos, não duplicatas.

Até 24/09/2026 o hash da linha de extrato (CSV e PDF da Genial) era só
conta + data + descrição + valor. Dois PIX de R$ 50,00 à mesma pessoa no mesmo
dia viravam uma linha: a segunda saía como "duplicada ignorada" e sumia da
conciliação. A fatura do cartão já numerava as repetições; o extrato não.

A primeira ocorrência mantém o hash antigo de propósito -- reimportar um
arquivo que já entrou pela regra velha acrescenta só a repetição perdida.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from bank_statements.adapters import CsvStatementAdapter, _parse_genial_lines, line_hash
from bank_statements.models import BankStatementImport, BankStatementLine
from bank_statements.services import import_statement_file
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.identity import USER_TYPE_ADMINISTRATOR

EXTRATO = (
    b"data;descricao;valor\r\n"
    b"2026-09-10;PIX ENVIADO FULANO;-50,00\r\n"
    b"2026-09-10;PIX ENVIADO FULANO;-50,00\r\n"
    b"2026-09-10;PIX ENVIADO FULANO;-50,00\r\n"
    b"2026-09-10;TARIFA;-2,00\r\n"
)


def _csv(conteudo: bytes = EXTRATO) -> SimpleUploadedFile:
    return SimpleUploadedFile("extrato.csv", conteudo, content_type="text/csv")


def test_csv_repetido_gera_um_hash_por_ocorrencia():
    linhas = CsvStatementAdapter().parse(_csv(), account_id=7)
    pix = [linha for linha in linhas if linha.description == "PIX ENVIADO FULANO"]

    assert len(pix) == 3
    assert len({linha.line_hash for linha in pix}) == 3


def test_primeira_ocorrencia_mantem_o_hash_antigo():
    linhas = CsvStatementAdapter().parse(_csv(), account_id=7)
    antigo = line_hash(7, date(2026, 9, 10), "PIX ENVIADO FULANO", Decimal("-50.00"))

    assert linhas[0].line_hash == antigo
    assert linhas[3].line_hash == line_hash(7, date(2026, 9, 10), "TARIFA", Decimal("-2.00"))


def test_mesmo_arquivo_gera_os_mesmos_hashes():
    primeira = [linha.line_hash for linha in CsvStatementAdapter().parse(_csv(), account_id=7)]
    segunda = [linha.line_hash for linha in CsvStatementAdapter().parse(_csv(), account_id=7)]
    assert primeira == segunda


def test_genial_repetido_tambem_e_numerado():
    texto = (
        "Extrato de conta corrente\n"
        "Qui 21 mai 2026 Corretagem - R$ 0,20\n"
        "Corretagem Executor - Btc\n"
        "Corretagem - R$ 0,20\n"
        "Corretagem Executor - Btc\n"
        "Nome: FULANO DE TAL\n"
    )
    linhas = _parse_genial_lines(texto, account_id=1)

    assert len(linhas) == 2
    assert linhas[0].line_hash != linhas[1].line_hash


@pytest.fixture
def conta():
    usuario = AppUser.objects.create_user(
        username="extrato", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Titular extrato")
    UserOwnerAccess.objects.create(
        user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Banco extrato", institution_type="Banco")
    conta = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Corrente extrato",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2026, 1, 1),
    )
    return usuario, conta


@pytest.mark.django_db
def test_importacao_grava_as_tres_e_reimportar_nao_duplica(conta):
    usuario, conta = conta
    _lote, inseridas, ignoradas = import_statement_file(usuario, account_id=conta.id, uploaded_file=_csv())
    _lote, de_novo, ignoradas_de_novo = import_statement_file(usuario, account_id=conta.id, uploaded_file=_csv())

    assert (inseridas, ignoradas) == (4, 0)
    assert (de_novo, ignoradas_de_novo) == (0, 4)
    assert BankStatementLine.objects.filter(account=conta, amount=Decimal("-50.00")).count() == 3


@pytest.mark.django_db
def test_reimportar_extrato_da_regra_antiga_recupera_so_as_repeticoes(conta):
    """O estado que a produção pode ter: o PIX entrou uma vez, pela regra velha."""
    usuario, conta = conta
    lote_antigo = BankStatementImport.objects.create(account=conta, source_filename="antigo.csv", row_count=2)
    for descricao, valor in (("PIX ENVIADO FULANO", "-50.00"), ("TARIFA", "-2.00")):
        BankStatementLine.objects.create(
            import_batch=lote_antigo, account=conta, statement_date=date(2026, 9, 10),
            description=descricao, amount=Decimal(valor), status="novo",
            line_hash=line_hash(conta.id, date(2026, 9, 10), descricao, Decimal(valor)),
        )

    _lote, inseridas, ignoradas = import_statement_file(usuario, account_id=conta.id, uploaded_file=_csv())

    assert (inseridas, ignoradas) == (2, 2)
    assert BankStatementLine.objects.filter(account=conta, amount=Decimal("-50.00")).count() == 3
