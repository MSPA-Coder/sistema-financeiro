"""Escritas de Lançamentos por HTMX: a tela fica, a tabela se atualiza.

Auditoria de 09/10/2026: criar, editar, realizar, desfazer e excluir
recarregavam a página inteira (CB-01), e dois cliques rápidos criavam dois
lançamentos (CB-10). Os formulários agora saem por HTMX com alvo na tabela; o
servidor responde sem redirecionar, e o formulário de novo lançamento leva um
token de uso único.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, STATUS_PROJECTED
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import TransactionRequest, create_transaction_batch

pytestmark = pytest.mark.django_db

#: Cabeçalhos de um envio HTMX com alvo na tabela (o que `transactions.js` manda).
HTMX_NA_TABELA = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "transactions-table-container"}


@pytest.fixture
def cenario(client):
    user = get_user_model().objects.create_user(
        username="escritas-htmx", password="senha-segura", user_type="administrator",
    )
    conta = FinancialAccount.objects.create(
        owner=AccountOwner.objects.create(name="Titular htmx"),
        institution=FinancialInstitution.objects.create(institution_name="Banco h", institution_type="Banco"),
        account_name="Conta htmx",
    )
    categoria = CashFlowCategory.objects.create(category_name="Despesa htmx")
    [entry] = create_transaction_batch(TransactionRequest(
        account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_EXPENSE,
        description="Lançamento htmx", entry_amount=Decimal("10.00"), installments=1,
        due_date=date.today(), status=STATUS_PROJECTED,
    ), user=user)
    client.force_login(user)
    return {"conta": conta, "categoria": categoria, "entry": entry}


def _gatilhos(response):
    bruto = response.get("HX-Trigger", "")
    return json.loads(bruto) if bruto.startswith("{") else {bruto: True} if bruto else {}


def test_realizar_por_htmx_nao_redireciona_e_atualiza_a_tabela(client, cenario):
    response = client.post(
        f"/mark_realized/{cenario['entry'].id}/",
        {"realized_date": date.today().isoformat(), "realized_amount": "10.00"},
        **HTMX_NA_TABELA,
    )

    assert response.status_code in (200, 204)
    assert "Location" not in response and "HX-Redirect" not in response
    assert {"tableRefresh", "lancamentoGravado"} <= set(_gatilhos(response))


def test_erro_por_htmx_nao_recarrega_a_tabela(client, cenario):
    """Com erro, o formulário precisa continuar preenchido: nada de tableRefresh."""
    response = client.post(
        f"/mark_realized/{cenario['entry'].id}/",
        {"realized_date": date.today().isoformat(), "realized_amount": "abc"},
        **HTMX_NA_TABELA,
    )

    assert "Location" not in response and "HX-Redirect" not in response
    assert "tableRefresh" not in _gatilhos(response)
    cenario["entry"].refresh_from_db()
    assert cenario["entry"].status == STATUS_PROJECTED


def _novo(cenario, token, descricao="Envio único"):
    return {
        "account_id": str(cenario["conta"].id), "category_id": str(cenario["categoria"].id),
        "entry_type": ENTRY_TYPE_EXPENSE, "description": descricao, "entry_amount": "7.00",
        "installments": "1", "due_date": date.today().isoformat(), "status": STATUS_PROJECTED,
        "submit_token": token,
    }


def test_mesmo_formulario_enviado_duas_vezes_cria_um_so(client, cenario):
    antes = CashFlowEntry.objects.count()

    client.post("/transaction/", _novo(cenario, "token-repetido"), **HTMX_NA_TABELA)
    segunda = client.post("/transaction/", _novo(cenario, "token-repetido"), **HTMX_NA_TABELA)

    assert CashFlowEntry.objects.count() == antes + 1
    assert "tableRefresh" not in _gatilhos(segunda)


def test_formulario_recebe_token_novo_para_o_proximo_lancamento(client, cenario):
    """Dois lançamentos iguais DE PROPÓSITO continuam possíveis: cada envio tem token próprio."""
    primeira = client.post("/transaction/", _novo(cenario, "token-1"), **HTMX_NA_TABELA)
    proximo = _gatilhos(primeira)["lancamentoGravado"]["submit_token"]
    antes = CashFlowEntry.objects.count()

    client.post("/transaction/", _novo(cenario, proximo), **HTMX_NA_TABELA)

    assert proximo != "token-1"
    assert CashFlowEntry.objects.count() == antes + 1


def test_falha_na_criacao_nao_gasta_o_token(client, cenario):
    """Sem descrição a criação é recusada; o mesmo token ainda vale no reenvio corrigido."""
    client.post("/transaction/", _novo(cenario, "token-reenvio", descricao=""), **HTMX_NA_TABELA)
    antes = CashFlowEntry.objects.count()

    client.post("/transaction/", _novo(cenario, "token-reenvio"), **HTMX_NA_TABELA)

    assert CashFlowEntry.objects.count() == antes + 1
