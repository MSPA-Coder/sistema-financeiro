"""Filtro request-scoped de moeda para telas financeiras.

Na tela é uma seleção múltipla (Real, Dólar), mas na URL continua com um valor
só: marcar as duas é ``ALL``, e marcar uma é ela. Com duas moedas as duas
formas dizem a mesma coisa, e o valor único mantém o contrato que as telas, os
favoritos e os links já usam. ``BRL,USD`` também é aceito, como sinônimo de
``ALL``.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.core.exceptions import SuspiciousOperation
from django.db.models import Q

from core.domain.finance import CURRENCY_OPTIONS, VALID_CURRENCIES

BRL = "BRL"
USD = "USD"
ALL = "ALL"
ALL_CURRENCIES = ALL
DEFAULT_CURRENCY = BRL
VALID_FILTERS = frozenset({*VALID_CURRENCIES, ALL})
VALID_CURRENCY_FILTERS = VALID_FILTERS
CURRENCY_PARAM = "currency"


def parse_currency_filter(params: Mapping[str, str]) -> str:
    """Normaliza ``currency``; entrada inválida resulta em resposta HTTP 400."""
    raw = params.get(CURRENCY_PARAM)
    if raw is None:
        return DEFAULT_CURRENCY
    parts = frozenset(part.strip().upper() for part in raw.split(",") if part.strip())
    if ALL in parts or parts == frozenset(VALID_CURRENCIES):
        return ALL
    if len(parts) != 1 or not parts <= VALID_FILTERS:
        raise SuspiciousOperation("Filtro de moeda inválido.")
    return next(iter(parts))


def selected_currency(params: Mapping[str, str]) -> str:
    """Alias de compatibilidade para callers existentes."""
    return parse_currency_filter(params)


def currency_matches(selected: str, currency: str) -> bool:
    return selected in (ALL, currency)


def selected_currencies(selected: str) -> frozenset[str]:
    """As moedas marcadas no filtro."""
    return frozenset(VALID_CURRENCIES) if selected == ALL else frozenset({selected})


def currency_q(selected: str, prefix: str = "") -> Q:
    """`Q` das contas na moeda do filtro; ``ALL`` rende ``Q()``."""
    if selected == ALL:
        return Q()
    return Q(**{f"{prefix}currency": selected})


def currency_filter_options(selected: str) -> list[dict]:
    """As caixas de moeda do menu de filtros."""
    marcadas = selected_currencies(selected)
    return [
        {"code": code, "label": label, "selected": code in marcadas}
        for code, label in CURRENCY_OPTIONS
    ]
