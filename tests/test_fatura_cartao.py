"""Importar a fatura do cartão (CSV da C6 e da XP).

As faturas aqui são sintéticas, no formato exato que os dois bancos exportam:
nenhum dado real de fatura entra no repositório.

O que os testes fixam:

- a leitura inverte o sinal (compra na fatura é positiva; no cartão é
  despesa), dá à parcela a data em que ela entra na fatura e mantém estáveis
  os hashes que impedem a reimportação de duplicar;
- a fatura só entra em conta de cartão, e cartão só recebe fatura;
- o processamento não lança de novo o que já está no saldo inicial, cria as
  parcelas futuras que ainda não estão nele, casa a parcela do mês seguinte
  com a que já existe e concilia o pagamento com a transferência.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements import fatura
from bank_statements.fatura_csv import FORMATO_C6, FORMATO_XP, formato_da_fatura, ler_fatura
from bank_statements.models import BankStatementLine
from bank_statements.services import import_statement_file
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from reports.services import VIEW_REALIZED, decimal_balance_before
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import TransactionRequest, create_transaction_batch

CABECALHO_C6 = (
    "Data de Compra;Nome no Cartão;Final do Cartão;Categoria;Descrição;Parcela;"
    "Valor (em US$);Cotação (em R$);Valor (em R$)"
)


def _c6(*linhas: str) -> bytes:
    return "\r\n".join([CABECALHO_C6, *linhas]).encode("utf-8")


def _xp(*linhas: str) -> bytes:
    return "\r\n".join(["Data;Estabelecimento;Portador;Valor;Parcela", *linhas]).encode("utf-8-sig")


FATURA_MARCO = _c6(
    "20/02/2026;MARIANO S;1111;Padaria;PADARIA ANTIGA;Única;0;0;50.00",
    "10/12/2025;MARIANO S;1111;Varejo;LOJA PARCELADA ANTIGA;3/4;0;0;120.00",
    "05/03/2026;MARIANO S;1111;Supermercados;MERCADO BOM;Única;0;0;200.00",
    "06/03/2026;CLAUDIA S;2222;Farmácia;FARMACIA;Única;0;0;80.00",
    "07/03/2026;MARIANO S;1111;Eletrônicos;TV NOVA;1/3;0;0;300.00",
    "08/03/2026;MARIANO S;1111;-;AMAZON US;Única;10.00;5.00;50.00",
    "08/03/2026;MARIANO S;1111;-;CAFE;Única;0;0;10.00",
    "08/03/2026;MARIANO S;1111;-;CAFE;Única;0;0;10.00",
    "09/03/2026;MARIANO S;1111;-;PAGUE MENOS;Única;0;0;30.00",
    "09/03/2026;MARIANO S;1111;-;Estorno Tarifa;Única;0;0;-15.00",
    "09/03/2026;MARIANO S;1111;-;Ajuste;Única;0;0;0.00",
    "25/03/2026;MARIANO S;1111;-;Pag Fatura Boleto;Única;0;0;-300.00",
    "25/03/2026;MARIANO S;1111;-;Pag Fatura Boleto;Única;0;0;-200.00",
)

FATURA_ABRIL = _c6(
    "07/03/2026;MARIANO S;1111;Eletrônicos;TV NOVA;2/3;0;0;300.00",
    "06/04/2026;CLAUDIA S;2222;Farmácia;FARMACIA;Única;0;0;40.00",
)


# --- Leitura (sem banco) ------------------------------------------------------


def _por_descricao(linhas):
    return {linha.description.split(" · ")[0]: linha for linha in linhas}


def test_c6_inverte_o_sinal_e_da_a_parcela_a_data_em_que_ela_entra():
    linhas = _por_descricao(ler_fatura(FATURA_MARCO, account_id=1))

    tv = linhas["TV NOVA"]
    assert tv.amount == Decimal("-300.00")
    assert (tv.installment_current, tv.installment_total) == (1, 3)
    antiga = linhas["LOJA PARCELADA ANTIGA"]
    assert antiga.purchase_date == date(2025, 12, 10)
    assert antiga.statement_date == date(2026, 2, 10)
    assert linhas["Estorno Tarifa"].amount == Decimal("15.00")
    assert linhas["Pag Fatura Boleto"].amount > 0


def test_parcela_que_ja_traz_a_propria_data_nao_e_deslocada():
    # A anuidade da C6 vem com a data do lançamento, não a da compra: somar
    # 3 meses a jogaria para dezembro, depois do fechamento desta fatura.
    arquivo = _c6(
        "13/09/2026;MARIANO S;1111;-;Anuidade Diferenciada;4/12;0;0;98.00",
        "24/06/2026;MARIANO S;1111;-;LOJA;3/6;0;0;50.00",
        "17/09/2026;MARIANO S;1111;-;MERCADO;Única;0;0;10.00",
    )

    linhas = _por_descricao(ler_fatura(arquivo, account_id=1, dia_de_fechamento=19))

    assert linhas["Anuidade Diferenciada"].statement_date == date(2026, 9, 13)
    assert linhas["LOJA"].statement_date == date(2026, 8, 24)


def test_fatura_so_com_parcelas_antigas_ainda_desloca_as_datas():
    # Cartão pouco usado: a fatura só traz uma parcela, com a data da compra.
    linhas = ler_fatura(_xp("05/10/2025;LOJA;MARIANO S;R$ 127,65;5 de 12"), account_id=1, dia_de_fechamento=1)

    assert linhas[0].statement_date == date(2026, 2, 5)


def test_c6_guarda_portador_dolar_e_categoria_do_banco():
    linhas = ler_fatura(FATURA_MARCO, account_id=1)
    descricoes = {linha.description for linha in linhas}

    # Dois portadores no arquivo: o nome vai para a descrição.
    assert "FARMACIA · Claudia" in descricoes
    assert "AMAZON US (US$ 10.00) · Mariano" in descricoes
    mercado = next(linha for linha in linhas if linha.description == "MERCADO BOM · Mariano")
    assert mercado.bank_category == "Supermercados"
    assert mercado.card_holder == "Mariano"


def test_linha_zerada_some_e_compras_identicas_viram_duas_linhas():
    linhas = ler_fatura(FATURA_MARCO, account_id=1)

    assert not any(linha.description.startswith("Ajuste") for linha in linhas)
    cafes = [linha for linha in linhas if linha.description.startswith("CAFE")]
    assert len(cafes) == 2
    assert cafes[0].line_hash != cafes[1].line_hash


def test_reler_o_mesmo_arquivo_gera_os_mesmos_hashes():
    primeira = [linha.line_hash for linha in ler_fatura(FATURA_MARCO, account_id=7)]
    segunda = [linha.line_hash for linha in ler_fatura(FATURA_MARCO, account_id=7)]

    assert primeira == segunda
    assert len(set(primeira)) == len(primeira)


def test_xp_le_valor_em_reais_parcela_por_extenso_e_pagamento():
    linhas = ler_fatura(
        _xp(
            "05/09/2025;LOJA X;MARIANO S;R$ 1.234,56;6 de 10",
            "10/02/2026;Pagamentos Validos Normais;MARIANO S;R$ -2.452,46;-",
        ),
        account_id=1,
    )

    loja, pagamento = linhas
    assert loja.amount == Decimal("-1234.56")
    assert (loja.installment_current, loja.installment_total) == (6, 10)
    assert loja.statement_date == date(2026, 2, 5)
    assert pagamento.amount == Decimal("2452.46")
    assert pagamento.installment_current is None
    # Um portador só: a descrição fica limpa.
    assert loja.description == "LOJA X"


def test_formato_vem_do_cabecalho():
    assert formato_da_fatura(FATURA_MARCO) == FORMATO_C6
    assert formato_da_fatura(_xp("05/09/2025;LOJA;MARIANO;R$ 1,00;-")) == FORMATO_XP
    assert formato_da_fatura(b"data;descricao;valor\r\n2026-01-01;x;1.00") is None


def test_parcela_invalida_e_recusada():
    with pytest.raises(ValueError, match="Parcela"):
        ler_fatura(_c6("05/03/2026;MARIANO S;1111;-;LOJA;5/3;0;0;10.00"), account_id=1)


@pytest.mark.parametrize(
    ("descricao", "valor", "esperado"),
    [
        ("Pag Fatura Boleto", "300.00", True),
        ("Pagamentos Validos Normais", "10.00", True),
        ("PAGUE MENOS", "30.00", False),
        ("Pag Fatura Boleto", "-300.00", False),
    ],
)
def test_so_credito_com_descricao_de_pagamento_e_pagamento(descricao, valor, esperado):
    linha = BankStatementLine(description=descricao, amount=Decimal(valor))

    assert fatura.eh_pagamento(linha) is esperado


# --- Importação e processamento ----------------------------------------------


@pytest.fixture
def cenario():
    usuario = AppUser.objects.create_user(
        username="fatura", password="troca-esta-senha-no-primeiro-acesso", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Maridito")
    UserOwnerAccess.objects.create(
        user=usuario, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="C6 teste", institution_type="Banco")
    corrente = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta corrente",
        initial_balance=Decimal("5000.00"), initial_balance_date=date(2026, 1, 1),
    )
    cartao = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Carbon",
        account_kind=ACCOUNT_KIND_CREDIT_CARD, card_closing_day=19, card_due_day=25,
        initial_balance=Decimal("-500.00"), initial_balance_date=date(2026, 3, 1),
    )
    save_transfer_destination_accesses(usuario, {cartao.id, corrente.id})
    transferencia = CashFlowCategory.objects.create(category_name="Transf. teste", kind=CATEGORY_KIND_TRANSFER)
    outros, _ = CashFlowCategory.objects.get_or_create(
        category_name="Outros", defaults={"kind": CATEGORY_KIND_MANAGERIAL}
    )
    mercado = CashFlowCategory.objects.create(category_name="Mercado teste", kind=CATEGORY_KIND_MANAGERIAL)
    saude = CashFlowCategory.objects.create(category_name="Saúde teste", kind=CATEGORY_KIND_MANAGERIAL)
    return {
        "usuario": usuario, "corrente": corrente, "cartao": cartao, "transferencia": transferencia,
        "outros": outros, "mercado": mercado, "saude": saude,
    }


def _arquivo(conteudo: bytes, nome: str = "fatura.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(nome, conteudo, content_type="text/csv")


def _importar(c, conteudo):
    return import_statement_file(c["usuario"], account_id=c["cartao"].id, uploaded_file=_arquivo(conteudo))


def _pagamento_de_300(c):
    return create_transaction_batch(
        TransactionRequest(
            account_id=c["corrente"].id, category_id=c["transferencia"].id, entry_type=ENTRY_TYPE_EXPENSE,
            description="Fatura", entry_amount=Decimal("300.00"), installments=1,
            due_date=date(2026, 3, 25), status=STATUS_PROJECTED, counterparty_account_id=c["cartao"].id,
        ),
        user=c["usuario"],
    )


def _compra_anterior_no_mercado(c):
    """Ensina a categoria: a última compra com essa descrição foi em Mercado."""
    create_transaction_batch(
        TransactionRequest(
            account_id=c["cartao"].id, category_id=c["mercado"].id, entry_type=ENTRY_TYPE_EXPENSE,
            description="MERCADO BOM", entry_amount=Decimal("99.00"), installments=1,
            due_date=date(2026, 1, 5), status=STATUS_REALIZED, realized_date=date(2026, 1, 5),
        ),
        user=c["usuario"],
    )


def _saldo_realizado(conta):
    return decimal_balance_before([conta.id], date(2027, 1, 1), VIEW_REALIZED)


@pytest.mark.django_db
def test_fatura_em_conta_comum_e_recusada(cenario):
    with pytest.raises(ValueError, match="fatura de cartão"):
        import_statement_file(
            cenario["usuario"], account_id=cenario["corrente"].id, uploaded_file=_arquivo(FATURA_MARCO)
        )


@pytest.mark.django_db
def test_extrato_em_conta_de_cartao_e_recusado(cenario):
    with pytest.raises(ValueError, match="Formato de fatura"):
        _importar(cenario, b"data;descricao;valor\r\n2026-03-05;Compra;-10.00")


@pytest.mark.django_db
def test_importar_so_grava_linhas_e_reimportar_nao_duplica(cenario):
    _lote, inseridas, _ = _importar(cenario, FATURA_MARCO)
    _outro, de_novo, ignoradas = _importar(cenario, FATURA_MARCO)

    assert inseridas == 12
    assert (de_novo, ignoradas) == (0, 12)
    assert not CashFlowEntry.objects.filter(account=cenario["cartao"]).exists()


@pytest.mark.django_db
def test_previa_diz_o_que_cada_linha_vira(cenario):
    _compra_anterior_no_mercado(cenario)
    _pagamento_de_300(cenario)
    lote, _, _ = _importar(cenario, FATURA_MARCO)

    planos = fatura.planejar(cenario["cartao"], fatura.linhas_novas(lote))
    acoes = {(p.linha.description.split(" · ")[0], p.linha.amount): p for p in planos}

    assert acoes[("PADARIA ANTIGA", Decimal("-50.00"))].acao == fatura.SALDO_INICIAL
    antiga = acoes[("LOJA PARCELADA ANTIGA", Decimal("-120.00"))]
    assert (antiga.acao, antiga.futuras_a_partir_de) == (fatura.SALDO_INICIAL, 4)
    assert acoes[("TV NOVA", Decimal("-300.00"))].acao == fatura.PARCELADO_NOVO
    assert acoes[("Estorno Tarifa", Decimal("15.00"))].acao == fatura.ESTORNO
    assert acoes[("PAGUE MENOS", Decimal("-30.00"))].acao == fatura.COMPRA
    assert acoes[("Pag Fatura Boleto", Decimal("300.00"))].acao == fatura.PAGAMENTO
    assert acoes[("Pag Fatura Boleto", Decimal("200.00"))].acao == fatura.PAGAMENTO_SEM_PAR
    assert acoes[("MERCADO BOM", Decimal("-200.00"))].categoria == cenario["mercado"]
    assert acoes[("FARMACIA", Decimal("-80.00"))].categoria == cenario["outros"]


@pytest.mark.django_db
def test_resumo_bate_com_o_total_da_fatura(cenario):
    lote, _, _ = _importar(cenario, FATURA_MARCO)

    resumo = fatura.resumir(lote.lines.all())

    # 50 + 120 + 200 + 80 + 300 + 50 + 10 + 10 + 30 - 15
    assert resumo.total_da_fatura == Decimal("835.00")
    assert resumo.pagamentos == Decimal("500.00")


@pytest.mark.django_db
def test_processar_lanca_concilia_e_nao_conta_o_saldo_inicial_duas_vezes(cenario):
    _compra_anterior_no_mercado(cenario)
    origem, destino = _pagamento_de_300(cenario)
    lote, _, _ = _importar(cenario, FATURA_MARCO)

    contagem = fatura.processar(
        cenario["usuario"], lote.id, {}
    )

    assert contagem[fatura.SALDO_INICIAL] == 2
    assert contagem[fatura.PAGAMENTO_SEM_PAR] == 1
    # A transferência foi realizada nas duas pontas, na data do pagamento.
    origem.refresh_from_db()
    destino.refresh_from_db()
    assert (origem.status, destino.status) == (STATUS_REALIZED, STATUS_REALIZED)
    assert destino.realized_date == date(2026, 3, 25)
    # A parcela 4/4 da compra antiga ficou para abril, sem a 3/4.
    antiga = CashFlowEntry.objects.get(account=cenario["cartao"], description__startswith="LOJA PARCELADA ANTIGA")
    assert (antiga.current_installment, antiga.installments, antiga.due_date) == (4, 4, date(2026, 3, 10))
    assert antiga.status != STATUS_REALIZED
    # TV nova: três parcelas, a primeira realizada.
    tv = CashFlowEntry.objects.filter(account=cenario["cartao"], description__startswith="TV NOVA").order_by("current_installment")
    assert [(e.current_installment, e.status == STATUS_REALIZED) for e in tv] == [(1, True), (2, False), (3, False)]
    estorno = CashFlowEntry.objects.get(account=cenario["cartao"], description__startswith="Estorno Tarifa")
    assert estorno.entry_type == ENTRY_TYPE_INCOME
    # Saldo: -500 inicial, -99 da compra de janeiro, -680 de compras, +15, +300 pagos.
    assert _saldo_realizado(cenario["cartao"]) == Decimal("-964.00")
    # O pagamento sem transferência continua pendente, e o lote não fecha.
    lote.refresh_from_db()
    assert lote.status == "importado"
    assert lote.lines.filter(status="novo").count() == 1


@pytest.mark.django_db
def test_parcela_do_mes_seguinte_casa_com_a_ja_lancada(cenario):
    lote, _, _ = _importar(cenario, FATURA_MARCO)
    fatura.processar(cenario["usuario"], lote.id, {})
    abril, inseridas, _ = _importar(cenario, FATURA_ABRIL)

    planos = {p.linha.description.split(" · ")[0]: p for p in fatura.planejar(cenario["cartao"], fatura.linhas_novas(abril))}
    assert planos["TV NOVA"].acao == fatura.PARCELA_LANCADA
    fatura.processar(cenario["usuario"], abril.id, {})

    tv = CashFlowEntry.objects.filter(account=cenario["cartao"], description__startswith="TV NOVA")
    assert tv.count() == 3
    segunda = tv.get(current_installment=2)
    assert segunda.status == STATUS_REALIZED
    assert segunda.realized_date == date(2026, 4, 7)
    assert inseridas == 2
    # A farmácia de abril herda a categoria da de março.
    farmacias = CashFlowEntry.objects.filter(account=cenario["cartao"], description="FARMACIA · Claudia")
    assert farmacias.count() == 2


@pytest.mark.django_db
def test_compra_lancada_a_mao_e_conciliada_em_vez_de_duplicada(cenario):
    manual = create_transaction_batch(
        TransactionRequest(
            account_id=cenario["cartao"].id, category_id=cenario["mercado"].id, entry_type=ENTRY_TYPE_EXPENSE,
            description="mercado", entry_amount=Decimal("200.00"), installments=1,
            due_date=date(2026, 3, 6), status=STATUS_PROJECTED,
        ),
        user=cenario["usuario"],
    )[0]
    lote, _, _ = _importar(cenario, FATURA_MARCO)

    fatura.processar(cenario["usuario"], lote.id, {})

    manual.refresh_from_db()
    assert manual.status == STATUS_REALIZED
    assert not CashFlowEntry.objects.filter(account=cenario["cartao"], description__startswith="MERCADO BOM").exists()


@pytest.mark.django_db
def test_processar_e_tudo_ou_nada(cenario):
    lote, _, _ = _importar(cenario, FATURA_MARCO)
    farmacia = lote.lines.get(description="FARMACIA · Claudia")

    with pytest.raises(ValueError, match="Categoria inválida"):
        fatura.processar(cenario["usuario"], lote.id, {str(farmacia.id): str(cenario["transferencia"].id)})

    assert not CashFlowEntry.objects.filter(account=cenario["cartao"]).exists()
    assert lote.lines.filter(status="novo").count() == 12


@pytest.mark.django_db
def test_tela_mostra_a_previa_e_grava_a_categoria_escolhida(cenario):
    client = Client()
    client.force_login(cenario["usuario"])
    resposta = client.post(
        "/banking/import/",
        {"account_id": cenario["cartao"].id, "statement_file": _arquivo(FATURA_MARCO)},
    )
    lote = cenario["cartao"].statement_imports.get()
    assert resposta.status_code == 302
    assert resposta["Location"] == f"/banking/import/{lote.id}/fatura/"

    previa = client.get(resposta["Location"])
    conteudo = previa.content.decode()
    assert previa.status_code == 200
    assert "Compra parcelada nova: cria as parcelas 1 a 3" in conteudo
    assert "Já está no saldo inicial; cria as parcelas 4 a 4 a vencer" in conteudo

    farmacia = lote.lines.get(description="FARMACIA · Claudia")
    gravada = client.post(
        f"/banking/import/{lote.id}/fatura/processar/", {f"categoria_{farmacia.id}": cenario["saude"].id}
    )
    assert gravada.status_code == 302
    entrada = CashFlowEntry.objects.get(account=cenario["cartao"], description="FARMACIA · Claudia")
    assert entrada.category == cenario["saude"]


@pytest.mark.django_db
def test_parcela_antiga_so_cria_as_parcelas_depois_do_saldo_inicial(cenario):
    # Saldo inicial em 01/03/2026. A compra de 10/11/2025 em 6x tem a 3ª e a
    # 4ª em faturas que o saldo inicial já cobre; só a 5ª e a 6ª são novas. A
    # de 3x termina antes do saldo inicial e não deixa nada.
    lote, _, _ = _importar(cenario, _c6(
        "10/11/2025;MARIANO S;1111;-;SEIS VEZES;2/6;0;0;60.00",
        "10/11/2025;MARIANO S;1111;-;TRES VEZES;2/3;0;0;30.00",
    ))

    planos = {p.linha.description: p for p in fatura.planejar(cenario["cartao"], fatura.linhas_novas(lote))}
    assert (planos["SEIS VEZES"].futuras_a_partir_de, planos["SEIS VEZES"].futuras_vencimento) == (5, date(2026, 3, 10))
    assert planos["TRES VEZES"].futuras_a_partir_de is None
    fatura.processar(cenario["usuario"], lote.id, {})

    criadas = CashFlowEntry.objects.filter(account=cenario["cartao"]).order_by("due_date")
    assert [(e.description, e.current_installment, e.due_date) for e in criadas] == [
        ("SEIS VEZES", 5, date(2026, 3, 10)), ("SEIS VEZES", 6, date(2026, 4, 10)),
    ]


@pytest.mark.django_db
def test_parcelado_pode_comecar_no_meio(cenario):
    criadas = create_transaction_batch(
        TransactionRequest(
            account_id=cenario["cartao"].id, category_id=cenario["outros"].id, entry_type=ENTRY_TYPE_EXPENSE,
            description="Compra antiga", entry_amount=Decimal("10.00"), installments=12,
            due_date=date(2026, 3, 10), status=STATUS_PROJECTED, first_installment=10,
        ),
        user=cenario["usuario"],
    )

    assert [(e.current_installment, e.installments, e.due_date) for e in criadas] == [
        (10, 12, date(2026, 3, 10)), (11, 12, date(2026, 4, 10)), (12, 12, date(2026, 5, 10)),
    ]
