"""Filtros de apresentação monetária compartilhados entre templates."""

from __future__ import annotations

from decimal import Decimal

from django import template

register = template.Library()


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _format_brl(amount: Decimal) -> str:
    us_fmt = f"{amount:,.2f}"
    return us_fmt.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


@register.filter
def neg(value) -> Decimal:
    return -_to_decimal(value)


@register.filter
def money(value) -> str:
    amount = _to_decimal(value)
    if amount == 0:
        return ""
    return f"R$ {_format_brl(amount)}"


@register.filter
def money_signed(value) -> str:
    amount = _to_decimal(value)
    if amount > 0:
        return f"+ R$ {_format_brl(amount)}"
    if amount < 0:
        return f"- R$ {_format_brl(abs(amount))}"
    return ""


@register.filter
def amount_class(value) -> str:
    amount = _to_decimal(value)
    if amount > 0:
        return "amount-positive"
    if amount < 0:
        return "amount-negative"
    return "amount-neutral"


@register.filter
def card_class(value) -> str:
    amount = _to_decimal(value)
    if amount > 0:
        return "card-positive"
    if amount < 0:
        return "card-negative"
    return "card-neutral"
