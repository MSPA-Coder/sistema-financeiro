"""Memo de leituras que valem por uma requisição inteira.

Uma tela pede a data inicial do sistema uma dúzia de vezes: cada serviço que
recorta um período a consulta por conta própria, e passar o valor de mão em mão
espalharia um parâmetro por meio projeto. O memo guarda a primeira leitura e a
devolve às seguintes.

O escopo é a requisição, de propósito. Um cache de processo deixaria cada
worker do Gunicorn com a sua cópia, e uma mudança feita em um deles continuaria
invisível nos outros. Fora de uma requisição -- comandos, shell, testes que
chamam serviços direto -- não há memo aberto e toda leitura vai ao banco, como
antes.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

_memo: ContextVar[dict[str, object] | None] = ContextVar("memo_requisicao", default=None)


def lembrar[T](chave: str, carregar: Callable[[], T]) -> T:
    """Devolve o valor já lido nesta requisição, ou lê e guarda."""
    memo = _memo.get()
    if memo is None:
        return carregar()
    if chave not in memo:
        memo[chave] = carregar()
    return memo[chave]  # type: ignore[return-value]


def esquecer(chave: str) -> None:
    """Descarta o valor guardado; quem grava chama depois de gravar."""
    memo = _memo.get()
    if memo is not None:
        memo.pop(chave, None)


class MemoRequisicaoMiddleware:
    """Abre um memo vazio para cada requisição e o fecha ao terminar."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _memo.set({})
        try:
            return self.get_response(request)
        finally:
            _memo.reset(token)
