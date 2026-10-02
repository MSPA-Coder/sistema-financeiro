"""Plano de ajustes de dados (`bank_statements.ajustes`, comando `aplicar_ajustes`).

O que fica fixado: a simulação roda as operações de verdade e não grava nada;
um erro em qualquer item desfaz o plano inteiro; mover uma ponta troca a conta e
refaz o texto das duas pontas sem mexer em valor nem data; excluir a ponta leva a
transferência inteira; e nada é movido de mês fechado, para outra moeda, para a
própria contraparte ou se o lançamento já foi conciliado com um extrato.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements import ajustes
from bank_statements.models import BankStatementImport, BankStatementLine
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions import services
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

DIA = date(2026, 6, 22)


def _conta_spec(instituicao, nome="Conta"):
    return {"titular": "Mariano", "instituicao": instituicao, "nome": nome}


@pytest.fixture
def mundo():
    admin = AppUser.objects.create_user(
        username="admin-ajustes", password="senha-segura", user_type=USER_TYPE_ADMINISTRATOR
    )
    titular = AccountOwner.objects.create(name="Mariano")
    UserOwnerAccess.objects.create(
        user=admin, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    instituicoes = {
        nome: FinancialInstitution.objects.create(institution_name=nome, institution_type="Banco")
        for nome in ("C6", "XP", "SCP XP Investimentos")
    }
    contas = {
        "c6": FinancialAccount.objects.create(owner=titular, institution=instituicoes["C6"], account_name="Conta"),
        "xp": FinancialAccount.objects.create(owner=titular, institution=instituicoes["XP"], account_name="Conta"),
        "scp": FinancialAccount.objects.create(
            owner=titular, institution=instituicoes["SCP XP Investimentos"], account_name="Conta"
        ),
        "dolar": FinancialAccount.objects.create(
            owner=titular, institution=instituicoes["XP"], account_name="Dólar", currency="USD"
        ),
    }
    save_transfer_destination_accesses(admin, {conta.id for conta in contas.values()})
    categorias = {
        "outros": CashFlowCategory.objects.create(category_name="Outros"),
        "transferencia": CashFlowCategory.objects.create(
            category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER
        ),
    }
    return admin, contas, categorias


def _avulso(mundo, conta, descricao="Lançamento", valor="100.00", tipo=ENTRY_TYPE_INCOME, dia=DIA):
    admin, contas, categorias = mundo
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=contas[conta].id, category_id=categorias["outros"].id, entry_type=tipo,
            description=descricao, entry_amount=Decimal(valor), installments=1, due_date=dia,
            status=STATUS_REALIZED, realized_date=dia, realized_amount=Decimal(valor),
        ),
        user=admin,
    )[0]


def _transferencia(mundo, origem, destino, valor="25000.00", dia=DIA):
    admin, contas, categorias = mundo
    services.create_transaction_batch(
        services.TransactionRequest(
            account_id=contas[origem].id, category_id=categorias["transferencia"].id,
            entry_type=ENTRY_TYPE_EXPENSE, description="Transferência", entry_amount=Decimal(valor),
            installments=1, due_date=dia, status=STATUS_REALIZED, realized_date=dia,
            realized_amount=Decimal(valor), counterparty_account_id=contas[destino].id,
        ),
        user=admin,
    )
    return (
        CashFlowEntry.objects.get(account=contas[destino], operation_type=OPERATION_INTERNAL_TRANSFER),
        CashFlowEntry.objects.get(account=contas[origem], operation_type=OPERATION_INTERNAL_TRANSFER),
    )


def _spec_do_lancamento(conta_spec, valor, tipo, dia=DIA, **extra):
    return {"conta": conta_spec, "data": dia.isoformat(), "tipo": tipo, "valor": valor, **extra}


def test_define_identificador_e_saldo_inicial_da_conta(mundo):
    admin, contas, _ = mundo
    plano = {"contas": [{
        "conta": _conta_spec("SCP XP Investimentos"), "identificador": "323220",
        "saldo_inicial": "60.85", "data_do_saldo_inicial": "2025-12-31",
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert relatorio.erros == []
    contas["scp"].refresh_from_db()
    assert contas["scp"].statement_identifier == "323220"
    assert contas["scp"].initial_balance == Decimal("60.85")
    assert contas["scp"].initial_balance_date == date(2025, 12, 31)


def test_simulacao_roda_de_verdade_mas_nao_grava_nada(mundo):
    admin, contas, _ = mundo
    destino, origem = _transferencia(mundo, "c6", "xp")
    plano = {
        "contas": [{"conta": _conta_spec("SCP XP Investimentos"), "identificador": "323220"}],
        "mover_pontas": [{
            "lancamento": _spec_do_lancamento(_conta_spec("XP"), "25000.00", ENTRY_TYPE_INCOME),
            "para_conta": _conta_spec("SCP XP Investimentos"),
        }],
    }

    relatorio = ajustes.executar(admin, plano, aplicar=False)

    assert relatorio.erros == []
    assert len(relatorio.linhas) == 2
    destino.refresh_from_db()
    contas["scp"].refresh_from_db()
    assert destino.account_id == contas["xp"].id
    assert contas["scp"].statement_identifier == ""


def test_mover_ponta_troca_a_conta_refaz_os_textos_e_preserva_valor_e_data(mundo):
    admin, contas, _ = mundo
    destino, origem = _transferencia(mundo, "c6", "xp")
    plano = {"mover_pontas": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "25000.00", ENTRY_TYPE_INCOME),
        "para_conta": _conta_spec("SCP XP Investimentos"),
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert relatorio.erros == []
    destino.refresh_from_db()
    origem.refresh_from_db()
    assert destino.account_id == contas["scp"].id
    assert (destino.entry_amount, destino.realized_date) == (Decimal("25000.00"), DIA)
    assert "SCP XP Investimentos" in origem.description
    assert "C6" in destino.description
    assert origem.account_id == contas["c6"].id


def test_excluir_ponta_leva_a_transferencia_inteira(mundo):
    admin, contas, _ = mundo
    _transferencia(mundo, "c6", "xp")
    plano = {"excluir": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "25000.00", ENTRY_TYPE_INCOME)
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert relatorio.erros == []
    assert "2 lançamento(s) removido(s)" in relatorio.linhas[0]
    assert not CashFlowEntry.objects.exists()


def test_erro_em_um_item_desfaz_o_plano_inteiro(mundo):
    admin, contas, _ = mundo
    plano = {
        "contas": [{"conta": _conta_spec("SCP XP Investimentos"), "identificador": "323220"}],
        "excluir": [{"lancamento": _spec_do_lancamento(_conta_spec("XP"), "1.00", ENTRY_TYPE_INCOME)}],
    }

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert len(relatorio.erros) == 1
    assert "[excluir]" in relatorio.erros[0]
    contas["scp"].refresh_from_db()
    assert contas["scp"].statement_identifier == ""


def test_lancamento_ambiguo_e_recusado(mundo):
    admin, contas, _ = mundo
    _avulso(mundo, "xp", descricao="Um")
    _avulso(mundo, "xp", descricao="Dois")
    plano = {"excluir": [{"lancamento": _spec_do_lancamento(_conta_spec("XP"), "100.00", ENTRY_TYPE_INCOME)}]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert "casa com 2 lançamentos" in relatorio.erros[0]
    assert CashFlowEntry.objects.count() == 2


def test_descricao_contem_desambigua(mundo):
    admin, contas, _ = mundo
    _avulso(mundo, "xp", descricao="JCP + Dividendos")
    _avulso(mundo, "xp", descricao="Outro")
    plano = {"excluir": [{"lancamento": _spec_do_lancamento(
        _conta_spec("XP"), "100.00", ENTRY_TYPE_INCOME, descricao_contem="jcp"
    )}]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert relatorio.erros == []
    assert list(CashFlowEntry.objects.values_list("description", flat=True)) == ["Outro"]


def test_conta_inexistente_ou_ambigua_e_recusada(mundo):
    admin, contas, _ = mundo
    plano = {"contas": [{"conta": _conta_spec("Banco que não existe"), "identificador": "1"}]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert "casa com 0 contas" in relatorio.erros[0]


def test_mover_para_conta_de_outra_moeda_e_recusado(mundo):
    admin, contas, _ = mundo
    _avulso(mundo, "xp")
    plano = {"mover_pontas": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "100.00", ENTRY_TYPE_INCOME),
        "para_conta": {"titular": "Mariano", "instituicao": "XP", "nome": "Dólar"},
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert "moedas diferentes" in relatorio.erros[0]


def test_mover_ponta_para_a_conta_da_propria_contraparte_e_recusado(mundo):
    admin, contas, _ = mundo
    destino, _origem = _transferencia(mundo, "c6", "xp")
    plano = {"mover_pontas": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "25000.00", ENTRY_TYPE_INCOME),
        "para_conta": _conta_spec("C6"),
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert "outra ponta" in relatorio.erros[0]
    destino.refresh_from_db()
    assert destino.account_id == contas["xp"].id


def test_mover_lancamento_conciliado_com_extrato_e_recusado(mundo):
    admin, contas, _ = mundo
    entrada = _avulso(mundo, "xp")
    lote = BankStatementImport.objects.create(account=contas["xp"], source_filename="x.ofx", row_count=1)
    BankStatementLine.objects.create(
        import_batch=lote, account=contas["xp"], statement_date=DIA, amount=Decimal("100.00"),
        description="Lançamento", line_hash="h1", status="conciliado", matched_entry=entrada,
    )
    plano = {"mover_pontas": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "100.00", ENTRY_TYPE_INCOME),
        "para_conta": _conta_spec("SCP XP Investimentos"),
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert "conciliado" in relatorio.erros[0]
    entrada.refresh_from_db()
    assert entrada.account_id == contas["xp"].id


def test_mover_de_mes_fechado_e_recusado(mundo):
    admin, contas, _ = mundo
    entrada = _avulso(mundo, "xp")
    services.close_month(contas["xp"], DIA.year, DIA.month, Decimal("100.00"), admin)
    plano = {"mover_pontas": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "100.00", ENTRY_TYPE_INCOME),
        "para_conta": _conta_spec("SCP XP Investimentos"),
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert relatorio.erros
    entrada.refresh_from_db()
    assert entrada.account_id == contas["xp"].id


def test_mover_para_conta_com_mes_fechado_e_recusado(mundo):
    admin, contas, _ = mundo
    entrada = _avulso(mundo, "xp")
    services.close_month(contas["scp"], DIA.year, DIA.month, Decimal("0.00"), admin)
    plano = {"mover_pontas": [{
        "lancamento": _spec_do_lancamento(_conta_spec("XP"), "100.00", ENTRY_TYPE_INCOME),
        "para_conta": _conta_spec("SCP XP Investimentos"),
    }]}

    relatorio = ajustes.executar(admin, plano, aplicar=True)

    assert "fechado na conta de destino" in relatorio.erros[0]
    entrada.refresh_from_db()
    assert entrada.account_id == contas["xp"].id


def test_operacao_desconhecida_no_plano_e_recusada(mundo):
    admin, _contas, _ = mundo

    with pytest.raises(ValueError, match="Operações desconhecidas"):
        ajustes.executar(admin, {"apagar_tudo": []})


def test_comando_simula_por_padrao_e_aplica_com_a_flag(mundo, tmp_path):
    admin, contas, _ = mundo
    arquivo = tmp_path / "plano.json"
    arquivo.write_text(
        json.dumps({"contas": [{"conta": _conta_spec("SCP XP Investimentos"), "identificador": "323220"}]}),
        encoding="utf-8",
    )

    saida = StringIO()
    call_command("aplicar_ajustes", usuario=admin.username, arquivo=str(arquivo), stdout=saida)
    contas["scp"].refresh_from_db()
    assert "Simulação: nada foi gravado." in saida.getvalue()
    assert contas["scp"].statement_identifier == ""

    saida = StringIO()
    call_command("aplicar_ajustes", usuario=admin.username, arquivo=str(arquivo), aplicar=True, stdout=saida)
    contas["scp"].refresh_from_db()
    assert "Aplicado." in saida.getvalue()
    assert contas["scp"].statement_identifier == "323220"


def test_comando_falha_com_erro_e_nao_grava(mundo, tmp_path):
    admin, contas, _ = mundo
    arquivo = tmp_path / "plano.json"
    arquivo.write_text(
        json.dumps({"contas": [{"conta": _conta_spec("Inexistente"), "identificador": "1"}]}), encoding="utf-8"
    )

    with pytest.raises(CommandError, match="nada foi gravado"):
        call_command(
            "aplicar_ajustes", usuario=admin.username, arquivo=str(arquivo), aplicar=True,
            stdout=StringIO(), stderr=StringIO(),
        )


def test_comando_recusa_usuario_inexistente_e_plano_ilegivel(mundo, tmp_path):
    admin, _contas, _ = mundo

    with pytest.raises(CommandError, match="não existe"):
        call_command("aplicar_ajustes", usuario="ninguem", arquivo="-")
    with pytest.raises(CommandError, match="Não consegui ler o plano"):
        call_command("aplicar_ajustes", usuario=admin.username, arquivo=str(tmp_path / "nao-existe.json"))
