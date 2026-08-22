"""Vocabulário e normalizadores do domínio financeiro."""
from __future__ import annotations

from typing import Final

ENTRY_TYPE_INCOME: Final = "receita"
ENTRY_TYPE_EXPENSE: Final = "despesa"
VALID_ENTRY_TYPES: Final = (
    ENTRY_TYPE_INCOME,
    ENTRY_TYPE_EXPENSE,
)

STATUS_PROJECTED: Final = "a_vencer"
STATUS_PENDING: Final = "vencidos"
STATUS_REALIZED: Final = "realizado"
# Este conjunto explicito e compartilhado por modelos, views e filtros.
VALID_STATUSES: Final = (
    STATUS_PROJECTED,
    STATUS_PENDING,
    STATUS_REALIZED,
)
STATUS_OPTIONS: Final = (
    (STATUS_PROJECTED, "A vencer"),
    (STATUS_PENDING, "Vencidos"),
    (STATUS_REALIZED, "Realizado"),
)
# Os filtros oferecem explicitamente todas as opcoes validas. Evite fatias:
# uma nova ordenacao poderia retirar silenciosamente um status valido.
STATUS_FILTER_OPTIONS: Final = STATUS_OPTIONS

OPERATION_SINGLE: Final = "single"
OPERATION_INSTALLMENT: Final = "installment"
OPERATION_RECURRING: Final = "recurring"
OPERATION_INTERNAL_TRANSFER: Final = "internal_transfer"
COMPOSITE_OPERATION_TYPES: Final = {
    OPERATION_INSTALLMENT,
    OPERATION_RECURRING,
    OPERATION_INTERNAL_TRANSFER,
}

OPERATION_SCOPE_ALL: Final = "all"
OPERATION_SCOPE_SINGLE: Final = "single"
OPERATION_SCOPE_CURRENT_FUTURE: Final = "current_future"
VALID_OPERATION_SCOPES: Final = (
    OPERATION_SCOPE_ALL,
    OPERATION_SCOPE_SINGLE,
    OPERATION_SCOPE_CURRENT_FUTURE,
)

CALC_REPEAT: Final = "repeat"
CALC_DIVIDE: Final = "divide"

MAX_TRANSACTION_INSTALLMENTS: Final = 48
MAX_TRANSACTION_DESCRIPTION_LENGTH: Final = 255
MAX_PROJECTION_RANGE_MONTHS: Final = 120

VIEW_PROJECTED: Final = STATUS_PROJECTED
VIEW_PENDING: Final = STATUS_PENDING
VIEW_REALIZED: Final = STATUS_REALIZED
VIEW_ALL: Final = "todos"
VIEW_MODE_OPTIONS: Final = (
    (VIEW_ALL, "Todos os modos"),
    (VIEW_PROJECTED, "A vencer"),
    (VIEW_PENDING, "Vencidos"),
    (VIEW_REALIZED, "Realizado"),
)
# O seletor de modo segue integralmente o vocabulario valido acima.
VIEW_FILTER_MODE_OPTIONS: Final = VIEW_MODE_OPTIONS
VALID_VIEW_MODES: Final = {mode for mode, _label in VIEW_MODE_OPTIONS}


def normalize_view_mode(value: str | None, default: str = VIEW_PROJECTED) -> str:
    return value if value in VALID_VIEW_MODES else default
