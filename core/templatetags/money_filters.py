"""Filtros de apresentação monetária compartilhados entre templates.

A formatação de milhar e decimal em pt-BR mora em `sharedauth.formatting`.
Este projeto Django instala somente o núcleo Python do pacote, sem Flask.

`ocultar_zero` é uma escolha desta aplicação: omitir "R$ 0,00" preserva
espaço nas tabelas largas. As classes por sinal ficam aqui porque são nomes
de CSS locais, não formatação compartilhada.
"""

from __future__ import annotations

from decimal import Decimal

from django import template
from sharedauth.formatting import moeda, moeda_com_sinal

register = template.Library()


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return value if isinstance(value, Decimal) else Decimal(str(value))


@register.filter
def neg(value) -> Decimal:
    return -_to_decimal(value)


@register.filter
def money(value) -> str:
    return moeda(_to_decimal(value), ocultar_zero=True)


@register.filter
def money_signed(value) -> str:
    return moeda_com_sinal(_to_decimal(value))


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
