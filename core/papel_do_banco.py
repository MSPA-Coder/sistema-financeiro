"""Recusa atender como superusuário do PostgreSQL.

A aplicação conecta com um papel próprio, só com DML (ver
`scripts/provision-db-runtime.sh`); o superusuário fica no banco e no
`migrate`. Até 09/2026 a garantia era recusar o NOME `postgres` no settings,
e ela não garantia nada: o `POSTGRES_USER` da imagem oficial nasce
superusuário qualquer que seja o nome, e a produção rodou assim sem que a
checagem acusasse. O que importa é o atributo, não o nome -- por isso a
pergunta vai ao próprio servidor, na primeira consulta de cada conexão.

Só vale onde `DB_EXIGIR_PAPEL_RESTRITO=1`: o `migrate` e a suíte usam o papel
administrativo de propósito.
"""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

VARIAVEL = "DB_EXIGIR_PAPEL_RESTRITO"


def exigido() -> bool:
    return os.environ.get(VARIAVEL, "").strip() == "1"


def conferir_papel(sender, connection, **kwargs) -> None:
    """Receptor de `connection_created`: superusuário derruba a conexão."""
    if not exigido() or connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SHOW is_superuser")
        superusuario = cursor.fetchone()[0] == "on"
    if superusuario:
        connection.close()
        raise ImproperlyConfigured(
            f"O papel '{connection.settings_dict['USER']}' é superusuário do "
            "PostgreSQL. A aplicação deve conectar com o papel restrito "
            "provisionado por scripts/provision-db-runtime.sh."
        )
