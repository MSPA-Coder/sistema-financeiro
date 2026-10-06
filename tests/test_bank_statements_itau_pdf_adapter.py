"""Testes do parser de texto do extrato de conta do Itaú (PDF).

Texto sintético no layout observado na extração real via pdfplumber: o mais
recente primeiro, sinal no próprio valor, "SALDO DO DIA" às vezes no meio dos
lançamentos do dia e, no topo, o saldo de hoje, depois do fim do período.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bank_statements.adapters import (
    _parse_itau_lines,
    _saldo_do_pdf,
    extract_conta_label,
    sniff_pdf_format,
)

SAMPLE_TEXT = """
FULANO DE TAL 000.000.000-00 agência: 1234 conta: 012345-6
saldo em conta Limite da Conta utilizado Limite da Conta disponível Limite da Conta total *
R$ 0,49 R$ 0,00 R$ 100,00 R$ 100,00
extrato conta / lançamentos
período de visualização: 01/01/2026 até 31/03/2026 emitido em: 04/10/2026 19:42:22
data lançamentos valor (R$) saldo (R$)
02/10/2026 SALDO DO DIA 0,49
02/03/2026 SALDO DO DIA 1,00
02/03/2026 PIX TRANSF FULANO02/03 -100,00
02/03/2026 PGTO INSS 00000000001 60,00
02/03/2026 PGTO INSS 00000000002 40,50
02/02/2026 PGTO INSS 00000000001 60,00
02/02/2026 SALDO DO DIA 0,50
02/02/2026 PIX TRANSF -60,00
02/02/2026 PIX TRANSF -60,00
02/02/2026 PGTO INSS 00000000002 60,00
31/12/2025 SALDO DO DIA 0,50
Aviso!
Os saldos acima são baseados nas informações disponíveis até esse instante.
Consulte www.itau.com.br/contacorrente ou fale com o Itaú.
"""


def test_le_os_lancamentos_e_ignora_as_linhas_de_saldo():
    lines = _parse_itau_lines(SAMPLE_TEXT, account_id=1)
    assert len(lines) == 7
    assert all("SALDO" not in line.description for line in lines)


def test_ordena_do_mais_antigo_para_o_mais_recente():
    lines = _parse_itau_lines(SAMPLE_TEXT, account_id=1)
    assert [line.statement_date for line in lines] == [date(2026, 2, 2)] * 4 + [date(2026, 3, 2)] * 3


def test_sinal_vem_do_valor():
    lines = _parse_itau_lines(SAMPLE_TEXT, account_id=1)
    por_descricao = {line.description: line.amount for line in lines}
    assert por_descricao["PIX TRANSF FULANO02/03"] == Decimal("-100.00")
    assert por_descricao["PGTO INSS 00000000002"] in {Decimal("60.00"), Decimal("40.50")}


def test_linhas_repetidas_no_mesmo_dia_tem_hash_distinto():
    lines = _parse_itau_lines(SAMPLE_TEXT, account_id=1)
    assert len({line.line_hash for line in lines}) == len(lines)


def test_recusa_quando_os_lancamentos_nao_fecham_com_o_saldo_do_dia():
    errado = SAMPLE_TEXT.replace("02/03/2026 SALDO DO DIA 1,00", "02/03/2026 SALDO DO DIA 9,00")
    with pytest.raises(ValueError, match="não fecham"):
        _parse_itau_lines(errado, account_id=1)


def test_saldo_de_hoje_fora_do_periodo_nao_entra_na_conferencia():
    # 0,49 em 02/10 não bate com 1,00 em 02/03; se entrasse, a leitura falharia.
    assert len(_parse_itau_lines(SAMPLE_TEXT, account_id=1)) == 7


def test_recusa_sem_periodo_de_visualizacao():
    with pytest.raises(ValueError, match="período"):
        _parse_itau_lines(SAMPLE_TEXT.replace("período de visualização", "periodo"), account_id=1)


def test_recusa_lancamento_antes_do_primeiro_saldo():
    # Sem o saldo de 31/12 e sem o de 02/02, os lançamentos de 02/02 não têm
    # saldo de partida contra o qual conferir.
    sem_saldo_inicial = SAMPLE_TEXT.replace("31/12/2025 SALDO DO DIA 0,50\n", "").replace(
        "02/02/2026 SALDO DO DIA 0,50\n", ""
    )
    with pytest.raises(ValueError, match="primeiro saldo"):
        _parse_itau_lines(sem_saldo_inicial, account_id=1)


def test_conta_vem_do_rotulo_minusculo_depois_da_agencia():
    assert extract_conta_label(SAMPLE_TEXT) == "012345-6"


def test_formato_reconhecido_pelo_endereco_do_itau():
    assert sniff_pdf_format(SAMPLE_TEXT) == "itaú"


def test_pix_para_o_itau_no_extrato_de_outro_banco_nao_confunde_o_formato():
    texto = "Mercado Pago\nConta: 11111111111\n05-09-2026 Pix enviado Itaú 111 R$ 50,00 R$ 150,00"
    assert sniff_pdf_format(texto) == "mercado pago"


def test_saldo_informado_e_o_do_ultimo_dia_dentro_do_periodo():
    assert _saldo_do_pdf(SAMPLE_TEXT) == (Decimal("1.00"), date(2026, 3, 2))
