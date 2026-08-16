"""Testes do parser de texto do extrato Genial (PDF).

Usa texto sintético no layout observado na extração real via pdfplumber
(categoria e sinal na mesma linha da data/valor, descrição podendo quebrar em
mais de uma linha, rodapé "Nome: ..." encerrando a leitura) - não depende de
um arquivo PDF real nem de dados de conta de usuário.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bank_statements.adapters import _parse_genial_lines

SAMPLE_TEXT = """
Extrato de conta corrente
De 01 mai 2026 a 31 mai 2026
R$ 1.087,68
Saldo final do período
Balanço do período Saldo inicial R$ 440,43
Total de entradas + R$ 754,68
Total de saídas - R$ 107,43
Qui 21 mai 2026 Corretagem - R$ 0,20
Corretagem Executor - Btc
Impostos e Tributos - R$ 0,14
Irrf S/Rendimentos Empréstimos
de Ações Ações Brraizacnpr6
Rendimentos + R$ 0,65
Taxa de Remuneração Emprestimo
Ações Brraizacnpr6
Ter 12 mai 2026 Operações em bolsa + R$ 548,19
Operações Bolsa D+1 Pr
11/05/2026 Nc. 5567
Nome: FULANO DE TAL Extrato gerado em
Conta: 1234567-8 15 ago 2026 - 13:32
Central de atendimento Ouvidoria
4004-8888 ouvidoria@genial.com.vc
"""


def test_parse_genial_lines_extracts_all_entries():
    lines = _parse_genial_lines(SAMPLE_TEXT, account_id=1)
    assert len(lines) == 4


def test_parse_genial_lines_carries_date_across_same_day_entries():
    lines = _parse_genial_lines(SAMPLE_TEXT, account_id=1)
    assert lines[0].statement_date == date(2026, 5, 21)
    assert lines[1].statement_date == date(2026, 5, 21)
    assert lines[2].statement_date == date(2026, 5, 21)
    assert lines[3].statement_date == date(2026, 5, 12)


def test_parse_genial_lines_applies_sign_to_amount():
    lines = _parse_genial_lines(SAMPLE_TEXT, account_id=1)
    assert lines[0].amount == Decimal("-0.20")
    assert lines[1].amount == Decimal("-0.14")
    assert lines[2].amount == Decimal("0.65")
    assert lines[3].amount == Decimal("548.19")


def test_parse_genial_lines_joins_wrapped_description_with_category_prefix():
    lines = _parse_genial_lines(SAMPLE_TEXT, account_id=1)
    assert lines[1].description == (
        "Impostos e Tributos - Irrf S/Rendimentos Empréstimos de Ações Ações Brraizacnpr6"
    )


def test_parse_genial_lines_stops_at_footer():
    lines = _parse_genial_lines(SAMPLE_TEXT, account_id=1)
    assert all("Extrato gerado" not in line.description for line in lines)
    assert all("Ouvidoria" not in line.description for line in lines)


def test_parse_genial_lines_raises_when_no_entries_found():
    with pytest.raises(ValueError):
        _parse_genial_lines("Extrato de conta corrente\nNome: FULANO", account_id=1)


def test_parse_genial_lines_distinct_hashes_for_same_day_different_amounts():
    lines = _parse_genial_lines(SAMPLE_TEXT, account_id=1)
    hashes = {line.line_hash for line in lines}
    assert len(hashes) == len(lines)
