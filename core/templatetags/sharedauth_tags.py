"""Ponte entre `django.contrib.messages` e o componente comum de avisos.

O JS de `sharedauth.ui` le um `<div data-sa-avisos='[...]'>` e transforma cada
item num toast (ver o cabecalho de `sharedauth-ui.js`). Django nao tem filtro
pronto pra isso: `json_script` gera uma tag `<script>`, nao o conteudo de um
atributo, e e isso que `data-sa-avisos` espera.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from django.contrib.messages.storage.base import Message
from django.template import Library
from django.utils.safestring import mark_safe
from sharedauth.ui import SEVERIDADES, svg_icone

register = Library()


@register.simple_tag
def sharedauth_icon(severidade: str) -> str:
    """SVG do icone da severidade -- mesmo tracado do modal/toast do sharedauth.

    Usado nos banners que continuam na pagina (ver `avisos_json` abaixo pra
    saber por que a maioria das mensagens NAO usa mais isto e virou toast).
    """
    return mark_safe(svg_icone(severidade))


def _severidade_da_mensagem(message: Message) -> str:
    """Mapeia o nivel do Django (`level_tag`) para uma das 4 severidades.

    `level_tag` e o nome do nivel sozinho -- diferente de `.tags`, que mistura
    `extra_tags`. Os nomes (`success/error/warning/info`) ja coincidem com o
    vocabulario do sharedauth, sem tabela de traducao. Nivel fora dessas
    quatro (ex.: `debug`) cai em "info", a mesma tolerancia de `svg_icone()`.
    """
    tag = message.level_tag
    return tag if tag in SEVERIDADES else "info"


@register.filter
def avisos_json(messages: Iterable[Message]) -> str:
    """Serializa `messages` no formato que `data-sa-avisos` espera.

    Retorna string PURA, sem `mark_safe`: o autoescape do Django (ligado por
    padrao) converte `"`/`'`/`<`/`>`/`&` em entidades ao interpolar esta string
    dentro de `data-sa-avisos='...'`. O navegador desfaz essas entidades ao ler
    o atributo (`getAttribute`), entao o JS recebe o JSON original intacto --
    uma mensagem com aspas, `<` ou `&` no texto nao quebra o HTML nem o parser
    de JSON do lado de la. Marcar como `safe` aqui seria o erro classico de
    injetar JSON cru num atributo HTML.
    """
    itens = [
        {"mensagem": str(message), "severidade": _severidade_da_mensagem(message)}
        for message in messages
    ]
    return json.dumps(itens, ensure_ascii=False)
