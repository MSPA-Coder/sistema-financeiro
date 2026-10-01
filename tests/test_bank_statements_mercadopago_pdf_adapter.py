"""Testes do parser de texto do extrato Mercado Pago (PDF).

Usa texto sintético no layout observado na extração real via pdfplumber (cada
linha de movimento já sai inteira: data, descrição, id da operação, valor e
saldo) - não depende de um arquivo PDF real nem de dados de conta de usuário.

O único extrato real disponível para desenhar este parser não tinha nenhum
débito (só subia o saldo), então o sample sintético inclui um débito de
propósito: é o caso em que a inferência de sinal pela variação do saldo
realmente importa, já que a coluna Valor não mostra sinal no formato real.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bank_statements.adapters import _parse_mercadopago_lines, extract_conta_label, sniff_pdf_format

SAMPLE_TEXT = """
EXTRATO DE CONTA
Fulano de Tal
CPF/CNPJ: 00000000000 Agência: 1 Conta: 11111111111
Periodo: De 01-09-2026 al 30-09-2026
Entradas: R$ 70,02
Saldo inicial: R$ 100,00 Saldo final: R$ 140,02
Saidas: R$ 30,00
DETALHE DOS MOVIMENTOS
Data Descrição ID da operação Valor Saldo
05-09-2026 Pix recebido de Fulano 111 R$ 50,00 R$ 150,00
06-09-2026 Pagamento de boleto 222 R$ 30,00 R$ 120,00
07-09-2026 Liberação de dinheiro 333 R$ 10,00 R$ 130,00
07-09-2026 Liberação de dinheiro 334 R$ 10,00 R$ 140,00
1/2

Data Descrição ID da operação Valor Saldo
08-09-2026 Rendimentos 444 R$ 0,02 R$ 140,02
Data de geração: 01-10-2026
Mercado Pago Instituição de Pagamento Ltda. CNPJ n.º 00.000.000/0001-00.
2/2
"""


def test_parse_mercadopago_lines_extracts_all_entries():
    lines = _parse_mercadopago_lines(SAMPLE_TEXT, account_id=1)
    assert len(lines) == 5


def test_parse_mercadopago_lines_dates_in_order():
    lines = _parse_mercadopago_lines(SAMPLE_TEXT, account_id=1)
    assert [line.statement_date for line in lines] == [
        date(2026, 9, 5),
        date(2026, 9, 6),
        date(2026, 9, 7),
        date(2026, 9, 7),
        date(2026, 9, 8),
    ]


def test_parse_mercadopago_lines_infers_sign_from_balance_delta():
    lines = _parse_mercadopago_lines(SAMPLE_TEXT, account_id=1)
    assert lines[0].amount == Decimal("50.00")
    # Débito: o texto da coluna Valor não tem "-", só a variação do saldo
    # (150,00 -> 120,00) revela que é uma saída.
    assert lines[1].amount == Decimal("-30.00")
    assert lines[4].amount == Decimal("0.02")


def test_parse_mercadopago_lines_description_is_just_the_description_column():
    lines = _parse_mercadopago_lines(SAMPLE_TEXT, account_id=1)
    assert lines[1].description == "Pagamento de boleto"


def test_parse_mercadopago_lines_stops_at_footer():
    lines = _parse_mercadopago_lines(SAMPLE_TEXT, account_id=1)
    assert all("geração" not in line.description for line in lines)
    assert all("Instituição de Pagamento" not in line.description for line in lines)
    assert len(lines) == 5


def test_parse_mercadopago_lines_distinct_hashes_for_same_day_repeated_entries():
    lines = _parse_mercadopago_lines(SAMPLE_TEXT, account_id=1)
    hashes = {line.line_hash for line in lines}
    assert len(hashes) == len(lines)


def test_parse_mercadopago_lines_raises_when_valor_does_not_match_balance_delta():
    bad_text = SAMPLE_TEXT.replace(
        "05-09-2026 Pix recebido de Fulano 111 R$ 50,00 R$ 150,00",
        "05-09-2026 Pix recebido de Fulano 111 R$ 999,00 R$ 150,00",
    )
    with pytest.raises(ValueError):
        _parse_mercadopago_lines(bad_text, account_id=1)


def test_parse_mercadopago_lines_raises_when_final_balance_does_not_close():
    bad_text = SAMPLE_TEXT.replace("Saldo final: R$ 140,02", "Saldo final: R$ 999,99")
    with pytest.raises(ValueError):
        _parse_mercadopago_lines(bad_text, account_id=1)


def test_parse_mercadopago_lines_raises_when_no_entries_found():
    with pytest.raises(ValueError):
        _parse_mercadopago_lines(
            "EXTRATO DE CONTA\nSaldo inicial: R$ 0,00 Saldo final: R$ 0,00\nData de geração: 01-10-2026",
            account_id=1,
        )


def test_extract_conta_label_reads_account_number_from_header():
    assert extract_conta_label(SAMPLE_TEXT) == "11111111111"


def test_sniff_pdf_format_recognizes_mercado_pago_by_name_in_text():
    assert sniff_pdf_format(SAMPLE_TEXT) == "mercado pago"
