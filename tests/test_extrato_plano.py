"""Plano do extrato de conta: classificação aprendida, transferências próprias
pareadas, regras explícitas e vínculo em mês fechado.

O CENÁRIO QUE MOTIVOU (setembro de 2026)

Um Pix da Genial para o C6 do próprio titular entrou como despesa em "Outros"
na conta de origem e como receita em "Outros" na de destino, inflando as duas
pontas do mês. O PDF da Genial rotula o aluguel de ações como "Rendimentos"
enquanto o usuário o classificava como "Aluguel de Ações": o rótulo do banco
errou onde o histórico acertava.

O QUE ESTE ARQUIVO FIXA

- o histórico vence o prefixo do extrato, e "Outros" nunca é aprendido;
- duas linhas de sinal oposto e mesmo valor em contas suas viram UMA
  transferência (nenhuma receita nem despesa gerencial);
- linha que cita um titular sem a outra ponta importada espera, em vez de virar
  lançamento;
- regra explícita manda a linha para a conta de destino certa;
- vincular um lançamento já realizado a uma linha vale em mês fechado.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess, UserTransferDestinationAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements import extrato
from bank_statements.classificacao import (
    ORIGEM_HISTORICO,
    ORIGEM_PADRAO,
    ORIGEM_PREFIXO,
    ORIGEM_REGRA,
    chave_da_linha,
    sugerir_categoria,
)
from bank_statements.models import (
    RULE_ACTION_CATEGORY,
    RULE_ACTION_IGNORE,
    RULE_ACTION_TRANSFER,
    RULE_SIGN_CREDIT,
    BankStatementImport,
    BankStatementLine,
    StatementRule,
)
from bank_statements.reconciliation import reconcile_line_with_entry
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_REALIZED,
)
from transactions import services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def mundo():
    user = AppUser.objects.create_user(username="operador-extrato", password="senha-segura")
    titular = AccountOwner.objects.create(name="Mariano")
    outro = AccountOwner.objects.create(name="Esther")
    for dono in (titular, outro):
        UserOwnerAccess.objects.create(
            user=user, owner=dono, can_view=True, can_create=True, can_update=True, can_delete=True
        )
    genial = FinancialInstitution.objects.create(institution_name="Genial", institution_type="Corretora")
    c6 = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    bb = FinancialInstitution.objects.create(institution_name="Banco do Brasil", institution_type="Banco")
    contas = {
        "genial": FinancialAccount.objects.create(owner=titular, institution=genial, account_name="Conta 03"),
        "c6": FinancialAccount.objects.create(owner=titular, institution=c6, account_name="Conta 02"),
        "bb": FinancialAccount.objects.create(owner=titular, institution=bb, account_name="Conta 06"),
        "rende": FinancialAccount.objects.create(
            owner=titular, institution=bb, account_name="Rende Fácil", account_kind="aplicacao"
        ),
    }
    save_transfer_destination_accesses(user, {conta.id for conta in contas.values()})
    categorias = {
        "outros": CashFlowCategory.objects.create(category_name="Outros"),
        "rendimentos": CashFlowCategory.objects.create(category_name="Rendimentos"),
        "aluguel": CashFlowCategory.objects.create(category_name="Aluguel de Açoes / Dividendos / JCP"),
        "corretagem": CashFlowCategory.objects.create(category_name="Corretagem"),
        "loterias": CashFlowCategory.objects.create(category_name="Loterias"),
        "bolsa": CashFlowCategory.objects.create(category_name="Operações em Bolsa", kind=CATEGORY_KIND_MOVEMENT),
        "transferencia": CashFlowCategory.objects.create(
            category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER
        ),
    }
    return user, contas, categorias


def _lancar(user, conta, categoria, descricao, valor, dia, *, tipo=ENTRY_TYPE_EXPENSE):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=tipo,
            description=descricao,
            entry_amount=Decimal(valor),
            installments=1,
            due_date=dia,
            status=STATUS_REALIZED,
            realized_date=dia,
            realized_amount=Decimal(valor),
        ),
        user=user,
    )[0]


def _linha(conta, descricao, valor, dia, *, bank_category=""):
    lote, _ = BankStatementImport.objects.get_or_create(
        account=conta, source_filename="extrato.pdf", defaults={"row_count": 0}
    )
    contador = BankStatementLine.objects.count()
    return BankStatementLine.objects.create(
        import_batch=lote, account=conta, statement_date=dia, description=descricao,
        amount=Decimal(valor), line_hash=f"h{contador}-{conta.id}", bank_category=bank_category,
    )


# --- classificação aprendida -------------------------------------------------


def test_chave_ignora_o_prefixo_do_pdf_mas_nao_o_de_descricao_comum(mundo):
    assert chave_da_linha("Corretagem - Corretagem Executor - Btc") == chave_da_linha("Corretagem Executor - Btc")
    assert chave_da_linha("Rendimentos - Taxa de Remuneração Emprestimo Ações Brraizacnpr6") == chave_da_linha(
        "Taxa de Remuneração Emprestimo Ações Brbbasacnor3"
    )


def test_historico_vence_o_prefixo_do_extrato(mundo):
    user, contas, cat = mundo
    _lancar(
        user, contas["genial"], cat["aluguel"], "Taxa de Remuneração Emprestimo Ações Brbbasacnor3",
        "10.00", date(2026, 8, 5), tipo=ENTRY_TYPE_INCOME,
    )
    sugestao = sugerir_categoria("Rendimentos - Taxa de Remuneração Emprestimo Ações Brraizacnpr6")
    assert sugestao.categoria == cat["aluguel"]
    assert sugestao.origem == ORIGEM_HISTORICO


def test_sem_historico_o_prefixo_vale_e_depois_o_padrao(mundo):
    _, _, cat = mundo
    assert sugerir_categoria("Rendimentos - Algo novo").origem == ORIGEM_PREFIXO
    assert sugerir_categoria("Rendimentos - Algo novo").categoria == cat["rendimentos"]
    padrao = sugerir_categoria("Mercadinho da esquina")
    assert (padrao.categoria, padrao.origem) == (cat["outros"], ORIGEM_PADRAO)


def test_outros_nunca_e_aprendido_e_a_mais_frequente_vence(mundo):
    user, contas, cat = mundo
    conta = contas["c6"]
    _lancar(user, conta, cat["outros"], "Padaria Central", "10.00", date(2026, 9, 3))
    assert sugerir_categoria("Padaria Central").origem == ORIGEM_PADRAO
    _lancar(user, conta, cat["corretagem"], "Padaria Central", "11.00", date(2026, 7, 3))
    _lancar(user, conta, cat["rendimentos"], "Padaria Central", "12.00", date(2026, 8, 3))
    _lancar(user, conta, cat["rendimentos"], "Padaria Central", "13.00", date(2026, 6, 3))
    # A mais recente é "Outros" (ignorada) e a mais frequente é "Rendimentos".
    assert sugerir_categoria("Padaria Central").categoria == cat["rendimentos"]


def test_movimentacao_pode_ser_sugerida_no_extrato_mas_nao_na_fatura(mundo):
    user, contas, cat = mundo
    _lancar(
        user, contas["genial"], cat["bolsa"], "Operações Bolsa D+2 Pr 28/08/2026 Nc. 11466",
        "100.00", date(2026, 8, 31),
    )
    descricao = "Operações em bolsa - Operações Bolsa D+2 Pr 28/08/2026 Nc. 11466"
    assert sugerir_categoria(descricao).categoria == cat["bolsa"]
    assert sugerir_categoria(descricao, apenas_gerenciais=True).categoria != cat["bolsa"]


def test_regra_de_categoria_vence_o_historico(mundo):
    user, contas, cat = mundo
    _lancar(user, contas["c6"], cat["rendimentos"], "Liberação de dinheiro", "3.50", date(2026, 8, 1),
            tipo=ENTRY_TYPE_INCOME)
    regra = StatementRule.objects.create(
        name="Prêmio", pattern="Liberacao de dinheiro", sign=RULE_SIGN_CREDIT,
        action=RULE_ACTION_CATEGORY, category=cat["loterias"],
    )
    sugestao = sugerir_categoria("Liberação de dinheiro", regra=regra)
    assert (sugestao.categoria, sugestao.origem) == (cat["loterias"], ORIGEM_REGRA)


# --- transferências próprias -------------------------------------------------


def test_duas_pontas_de_transferencia_viram_uma_so_transferencia(mundo):
    user, contas, cat = mundo
    saida = _linha(contas["genial"], "Pix - Para Mariano Sergio Pacheco de Angelo", "-200.00", date(2026, 9, 24))
    entrada = _linha(contas["c6"], "Pix recebido de MARIANO SERGIO PACHECO DE ANGELO", "200.00", date(2026, 9, 24))

    planos = {plano.linha.id: plano for plano in extrato.planejar(user, [saida, entrada])}
    assert planos[saida.id].acao == extrato.TRANSFERENCIA_PAR
    assert planos[entrada.id].acao == extrato.TRANSFERENCIA_PAR

    feitas, erros = extrato.aplicar(user, [saida, entrada])
    assert erros == []
    assert feitas[extrato.TRANSFERENCIA_PAR] == 1

    saida.refresh_from_db()
    entrada.refresh_from_db()
    assert saida.status == entrada.status == "conciliado"
    origem, destino = saida.matched_entry, entrada.matched_entry
    assert origem.account_id == contas["genial"].id and origem.entry_type == ENTRY_TYPE_EXPENSE
    assert destino.account_id == contas["c6"].id and destino.entry_type == ENTRY_TYPE_INCOME
    assert destino.source_entry_id == origem.id
    assert origem.category == cat["transferencia"] == destino.category
    assert CashFlowEntry.objects.exclude(category=cat["transferencia"]).count() == 0


def test_pares_com_um_dia_de_diferenca_mantem_cada_ponta_na_sua_data(mundo):
    user, contas, _ = mundo
    saida = _linha(contas["genial"], "TED para Mariano", "-80.00", date(2026, 9, 10))
    entrada = _linha(contas["c6"], "TED recebida de MARIANO", "80.00", date(2026, 9, 11))
    feitas, erros = extrato.aplicar(user, [saida, entrada])
    assert erros == [] and feitas[extrato.TRANSFERENCIA_PAR] == 1
    entrada.refresh_from_db()
    assert entrada.matched_entry.realized_date == date(2026, 9, 11)
    assert entrada.matched_entry.due_date == date(2026, 9, 11)


def test_linha_que_cita_titular_sem_outra_ponta_espera(mundo):
    user, contas, _ = mundo
    saida = _linha(contas["genial"], "Pix - Para Mariano Sergio Pacheco de Angelo", "-200.00", date(2026, 9, 24))
    (plano,) = extrato.planejar(user, [saida])
    assert plano.acao == extrato.TRANSFERENCIA_SEM_PAR
    assert not plano.executavel
    feitas, erros = extrato.aplicar(user, [saida])
    assert not feitas and len(erros) == 1
    saida.refresh_from_db()
    assert saida.status == "novo"
    assert CashFlowEntry.objects.count() == 0


def test_mesmo_valor_sem_indicio_de_transferencia_nao_e_pareado(mundo):
    user, contas, _ = mundo
    a = _linha(contas["genial"], "Mercado Alfa", "-28.00", date(2026, 9, 24))
    b = _linha(contas["c6"], "Estorno Beta", "28.00", date(2026, 9, 24))
    planos = {plano.linha.id: plano.acao for plano in extrato.planejar(user, [a, b])}
    assert extrato.TRANSFERENCIA_PAR not in planos.values()


def test_par_ambiguo_nao_e_pareado(mundo):
    user, contas, _ = mundo
    saida = _linha(contas["genial"], "Pix para Mariano", "-50.00", date(2026, 9, 24))
    _linha(contas["c6"], "Pix de Mariano", "50.00", date(2026, 9, 24))
    _linha(contas["bb"], "Pix de Mariano", "50.00", date(2026, 9, 24))
    (plano,) = extrato.planejar(user, [saida])
    assert plano.acao == extrato.TRANSFERENCIA_SEM_PAR


# --- regras explícitas -------------------------------------------------------


def test_regra_de_transferencia_leva_o_dinheiro_para_a_conta_do_mesmo_titular(mundo):
    user, contas, cat = mundo
    StatementRule.objects.create(
        name="Rende Fácil", pattern="Rende Facil", institution=contas["bb"].institution,
        action=RULE_ACTION_TRANSFER, destination_account_name="Rende Fácil",
    )
    aplicou = _linha(contas["bb"], "Rende Facil", "-212.30", date(2026, 9, 1))
    resgatou = _linha(contas["bb"], "Rende Facil", "213.90", date(2026, 9, 11))

    feitas, erros = extrato.aplicar(user, [aplicou, resgatou])
    assert erros == [] and feitas[extrato.TRANSFERENCIA_REGRA] == 2

    aplicou.refresh_from_db()
    resgatou.refresh_from_db()
    assert aplicou.matched_entry.account_id == contas["bb"].id
    assert aplicou.matched_entry.entry_type == ENTRY_TYPE_EXPENSE
    assert CashFlowEntry.objects.get(source_entry=aplicou.matched_entry).account_id == contas["rende"].id
    # No resgate o dinheiro sai da aplicação e entra na conta do BB.
    assert resgatou.matched_entry.account_id == contas["bb"].id
    assert resgatou.matched_entry.entry_type == ENTRY_TYPE_INCOME
    assert resgatou.matched_entry.source_entry.account_id == contas["rende"].id
    assert resgatou.matched_entry.category == cat["transferencia"]
    # Nenhuma receita nem despesa gerencial nasceu.
    assert not CashFlowEntry.objects.filter(category__kind="gerencial").exists()


def test_regra_de_transferencia_sem_a_conta_de_destino_pede_decisao(mundo):
    user, contas, _ = mundo
    FinancialAccount.objects.filter(id=contas["rende"].id).update(account_name="Outra conta")
    StatementRule.objects.create(
        name="Rende Fácil", pattern="Rende Facil", action=RULE_ACTION_TRANSFER,
        destination_account_name="Rende Fácil",
    )
    linha = _linha(contas["bb"], "Rende Facil", "-10.00", date(2026, 9, 1))
    (plano,) = extrato.planejar(user, [linha])
    assert plano.acao == extrato.MANUAL
    assert "Rende Fácil" in plano.motivo


def test_sem_permissao_de_destino_a_transferencia_pede_decisao(mundo):
    user, contas, _ = mundo
    UserTransferDestinationAccess.objects.filter(user=user, destination_account=contas["c6"]).delete()
    saida = _linha(contas["genial"], "Pix - Para Mariano", "-200.00", date(2026, 9, 24))
    entrada = _linha(contas["c6"], "Pix recebido de MARIANO", "200.00", date(2026, 9, 24))
    planos = {plano.linha.id: plano for plano in extrato.planejar(user, [saida, entrada])}
    assert planos[saida.id].acao == extrato.MANUAL
    assert "permissão de destino" in planos[saida.id].motivo
    feitas, erros = extrato.aplicar(user, [saida, entrada])
    assert not feitas and erros
    assert CashFlowEntry.objects.count() == 0


def test_regra_so_vale_na_instituicao_e_no_sinal_dela(mundo):
    user, contas, cat = mundo
    StatementRule.objects.create(
        name="Prêmio", pattern="Liberacao de dinheiro", institution=contas["c6"].institution,
        sign=RULE_SIGN_CREDIT, action=RULE_ACTION_CATEGORY, category=cat["loterias"],
    )
    no_c6 = _linha(contas["c6"], "Liberação de dinheiro", "3.50", date(2026, 9, 17))
    no_bb = _linha(contas["bb"], "Liberação de dinheiro", "3.50", date(2026, 9, 17))
    debito = _linha(contas["c6"], "Liberação de dinheiro", "-3.50", date(2026, 9, 17))
    planos = {plano.linha.id: plano for plano in extrato.planejar(user, [no_c6, no_bb, debito])}
    assert planos[no_c6.id].sugestao.categoria == cat["loterias"]
    assert planos[no_bb.id].sugestao.categoria == cat["outros"]
    assert planos[debito.id].sugestao.categoria == cat["outros"]


def test_regra_de_ignorar(mundo):
    user, contas, _ = mundo
    StatementRule.objects.create(name="Ruído", pattern="Saldo anterior", action=RULE_ACTION_IGNORE)
    linha = _linha(contas["c6"], "SALDO ANTERIOR", "1.00", date(2026, 9, 1))
    feitas, _ = extrato.aplicar(user, [linha])
    assert feitas[extrato.IGNORA] == 1
    linha.refresh_from_db()
    assert linha.status == "ignorado"


# --- conciliar, criar e mês fechado ------------------------------------------


def test_linha_com_um_candidato_concilia_e_sem_candidato_cria_com_a_categoria_aprendida(mundo):
    user, contas, cat = mundo
    _lancar(user, contas["genial"], cat["corretagem"], "Corretagem Executor - Btc", "8.63", date(2026, 9, 29))
    com_candidato = _linha(contas["genial"], "Corretagem - Corretagem Executor - Btc", "-8.63", date(2026, 9, 29))
    # Reconciliar exige que o candidato não esteja conciliado: o já realizado é vinculado.
    sem_candidato = _linha(contas["genial"], "Corretagem - Corretagem Executor - Btc", "-19.54", date(2026, 9, 25))
    planos = {plano.linha.id: plano for plano in extrato.planejar(user, [com_candidato, sem_candidato])}
    assert planos[com_candidato.id].acao == extrato.CONCILIA
    assert planos[sem_candidato.id].acao == extrato.CRIA
    assert planos[sem_candidato.id].sugestao.categoria == cat["corretagem"]
    feitas, erros = extrato.aplicar(user, [com_candidato, sem_candidato])
    assert erros == [] and feitas[extrato.CONCILIA] == 1 and feitas[extrato.CRIA] == 1


def test_varios_candidatos_pedem_escolha(mundo):
    user, contas, cat = mundo
    _lancar(user, contas["c6"], cat["outros"], "A", "28.00", date(2026, 9, 3))
    _lancar(user, contas["c6"], cat["outros"], "B", "28.00", date(2026, 9, 20))
    linha = _linha(contas["c6"], "Estacionamento", "-28.00", date(2026, 9, 22))
    (plano,) = extrato.planejar(user, [linha])
    assert plano.acao == extrato.AMBIGUA and not plano.executavel


def test_vinculo_de_lancamento_ja_realizado_vale_em_mes_fechado(mundo):
    user, contas, cat = mundo
    conta = contas["c6"]
    lancamento = _lancar(user, conta, cat["outros"], "Estacionamento", "28.00", date(2026, 7, 10))
    services.close_month(conta, 2026, 7, None, user)
    linha = _linha(conta, "FOT6511-SHOPPING", "-28.00", date(2026, 7, 10))
    reconcile_line_with_entry(user, line_id=linha.id, entry_id=lancamento.id)
    linha.refresh_from_db()
    assert linha.status == "conciliado" and linha.matched_entry_id == lancamento.id


def test_realizar_em_mes_fechado_continua_bloqueado(mundo):
    user, contas, cat = mundo
    conta = contas["c6"]
    aberto = services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=cat["outros"].id, entry_type=ENTRY_TYPE_EXPENSE,
            description="Estacionamento", entry_amount=Decimal("28.00"), installments=1,
            due_date=date(2026, 7, 10),
        ),
        user=user,
    )[0]
    services.close_month(conta, 2026, 7, None, user)
    linha = _linha(conta, "FOT6511-SHOPPING", "-28.00", date(2026, 7, 10))
    with pytest.raises(ValueError, match="fechad"):
        reconcile_line_with_entry(user, line_id=linha.id, entry_id=aberto.id)
