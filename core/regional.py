"""Formato regional de apresentação (Brasil ou EUA), por usuário.

Só UX: decide como datas e números aparecem e são digitados na tela. O que o
servidor recebe, grava, exporta ou importa não passa por aqui -- continua ISO
(`AAAA-MM-DD`) e decimal com ponto (`1234.56`), como sempre foi.

O formato do usuário da requisição fica numa `ContextVar` preenchida pelo
`FormatoRegionalMiddleware`. Fora de uma requisição (comando de manutenção,
teste de serviço) vale o padrão Brasil, que é o que o sistema sempre mostrou.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime

from core.domain.settings import REGIONAL_FORMAT_BR, REGIONAL_FORMAT_US, normalize_regional_format

_formato: ContextVar[str] = ContextVar("formato_regional", default=REGIONAL_FORMAT_BR)

# Padrões de data de cada formato, no vocabulário do `strftime`.
_PADRAO_DATA = {REGIONAL_FORMAT_BR: "%d/%m/%Y", REGIONAL_FORMAT_US: "%m/%d/%Y"}
_PADRAO_MES = {REGIONAL_FORMAT_BR: "%m/%Y", REGIONAL_FORMAT_US: "%m/%Y"}
_PADRAO_DIA_MES = {REGIONAL_FORMAT_BR: "%d/%m", REGIONAL_FORMAT_US: "%m/%d"}


def formato_ativo() -> str:
    return _formato.get()


def ativar(formato: str | None):
    """Ativa o formato e devolve o token para `desativar`."""
    return _formato.set(normalize_regional_format(formato))


def desativar(token) -> None:
    _formato.reset(token)


def _eua() -> bool:
    return _formato.get() == REGIONAL_FORMAT_US


def formatar_data(valor: date | datetime | None, *, ausente: str = "") -> str:
    if not valor:
        return ausente
    return valor.strftime(_PADRAO_DATA[_formato.get()])


def formatar_data_hora(valor: datetime | None, *, segundos: bool = False, ausente: str = "") -> str:
    if not valor:
        return ausente
    hora = "%H:%M:%S" if segundos else "%H:%M"
    return valor.strftime(f"{_PADRAO_DATA[_formato.get()]} {hora}")


def formatar_mes(valor: date | datetime | None, *, ausente: str = "") -> str:
    if not valor:
        return ausente
    return valor.strftime(_PADRAO_MES[_formato.get()])


def formatar_dia_mes(valor: date | datetime | None, *, ausente: str = "") -> str:
    if not valor:
        return ausente
    return valor.strftime(_PADRAO_DIA_MES[_formato.get()])


def adaptar_numero(texto: str) -> str:
    """Troca os separadores de um número já formatado no padrão brasileiro.

    `1.234,56` vira `1,234.56` no formato EUA. O marcador `\\x00` evita passar
    duas vezes pelo mesmo caractere (o mesmo cuidado de `sharedauth.formatting`).
    """
    if not _eua():
        return texto
    return texto.replace(".", "\x00").replace(",", ".").replace("\x00", ",")
