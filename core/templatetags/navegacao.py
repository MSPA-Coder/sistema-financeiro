"""O contrato de navegação por filtro, num lugar só.

Filtrar uma tela deste sistema troca duas regiões: `#appMain`, com o conteúdo,
e `#appPageHeader`, com os próprios seletores — o cabeçalho fica **fora** do
`main`, e opções dependentes (conta de um titular, por exemplo) mudam junto com
o resultado.

Antes isso era feito por `static/js/core/application.js`, que buscava a página
inteira com `fetch`, recortava as duas regiões com `DOMParser`, empurrava a URL
no histórico, reexecutava scripts e reinstalava listeners. O HTMX já faz tudo
isso — `hx-select` recorta, `hx-select-oob` cuida da segunda região, e o
conteúdo trocado é processado sozinho, sem reinstalar nada.

Esta tag existe para que o contrato seja **um** e apareça inteiro no ponto de
uso, em vez de cinco atributos repetidos em quinze templates. Herança por
ancestral (pôr os atributos em `#appMain`) seria mais curta e pior: alcançaria
também os fragmentos HTMX que já existem — conciliação, anexos, importações —,
que devolvem pedaço, não página, e para os quais `hx-select="#appMain"` não
acharia nada.
"""

from __future__ import annotations

from django.template import Library
from django.utils.safestring import mark_safe

register = Library()

#: `hx-select` recorta `#appMain` da página inteira que a view devolve, então
#: nenhuma view precisa aprender a responder fragmento.
#:
#: `hx-select-oob` traz o `#appPageHeader` da mesma resposta. O sufixo
#: `:innerHTML` é deliberado: trocar o elemento inteiro descartaria o próprio
#: `<header>`, e com ele a âncora que o CSS e o foco usam.
#:
#: `hx-push-url` mantém favorito, F5 e link compartilhado válidos. A URL que
#: chega à barra é a canônica, sem parâmetro vazio — quem a limpa é
#: `core.navegacao.UrlCanonicaMiddleware`, no servidor, pelo cabeçalho
#: `HX-Replace-Url`.
_ATRIBUTOS = (
    'hx-target="#appMain" '
    'hx-swap="innerHTML" '
    'hx-select="#appMain" '
    'hx-select-oob="#appPageHeader:innerHTML" '
    'hx-push-url="true" '
    'hx-indicator="#ajaxLoadingBar"'
)


@register.simple_tag
def nav_filtro() -> str:
    """Atributos que fazem um link ou formulário trocar a tela por filtro.

    Use junto de um `hx-get` (link) ou num `<form method="get">` com
    `hx-trigger="change"`. Exemplo::

        <form method="get" hx-get="{% url 'x' %}" hx-trigger="change" {% nav_filtro %}>
    """
    return mark_safe(_ATRIBUTOS)  # noqa: S308 - constante literal, sem entrada
