"""Testes do parser OFX/OFC/QFX, inclusive formato XML malformado."""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from bank_statements.adapters import (
    OfxStatementAdapter,
    _extract_transactions_xml,
    extract_ofx_account_hint,
)


def _ofx_file(name: str, *transactions: str) -> SimpleUploadedFile:
    body = "\n".join(transactions)
    content = f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKTRANLIST>
{body}
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="application/x-ofx")


_CDB_A = """<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260911
<TRNAMT>-1580.00
<FITID>FITID_DA_PRIMEIRA_EXPORTACAO
<MEMO>EMISSAO DE CDB
</STMTTRN>"""

_CDB_B = """<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260911
<TRNAMT>-1580.00
<FITID>FITID_DIFERENTE_DA_SEGUNDA_EXPORTACAO
<MEMO>EMISSAO DE CDB
</STMTTRN>"""


def test_ofx_adapter_hash_ignores_fitid_so_reexport_is_recognized_as_duplicate():
    """Mesma transação, FITID diferente entre duas exportações (caso real do
    C6) -> precisa gerar o mesmo hash, senão a reimportação de um período já
    coberto nunca é reconhecida como duplicata."""
    linha_a = OfxStatementAdapter().parse(_ofx_file("export1.ofx", _CDB_A), account_id=4)
    linha_b = OfxStatementAdapter().parse(_ofx_file("export2.ofx", _CDB_B), account_id=4)
    assert linha_a[0].line_hash == linha_b[0].line_hash


def test_ofx_adapter_distinguishes_same_day_same_amount_duplicates_in_one_file():
    """Duas transações reais diferentes, mesmo dia/descrição/valor, no mesmo
    arquivo: `numerar_repeticoes` precisa dar hashes distintos, senão a
    segunda vira duplicata fantasma da primeira."""
    lines = OfxStatementAdapter().parse(_ofx_file("duas.ofx", _CDB_A, _CDB_B), account_id=4)
    assert len(lines) == 2
    assert lines[0].line_hash != lines[1].line_hash


def test_extract_transactions_xml_parses_multiple_blocks():
    content = """
    <OFX>
      <STMTTRN><DTPOSTED>20260815<TRNAMT>12.34<FITID>um</STMTTRN>
      <STMTTRN><DTPOSTED>20260816<TRNAMT>-5.67<FITID>dois</STMTTRN>
    </OFX>
    """

    assert _extract_transactions_xml(content) == [
        {"DTPOSTED": "20260815", "TRNAMT": "12.34", "FITID": "um"},
        {"DTPOSTED": "20260816", "TRNAMT": "-5.67", "FITID": "dois"},
    ]


def test_extract_transactions_xml_ignores_many_unclosed_blocks_without_backtracking():
    # A forma malformada que acionava o alerta de ReDoS: cada abertura não tem
    # fechamento. O parser linear não cria transação nem revisita o sufixo.
    content = "<STMTTRN>a" * 10_000

    assert _extract_transactions_xml(content) == []


def test_extract_ofx_account_hint_reads_acctid_from_header():
    content = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKACCTFROM>
<BANKID>001
<ACCTID>41861-7
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260911
<TRNAMT>-10.00
<FITID>abc
<MEMO>Teste
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
    assert extract_ofx_account_hint(content) == "41861-7"


def test_extract_ofx_account_hint_is_none_without_bankacctfrom():
    content = "<OFX><BANKTRANLIST><STMTTRN><DTPOSTED>20260911<TRNAMT>-10.00<FITID>abc</STMTTRN></BANKTRANLIST></OFX>"
    assert extract_ofx_account_hint(content) is None
