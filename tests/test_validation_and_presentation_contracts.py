"""Regressões dos achados CB-03, CB-04, CB-12 e CB-13.

Nomes duplicados são recusados em duas camadas -- o serviço dá a mensagem, o
índice do banco fecha a corrida entre duas gravações --, e as duas são medidas
contra o PostgreSQL, não contra um duble do ORM.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string

from accounts import services as account_services
from banking import services as banking_services


@pytest.mark.django_db
def test_owner_duplicate_is_rejected_case_insensitively():
    account_services.create_owner("Ana")

    with pytest.raises(ValueError, match="Já existe um titular"):
        account_services.create_owner("ANA")


@pytest.mark.django_db
def test_institution_duplicate_is_rejected_case_insensitively():
    banking_services.create_institution("Banco Azul", "Banco")

    with pytest.raises(ValueError, match="Já existe uma instituição"):
        banking_services.create_institution("banco azul", "Banco")


@pytest.mark.django_db
def test_name_duplicates_also_have_database_constraints():
    """Duas gravações simultâneas passam juntas pela checagem do serviço."""
    from accounts.models import AccountOwner
    from banking.models import FinancialInstitution

    AccountOwner.objects.create(name="Ana")
    FinancialInstitution.objects.create(institution_name="Banco Azul", institution_type="Banco")

    with pytest.raises(IntegrityError), transaction.atomic():
        AccountOwner.objects.create(name="ana")
    with pytest.raises(IntegrityError), transaction.atomic():
        FinancialInstitution.objects.create(institution_name="BANCO AZUL", institution_type="Banco")


def test_management_template_displays_expense_with_negative_sign():
    """Despesa é gravada positiva; na tela ela sai com o sinal de saída."""
    conta = SimpleNamespace(currency="BRL", account_name="Conta", owner=SimpleNamespace(name="Ana"))
    despesa = SimpleNamespace(
        id=1, due_date=None, account=conta, description="Mercado", entry_type="despesa",
        entry_amount=Decimal("12.50"), linked_project=None, linked_tags=[],
    )

    html = render_to_string("management/partials/management_content.html", {"recent_entries": [despesa]})

    assert "- R$ 12,50" in html


def test_password_floor_cannot_be_configured_away(monkeypatch):
    """O piso de 8 vale mesmo que a configuração grave um valor menor."""
    from accounts import password_validators
    from core import services as core_services

    monkeypatch.setattr(core_services, "get_app_setting", lambda chave, padrao: "4")

    assert password_validators.current_min_length() == password_validators.MIN_LENGTH_FLOOR
