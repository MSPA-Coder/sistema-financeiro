"""Depois de uma escrita, a tela volta com os filtros que mostrava.

Auditoria de filtros e recargas (09/10/2026): as tabelas cadastrais voltavam
zeradas, Lançamentos perdia moeda e grupos, e a volta de todas lia a query
congelada no `action` do formulário -- velha depois de qualquer filtro trocado
por HTMX. A volta agora sai de `core.volta`, que lê a query viva.
"""

import json
import re
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from core.volta import url_de_volta, voltar_para


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


# ---------------------------------------------------------------------------
# De onde vem a query de origem (sem banco)
# ---------------------------------------------------------------------------

def test_campo_volta_vence_a_query_congelada_do_action():
    """O `action` traz a query da carga da página; `volta`, a da barra agora."""
    request = RequestFactory().post(
        "/tables/banks/create/?filter_type=Banco", {"volta": "filter_type=Corretora&currency=USD"},
    )
    assert _query(url_de_volta(request, "/tables/banks/")) == {"filter_type": "Corretora", "currency": "USD"}


def test_cabecalho_do_htmx_serve_quando_nao_ha_campo_volta():
    request = RequestFactory().post(
        "/mark_realized/1/", HTTP_HX_CURRENT_URL="http://testserver/transactions/?owner_id=2&grupos=cartoes",
    )
    assert _query(url_de_volta(request, "/transactions/")) == {"owner_id": "2", "grupos": "cartoes"}


def test_cabecalho_de_outro_host_e_ignorado():
    request = RequestFactory().post(
        "/mark_realized/1/?owner_id=1", HTTP_HX_CURRENT_URL="http://outro.exemplo/x/?owner_id=9",
    )
    assert _query(url_de_volta(request, "/transactions/")) == {"owner_id": "1"}


def test_so_a_query_viaja_o_destino_e_do_servidor():
    """Nada na entrada consegue trocar o caminho de destino."""
    request = RequestFactory().post("/x/", {"volta": "//malicioso.exemplo/?a=1"})
    url = url_de_volta(request, "/tables/banks/")
    assert url.startswith("/tables/banks/?")
    assert urlsplit(url).netloc == ""


def test_manter_filtra_os_parametros_mas_os_globais_passam():
    request = RequestFactory().post(
        "/x/", {"volta": "period=2026-11&outro=1&currency=USD&grupos=cartoes&volta=lixo"},
    )
    url = url_de_volta(request, "/transactions/", manter=("period",))
    assert _query(url) == {"period": "2026-11", "currency": "USD", "grupos": "cartoes"}


def test_extra_acrescenta_e_vazio_remove():
    request = RequestFactory().post("/x/", {"volta": "period=2026-11&entry_id=7"})
    url = url_de_volta(request, "/transactions/", extra={"new_entry_open": "1", "entry_id": ""})
    assert _query(url) == {"period": "2026-11", "new_entry_open": "1"}


def test_voltar_para_no_htmx_navega_com_a_query():
    request = RequestFactory().post(
        "/tables/banks/create/", {"volta": "filter_type=Banco"},
        HTTP_HX_REQUEST="true", HTTP_HX_TARGET="banks-table",
    )
    response = voltar_para(request, "banking:institutions_view")
    assert response["HX-Redirect"] == "/tables/banks/?filter_type=Banco"


def test_voltar_para_sem_htmx_redireciona_com_a_query():
    request = RequestFactory().post("/tables/banks/create/", {"volta": "filter_type=Banco"})
    response = voltar_para(request, "banking:institutions_view")
    assert response.status_code == 302
    assert response["Location"] == "/tables/banks/?filter_type=Banco"


# ---------------------------------------------------------------------------
# Nas telas (com banco)
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(db):
    return get_user_model().objects.create_user(
        username="volta-filtros", password="senha-segura", user_type="administrator",
    )


@pytest.mark.django_db
def test_tabela_de_bancos_volta_com_o_filtro_de_tipo(client, admin):
    client.force_login(admin)

    response = client.post(
        "/tables/banks/create/",
        {"institution_name": "Banco volta", "institution_type": "Banco", "volta": "filter_type=Corretora"},
    )

    assert response.status_code == 302
    assert _query(response["Location"]) == {"filter_type": "Corretora"}


@pytest.fixture
def lancamento(admin):
    from accounts.models import AccountOwner
    from banking.models import FinancialAccount, FinancialInstitution
    from core.domain.finance import ENTRY_TYPE_EXPENSE, STATUS_PROJECTED
    from transactions.models import CashFlowCategory
    from transactions.services import TransactionRequest, create_transaction_batch

    conta = FinancialAccount.objects.create(
        owner=AccountOwner.objects.create(name="Titular volta"),
        institution=FinancialInstitution.objects.create(institution_name="Banco v", institution_type="Banco"),
        account_name="Conta volta",
    )
    categoria = CashFlowCategory.objects.create(category_name="Despesa volta")
    [entry] = create_transaction_batch(TransactionRequest(
        account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_EXPENSE,
        description="Lançamento volta", entry_amount=Decimal("10.00"), installments=1,
        due_date=date.today(), status=STATUS_PROJECTED,
    ), user=admin)
    return entry


@pytest.mark.django_db
def test_realizar_volta_com_moeda_e_grupos(client, admin, lancamento):
    """Moeda e grupos não estavam na lista de parâmetros que Lançamentos preservava."""
    client.force_login(admin)

    response = client.post(
        f"/mark_realized/{lancamento.id}/",
        {
            "realized_date": date.today().isoformat(), "realized_amount": "10.00",
            "volta": "period=2026-11&mode=a_vencer&currency=ALL&grupos=bancos,cartoes",
        },
    )

    assert response.status_code == 302
    assert _query(response["Location"]) == {
        "period": "2026-11", "mode": "a_vencer", "currency": "ALL", "grupos": "bancos,cartoes",
    }


@pytest.mark.django_db
def test_lancamento_manual_sem_descricao_e_recusado(client, admin, lancamento):
    from transactions.models import CashFlowEntry

    client.force_login(admin)
    antes = CashFlowEntry.objects.count()

    client.post("/transaction/", {
        "account_id": str(lancamento.account_id), "category_id": str(lancamento.category_id),
        "entry_type": lancamento.entry_type, "description": "   ", "entry_amount": "5.00",
        "installments": "1", "due_date": date.today().isoformat(), "status": "a_vencer",
    })

    assert CashFlowEntry.objects.count() == antes


# ---------------------------------------------------------------------------
# Tabelas cadastrais por HTMX: sem recarga (decisão D1 de 09/10/2026)
# ---------------------------------------------------------------------------

HTMX_NA_TABELA = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "institutionsTableBody"}


@pytest.mark.django_db
def test_incluir_banco_por_htmx_atualiza_so_a_tabela(client, admin):
    client.force_login(admin)

    response = client.post(
        "/tables/banks/create/", {"institution_name": "Banco htmx", "institution_type": "Banco"},
        **HTMX_NA_TABELA,
    )

    assert response.status_code in (200, 204)
    assert "Location" not in response and "HX-Redirect" not in response
    assert {"tabelaAtualizar", "cadastroGravado"} <= set(json.loads(response["HX-Trigger"]))


@pytest.mark.django_db
def test_erro_no_cadastro_por_htmx_nao_recarrega_a_tabela(client, admin):
    """Com erro, a linha em edição precisa manter o que foi digitado."""
    client.force_login(admin)

    response = client.post(
        "/tables/banks/create/", {"institution_name": "", "institution_type": "Banco"}, **HTMX_NA_TABELA,
    )

    assert "Location" not in response and "HX-Redirect" not in response
    assert "tabelaAtualizar" not in response.get("HX-Trigger", "")


# ---------------------------------------------------------------------------
# Reteste R1 (09/10/2026): o que a primeira rodada de correções deixou passar
# ---------------------------------------------------------------------------

def test_fechar_mes_volta_com_moeda_e_grupos():
    """R1.13: o retorno do Fechamento levava só os filtros da própria lista."""
    from core.views import _monthly_close_redirect

    request = RequestFactory().post(
        "/settings/month-close/close/",
        {"filter_account_id": "4", "filter_year": "2026", "volta": "filter_account_id=4&currency=ALL&grupos=cartoes"},
    )
    query = _query(_monthly_close_redirect(request)["Location"])
    assert query == {"filter_account_id": "4", "filter_year": "2026", "currency": "ALL", "grupos": "cartoes"}


@pytest.mark.django_db
def test_filtro_de_coluna_escolhido_continua_entre_as_opcoes_mesmo_sem_linhas(client, admin, lancamento):
    """R1.1: tipo e categoria juntos sem nenhuma linha faziam o seletor de tipo
    perder a opção escolhida -- a tela mostrava "Todos" com o filtro valendo."""
    client.force_login(admin)
    periodo = lancamento.due_date.strftime("%Y-%m")

    response = client.get(
        f"/transactions/?period={periodo}&mode=todos&filter_type=receita&filter_category=Inexistente"
    )

    assert response.status_code == 200
    seletor = re.search(r'<select data-table-filter name="filter_type".*?</select>', response.content.decode(), re.S)
    assert seletor and re.search(r'<option value="receita"\s+selected', seletor.group(0)), seletor
