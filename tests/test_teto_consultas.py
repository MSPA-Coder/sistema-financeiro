"""Custo em consultas das telas mais abertas: Lançamentos e dashboard.

Dois tipos de guarda, porque cada um pega um defeito diferente:

- o TETO fixa quantas consultas a tela faz hoje; subir o número é uma
  decisão, não um acidente (o detalhe da conta chegou a 89 antes do #74);
- a ESCALA compara a mesma tela com uma e com cinco rodadas de lançamentos.
  Um N+1 não aparece num teto medido com dois lançamentos, mas aparece aqui,
  porque o número de consultas passa a crescer junto com as linhas.

Cada rodada tem um lançamento simples, um parcelado e uma transferência
interna: são os três formatos que a edição inline decora de jeito diferente
(contraparte, operação, anexos).
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import AccountOwner, UserOwnerAccess
from accounts.services import save_function_permissions, save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_ALL,
    VIEW_REALIZED,
)
from core.domain.identity import USER_TYPE_USER
from transactions.models import CashFlowCategory
from transactions.services import TransactionRequest, create_transaction_batch

pytestmark = pytest.mark.django_db

PERMISSOES = {
    "dashboard.view",
    "transactions.view",
    "transactions.create",
    "transactions.update",
    "transactions.delete",
    "transactions.realize",
    "operations.view",
}


@pytest.fixture
def cenario():
    user = get_user_model().objects.create_user(
        username="operador", password="senha-segura", user_type=USER_TYPE_USER
    )
    save_function_permissions(user, PERMISSOES)
    owner = AccountOwner.objects.create(name="Titular")
    UserOwnerAccess.objects.create(user=user, owner=owner, can_view=True)
    institution = FinancialInstitution.objects.create(
        institution_name="Banco", institution_type="Banco"
    )
    conta = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Conta corrente",
        initial_balance=Decimal("1000.00"), initial_balance_date=date(2026, 1, 1),
    )
    reserva = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Reserva",
        initial_balance=Decimal("0.00"), initial_balance_date=date(2026, 1, 1),
    )
    save_transfer_destination_accesses(user, {reserva.id})
    client = Client()
    client.force_login(user)
    return SimpleNamespace(
        user=user, client=client, conta=conta, reserva=reserva,
        categorias=[
            CashFlowCategory.objects.create(category_name=f"Categoria {i}") for i in range(3)
        ],
        transferencia=CashFlowCategory.objects.create(
            category_name="Transferência", kind=CATEGORY_KIND_TRANSFER
        ),
    )


def _povoar(c, rodadas: int) -> None:
    inicio = date.today().replace(day=1)
    conta = c.conta
    for i in range(rodadas):
        categoria = c.categorias[i % len(c.categorias)]
        create_transaction_batch(TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_INCOME,
            description=f"Simples {i}", entry_amount=Decimal("50.00"), installments=1,
            due_date=inicio, status=STATUS_REALIZED, realized_date=inicio,
            realized_amount=Decimal("50.00"),
        ), user=c.user)
        create_transaction_batch(TransactionRequest(
            account_id=conta.id, category_id=categoria.id, entry_type=ENTRY_TYPE_EXPENSE,
            description=f"Parcelado {i}", entry_amount=Decimal("30.00"), installments=3,
            due_date=inicio, status=STATUS_PROJECTED,
        ), user=c.user)
        create_transaction_batch(TransactionRequest(
            account_id=conta.id, category_id=c.transferencia.id, entry_type=ENTRY_TYPE_EXPENSE,
            description=f"Transferência {i}", entry_amount=Decimal("10.00"), installments=1,
            due_date=inicio, status=STATUS_REALIZED, realized_date=inicio,
            realized_amount=Decimal("10.00"), counterparty_account_id=c.reserva.id,
        ), user=c.user)


def _consultas(client, url: str, **headers) -> int:
    # A primeira chamada aquece o que é legítimo guardar entre requisições
    # (sessão, ContentType); só a segunda é medida.
    client.get(url, **headers)
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url, **headers).status_code == 200
    return len(queries)


def _periodo() -> str:
    return date.today().strftime("%Y-%m")


def _url_lancamentos(conta, modo: str) -> str:
    return f"/transactions/?account_id={conta.id}&period={_periodo()}&mode={modo}"


HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.mark.parametrize("modo", [VIEW_REALIZED, VIEW_ALL])
@pytest.mark.parametrize("headers", [{}, HTMX], ids=["pagina", "fragmento"])
def test_lancamentos_nao_cresce_com_as_linhas(cenario, modo, headers):
    url = _url_lancamentos(cenario.conta, modo)

    _povoar(cenario, 1)
    com_uma = _consultas(cenario.client, url, **headers)
    _povoar(cenario, 4)
    com_cinco = _consultas(cenario.client, url, **headers)

    assert com_cinco == com_uma


@pytest.mark.parametrize("headers", [{}, HTMX], ids=["pagina", "fragmento"])
def test_dashboard_nao_cresce_com_as_linhas(cenario, headers):
    url = f"/dashboard/?period={_periodo()}"

    _povoar(cenario, 1)
    com_uma = _consultas(cenario.client, url, **headers)
    _povoar(cenario, 4)
    com_cinco = _consultas(cenario.client, url, **headers)

    assert com_cinco == com_uma


# Medido quando o teto foi fixado (entre parênteses), com folga de duas.
TETO_LANCAMENTOS = {
    (VIEW_REALIZED, "pagina"): 33,  # (31)
    (VIEW_REALIZED, "fragmento"): 33,  # (31)
    (VIEW_ALL, "pagina"): 35,  # (33)
    (VIEW_ALL, "fragmento"): 35,  # (33)
}
TETO_DASHBOARD = {"pagina": 18, "fragmento": 15}  # (16) e (13)


@pytest.mark.parametrize("modo", [VIEW_REALIZED, VIEW_ALL])
@pytest.mark.parametrize("headers", [{}, HTMX], ids=["pagina", "fragmento"])
def test_lancamentos_teto(cenario, modo, headers):
    _povoar(cenario, 2)
    tipo = "fragmento" if headers else "pagina"

    medidas = _consultas(cenario.client, _url_lancamentos(cenario.conta, modo), **headers)

    assert medidas <= TETO_LANCAMENTOS[(modo, tipo)], medidas


@pytest.mark.parametrize("headers", [{}, HTMX], ids=["pagina", "fragmento"])
def test_dashboard_teto(cenario, headers):
    _povoar(cenario, 2)
    tipo = "fragmento" if headers else "pagina"

    medidas = _consultas(cenario.client, f"/dashboard/?period={_periodo()}", **headers)

    assert medidas <= TETO_DASHBOARD[tipo], medidas
