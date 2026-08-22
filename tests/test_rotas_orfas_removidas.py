"""Rota que muda estado sem tela que a acione nao volta sem querer.

Tres rotas POST de `transactions` foram removidas em 2026-08-22 --
`cancel_entry`, `close_month` e `reopen_month`. Mudavam estado e nenhum
template as acionava: eram alcancaveis so por requisicao direta.

O teste e barato e o defeito que ele previne e caro. Uma rota reintroduzida
"para uso interno" e exatamente assim que a superficie cresce sem ninguem
decidir aumenta-la, e o `test_auth_required` desta suite nao pegaria: ele
confere que rota GET nega acesso anonimo, e o que saiu aqui e POST autenticado
-- rota que o gate aprova e que tela nenhuma oferece.

Fechar e reabrir mes continuam existindo pelos caminhos de `core:`, e o teste
exige isso: sem essa metade, ele passaria tambem se a funcionalidade tivesse
sido perdida junto.
"""

from __future__ import annotations

import pytest
from django.urls import NoReverseMatch, reverse

REMOVIDAS = [
    "transactions:cancel_entry",
    "transactions:close_month",
    "transactions:reopen_month",
]

# `close_month` e `reopen_month` da versao removida recebiam conta, ano e mes
# pela URL; os que ficaram recebem tudo pelo corpo do POST. Por isso os vivos
# resolvem sem argumento e os mortos nao resolveriam nem com eles.
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
