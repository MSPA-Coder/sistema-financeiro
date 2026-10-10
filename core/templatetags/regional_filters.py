"""Filtros de data e número no formato regional do usuário (Brasil ou EUA).

Substituem os `|date:"d/m/Y"` espalhados pelos templates. Só os textos que o
usuário LÊ passam por aqui; valores de `<input>` e atributos de máquina seguem
em ISO (`|date:'Y-m-d'`), porque é o que o servidor espera receber.
"""

from __future__ import annotations

from django import template

from core import regional

register = template.Library()


@register.filter
def udate(value) -> str:
    """31/12/2026 no Brasil, 12/31/2026 nos EUA. Vazio se não há data."""
    return regional.formatar_data(value)


@register.filter
def udatetime(value) -> str:
    """Data e hora até o minuto no formato do usuário."""
    return regional.formatar_data_hora(value)


@register.filter
def udatetime_s(value) -> str:
    """Data e hora até o segundo no formato do usuário."""
    return regional.formatar_data_hora(value, segundos=True)


@register.filter
def umonth(value) -> str:
    """12/2026 nos dois formatos (mês antes do ano em ambos)."""
    return regional.formatar_mes(value)
