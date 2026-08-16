"""Filtro utilitário para lookup de dict por chave dinâmica em templates."""
from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if mapping else None


@register.filter
def in_set(value, container):
    return value in container if container else False
