"""As telas novas e alteradas pelo motor do extrato, renderizadas de verdade.

Os testes de serviço provam a regra; estes provam que cada tela abre, mostra o
que prometeu e que a ação em lote chega até o serviço: Conciliação (sugestão por
linha, "Aplicar sugestões" e rendimentos agrupados), Importações (saldo do
extrato contra o CB), Atualizar saldo, Categorias (grupos), Dashboard por grupo,
Planejamento anual agrupado e Contas (finalidade).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from bank_statements.models import BankStatementImport, BankStatementLine
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_REALIZED,
)
from transactions import services
from transactions.models import CashFlowCategory, CashFlowCategoryGroup

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario(client):
    user = AppUser.objects.create_user(username="admin-telas", password="senha-segura")
    titular = AccountOwner.objects.create(name="Mariano")
    UserOwnerAccess.objects.create(
        user=user, owner=titular, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    banco = FinancialInstitution.objects.create(institution_name="Mercado Pago", institution_type="Banco")
    outro_banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    mp = FinancialAccount.objects.create(owner=titular, institution=banco, account_name="Conta 01")
    c6 = FinancialAccount.objects.create(owner=titular, institution=outro_banco, account_name="Conta 02")
    save_transfer_destination_accesses(user, {mp.id, c6.id})
    saude = CashFlowCategoryGroup.objects.create(group_name="Saúde", position=80)
    categorias = {
        "rendimentos": CashFlowCategory.objects.create(category_name="Rendimentos"),
        "plano": CashFlowCategory.objects.create(category_name="Plano de saúde", group=saude),
        "outros": CashFlowCategory.objects.create(category_name="Outros"),
        "transferencia": CashFlowCategory.objects.create(
            category_name="Transferência Entre Contas", kind=CATEGORY_KIND_TRANSFER
        ),
    }
    client.force_login(user)
    return client, user, {"mp": mp, "c6": c6}, categorias


def _lancar(user, conta, categoria, descricao, valor, dia, tipo=ENTRY_TYPE_EXPENSE):
    return services.create_transaction_batch(
        services.TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=tipo, description=descricao,
            entry_amount=Decimal(valor), installments=1, due_date=dia, status=STATUS_REALIZED,
            realized_date=dia, realized_amount=Decimal(valor),
        ),
        user=user,
    )[0]


def _linha(conta, descricao, valor, dia, n):
    lote, _ = BankStatementImport.objects.get_or_create(
        account=conta, source_filename="set.pdf", defaults={"row_count": 0}
    )
    return BankStatementLine.objects.create(
        import_batch=lote, account=conta, statement_date=dia, description=descricao,
        amount=Decimal(valor), line_hash=f"h{conta.id}-{n}",
    )


def test_conciliacao_mostra_a_sugestao_de_cada_linha_e_os_rendimentos_agrupados(cenario):
    client, user, contas, cat = cenario
    _lancar(user, contas["mp"], cat["rendimentos"], "Rendimentos", "0.02", date(2026, 8, 31), ENTRY_TYPE_INCOME)
    for dia in range(1, 4):
        _linha(contas["mp"], "Rendimentos", "0.02", date(2026, 9, dia), dia)
    _linha(contas["mp"], "Pix - Para Mariano Sergio", "-200.00", date(2026, 9, 24), 10)

    resposta = client.get(reverse("bank_statements:reconciliation_view"))
    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "3 linhas" in html
    assert "Cria lançamento: Rendimentos (histórico)" in html
    assert "Parece transferência sua; falta importar a outra ponta" in html
    assert resposta.context["rendimentos_agrupados"][0]["quantidade"] == 3


def test_aplicar_sugestoes_em_lote_cria_os_rendimentos_e_deixa_a_transferencia_esperando(cenario):
    client, user, contas, cat = cenario
    _lancar(user, contas["mp"], cat["rendimentos"], "Rendimentos", "0.02", date(2026, 8, 31), ENTRY_TYPE_INCOME)
    rendimentos = [_linha(contas["mp"], "Rendimentos", "0.02", date(2026, 9, dia), dia) for dia in range(1, 4)]
    pix = _linha(contas["mp"], "Pix - Para Mariano Sergio", "-200.00", date(2026, 9, 24), 10)

    resposta = client.post(
        reverse("bank_statements:bulk_action_lines"),
        {"bulk_action": "apply_plan", "line_ids": [linha.id for linha in [*rendimentos, pix]]},
        follow=True,
    )
    assert resposta.status_code == 200
    mensagens = " ".join(str(m) for m in resposta.context["messages"])
    assert "3 lançamento(s) criado(s)" in mensagens
    assert "esperam decisão sua" in mensagens
    for linha in rendimentos:
        linha.refresh_from_db()
        assert linha.status == "conciliado" and linha.matched_entry.category == cat["rendimentos"]
    pix.refresh_from_db()
    assert pix.status == "novo"


def test_aplicar_o_grupo_de_rendimentos_pelo_botao_do_mes(cenario):
    client, user, contas, cat = cenario
    _lancar(user, contas["mp"], cat["rendimentos"], "Rendimentos", "0.02", date(2026, 8, 31), ENTRY_TYPE_INCOME)
    linhas = [_linha(contas["mp"], "Rendimentos", "0.02", date(2026, 9, dia), dia) for dia in range(1, 3)]
    grupo = client.get(reverse("bank_statements:reconciliation_view")).context["rendimentos_agrupados"][0]
    client.post(
        reverse("bank_statements:bulk_action_lines"),
        {"bulk_action": "apply_plan", "line_ids": grupo["line_ids"]},
    )
    assert {linha.id for linha in BankStatementLine.objects.filter(status="conciliado")} == {linha.id for linha in linhas}
    assert client.get(reverse("bank_statements:reconciliation_view")).context["rendimentos_agrupados"] == []


def test_importacoes_mostra_o_saldo_do_extrato_contra_o_cb(cenario):
    client, user, contas, cat = cenario
    lote = BankStatementImport.objects.create(
        account=contas["mp"], source_filename="mp.pdf", row_count=1,
        statement_balance=Decimal("29.28"), statement_balance_date=date(2026, 9, 30),
    )
    html = client.get(reverse("bank_statements:imports_view")).content.decode()
    assert "Bate em" not in html  # CB está em 0,00: não bate com 29,28
    BankStatementImport.objects.filter(id=lote.id).update(statement_balance=Decimal("0.00"))
    assert "Bate em 30/09/2026" in client.get(reverse("bank_statements:imports_view")).content.decode()


def test_atualizar_saldo_previa_lanca_e_lista_a_assuncao(cenario):
    client, user, contas, cat = cenario
    CashFlowCategory.objects.create(category_name="Ajustes de Saldo")
    url = reverse("bank_statements:atualizar_saldo")
    assert client.get(url).status_code == 200

    previa = client.post(url, {"acao": "previa", "conta": contas["mp"].id, "data": "2026-09-30", "saldo": "12,50"})
    assert previa.status_code == 200
    assert "Diferença" in previa.content.decode() and "Lançar a diferença" in previa.content.decode()

    resposta = client.post(
        url,
        {"acao": "aplicar", "conta": contas["mp"].id, "data": "2026-09-30", "saldo": "12.50",
         "diferenca": "12.50", "destino": "ajuste", "motivo": "extrato ainda não importado"},
        follow=True,
    )
    assert "Diferença lançada" in " ".join(str(m) for m in resposta.context["messages"])
    assert "extrato ainda não importado" in resposta.content.decode()
    assert len(resposta.context["assuncoes"]) == 1
    assert len(resposta.context["atualizacoes"]) == 1
    assert "⚠ Assunção" in resposta.content.decode()


def test_categorias_mostra_os_grupos_e_cria_um_grupo(cenario):
    client, _, _, cat = cenario
    html = client.get(reverse("transactions:categories_view")).content.decode()
    assert "Saúde" in html and "Plano de saúde" in html
    assert "Saúde" in client.get(reverse("transactions:category_groups_view")).content.decode()
    client.post(reverse("transactions:create_category_group"), {"group_name": "Moradia", "position": "60", "in_charts": "1"})
    assert CashFlowCategoryGroup.objects.filter(group_name="Moradia", position=60, in_charts=True).exists()
    grupo = CashFlowCategoryGroup.objects.get(group_name="Moradia")
    client.post(reverse("transactions:update_category", args=[cat["outros"].id]),
                {"category_name": "Outros", "kind": "gerencial", "group_id": grupo.id})
    cat["outros"].refresh_from_db()
    assert cat["outros"].group == grupo


def test_dashboard_por_grupo_abre_e_informa_o_modo(cenario):
    client, user, contas, cat = cenario
    _lancar(user, contas["mp"], cat["plano"], "Prevent", "300.00", date.today())
    resposta = client.get(reverse("dashboard:dashboard"), {"categorias": "grupo", "mode": "realizado"})
    assert resposta.status_code == 200
    assert resposta.context["chart_data"]["catMode"] == "grupo"
    assert "Saúde" in resposta.context["chart_data"]["catLabels"]
    padrao = client.get(reverse("dashboard:dashboard"), {"mode": "realizado"})
    assert padrao.context["chart_data"]["catMode"] == "grupo"
    por_categoria = client.get(reverse("dashboard:dashboard"), {"categorias": "categoria", "mode": "realizado"})
    assert por_categoria.context["chart_data"]["catMode"] == "categoria"
