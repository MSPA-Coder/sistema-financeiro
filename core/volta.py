"""Volta de uma escrita para a tela de origem, com os filtros que ela mostrava.

Uma escrita (criar, editar, excluir, realizar...) é um POST para um endereço
próprio; depois dela a pessoa precisa voltar para a lista **como estava**.
Até outubro/2026 cada view montava essa volta com a query congelada no
`action` do formulário, isto é, a da carga da página. Bastava trocar um
filtro por HTMX antes de operar para a volta trazer o filtro velho, e as
views que não montavam nada voltavam para a tela zerada.

A query de origem sai daqui, nesta ordem:

1. o campo ``volta`` do formulário, que `application.js` preenche no envio
   com a query **atual** da barra de endereço;
2. o cabeçalho ``HX-Current-URL`` que o HTMX manda em toda requisição, do
   mesmo host;
3. a query do próprio request (o `action` do formulário), para quem envia
   sem JavaScript.

Só a *query* é aproveitada: o caminho de destino é sempre resolvido pelo
servidor, então nada aqui permite redirecionar para outro lugar.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect
from django.urls import reverse

from core.htmx import quer_fragmento

# Filtros globais: valem para o sistema inteiro e voltam sempre, mesmo quando
# a tela de destino restringe os seus próprios parâmetros.
PARAMETROS_GLOBAIS = ("currency", "grupos")


def query_de_origem(request: HttpRequest) -> QueryDict:
    """Query da tela de onde a escrita partiu (cópia mutável)."""
    bruto = request.POST.get("volta") if request.method == "POST" else None
    if bruto is None:
        atual = request.headers.get("HX-Current-URL")
        if atual:
            partes = urlsplit(atual)
            if partes.netloc in ("", request.get_host()):
                bruto = partes.query
    if bruto is None:
        return request.GET.copy()
    return QueryDict(bruto.lstrip("?"), mutable=True)


def url_de_volta(
    request: HttpRequest,
    destino: str,
    *,
    manter: Iterable[str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> str:
    """``destino`` com a query de origem.

    ``manter`` restringe aos parâmetros que a tela de destino entende (os
    globais passam sempre); ``None`` mantém todos. ``extra`` acrescenta ou
    substitui valores; valor vazio remove o parâmetro.
    """
    origem = query_de_origem(request)
    permitidos = None if manter is None else set(manter) | set(PARAMETROS_GLOBAIS)
    final = QueryDict(mutable=True)
    for chave in origem:
        if chave == "volta" or (permitidos is not None and chave not in permitidos):
            continue
        valores = [v for v in origem.getlist(chave) if v != ""]
        if valores:
            final.setlist(chave, valores)
    for chave, valor in (extra or {}).items():
        if valor:
            final[chave] = valor
        else:
            final.pop(chave, None)
    query = final.urlencode()
    return f"{destino}?{query}" if query else destino


def resposta_de_cadastro(request: HttpRequest, nome_url: str, *, sucesso: bool = True) -> HttpResponse:
    """Fim de uma escrita numa tabela cadastral (contas, bancos, categorias, titulares).

    No HTMX a tela fica: 204 e, se deu certo, `tabelaAtualizar` (o corpo da
    tabela se recarrega com o filtro vivo) e `cadastroGravado` (o formulário
    de inclusão se limpa). Se deu errado, nada se recarrega -- a linha em
    edição continua com o que a pessoa digitou, e a mensagem chega fora de
    banda (`core.htmx`). Sem HTMX, volta para a tabela com os filtros.
    """
    if quer_fragmento(request):
        response = HttpResponse(status=204)
        if sucesso:
            response["HX-Trigger"] = json.dumps({"tabelaAtualizar": True, "cadastroGravado": True})
        return response
    return redirect(url_de_volta(request, reverse(nome_url)))


def voltar_para(request: HttpRequest, nome_url: str, **kwargs) -> HttpResponse:
    """Resposta de uma escrita que volta para a tela ``nome_url`` filtrada.

    No HTMX, ``HX-Redirect`` (navegação completa, que leva as mensagens);
    sem HTMX, redirect comum. Nos dois casos com a query de origem.
    """
    url = url_de_volta(request, reverse(nome_url), **kwargs)
    if quer_fragmento(request):
        response = HttpResponse(status=200)
        response["HX-Redirect"] = url
        return response
    return redirect(url)
