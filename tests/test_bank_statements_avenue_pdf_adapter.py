"""Testes do parser de texto do extrato da conta brasileira da Avenue (PDF).

Texto sintético no layout observado na extração real via pdfplumber: descrição,
linha com as duas datas, sinal, valor e saldo, e a linha do ID; cabeçalho da
tabela repetido no topo de cada página.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bank_statements.adapters import (
    _parse_avenue_lines,
    _saldo_do_pdf,
    extract_conta_label,
    sniff_pdf_format,
)

SAMPLE_TEXT = """
FULANO DE TAL
CPF 000.***.***-00
Nº conta 012345678
Resumo da atividade da conta
Rua Tal - São Paulo - SP Saldo inicial R$ 10,00
Depósitos R$ 1.000,00
Retiradas R$ -1.000,00
Saldo final R$ 10,00
Histórico de transações liquidadas Período(01/01/2026 - 30/09/2026)
Data da Data da Valor em Saldo em
Descrição
liquidação transação R$ R$
Recurso recebido via PIX
19/03/2026 19/03/2026 + 1.000,00 1.010,00
ID aaaa
BI Remessa de Câmbio Padrão Investimento BR-EUA
20/03/2026 19/03/2026 - 989,00 21,00
ID bbbb
www.avenue.us 1/2
FULANO DE TAL
CPF 000.***.***-00
Nº conta 012345678
Data da Data da Valor em Saldo em
Descrição
liquidação transação R$ R$
BI IOF sob remessa de Câmbio Padrão BR-EUA
20/03/2026 19/03/2026 - 11,00 10,00
ID cccc
www.avenue.us 2/2
"""


def test_le_os_lancamentos_pela_data_da_liquidacao():
    lines = _parse_avenue_lines(SAMPLE_TEXT, account_id=1)
    assert [(line.statement_date, line.amount, line.description) for line in lines] == [
        (date(2026, 3, 19), Decimal("1000.00"), "Recurso recebido via PIX"),
        (date(2026, 3, 20), Decimal("-989.00"), "BI Remessa de Câmbio Padrão Investimento BR-EUA"),
        (date(2026, 3, 20), Decimal("-11.00"), "BI IOF sob remessa de Câmbio Padrão BR-EUA"),
    ]


def test_recusa_quando_o_saldo_da_linha_nao_bate():
    with pytest.raises(ValueError, match="não bate"):
        _parse_avenue_lines(SAMPLE_TEXT.replace("- 989,00 21,00", "- 989,00 99,00"), account_id=1)


def test_recusa_quando_nao_fecha_com_o_saldo_final():
    with pytest.raises(ValueError, match="saldo final"):
        _parse_avenue_lines(SAMPLE_TEXT.replace("Saldo final R$ 10,00", "Saldo final R$ 0,00"), account_id=1)


def test_recusa_sem_descricao():
    sem_descricao = SAMPLE_TEXT.replace("Recurso recebido via PIX\n", "")
    with pytest.raises(ValueError, match="descrição"):
        _parse_avenue_lines(sem_descricao, account_id=1)


def test_reconhece_formato_conta_e_saldo():
    assert sniff_pdf_format(SAMPLE_TEXT) == "avenue"
    assert extract_conta_label(SAMPLE_TEXT) == "012345678"
    assert _saldo_do_pdf(SAMPLE_TEXT) == (Decimal("10.00"), date(2026, 9, 30))
