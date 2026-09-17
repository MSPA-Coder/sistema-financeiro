import inspect
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import RequestFactory

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import (
    save_function_permissions,
    save_transfer_destination_accesses,
    transfer_destination_access_ids,
)
from bank_statements.models import BankStatementImport, BankStatementLine
from bank_statements.reconciliation import reconcile_line_with_entry
from banking.models import FinancialAccount, FinancialInstitution
from core import views as core_views
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SCOPE_ALL,
    OPERATION_SCOPE_SINGLE,
    STATUS_PROJECTED,
)
from reports.services import InvalidMonthPeriodError, resolve_month_period
from transactions import services, views
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.recurring_projection import ensure_recurring_projection_horizon

pytestmark = pytest.mark.django_db


@pytest.fixture
def transfer_setup():
    user = AppUser.objects.create_user(username="operador", password="senha-segura")
    owner = AccountOwner.objects.create(name="Titular A")
    destination_owner = AccountOwner.objects.create(name="Titular B")
    institution = FinancialInstitution.objects.create(institution_name="Banco dos testes", institution_type="Banco")
    origin = FinancialAccount.objects.create(owner=owner, institution=institution, account_name="Origem")
    destination = FinancialAccount.objects.create(owner=destination_owner, institution=institution, account_name="Destino secreto")
    UserOwnerAccess.objects.create(user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True)
    category = CashFlowCategory.objects.create(category_name="Interna", kind=CATEGORY_KIND_TRANSFER)
    return user, origin, destination, category


def _request(origin, destination, category):
    return services.TransactionRequest(
        account_id=origin.id, category_id=category.id, entry_type=ENTRY_TYPE_EXPENSE,
        description="transferir", entry_amount=Decimal("10.00"), installments=1,
        due_date=date(2026, 9, 10), status=STATUS_PROJECTED,
        counterparty_account_id=destination.id,
    )


def test_grant_is_required_to_create_and_revocation_blocks_both_legs(transfer_setup):
    user, origin, destination, category = transfer_setup
    req = _request(origin, destination, category)
    with pytest.raises(ValueError, match="destino não autorizada"):
        services.create_transaction_batch(req, user=user)
    assert CashFlowEntry.objects.count() == 0

    save_transfer_destination_accesses(user, {destination.id})
    entries = services.create_transaction_batch(req, user=user)
    assert len(entries) == 2
    save_transfer_destination_accesses(user, set())
    with pytest.raises(ValueError, match="destino não autorizada"):
        services.realize_transaction(entries[0], user=user)
    with pytest.raises(ValueError, match="destino não autorizada"):
        services.realize_transaction(entries[1], user=user)
    assert CashFlowEntry.objects.exclude(status="realizado").count() == 2


def test_only_granted_destination_is_exposed_to_transaction_context(transfer_setup):
    user, origin, destination, category = transfer_setup
    assert services.counterparty_accounts_for_transfer(user) == []
    save_transfer_destination_accesses(user, {destination.id})
    accounts = services.counterparty_accounts_for_transfer(user)
    assert [account.id for account in accounts] == [destination.id]
    assert origin.id not in [account.id for account in accounts]


@pytest.mark.parametrize("period,year,month", [("9999-12", None, None), (None, -1, 9), (None, 10000, 9), (None, 2026, 0)])
def test_explicit_invalid_period_is_rejected(period, year, month):
    with pytest.raises(InvalidMonthPeriodError):
        resolve_month_period(period, year, month, date(2026, 9, 1))


def test_transactions_view_returns_400_for_invalid_period(monkeypatch):
    request = RequestFactory().get("/transactions/?period=9999-12")
    request.user = object()
    request.session = {}
    response = inspect.unwrap(views.transactions_view)(request)
    assert response.status_code == 400


def test_transactions_htmx_invalid_period_keeps_400_and_emits_specific_flash_trigger():
    request = RequestFactory().get('/transactions/?period=9999-12', HTTP_HX_REQUEST='true')
    request.user = object()
    request.session = {}
    response = inspect.unwrap(views.transactions_view)(request)
    assert response.status_code == 400
    assert 'app:invalid-period' in response['HX-Trigger']


def test_projection_uses_last_transfer_template_grant(transfer_setup):
    user, origin, destination, category = transfer_setup
    later_owner = AccountOwner.objects.create(name="Titular C")
    later_destination = FinancialAccount.objects.create(
        owner=later_owner, institution=origin.institution, account_name="Destino revogado"
    )
    operation = BankOperation.objects.create(
        operation_key="recorrencia-grant-template", operation_type="internal_transfer", responsible_user=user
    )
    for due_date, target in ((date(2026, 8, 10), destination), (date(2026, 9, 10), later_destination)):
        source = CashFlowEntry.objects.create(
            account=origin, category=category, entry_type="despesa", entry_amount=Decimal("10"),
            due_date=due_date, is_recurring=True, operation_type="internal_transfer", bank_operation=operation,
        )
        CashFlowEntry.objects.create(
            account=target, category=category, entry_type="receita", entry_amount=Decimal("10"), due_date=due_date,
            is_recurring=True, operation_type="internal_transfer", bank_operation=operation, source_entry=source,
        )
    save_transfer_destination_accesses(user, {destination.id})
    result = ensure_recurring_projection_horizon(today=date(2026, 9, 12), horizon_months=2, update_last_run=False)
    assert result.generated_count == 0
    assert CashFlowEntry.objects.filter(bank_operation=operation).count() == 4


def _assign_responsible(request, *, operation_id, target):
    request.user, _ = AppUser.objects.get_or_create(
        username='administrator-assignment', defaults={'user_type': 'administrator'}
    )
    request.POST = request.POST.copy()
    request.POST['operation_id'] = str(operation_id)
    request.POST['user_id'] = str(target.id)
    with patch('core.views.messages'):
        return inspect.unwrap(core_views.permissions_view)(request)


def test_assign_recurring_responsible_requires_full_mandate_and_audits(transfer_setup):
    target, origin, destination, category = transfer_setup
    save_function_permissions(target, {'transactions.create'})
    operation = BankOperation.objects.create(operation_key='legacy-recurring', operation_type='internal_transfer')
    source = CashFlowEntry.objects.create(
        account=origin, category=category, entry_type='despesa', entry_amount=Decimal('10'),
        due_date=date(2026, 9, 10), is_recurring=True, operation_type='internal_transfer', bank_operation=operation,
    )
    CashFlowEntry.objects.create(
        account=destination, category=category, entry_type='receita', entry_amount=Decimal('10'),
        due_date=date(2026, 9, 10), is_recurring=True, operation_type='internal_transfer', bank_operation=operation,
        source_entry=source,
    )
    request = RequestFactory().post('/permissions/', data={'action': 'assign_operation_responsible'})

    _assign_responsible(request, operation_id=operation.id, target=target)
    operation.refresh_from_db()
    assert operation.responsible_user is None

    save_transfer_destination_accesses(target, {destination.id})
    _assign_responsible(request, operation_id=operation.id, target=target)
    operation.refresh_from_db()
    assert operation.responsible_user_id == target.id
    from core.models import AuditLog
    assert AuditLog.objects.filter(entity_name='bank_operation', entity_id=str(operation.id), action='assign_responsible').exists()


def test_assign_responsible_rejects_invalid_id_without_server_error(transfer_setup):
    target, _origin, _destination, _category = transfer_setup
    request = RequestFactory().post('/permissions/', data={'action': 'assign_operation_responsible'})
    response = _assign_responsible(request, operation_id='not-an-id', target=target)
    assert response.status_code == 302


def test_transfer_destination_matrix_rolls_back_when_audit_fails(transfer_setup):
    target, _origin, destination, _category = transfer_setup
    request = RequestFactory().post(
        '/permissions/',
        data={'action': 'save_transfer_destinations', 'user_id': target.id, f'transfer_destination_{destination.id}': 'on'},
    )
    request.user = AppUser.objects.create_user(
        username='administrator-matrix', password='senha-segura', user_type='administrator'
    )
    with (
        patch('core.views.log_audit_event', side_effect=RuntimeError('audit unavailable')),
        patch('core.views.messages'),
        pytest.raises(RuntimeError, match='audit unavailable'),
    ):
        inspect.unwrap(core_views.permissions_view)(request)
    assert transfer_destination_access_ids(target) == set()


def test_conversion_to_transfer_is_refused_after_destination_access_is_revoked(transfer_setup):
    user, origin, destination, category = transfer_setup
    regular_category = CashFlowCategory.objects.create(category_name='Regular')
    entry = CashFlowEntry.objects.create(
        account=origin, category=regular_category, entry_type='despesa', entry_amount=Decimal('10'),
        due_date=date(2026, 9, 10), status=STATUS_PROJECTED,
    )

    with pytest.raises(ValueError, match='destino não autorizada'):
        services.update_transaction_operation(entry, _request(origin, destination, category), user=user)

    entry.refresh_from_db()
    assert entry.category_id == regular_category.id
    assert entry.operation_type == 'single'
    assert CashFlowEntry.objects.count() == 1


def test_revoked_destination_only_blocks_the_affected_transfer_pair(transfer_setup):
    user, origin, destination, category = transfer_setup
    later_owner = AccountOwner.objects.create(name='Titular C')
    later_destination = FinancialAccount.objects.create(
        owner=later_owner, institution=origin.institution, account_name='Segundo destino'
    )
    operation = BankOperation.objects.create(operation_key='two-transfer-pairs', operation_type='internal_transfer')
    pairs = []
    for due_date, target in ((date(2026, 9, 10), destination), (date(2026, 10, 10), later_destination)):
        source = CashFlowEntry.objects.create(
            account=origin, category=category, entry_type='despesa', entry_amount=Decimal('10'), due_date=due_date,
            is_recurring=True, operation_type='internal_transfer', bank_operation=operation,
        )
        counterpart = CashFlowEntry.objects.create(
            account=target, category=category, entry_type='receita', entry_amount=Decimal('10'), due_date=due_date,
            is_recurring=True, operation_type='internal_transfer', bank_operation=operation, source_entry=source,
        )
        pairs.append((source, counterpart))

    save_transfer_destination_accesses(user, {destination.id})
    assert services.delete_transaction_or_operation(pairs[0][0], OPERATION_SCOPE_SINGLE, user=user) == 2
    assert CashFlowEntry.objects.filter(account=later_destination).count() == 1
    with pytest.raises(ValueError, match='destino não autorizada'):
        services.delete_transaction_or_operation(pairs[1][0], OPERATION_SCOPE_ALL, user=user)
    assert CashFlowEntry.objects.filter(account=later_destination).count() == 1


def test_reconciliation_does_not_bypass_a_revoked_transfer_destination_grant(transfer_setup):
    user, origin, destination, category = transfer_setup
    save_transfer_destination_accesses(user, {destination.id})
    origin_entry, _destination_entry = services.create_transaction_batch(_request(origin, destination, category), user=user)
    save_transfer_destination_accesses(user, set())
    import_batch = BankStatementImport.objects.create(account=origin, source_filename='review.csv')
    line = BankStatementLine.objects.create(
        import_batch=import_batch, account=origin, statement_date=date(2026, 9, 10),
        description='Transferência', amount=Decimal('-10.00'), line_hash='revoked-grant-reconcile',
    )

    with pytest.raises(ValueError, match='destino não autorizada'):
        reconcile_line_with_entry(user, line_id=line.id, entry_id=origin_entry.id)

    line.refresh_from_db()
    origin_entry.refresh_from_db()
    assert line.status == 'novo'
    assert line.matched_entry_id is None
    assert origin_entry.status != 'realizado'


def test_matching_realized_transfer_cannot_be_reconciled_after_grant_revocation(transfer_setup):
    user, origin, destination, category = transfer_setup
    save_transfer_destination_accesses(user, {destination.id})
    origin_entry, _destination_entry = services.create_transaction_batch(_request(origin, destination, category), user=user)
    services.realize_transaction(origin_entry, realized_date=date(2026, 9, 10), user=user)
    save_transfer_destination_accesses(user, set())
    import_batch = BankStatementImport.objects.create(account=origin, source_filename='review-realized.csv')
    line = BankStatementLine.objects.create(
        import_batch=import_batch, account=origin, statement_date=date(2026, 9, 10),
        description='Transferência já realizada', amount=Decimal('-10.00'), line_hash='revoked-grant-realized',
    )

    with pytest.raises(ValueError, match='destino não autorizada'):
        reconcile_line_with_entry(user, line_id=line.id, entry_id=origin_entry.id)

    line.refresh_from_db()
    assert line.status == 'novo'
    assert line.matched_entry_id is None
