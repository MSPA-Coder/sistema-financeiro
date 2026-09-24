import pytest
from django.core.exceptions import SuspiciousOperation

from core.currency_filter import ALL, BRL, USD, selected_currency


def test_currency_filter_defaults_to_brl():
    assert selected_currency({}) == BRL


def test_currency_filter_accepts_all_supported_values():
    assert selected_currency({"currency": "BRL"}) == BRL
    assert selected_currency({"currency": "usd"}) == USD
    assert selected_currency({"currency": " all "}) == ALL


def test_currency_filter_rejects_invalid_values_to_safe_default():
    with pytest.raises(SuspiciousOperation):
        selected_currency({"currency": "EUR"})


def test_as_duas_moedas_marcadas_sao_todas():
    """A seleção múltipla do menu vira o mesmo `ALL` que as telas já entendem."""
    assert selected_currency({"currency": "BRL,USD"}) == ALL
    assert selected_currency({"currency": "usd, brl"}) == ALL
    assert selected_currency({"currency": "USD,"}) == USD


def test_lista_com_moeda_invalida_responde_400():
    with pytest.raises(SuspiciousOperation):
        selected_currency({"currency": "BRL,EUR"})
