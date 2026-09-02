"""Serviços do módulo Gestão: tags, projetos/centros de custo e orçamento mensal.

Camada de regra de negócio entre `management/views.py` (HTTP) e os models
(`management/models.py`). Segue o mesmo padrão de `reports/services.py` e
`transactions/services.py`: views tratam só HTTP, services tratam validação e
consulta, models só guardam dados.

O "realizado" de `MonthlyBudget` não é lido do campo persistido
`actual_amount` (que existe no schema herdado, mas não é fonte de verdade) —
é calculado em tempo real a partir dos lançamentos (`CashFlowEntry`)
realizados no mês/categoria/titular, evitando ficar dessincronizado conforme
lançamentos são criados/editados/estornados depois do orçamento salvo.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import IntegrityError
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from accounts.models import AccountOwner
from accounts.services import accessible_owner_ids
from core.domain.finance import STATUS_REALIZED
from reports.services import add_months, to_decimal
from transactions.access import can_access_entry
from transactions.models import CashFlowCategory, CashFlowEntry

from .models import (
    CashFlowEntryProject,
    CashFlowEntryTag,
    ManagementProject,
    ManagementTag,
    MonthlyBudget,
)

MONEY_QUANT = Decimal("0.01")
_AMOUNT_FIELD: DecimalField = DecimalField(max_digits=14, decimal_places=2)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def list_tags(*, active_only: bool = False):
    qs = ManagementTag.objects.all()
    return qs.filter(active=True) if active_only else qs


def create_tag(tag_name: str) -> ManagementTag:
    clean_name = (tag_name or "").strip()
    if not clean_name:
        raise ValueError("Nome da tag é obrigatório.")
    if len(clean_name) > 80:
        raise ValueError("Nome da tag não pode exceder 80 caracteres.")
    if ManagementTag.objects.filter(tag_name__iexact=clean_name).exists():
        raise ValueError("Já existe uma tag com esse nome.")
    try:
        return ManagementTag.objects.create(tag_name=clean_name)
    except IntegrityError as exc:
        raise ValueError("Já existe uma tag com esse nome.") from exc


def retire_tag(tag_id) -> tuple[ManagementTag, str]:
    tag = _resolve_tag(tag_id)
    if tag.entry_links.exists():
        tag.active = False
        tag.save(update_fields=["active", "updated_at"])
        return tag, "archived"
    tag.delete()
    return tag, "deleted"


# ---------------------------------------------------------------------------
# Projetos / centros de custo
# ---------------------------------------------------------------------------

def list_projects(*, active_only: bool = False):
    qs = ManagementProject.objects.all()
    if active_only:
        qs = qs.filter(active=True)
    return qs


def create_project(project_name: str, description: str = "") -> ManagementProject:
    clean_name = (project_name or "").strip()
    if not clean_name:
        raise ValueError("Nome do projeto é obrigatório.")
    if len(clean_name) > 120:
        raise ValueError("Nome do projeto não pode exceder 120 caracteres.")
    clean_description = (description or "").strip()
    if len(clean_description) > 255:
        raise ValueError("Descrição não pode exceder 255 caracteres.")
    if ManagementProject.objects.filter(project_name__iexact=clean_name).exists():
        raise ValueError("Já existe um projeto com esse nome.")
    try:
        return ManagementProject.objects.create(project_name=clean_name, description=clean_description)
    except IntegrityError as exc:
        raise ValueError("Já existe um projeto com esse nome.") from exc


def retire_project(project_id) -> tuple[ManagementProject, str]:
    project = _resolve_project(project_id)
    if project.entry_links.exists():
        project.active = False
        project.save(update_fields=["active", "updated_at"])
        return project, "archived"
    project.delete()
    return project, "deleted"


# ---------------------------------------------------------------------------
# Vínculos tag/projeto <-> lançamento
# ---------------------------------------------------------------------------

def assign_tag_to_entry(user, entry_id, tag_id) -> CashFlowEntryTag:
    entry = _resolve_owned_entry(user, entry_id)
    tag = _resolve_tag(tag_id)
    link, _created = CashFlowEntryTag.objects.get_or_create(entry=entry, tag=tag)
    return link


def assign_project_to_entry(user, entry_id, project_id) -> CashFlowEntryProject:
    entry = _resolve_owned_entry(user, entry_id)
    project = _resolve_project(project_id)
    link, _created = CashFlowEntryProject.objects.update_or_create(
        entry=entry, defaults={"project": project}
    )
    return link


def _resolve_owned_entry(user, entry_id) -> CashFlowEntry:
    try:
        entry_id_int = int(entry_id)
    except (TypeError, ValueError):
        raise ValueError("Informe o ID do lançamento.") from None
    entry = CashFlowEntry.objects.filter(id=entry_id_int).select_related("account").first()
    if entry is None:
        raise ValueError("Lançamento não encontrado.")
    if not can_access_entry(user, entry):
        raise ValueError("Acesso negado: você não pode vincular este lançamento.")
    return entry


def _resolve_tag(tag_id) -> ManagementTag:
    try:
        tag = ManagementTag.objects.filter(id=int(tag_id)).first()
    except (TypeError, ValueError):
        tag = None
    if tag is None:
        raise ValueError("Tag não encontrada.")
    if not tag.active:
        raise ValueError("Tag arquivada não pode receber novos vínculos.")
    return tag


def _resolve_project(project_id) -> ManagementProject:
    try:
        project = ManagementProject.objects.filter(id=int(project_id)).first()
    except (TypeError, ValueError):
        project = None
    if project is None:
        raise ValueError("Projeto não encontrado.")
    if not project.active:
        raise ValueError("Projeto arquivado não pode receber novos vínculos.")
    return project


# ---------------------------------------------------------------------------
# Orçamento mensal (orçado x realizado)
# ---------------------------------------------------------------------------

def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    return start, add_months(start, 1)


def save_budget(user, *, owner_id, category_id, year, month, planned_amount) -> MonthlyBudget:
    try:
        owner_id_int = int(owner_id)
    except (TypeError, ValueError):
        raise ValueError("Titular é obrigatório.") from None
    try:
        category_id_int = int(category_id)
    except (TypeError, ValueError):
        raise ValueError("Categoria é obrigatória.") from None
    try:
        year_int = int(year)
        month_int = int(month)
    except (TypeError, ValueError):
        raise ValueError("Mês do orçamento é obrigatório.") from None
    if not 1 <= month_int <= 12:
        raise ValueError("Mês inválido.")
    if not 2000 <= year_int <= 2100:
        raise ValueError("Ano inválido.")

    if owner_id_int not in set(accessible_owner_ids(user, "update")):
        raise ValueError("Acesso negado: você não pode gerenciar orçamento deste titular.")

    if not AccountOwner.objects.filter(id=owner_id_int).exists():
        raise ValueError("Titular não encontrado.")
    if not CashFlowCategory.objects.filter(id=category_id_int).exists():
        raise ValueError("Categoria não encontrada.")

    try:
        amount = Decimal(str(planned_amount).strip().replace(",", "."))
    except Exception as exc:  # noqa: BLE001 - entrada de formulário livre
        raise ValueError("Valor orçado inválido.") from exc
    if amount < 0:
        raise ValueError("Valor orçado não pode ser negativo.")

    budget, _created = MonthlyBudget.objects.update_or_create(
        owner_id=owner_id_int,
        category_id=category_id_int,
        year=year_int,
        month=month_int,
        defaults={"planned_amount": amount.quantize(MONEY_QUANT), "active": True},
    )
    return budget


def retire_budget(user, budget_id) -> tuple[MonthlyBudget, str]:
    """Arquiva o orçamento que já tem realizado no período, ou o remove.

    Recebe `user` e confere o escopo por titular, como `save_budget` faz logo
    acima. Sem essa checagem, a permissão funcional `management.manage` bastava
    para remover orçamento de um titular que a pessoa nem enxerga na tela: a
    listagem já sai filtrada por `accessible_owner_ids` (ver `management_view`),
    então o único caminho era o POST direto -- e ele passava.

    A recusa reaproveita a MESMA mensagem do orçamento inexistente, de
    propósito: distinguir os dois casos diria a quem tenta quais ids existem
    sob titulares alheios.
    """
    nao_encontrado = "Orçamento não encontrado."
    try:
        budget = MonthlyBudget.objects.select_related("owner", "category").get(id=int(budget_id))
    except (MonthlyBudget.DoesNotExist, TypeError, ValueError):
        raise ValueError(nao_encontrado) from None

    # A ação é `delete`, não `update`: `UserOwnerAccess` tem uma coluna por
    # verbo, e quem só recebeu escrita não deveria ganhar remoção de brinde.
    # Administrador e super usuário continuam passando, por
    # `_has_broad_owner_access` dentro de `accessible_owner_ids`.
    if budget.owner_id not in set(accessible_owner_ids(user, "delete")):
        raise ValueError(nao_encontrado)

    # Lido ANTES da exclusão: `Model.delete()` do Django zera a chave primária
    # da instância, e depois dele `budget.id` é None -- registrar a auditoria
    # com o id em mãos é o motivo de ela morar aqui, e não na view.
    identificador = budget.id
    anterior = {
        "owner": budget.owner.name,
        "category": budget.category.category_name,
        "year": budget.year,
        "month": budget.month,
        "planned_amount": str(budget.planned_amount),
    }

    start, end_exclusive = month_bounds(budget.year, budget.month)
    has_history = CashFlowEntry.objects.filter(
        account__owner_id=budget.owner_id,
        category_id=budget.category_id,
        status=STATUS_REALIZED,
        realized_date__gte=start,
        realized_date__lt=end_exclusive,
    ).exists()
    if has_history:
        budget.active = False
        budget.save(update_fields=["active", "updated_at"])
        acao = "archived"
    else:
        budget.delete()
        acao = "deleted"

    # Import local, como em `transactions/services.py`: a trilha de auditoria
    # das escritas financeiras é registrada na camada de serviço.
    from core.services import log_audit_event

    log_audit_event("monthly_budget", identificador, acao, old_values=anterior, user=user)
    return budget, acao


def actual_amount_for_budget(owner_id: int, category_id: int, year: int, month: int) -> Decimal:
    """Soma dos lançamentos realizados no mês/categoria/titular (não persistido)."""
    start, end_exclusive = month_bounds(year, month)
    amount_expr = Coalesce("realized_amount", "entry_amount", output_field=_AMOUNT_FIELD)
    total = (
        CashFlowEntry.objects.filter(
            account__owner_id=owner_id,
            category_id=category_id,
            status=STATUS_REALIZED,
            realized_date__gte=start,
            realized_date__lt=end_exclusive,
        )
        .aggregate(
            total=Coalesce(Sum(amount_expr), Value(Decimal("0.00")), output_field=_AMOUNT_FIELD)
        )
    )["total"]
    return to_decimal(total).quantize(MONEY_QUANT)


@dataclass(frozen=True)
class BudgetRow:
    budget: MonthlyBudget
    actual_amount: Decimal
    difference: Decimal


def budget_rows_for_period(owner_ids: Iterable[int], year: int, month: int) -> list[BudgetRow]:
    """Linhas de orçado x realizado para os titulares acessíveis, no mês informado.

    O "realizado" de cada linha é resolvido em lote (uma única query agregada
    por titular/categoria) em vez de uma chamada a `actual_amount_for_budget`
    por orçamento — evitava 1 query extra por linha (N+1) quando há vários
    orçamentos cadastrados no mesmo período.
    """
    ids = list(owner_ids)
    if not ids:
        return []
    budgets = list(
        MonthlyBudget.objects.select_related("owner", "category")
        .filter(owner_id__in=ids, year=year, month=month)
        .order_by("owner__name", "category__category_name")
    )
    if not budgets:
        return []

    start, end_exclusive = month_bounds(year, month)
    amount_expr = Coalesce("realized_amount", "entry_amount", output_field=_AMOUNT_FIELD)
    actual_rows = (
        CashFlowEntry.objects.filter(
            account__owner_id__in=ids,
            category_id__in={budget.category_id for budget in budgets},
            status=STATUS_REALIZED,
            realized_date__gte=start,
            realized_date__lt=end_exclusive,
        )
        .values("account__owner_id", "category_id")
        .annotate(total=Coalesce(Sum(amount_expr), Value(Decimal("0.00")), output_field=_AMOUNT_FIELD))
    )
    actual_by_key = {
        (row["account__owner_id"], row["category_id"]): to_decimal(row["total"]) for row in actual_rows
    }

    rows: list[BudgetRow] = []
    for budget in budgets:
        actual = actual_by_key.get((budget.owner_id, budget.category_id), Decimal("0.00")).quantize(MONEY_QUANT)
        difference = (budget.planned_amount - actual).quantize(MONEY_QUANT)
        rows.append(BudgetRow(budget=budget, actual_amount=actual, difference=difference))
    return rows


# ---------------------------------------------------------------------------
# Movimentos recentes classificados (tags/projeto)
# ---------------------------------------------------------------------------

def recent_classified_entries(account_ids: Iterable[int], limit: int = 50):
    """Lançamentos recentes com tag(s)/projeto vinculados anexados como atributos.

    `CashFlowEntryProject.entry` é `ForeignKey` (não `OneToOneField`, mesmo com
    `UniqueConstraint` garantindo 1:N) — o acessor reverso `project_link` é um
    manager, não select_related-ável. Resolvido via lookup em lote e atributos
    anexados (`linked_project`/`linked_tags`) em vez de tentar select_related
    num campo que o Django trata como to-many.
    """
    ids = [int(a) for a in account_ids if a]
    if not ids:
        return []
    entries = list(
        CashFlowEntry.objects.select_related("account", "account__owner")
        .filter(account_id__in=ids)
        .order_by("-due_date", "-id")[:limit]
    )
    entry_ids = [entry.id for entry in entries]
    if not entry_ids:
        return entries

    project_by_entry_id = {
        link.entry_id: link.project
        for link in CashFlowEntryProject.objects.select_related("project").filter(entry_id__in=entry_ids)
    }
    tags_by_entry_id: dict[int, list[ManagementTag]] = {}
    for link in CashFlowEntryTag.objects.select_related("tag").filter(entry_id__in=entry_ids):
        tags_by_entry_id.setdefault(link.entry_id, []).append(link.tag)

    for entry in entries:
        entry.linked_project = project_by_entry_id.get(entry.id)
        entry.linked_tags = tags_by_entry_id.get(entry.id, [])
    return entries
