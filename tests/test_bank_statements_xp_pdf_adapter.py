"""Extrato em PDF da conta de investimento da XP Investimentos.

O texto corrido do PDF mistura as linhas de descrição de linhas vizinhas
("TED BCO 336 ..." vem antes da data, "DE TED - SPB" depois), então o adapter lê
a geometria: filetes horizontais separam as linhas da tabela. Estes testes
montam páginas sintéticas com essa geometria (valores inventados) e fixam: a
descrição de várias linhas fica na linha certa; o sinal vem do "-R$" e a cadeia
de saldos confere cada linha; um saldo que não fecha reprova; a continuação sem
cabeçalho funciona; sem filetes cada palavra vai para a data mais próxima; o
saldo do fim do período sai da linha mais recente; e a instituição cadastrada
como "SCP XP Investimestos" usa o mesmo adapter.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from bank_statements import adapters
from bank_statements.adapters import (
    XpPdfStatementAdapter,
    _parse_xp_linhas,
    _saldo_do_xp,
    _xp_ler,
    get_statement_adapter,
    pdf_institution_names,
    sniff_pdf_format,
)

X_LIQ, X_MOV, X_HIST, X_VALOR, X_SALDO = 35, 117, 199, 417, 499


def _palavras(texto, x, top):
    """Uma palavra por token, com x crescente, como o pdfplumber devolve."""
    achadas = []
    for token in texto.split(" "):
        achadas.append({"text": token, "x0": x, "top": top})
        x += 6 * len(token) + 4
    return achadas


def _pagina(linhas, *, com_cabecalho=True, com_filetes=True, topo_cabecalho=277.1):
    """Página sintética. Cada linha: `data`, `top` (da linha da data), `desc`
    (lista de `(top, texto)`), `valor` e `saldo` (textos)."""
    palavras = []
    rects = []
    if com_cabecalho:
        palavras += _palavras("Saldo total projetado:", 25, 256.4)
        for texto, x in (("Liq", X_LIQ), ("Mov", X_MOV), ("Histórico", X_HIST), ("Valor", X_VALOR), ("Saldo", X_SALDO)):
            palavras += _palavras(texto, x, topo_cabecalho)
        if com_filetes:
            rects.append({"x0": 25, "x1": 107, "top": 293.6, "bottom": 294.6})
    for linha in linhas:
        palavras += _palavras(linha["data"], X_LIQ, linha["top"])
        palavras += _palavras(linha["data"], X_MOV, linha["top"])
        for top, texto in linha["desc"]:
            palavras += _palavras(texto, X_HIST, top)
        palavras += _palavras(linha["valor"], X_VALOR, linha["top"])
        palavras += _palavras(linha["saldo"], X_SALDO, linha["top"])
        if com_filetes:
            rects.append({"x0": 25, "x1": 107, "top": linha["fim"], "bottom": linha["fim"] + 1})
    palavras += _palavras("Lançamentos futuros", 25, 700.0)
    return SimpleNamespace(extract_words=lambda: palavras, rects=rects)


LINHAS = [
    {"data": "24/06/2026", "top": 299.4, "fim": 314.6, "desc": [(299.4, "COMPRA TESOURO DIRETO CLIENTES")],
     "valor": "-R$ 100,00", "saldo": "R$ 50,00"},
    {"data": "23/06/2026", "top": 320.4, "fim": 340.0,
     "desc": [(316.5, "TED BCO 336 AGE 1 CTA 99 - RECEBIMENTO"), (328.7, "DE TED - SPB")],
     "valor": "R$ 150,00", "saldo": "R$ 150,00"},
    {"data": "22/06/2026", "top": 345.8, "fim": 361.0,
     "desc": [(341.5, "IRRF S/RESGATE FUNDOS - Fundo X -"), (350.0, "Inifinite")],
     "valor": "-R$ 5,00", "saldo": "R$ 0,00"},
]


def _linhas(pagina):
    return _parse_xp_linhas(_xp_ler([pagina]), account_id=1)


def test_descricao_de_varias_linhas_fica_na_linha_certa_e_o_sinal_vem_do_texto():
    linhas = _linhas(_pagina(LINHAS))
    por_data = {linha.statement_date: linha for linha in linhas}
    assert por_data[date(2026, 6, 24)].description == "COMPRA TESOURO DIRETO CLIENTES"
    assert por_data[date(2026, 6, 24)].amount == Decimal("-100.00")
    assert por_data[date(2026, 6, 23)].description == "TED BCO 336 AGE 1 CTA 99 - RECEBIMENTO DE TED - SPB"
    assert por_data[date(2026, 6, 23)].amount == Decimal("150.00")
    assert por_data[date(2026, 6, 22)].description == "IRRF S/RESGATE FUNDOS - Fundo X - Inifinite"
    assert por_data[date(2026, 6, 22)].amount == Decimal("-5.00")
    assert len(linhas) == 3


def test_saldo_que_nao_fecha_com_o_valor_reprova():
    errada = [dict(LINHAS[0]), dict(LINHAS[1]), {**LINHAS[2], "saldo": "R$ 1,00"}]
    with pytest.raises(ValueError, match="Inconsistência no extrato XP"):
        _linhas(_pagina(errada))


def test_extrato_sem_linhas_e_recusado():
    with pytest.raises(ValueError, match="Nenhum lançamento"):
        _linhas(_pagina([]))


def test_sem_filetes_cada_palavra_vai_para_a_data_mais_proxima():
    linhas = _linhas(_pagina(LINHAS, com_filetes=False))
    por_data = {linha.statement_date: linha for linha in linhas}
    assert por_data[date(2026, 6, 23)].description == "TED BCO 336 AGE 1 CTA 99 - RECEBIMENTO DE TED - SPB"


def test_pagina_de_continuacao_sem_cabecalho_usa_as_colunas_da_anterior():
    primeira = _pagina(LINHAS[:2])
    continuacao = _pagina([LINHAS[2]], com_cabecalho=False)
    linhas = _parse_xp_linhas(_xp_ler([primeira, continuacao]), account_id=1)
    assert len(linhas) == 3


def test_linhas_repetidas_do_mesmo_dia_recebem_hashes_diferentes():
    iguais = [
        {"data": "23/06/2026", "top": 299.4, "fim": 314.6, "desc": [(299.4, "COMPRA TESOURO DIRETO CLIENTES")],
         "valor": "-R$ 10,00", "saldo": "R$ 0,00"},
        {"data": "23/06/2026", "top": 320.4, "fim": 340.0, "desc": [(320.4, "COMPRA TESOURO DIRETO CLIENTES")],
         "valor": "-R$ 10,00", "saldo": "R$ 10,00"},
    ]
    linhas = _linhas(_pagina(iguais))
    assert len({linha.line_hash for linha in linhas}) == 2


class _PdfFalso:
    def __init__(self, paginas):
        self.pages = paginas

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_saldo_do_fim_do_periodo_e_o_da_linha_mais_recente(monkeypatch):
    monkeypatch.setattr(adapters, "_xp_abrir", lambda raw: _PdfFalso([_pagina(LINHAS)]))
    texto = "XP INVESTIMENTOS ... De: 01/04/2026 Até: 30/06/2026 Saldo disponível: R$ 0,00"
    assert _saldo_do_xp(b"x", texto) == (Decimal("50.00"), date(2026, 6, 30))
    assert _saldo_do_xp(b"x", "sem periodo") is None


def test_o_adapter_le_o_arquivo_pelo_pdf(monkeypatch):
    monkeypatch.setattr(adapters, "_xp_abrir", lambda raw: _PdfFalso([_pagina(LINHAS)]))
    arquivo = SimpleUploadedFile("extrato.pdf", b"%PDF-falso", content_type="application/pdf")
    assert len(XpPdfStatementAdapter().parse(arquivo, account_id=7)) == 3


def test_o_formato_e_reconhecido_pelo_texto():
    assert sniff_pdf_format("02/10/2026 XP INVESTIMENTOS CORRETORA DE CÂMBIO ... Extrato da conta") == "xp investimentos"
    assert sniff_pdf_format("Extrato Genial Investimentos") == "genial"


@pytest.mark.parametrize("nome", ["XP Investimentos", "SCP XP Investimentos", "SCP XP Investimestos", "scp xp investimestos"])
def test_a_instituicao_cadastrada_com_qualquer_um_dos_nomes_usa_o_adapter_da_xp(nome):
    instituicao = SimpleNamespace(homologada=True, institution_name=nome)
    arquivo = SimpleUploadedFile("x.pdf", b"%PDF", content_type="application/pdf")
    assert isinstance(get_statement_adapter(arquivo, institution=instituicao), XpPdfStatementAdapter)
    assert "scp xp investimestos" in pdf_institution_names("xp investimentos")


def test_a_xp_banco_nao_ganha_o_adapter_de_pdf():
    arquivo = SimpleUploadedFile("x.pdf", b"%PDF", content_type="application/pdf")
    with pytest.raises(ValueError, match="Não há suporte"):
        get_statement_adapter(arquivo, institution=SimpleNamespace(homologada=True, institution_name="XP"))


# --- Detecção da conta e importação (com banco) ----------------------------------


@pytest.fixture
def conta_scp(db):
    from accounts.models import AccountOwner, AppUser, UserOwnerAccess
    from banking.models import FinancialAccount, FinancialInstitution

    user = AppUser.objects.create_user(username="operador-xp", password="senha-segura")
    titular = AccountOwner.objects.create(name="Mariano")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    instituicao = FinancialInstitution.objects.create(
        institution_name="SCP XP Investimestos", institution_type="Corretora", homologada=True
    )
    conta = FinancialAccount.objects.create(
        owner=titular, institution=instituicao, account_name="Conta 05", statement_identifier="323220"
    )
    return user, conta


TEXTO_XP = (
    "02/10/2026 15:58 XP INVESTIMENTOS CORRETORA DE CÂMBIO, TÍTULOS E VALORES MOBILIÁRIOS\n"
    "Extrato da conta\nMARIANO SERGIO PACHECO DE ANGELO Conta: 323220\nDe: 01/04/2026 Até: 30/06/2026\n"
)


def test_a_conta_e_detectada_pelo_numero_mesmo_com_o_nome_antigo_da_instituicao(conta_scp, monkeypatch):
    from bank_statements import pending_imports

    user, conta = conta_scp
    monkeypatch.setattr(pending_imports, "extract_pdf_text", lambda raw: TEXTO_XP)
    monkeypatch.setattr(adapters, "_xp_abrir", lambda raw: _PdfFalso([_pagina(LINHAS)]))
    detectada, rotulo, erro = pending_imports.detect_account(
        user, filename="extrato_de_01-04-2026_ate_30-06-2026.pdf", content_type="application/pdf", raw=b"%PDF"
    )
    assert erro == ""
    assert detectada == conta
    assert rotulo == "SCP XP Investimestos · conta 323220"


def test_importar_o_pdf_grava_as_linhas_com_o_sinal_certo_e_reimportar_nao_duplica(conta_scp, monkeypatch):
    from bank_statements.models import BankStatementLine
    from bank_statements.services import import_statement_file

    user, conta = conta_scp
    monkeypatch.setattr(adapters, "_xp_abrir", lambda raw: _PdfFalso([_pagina(LINHAS)]))
    arquivo = SimpleUploadedFile("extrato.pdf", b"%PDF-falso", content_type="application/pdf")
    lote, inseridas, repetidas = import_statement_file(user, account_id=conta.id, uploaded_file=arquivo)
    assert (inseridas, repetidas) == (3, 0)
    assert sorted(BankStatementLine.objects.filter(import_batch=lote).values_list("amount", flat=True)) == [
        Decimal("-100.00"), Decimal("-5.00"), Decimal("150.00"),
    ]
    arquivo.seek(0)
    _, inseridas, repetidas = import_statement_file(user, account_id=conta.id, uploaded_file=arquivo)
    assert (inseridas, repetidas) == (0, 3)
