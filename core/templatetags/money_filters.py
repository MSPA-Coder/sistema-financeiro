"""Filtros de apresentação monetária compartilhados entre templates.

A formatação de milhar e decimal em pt-BR mora em `sharedauth.formatting`.
Este projeto Django instala somente o núcleo Python do pacote, sem Flask.

`ocultar_zero` é uma escolha desta aplicação: omitir "R$ 0,00" preserva
espaço nas tabelas largas. As classes por sinal ficam aqui porque são nomes
de CSS locais, não formatação compartilhada.

A moeda é argumento, não configuração: `{{ valor|money:conta.currency }}`. Sem
argumento vale a moeda base, que é o que quase toda tela mostra -- e o que todo
valor sem conta definida (orçamento, por exemplo) é. Uma sigla desconhecida não
inventa símbolo: cai na base, porque a tela não é lugar de descobrir erro de
cadastro. Quem recusa moeda inválida é o banco, pela
`ck_financial_account_currency_valid`.
"""

from __future__ import annotations

from decimal import Decimal

from django import template
from sharedauth.formatting import moeda, moeda_com_sinal

from core.domain.finance import BASE_CURRENCY, CURRENCY_OPTIONS, CURRENCY_SYMBOLS

_CURRENCY_LABELS = dict(CURRENCY_OPTIONS)

register = template.Library()


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _symbol(currency) -> str:
    return CURRENCY_SYMBOLS.get(currency or BASE_CURRENCY, CURRENCY_SYMBOLS[BASE_CURRENCY])


@register.filter
def neg(value) -> Decimal:
    return -_to_decimal(value)


@register.filter
def money(value, currency=None) -> str:
    return moeda(_to_decimal(value), ocultar_zero=True, simbolo=_symbol(currency))


@register.filter
def money_signed(value, currency=None) -> str:
    return moeda_com_sinal(_to_decimal(value), simbolo=_symbol(currency))


@register.filter
def currency_symbol(currency) -> str:
    """O símbolo sozinho, para rótulos de formulário.

    O campo de valor não passa por `money` (o navegador precisa de um
    `<input type="number">`), mas o rótulo dele tem de dizer em que moeda se
    digita -- senão um valor em dólar é oferecido como se fosse em real.
    """
    return _symbol(currency)


@register.filter
def currency_label(currency) -> str:
    """O nome da moeda, para o cabeçalho de um bloco de totais.

    Só aparece quando a tela mostra mais de um bloco: com uma moeda só, o
    cabeçalho seria ruído -- e a tela tem de continuar idêntica ao que era.
    """
    return _CURRENCY_LABELS.get(currency or BASE_CURRENCY, _CURRENCY_LABELS[BASE_CURRENCY])


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
