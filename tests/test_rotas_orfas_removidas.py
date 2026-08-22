"""Rota que muda estado sem tela que a acione nao volta sem querer.

`cancel_entry`, `close_month` e `reopen_month` nao fazem parte das URLs de
`transactions`; uma rota POST autenticada sem tela correspondente ampliaria a
superficie mutante sem ampliar o contrato da interface.

Fechar e reabrir mes continuam existindo pelos caminhos de `core:`, e o teste
exige isso como controle positivo.
"""

from __future__ import annotations

import pytest
from django.urls import NoReverseMatch, reverse

REMOVIDAS = [
    "transactions:cancel_entry",
    "transactions:close_month",
    "transactions:reopen_month",
]

# Os caminhos vivos recebem os dados pelo POST e resolvem sem argumentos. As
# assinaturas abaixo tambem garantem que os nomes ausentes nao resolvam com os
# antigos formatos posicionais.
MANTIDAS = [
    "core:settings_close_month",
    "core:settings_reopen_month",
    "core:settings_monthly_close",
]


@pytest.mark.parametrize("nome", REMOVIDAS)
def test_rota_orfa_nao_resolve(nome):
    with pytest.raises(NoReverseMatch):
        reverse(nome, args=[1, 2026, 1])

    with pytest.raises(NoReverseMatch):
        reverse(nome, args=[1])


@pytest.mark.parametrize("nome", MANTIDAS)
def test_caminho_vivo_de_fechamento_continua_existindo(nome):
    assert reverse(nome)
