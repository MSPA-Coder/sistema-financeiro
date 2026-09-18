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
