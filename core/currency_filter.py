"""Filtro request-scoped de moeda para telas financeiras."""

from __future__ import annotations

from collections.abc import Mapping

from django.core.exceptions import SuspiciousOperation

BRL = "BRL"
USD = "USD"
ALL = "ALL"
ALL_CURRENCIES = ALL
DEFAULT_CURRENCY = BRL
VALID_FILTERS = frozenset({BRL, USD, ALL})
VALID_CURRENCY_FILTERS = VALID_FILTERS


def parse_currency_filter(params: Mapping[str, str]) -> str:
    """Normaliza ``currency``; entrada inválida resulta em resposta HTTP 400."""
    raw = params.get("currency")
    value = DEFAULT_CURRENCY if raw is None else raw.strip().upper()
    if value not in VALID_FILTERS:
        raise SuspiciousOperation("Filtro de moeda inválido.")
    return value


def selected_currency(params: Mapping[str, str]) -> str:
    """Alias de compatibilidade para callers existentes."""
    return parse_currency_filter(params)


def currency_matches(selected: str, currency: str) -> bool:
    return selected in (ALL, currency)
