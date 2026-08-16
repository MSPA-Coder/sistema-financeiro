"""Testes do parser OFX/OFC/QFX, inclusive formato XML malformado."""
from __future__ import annotations

from bank_statements.adapters import _extract_transactions_xml


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
