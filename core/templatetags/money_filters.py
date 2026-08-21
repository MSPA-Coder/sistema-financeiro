"""Filtros de apresentação monetária compartilhados entre templates.

A conta de milhar e decimal em pt-BR mora em `sharedauth.formatting`. Ela era
idêntica, caractere por caractere, à do ControleRendaVariavel — as duas foram
escritas separadamente e coincidiram até no truque de usar `\\x00` como
marcador para trocar os separadores sem passar duas vezes pelo mesmo
caractere. Este projeto é Django e instala só o núcleo do pacote, que é
Python puro e não arrasta Flask.

`ocultar_zero` continua sendo escolha desta aplicação, agora explícita no
ponto da chamada: uma tabela de lançamentos larga cheia de "R$ 0,00" só gasta
largura. As classes por sinal ficam aqui — são nomes de CSS deste projeto,
não formatação.
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
