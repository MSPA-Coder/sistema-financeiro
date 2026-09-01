"""Serviços de Cadastros: Instituições e Contas financeiras."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.db.models import ProtectedError

from accounts.services import accessible_owner_ids, can_access_owner

from .models import FinancialAccount, FinancialInstitution

_MAX_NAME_LENGTH = 100
_VALID_INSTITUTION_TYPES = ("Banco", "Corretora")


# --- Instituições ---

def list_institutions(institution_type: str | None = None):
    queryset = FinancialInstitution.objects.all()
    if institution_type:
        queryset = queryset.filter(institution_type=institution_type)
    return queryset


def _clean_institution_fields(name: str, institution_type: str) -> tuple[str, str]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Nome da instituição é obrigatório.")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"Nome da instituição não pode exceder {_MAX_NAME_LENGTH} caracteres.")

    institution_type = (institution_type or "Banco").strip().capitalize()
    if institution_type not in _VALID_INSTITUTION_TYPES:
        raise ValueError("Tipo de instituição inválido.")

    return name, institution_type


def create_institution(name: str, institution_type: str) -> FinancialInstitution:
    clean_name, clean_type = _clean_institution_fields(name, institution_type)
    if FinancialInstitution.objects.filter(institution_name__iexact=clean_name).exists():
        raise ValueError("Já existe uma instituição com esse nome.")
    try:
        return FinancialInstitution.objects.create(
            institution_name=clean_name, institution_type=clean_type
        )
    except IntegrityError as exc:
        raise ValueError("Já existe uma instituição com esse nome.") from exc


def update_institution(institution: FinancialInstitution, name: str, institution_type: str) -> FinancialInstitution:
    clean_name, clean_type = _clean_institution_fields(name, institution_type)
    if FinancialInstitution.objects.filter(institution_name__iexact=clean_name).exclude(id=institution.id).exists():
        raise ValueError("Já existe uma instituição com esse nome.")
    institution.institution_name = clean_name
    institution.institution_type = clean_type
    try:
        institution.save(update_fields=["institution_name", "institution_type", "updated_at"])
    except IntegrityError as exc:
        raise ValueError("Já existe uma instituição com esse nome.") from exc
    return institution


def delete_institution(institution: FinancialInstitution) -> None:
    try:
        institution.delete()
    except ProtectedError as exc:
        raise ValueError(
            "Não é possível excluir esta instituição: existem contas vinculadas a ela."
        ) from exc


# --- Contas ---

def accessible_account_ids(user, action: str = "view") -> list[int]:
    """IDs de contas que `user` pode acessar para a ação informada.

    Contas não têm controle de acesso próprio: o escopo é herdado do
    titular (`AccountOwner`) a que pertencem, via `accessible_owner_ids`.
    Helper central para qualquer módulo (Cadastros, Transações, Bancos) que
    precise restringir consultas a contas visíveis ao usuário.
    """
    owner_ids = accessible_owner_ids(user, action)
    if not owner_ids:
        return []
    return list(FinancialAccount.objects.filter(owner_id__in=owner_ids).values_list('id', flat=True))


def can_access_account(user, account_id, action: str = "view") -> bool:
    """True se `user` pode acessar a conta `account_id` para a ação informada."""
    if not account_id:
        return False
    try:
        account = FinancialAccount.objects.only('owner_id').get(id=account_id)
    except FinancialAccount.DoesNotExist:
        return False
    return can_access_owner(user, account.owner_id, action)


def list_accounts_for_user(user, owner_id: int | None = None, institution_id: int | None = None):
    """Contas visíveis para `user`, restritas aos titulares acessíveis."""
    owner_ids = accessible_owner_ids(user, "view")
    queryset = FinancialAccount.objects.select_related('owner', 'institution').filter(
        owner_id__in=owner_ids
    )
    if owner_id:
        queryset = queryset.filter(owner_id=owner_id)
    if institution_id:
        queryset = queryset.filter(institution_id=institution_id)
    return queryset


def _parse_initial_balance(raw_value: str | None) -> Decimal:
    if raw_value is None or str(raw_value).strip() == "":
        return Decimal("0.00")
    try:
        return Decimal(str(raw_value).strip().replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError("Saldo inicial inválido.") from exc


def _clean_account_fields(owner_id: str, institution_id: str, account_name: str, initial_balance: str):
    try:
        owner_id_int = int(owner_id)
        if owner_id_int <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Titular é obrigatório.") from exc

    try:
        institution_id_int = int(institution_id)
        if institution_id_int <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Instituição é obrigatória.") from exc

    account_name = (account_name or "").strip()
    if not account_name:
        raise ValueError("Nome da conta é obrigatório.")
    if len(account_name) > _MAX_NAME_LENGTH:
        raise ValueError(f"Nome da conta não pode exceder {_MAX_NAME_LENGTH} caracteres.")

    balance = _parse_initial_balance(initial_balance)

    return owner_id_int, institution_id_int, account_name, balance


def create_account(user, *, owner_id: str, institution_id: str, account_name: str, initial_balance: str) -> FinancialAccount:
    clean_owner_id, clean_institution_id, clean_name, balance = _clean_account_fields(
        owner_id, institution_id, account_name, initial_balance
    )
    if not can_access_owner(user, clean_owner_id, "create"):
        raise ValueError("Acesso negado: você não pode criar contas para este titular.")
    if not FinancialInstitution.objects.filter(id=clean_institution_id).exists():
        raise ValueError("Instituição não encontrada.")

    return FinancialAccount.objects.create(
        owner_id=clean_owner_id,
        institution_id=clean_institution_id,
        account_name=clean_name,
        initial_balance=balance,
    )


def update_account(user, account: FinancialAccount, *, owner_id: str, institution_id: str, account_name: str, initial_balance: str) -> FinancialAccount:
    clean_owner_id, clean_institution_id, clean_name, balance = _clean_account_fields(
        owner_id, institution_id, account_name, initial_balance
    )
    if not can_access_owner(user, account.owner_id, "update"):
        raise ValueError("Acesso negado: você não pode alterar contas deste titular.")
    if not can_access_owner(user, clean_owner_id, "update"):
        raise ValueError("Acesso negado: você não pode transferir a conta para este titular.")
    if not FinancialInstitution.objects.filter(id=clean_institution_id).exists():
        raise ValueError("Instituição não encontrada.")

    account.owner_id = clean_owner_id
    account.institution_id = clean_institution_id
    account.account_name = clean_name
    account.initial_balance = balance
    account.save(update_fields=["owner", "institution", "account_name", "initial_balance", "updated_at"])
    return account


def delete_account(user, account: FinancialAccount) -> None:
    if not can_access_owner(user, account.owner_id, "delete"):
        raise ValueError("Acesso negado: você não pode excluir contas deste titular.")
    try:
        account.delete()
    except ProtectedError as exc:
        raise ValueError(
            "Não é possível excluir esta conta: existem lançamentos vinculados a ela."
        ) from exc
