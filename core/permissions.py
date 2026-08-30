"""Decorator de permissao funcional com resposta amigavel para acesso negado.

`django.contrib.auth.decorators.permission_required(..., raise_exception=True)`
lanca `PermissionDenied` e entrega a pagina 403 crua e sem estilo do Django,
fora do layout da aplicacao. Este decorator resolve a permissao pelo
`AppPermissionBackend` e, quando nega, exibe uma mensagem e redireciona,
mantendo o usuario dentro da interface.

A recusa continua sendo do servidor: o redirect e apresentacao, nao o controle.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect
from django_htmx.http import HttpResponseClientRedirect


def permission_required(perm: str, fallback: str = "core:inicio") -> Callable:
    """Bloqueia a view quando o usuario ativo nao possui a permissao funcional.

    Espera vir depois de `@login_required` na pilha de decorators (nao trata
    usuario anonimo). Em requisicoes HTMX, redireciona via `HX-Redirect` para
    que o client-side siga a navegacao mesmo dentro de um swap parcial.

    O `fallback` padrao e `core:inicio`, que resolve o destino a partir do que
    a pessoa pode ver. Era `dashboard:dashboard`, o que so funcionava enquanto
    o dashboard nao exigia permissao: assim que passou a exigir, negar o
    dashboard mandaria a pessoa de volta para o dashboard.
    """

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.has_perm(perm):
                messages.warning(
                    request, "Acesso negado: você não tem permissão para esta funcionalidade."
                )
                fallback_url = redirect(fallback).url
                if getattr(request, "htmx", False):
                    return HttpResponseClientRedirect(fallback_url)
                return redirect(fallback)
            return view_func(request, *args, **kwargs)

        # A permissao exigida fica legivel de fora. Sem isto, "esta rota checa
        # permissao?" so se responde lendo o decorator no fonte -- e foi
        # exatamente assim que quatro telas ficaram com chave no catalogo e
        # nenhuma verificacao, por tempo indeterminado. `tests/
        # test_permissoes_por_rota.py` varre a URLconf lendo este atributo.
        wrapper.permissao_exigida = perm

        return wrapper

    return decorator
