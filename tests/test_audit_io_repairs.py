"""Regressões das correções de concorrência e consistência de I/O."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError

from accounts.login_lockout import register_failed_login_attempt
from accounts.models import AccountOwner, AppUser, LoginLockout, UserOwnerAccess
from bank_statements.attachments import delete_entry_attachment, save_entry_attachment
from bank_statements.models import BankStatementImport, BankStatementLine, EntryAttachment
from bank_statements.reconciliation import reconcile_line_with_entry
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import ENTRY_TYPE_EXPENSE, STATUS_PENDING
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def audit_io_setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    user = AppUser.objects.create_user(username="auditoria", password="senha-segura")
    owner = AccountOwner.objects.create(name="Titular auditado")
    institution = FinancialInstitution.objects.create(
        institution_name="Banco auditado", institution_type="Banco"
    )
    account = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Conta auditada"
    )
    UserOwnerAccess.objects.create(user=user, owner=owner, can_view=True, can_create=True, can_update=True)
    category = CashFlowCategory.objects.create(category_name="Despesa auditada")
    entry = CashFlowEntry.objects.create(
        account=account,
        category=category,
        entry_type=ENTRY_TYPE_EXPENSE,
        description="Compra",
        entry_amount=Decimal("10.00"),
        due_date=date(2026, 9, 10),
        status=STATUS_PENDING,
    )
    statement_import = BankStatementImport.objects.create(
        account=account, source_filename="auditoria.txt"
    )
    return SimpleNamespace(
        user=user, account=account, entry=entry, statement_import=statement_import
    )


def test_attachment_db_failure_removes_written_file(audit_io_setup, settings):
    upload = SimpleUploadedFile("comprovante.txt", b"conteudo")
    with (
        patch.object(EntryAttachment.objects, "create", side_effect=IntegrityError("db indisponivel")),
        pytest.raises(IntegrityError),
    ):
        save_entry_attachment(audit_io_setup.user, audit_io_setup.entry.id, upload)
    assert not list((Path(settings.MEDIA_ROOT) / "attachments").iterdir())


@pytest.mark.django_db(transaction=True)
def test_attachment_delete_is_authorized_and_removes_file_after_commit(audit_io_setup, settings):
    attachment = save_entry_attachment(
        audit_io_setup.user,
        audit_io_setup.entry.id,
        SimpleUploadedFile("comprovante.txt", b"conteudo"),
    )
    path = Path(settings.MEDIA_ROOT) / attachment.stored_path
    assert path.is_file()

    delete_entry_attachment(audit_io_setup.user, attachment_id=attachment.id)

    assert not path.exists()
    assert not EntryAttachment.objects.filter(id=attachment.id).exists()


def test_reconciliation_revalidates_unique_match_after_first_commit(audit_io_setup):
    line_one = BankStatementLine.objects.create(
        import_batch=audit_io_setup.statement_import,
        account=audit_io_setup.account,
        statement_date=date(2026, 9, 10),
        description="Compra 1",
        amount=Decimal("-10.00"),
        line_hash="compra-1",
    )
    line_two = BankStatementLine.objects.create(
        import_batch=audit_io_setup.statement_import,
        account=audit_io_setup.account,
        statement_date=date(2026, 9, 10),
        description="Compra 2",
        amount=Decimal("-10.00"),
        line_hash="compra-2",
    )

    reconcile_line_with_entry(
        audit_io_setup.user, line_id=line_one.id, entry_id=audit_io_setup.entry.id
    )
    with pytest.raises(ValueError, match="já está conciliado"):
        reconcile_line_with_entry(
            audit_io_setup.user, line_id=line_two.id, entry_id=audit_io_setup.entry.id
        )


def test_login_lockout_keeps_consistent_increment_and_threshold():
    with patch("core.services.get_login_lockout_policy_settings") as policy:
        policy.return_value = SimpleNamespace(max_failures=3, lock_seconds=60)
        assert register_failed_login_attempt("Usuario", "127.0.0.1") == (2, None)
        assert register_failed_login_attempt("usuario", "127.0.0.1") == (1, None)
        remaining, wait_seconds = register_failed_login_attempt("usuario", "127.0.0.1")

    assert remaining == 0
    assert wait_seconds == 60
    row = LoginLockout.objects.get(identity_key="usuario|127.0.0.1")
    assert row.failure_count == 0
    assert row.locked_until_ts is not None
