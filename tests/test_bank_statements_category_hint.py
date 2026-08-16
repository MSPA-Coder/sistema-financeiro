"""Teste do parser de prefixo de categoria usado para sugerir categoria ao
criar um lançamento a partir de uma linha de extrato (Bancos > Conciliação).

Só testa a função pura de extração de texto (`_category_hint`) - o resto de
`suggested_category_for_line` consulta `CashFlowCategory` no banco, fora do
escopo desta suíte (ver `tests/conftest.py`).
"""
from __future__ import annotations

from bank_statements.reconciliation import _category_hint


def test_category_hint_extracts_prefix_before_dash():
    assert _category_hint("Corretagem - Corretagem Executor - Btc") == "Corretagem"


def test_category_hint_extracts_multi_word_prefix():
    assert _category_hint("Impostos e Tributos - Irrf S/Rendimentos") == "Impostos e Tributos"


def test_category_hint_returns_none_without_dash_separator():
    assert _category_hint("Pix recebido de fulano") is None


def test_category_hint_returns_none_for_empty_description():
    assert _category_hint("") is None
