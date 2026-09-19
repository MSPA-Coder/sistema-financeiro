"""Regressões para concorrência e atomicidade dos services financeiros."""

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import AccountOwner, AppUser, UserOwnerAccess
from accounts.services import save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SCOPE_CURRENT_FUTURE,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions import services
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def cenario():
    user = AppUser.objects.create_user(username="concorrencia", password="senha-segura")
    owner = AccountOwner.objects.create(name="Titular")
    institution = FinancialInstitution.objects.create(
        institution_name="Banco dos testes", institution_type="Banco"
    )
    origem = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Origem"
    )
    destino = FinancialAccount.objects.create(
        owner=owner, institution=institution, account_name="Destino"
    )
    UserOwnerAccess.objects.create(
        user=user, owner=owner, can_view=True, can_create=True, can_update=True, can_delete=True
    )
    save_transfer_destination_accesses(user, {destino.id})
    categoria = CashFlowCategory.objects.create(
        category_name="Transferência", kind=CATEGORY_KIND_TRANSFER
    )
    normal = CashFlowCategory.objects.create(category_name="Despesa")
    return user, origem, destino, categoria, normal


def _request(account, category, *, due_date, **overrides):
    values = {
        "account_id": account.id,
        "category_id": category.id,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Movimento de teste",
        "entry_amount": Decimal("100.00"),
        "installments": 1,
        "due_date": due_date,
        "status": STATUS_PROJECTED,
    }
    values.update(overrides)
    return services.TransactionRequest(**values)


def test_realize_revalida_lancamento_obsoleto_apos_adquirir_lock(cenario):
    user, origem, destino, transferencia, _normal = cenario
    origem_entry, destino_entry = services.create_transaction_batch(
        _request(origem, transferencia, due_date=date(2026, 9, 10), counterparty_account_id=destino.id),
        user=user,
    )
    stale_origin = CashFlowEntry.objects.get(pk=origem_entry.pk)
    CashFlowEntry.objects.filter(pk=origem_entry.pk).update(
        status=STATUS_REALIZED,
        realized_date=date(2026, 9, 11),
        realized_amount=Decimal("100.00"),
    )

    with pytest.raises(ValueError, match="já está realizado"):
        services.realize_transaction(
            stale_origin, realized_date=date(2026, 9, 12), user=user
        )

    destino_entry.refresh_from_db()
    assert destino_entry.status == STATUS_PENDING


def test_unrealize_transferencia_desfaz_as_duas_pontas_se_a_segunda_falhar(
    cenario, monkeypatch
):
    user, origem, destino, transferencia, _normal = cenario
    origem_entry, destino_entry = services.create_transaction_batch(
        _request(
            origem,
            transferencia,
            due_date=date(2026, 9, 10),
            counterparty_account_id=destino.id,
            status=STATUS_REALIZED,
            realized_date=date(2026, 9, 10),
        ),
        user=user,
    )

    original_save = CashFlowEntry.save
    calls = 0

    def fail_on_second_save(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("falha na segunda ponta")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(CashFlowEntry, "save", fail_on_second_save)
    with pytest.raises(RuntimeError, match="segunda ponta"):
        services.unrealize_transaction(origem_entry, user=user)

    assert list(
        CashFlowEntry.objects.filter(pk__in=[origem_entry.pk, destino_entry.pk])
        .order_by("id")
        .values_list("status", "realized_date", "realized_amount")
    ) == [
        (STATUS_REALIZED, date(2026, 9, 10), Decimal("100.00")),
        (STATUS_REALIZED, date(2026, 9, 10), Decimal("100.00")),
    ]


def test_fechamento_revalida_criacao_e_mutacao_de_lancamento(cenario):
    user, origem, _destino, _transferencia, normal = cenario
    services.close_month(origem, 2026, 9, Decimal("0.00"), user)

    with pytest.raises(ValueError, match="fechado"):
        services.create_transaction_batch(
            _request(origem, normal, due_date=date(2026, 9, 10)), user=user
        )

    aberta = services.create_transaction_batch(
        _request(origem, normal, due_date=date(2026, 10, 10)), user=user
    )[0]
    aberta.refresh_from_db()
    with pytest.raises(ValueError, match="fechado"):
        services.update_transaction_operation(
            aberta,
            _request(origem, normal, due_date=date(2026, 9, 10)),
            user=user,
        )

    assert not CashFlowEntry.objects.filter(due_date=date(2026, 9, 10)).exists()


def test_edicao_composta_reverte_recriacao_se_novo_periodo_estiver_fechado(cenario):
    user, origem, _destino, _transferencia, normal = cenario
    entradas = services.create_transaction_batch(
        _request(
            origem,
            normal,
            due_date=date(2026, 9, 10),
            installments=2,
        ),
        user=user,
    )
    ids_originais = [entrada.id for entrada in entradas]
    services.close_month(origem, 2026, 11, Decimal("0.00"), user)

    with pytest.raises(ValueError, match="fechado"):
        services.update_transaction_operation(
            entradas[0],
            _request(
                origem,
                normal,
                due_date=date(2026, 11, 10),
                installments=2,
            ),
            operation_scope=OPERATION_SCOPE_CURRENT_FUTURE,
            current_future_confirmation_token=services.current_future_confirmation_token(
                entradas[0].id
            ),
            user=user,
        )

    assert list(
        CashFlowEntry.objects.filter(bank_operation_id=entradas[0].bank_operation_id)
        .order_by("id")
        .values_list("id", "due_date", "status")
    ) == [
        (ids_originais[0], date(2026, 9, 10), STATUS_PENDING),
        (ids_originais[1], date(2026, 10, 10), STATUS_PROJECTED),
    ]
    assert AccountMonthClose.objects.filter(account=origem, year=2026, month=11, active=True).exists()
