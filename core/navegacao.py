"""O endereço que chega à barra depois de uma troca de tela por filtro.

Um formulário HTML serializa **todos** os seus campos quando é enviado,
inclusive os que estão vazios. A tela de Lançamentos sem nenhum filtro
aplicado chegaria à barra como::

    /lancamentos/?owner_id=&institution_id=&account_id=&filter_type=

Nada ali foi escolhido por ninguém.

Enquanto a navegação era feita à mão em `application.js`, quem removia esses
campos era o `_buildFormUrl`, montando a URL antes do `fetch`. Com HTMX o
formulário é serializado pelo próprio navegador, e a limpeza passa a ser
responsabilidade do servidor: este middleware devolve o endereço equivalente
sem ruído no cabeçalho ``HX-Replace-Url``, e o HTMX troca a barra sem
recarregar nada.

**O filtro continua na URL quando é um filtro de verdade.** Só sai o que está
vazio e o que é estado de interface — `?owner_id=3` aparece exatamente quando
alguém escolheu o titular 3, e o endereço continua servindo para F5, favorito
e link compartilhado.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils.http import urlencode

#: Parâmetros que descrevem como a tela está desenhada, não quais dados ela
#: mostra. Viajam na requisição porque o servidor precisa deles para renderizar,
#: mas não têm o que fazer num link que alguém vá guardar ou enviar.
#:
#: `show_descriptions` NÃO entra aqui de propósito: ele muda o que o relatório
#: exibe, não como. Retirá-lo faria um link compartilhado mostrar conteúdo
#: diferente do que quem o enviou estava vendo.
ESTADO_DE_INTERFACE = frozenset({"filters_open"})


class UrlCanonicaMiddleware:
    """Limpa a barra de endereços das telas trocadas por HTMX.

    Só age em GET que responde 200 a uma requisição HTMX, e só quando há algo
    a remover — sem isso o cabeçalho não é enviado, e o `hx-push-url` do
    elemento continua mandando sozinho.

    **Parâmetro desconhecido é preservado, não descartado.** A escolha é
    deliberada e igual à do ControleRendaVariavel: um filtro novo que alguém
    acrescente sem lembrar deste arquivo continua funcionando na barra, e no
    máximo aparece com valor vazio. O caminho oposto — manter só uma lista
    branca — faria esse mesmo filtro sumir do endereço em silêncio, quebrando
    favorito e link sem nada apontar para a causa.

    Entre uma falha visível e uma silenciosa, este middleware escolhe a
    visível.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if request.method != "GET" or response.status_code != 200:
            return response
        if not getattr(request, "htmx", False):
            return response
        # Uma view que já decidiu o próprio endereço continua mandando; um
        # redirecionamento faz navegação completa e não lê este cabeçalho.
        if response.has_header("HX-Replace-Url") or response.has_header("HX-Push-Url"):
            return response
        if response.has_header("HX-Redirect"):
            return response

        destino = url_canonica(request)
        if destino is not None:
            response["HX-Replace-Url"] = destino
        return response


def url_canonica(request: HttpRequest) -> str | None:
    """Endereço desta requisição sem ruído, ou ``None`` se não há o que tirar.

    Percorre `lists()`, não `items()`: um `QueryDict` guarda várias ocorrências
    da mesma chave, e `items()` devolve só a última. Com `items()`, um filtro
    de seleção múltipla perderia todas as escolhas menos uma — em silêncio.
    """
    originais = 0
    mantidos: list[tuple[str, str]] = []
    for chave, valores in request.GET.lists():
        originais += len(valores)
        if chave in ESTADO_DE_INTERFACE:
            continue
        mantidos.extend((chave, valor) for valor in valores if valor != "")

    if len(mantidos) == originais:
        return None

    consulta = urlencode(mantidos)
    return f"{request.path}?{consulta}" if consulta else request.path
