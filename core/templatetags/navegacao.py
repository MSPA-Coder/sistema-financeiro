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
que devolvem pedaço, não página, e para os quais o `hx-select` não acharia
nada.
"""

from __future__ import annotations

from django.template import Library
from django.urls import reverse
from django.utils.safestring import mark_safe

register = Library()

#: `hx-select` recorta o CONTEÚDO de `#appMain` da página inteira que a view
#: devolve, então nenhuma view precisa aprender a responder fragmento.
#:
#: O `> *` não é enfeite. `hx-select="#appMain"` recorta o próprio `<main>`, e
#: `hx-swap="innerHTML"` o deposita DENTRO do alvo -- o resultado era
#: `<main id="appMain"><main id="appMain">`, com id duplicado, dois landmarks
#: `<main>`, dois contêineres de rolagem aninhados e o padding aplicado duas
#: vezes. Pior: `document.getElementById('appMain')` passava a devolver o
#: invólucro ANTIGO, e só funcionava por acidente, porque o conteúdo novo
#: estava dentro dele.
#:
#: `hx-swap="outerHTML"` também resolveria o aninhamento, mas troca o
#: contrato: com ele o `htmx:afterSwap` dispara no PAI do alvo, e a guarda
#: `alvo !== principal` de `application.js` deixaria de casar -- `app:contentLoaded`
#: nunca sairia e os três consumidores parariam de reconstruir gráfico e
#: calendário. Recortar os filhos mantém `#appMain` sendo o mesmo elemento
#: antes e depois da troca, que é do que a rolagem, o CSS e aquele evento
#: dependem.
#:
#: `hx-select-oob` traz o `#appPageHeader` da mesma resposta. O sufixo
#: `:innerHTML` é deliberado: trocar o elemento inteiro descartaria o próprio
#: `<header>`, e com ele a âncora que o CSS e o foco usam. A faixa de filtros
#: globais (`#globalFiltersBanner`) vem junto, inteira: desde 09/10/2026 ela
#: mostra também o recorte de titular, instituição e conta, que muda com
#: qualquer filtro da tela (`core/contexto_global.py`).
#:
#: `hx-replace-url` mantém favorito, F5 e link compartilhado válidos e troca a
#: entrada atual do histórico em vez de criar outra: "voltar" sai da tela, e
#: não desfaz filtro por filtro (decisão de 09/10/2026, a mesma do
#: ControleRendaVariavel). A URL que chega à barra é a canônica, sem parâmetro
#: vazio -- quem a limpa é `core.navegacao.UrlCanonicaMiddleware`, no
#: servidor, pelo cabeçalho `HX-Replace-Url`. Até essa data aqui estava
#: `hx-push-url`, mas o cabeçalho da resposta prevalece sobre o atributo e o
#: efeito já era o de substituir; o atributo agora diz o que acontece.
_ATRIBUTOS = (
    'hx-target="#appMain" '
    'hx-swap="innerHTML" '
    'hx-select="#appMain > *" '
    'hx-select-oob="#appPageHeader:innerHTML,#globalFiltersBanner:outerHTML" '
    'hx-replace-url="true" '
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


@register.simple_tag(takes_context=True)
def url_sem(context, nome_url: str, *remover: str) -> str:
    """URL de ``nome_url`` com a query atual, menos os parâmetros ``remover``.

    Para "limpar filtro" que tira só um recorte (a operação, o lançamento
    selecionado) e mantém período, modo, titular, conta, moeda e grupos::

        <a href="{% url_sem 'transactions:transactions_view' 'operation_id' 'entry_id' %}">
    """
    query = context["request"].GET.copy()
    for nome in remover:
        query.pop(nome, None)
    destino = reverse(nome_url)
    texto = query.urlencode()
    return f"{destino}?{texto}" if texto else destino


#: Formulário de escrita que volta para a própria tela (Gerencial, Fechamento,
#: Reclassificação): o POST sai por HTMX (`hx-boost`), o servidor responde com
#: o redirect de sempre -- que já preserva os filtros --, o HTMX segue o
#: redirect e troca só `#appMain`, como a navegação por filtro. A página não
#: recarrega; a URL é acertada pelo `HX-Replace-Url` do middleware na
#: resposta final; as mensagens chegam fora de banda. `hx-disabled-elt`
#: desabilita o botão de envio enquanto a resposta não volta: dois cliques
#: rápidos não viram dois POSTs. Sem JavaScript, o formulário é um POST comum.
#: (Auditoria de filtros e recargas, 09/10/2026.)
_ATRIBUTOS_ESCRITA = (
    'hx-boost="true" '
    + _ATRIBUTOS.replace('hx-replace-url="true" ', "")
    + ' hx-disabled-elt="find button[type=submit]"'
)


@register.simple_tag
def nav_escrita() -> str:
    """Atributos para um `<form method="post">` que volta para a própria tela."""
    return mark_safe(_ATRIBUTOS_ESCRITA)  # noqa: S308 - constante literal, sem entrada
