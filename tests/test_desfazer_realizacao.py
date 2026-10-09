"""Desfazer realização pela tela de lançamentos.

O incidente de 25/09/2026 mostrou que a regra já existia no service, mas só
ficava alcançável indiretamente ao desfazer uma conciliação. Este fluxo precisa
voltar o lançamento a ficar em aberto, limpar os dados de realização e deixar
trilha de auditoria, sem exigir uma data que deixou de fazer sentido.

"Em aberto" segue a data, como em toda a tela: vencimento a partir de hoje é
"A vencer", vencimento passado é "Vencidos". Até 09/10/2026 desfazer gravava
sempre `vencidos`, e um lançamento de vencimento futuro (ou de hoje) sumia das
duas visões -- só "Todos os modos" o mostrava (auditoria, lateral L01).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from accounts.models import AccountOwner
from accounts.services import save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import CATEGORY_KIND_TRANSFER, ENTRY_TYPE_EXPENSE, STATUS_REALIZED
from core.models import AuditLog
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import TransactionRequest, close_month, create_transaction_batch

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = get_user_model().objects.create_user(
        username="desfazer-realizacao", password="senha-segura", user_type="administrator",
    )
    owner = AccountOwner.objects.create(name="Titular do teste")
    institution = FinancialInstitution.objects.create(
        institution_name="Banco do teste", institution_type="Banco",
    )
    origin = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Conta origem",
    )
    destination = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Conta destino",
    )
    save_transfer_destination_accesses(user, {destination.id})
    return {
        "user": user,
        "origin": origin,
        "destination": destination,
        "expense": CashFlowCategory.objects.create(category_name="Despesa de teste"),
        "transfer": CashFlowCategory.objects.create(
            category_name="Transferência de teste", kind=CATEGORY_KIND_TRANSFER,
        ),
    }


def _realized_request(cenario, *, category=None, counterparty_account_id=None, due_date=None):
    due_date = due_date or date.today()
    return TransactionRequest(
        account_id=cenario["origin"].id,
        category_id=(category or cenario["expense"]).id,
        entry_type=ENTRY_TYPE_EXPENSE,
        description="Movimento realizado",
        entry_amount=Decimal("100.00"),
        installments=1,
        due_date=due_date,
        status=STATUS_REALIZED,
        realized_date=min(due_date, date.today()),
        counterparty_account_id=counterparty_account_id,
    )


def test_tela_desfaz_realizacao_limpa_campos_e_audita(client, cenario):
    [entry] = create_transaction_batch(_realized_request(cenario), user=cenario["user"])
    client.force_login(cenario["user"])

    response = client.post(f"/mark_unrealized/{entry.id}/")

    assert response.status_code == 302
    entry.refresh_from_db()
    assert (entry.realized_date, entry.realized_amount) == (None, None)
    assert _aparece_na_visao(client, entry, "a_vencer")
    audit = AuditLog.objects.get(entity_name="cash_flow_entry", entity_id=str(entry.id), action="unrealize")
    assert audit.user_id == cenario["user"].id


def test_tela_desfaz_as_duas_pontas_de_transferencia(client, cenario):
    origin, destination = create_transaction_batch(
        _realized_request(
            cenario, category=cenario["transfer"], counterparty_account_id=cenario["destination"].id,
        ),
        user=cenario["user"],
    )
    client.force_login(cenario["user"])

    response = client.post(f"/mark_unrealized/{origin.id}/")

    assert response.status_code == 302
    entries = list(CashFlowEntry.objects.filter(id__in=[origin.id, destination.id]).order_by("id"))
    assert [(entry.status, entry.realized_date, entry.realized_amount) for entry in entries] == [
        ("a_vencer", None, None),
        ("a_vencer", None, None),
    ]
    assert AuditLog.objects.filter(
        entity_name="cash_flow_entry", entity_id__in=[str(origin.id), str(destination.id)], action="unrealize",
    ).count() == 2


def test_tela_recusa_desfazer_realizacao_em_mes_fechado(client, cenario):
    [entry] = create_transaction_batch(_realized_request(cenario), user=cenario["user"])
    close_month(
        cenario["origin"], date.today().year, date.today().month, Decimal("0.00"), cenario["user"],
    )
    client.force_login(cenario["user"])

    response = client.post(f"/mark_unrealized/{entry.id}/")

    assert response.status_code == 302
    entry.refresh_from_db()
    assert entry.status == STATUS_REALIZED
    assert AuditLog.objects.filter(
        entity_name="cash_flow_entry", entity_id=str(entry.id), action="unrealize", result="failure",
    ).exists()


def test_lista_exibe_acao_para_lancamento_realizado(client, cenario):
    create_transaction_batch(_realized_request(cenario), user=cenario["user"])
    client.force_login(cenario["user"])

    response = client.get("/transactions/?mode=realizado")

    assert response.status_code == 200
    assert b'data-transaction-action="unrealize"' in response.content


def _aparece_na_visao(client, entry, modo):
    """O lançamento está na lista de Lançamentos no modo e no mês dele."""
    periodo = entry.due_date.strftime("%Y-%m")
    response = client.get(f"/transactions/?period={periodo}&mode={modo}")
    assert response.status_code == 200
    return f'id="tx-row-{entry.id}"'.encode() in response.content


def _mes_seguinte(dia):
    return (dia.replace(day=1) + timedelta(days=32)).replace(day=10)


def test_desfazer_com_vencimento_futuro_volta_para_a_vencer(client, cenario):
    """O caso que sumia: vencimento no mês seguinte, realizado antecipado."""
    futuro = _mes_seguinte(date.today())
    [entry] = create_transaction_batch(_realized_request(cenario, due_date=futuro), user=cenario["user"])
    client.force_login(cenario["user"])

    client.post(f"/mark_unrealized/{entry.id}/")

    entry.refresh_from_db()
    assert _aparece_na_visao(client, entry, "a_vencer")
    assert not _aparece_na_visao(client, entry, "vencidos")


def test_desfazer_com_vencimento_passado_volta_para_vencidos(client, cenario):
    passado = (date.today().replace(day=1) - timedelta(days=1)).replace(day=10)
    [entry] = create_transaction_batch(_realized_request(cenario, due_date=passado), user=cenario["user"])
    client.force_login(cenario["user"])

    client.post(f"/mark_unrealized/{entry.id}/")

    entry.refresh_from_db()
    assert _aparece_na_visao(client, entry, "vencidos")
    assert not _aparece_na_visao(client, entry, "a_vencer")
