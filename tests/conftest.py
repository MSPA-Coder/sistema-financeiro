"""Fixtures da suite minima.

A suite nao toca o banco. Isso e desenho, nao limitacao: as coisas que ela
protege -- cabecalhos, negacao por padrao, CSRF, autorizacao e integridade das
migracoes -- sao decididas antes de qualquer consulta, e mante-la sem banco e o
que faz caber no orcamento de 30 segundos sem infraestrutura de teste.

O bootstrap do schema em PostgreSQL vazio continua sendo verificacao manual
obrigatoria para mudanca de schema, como a base e o TESTING.md registram.
"""

from __future__ import annotations

import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def client_com_csrf() -> Client:
    # O cliente de teste do Django dispensa CSRF por padrao; religar e o que
    # torna o teste de CSRF nao decorativo.
    return Client(enforce_csrf_checks=True)


@pytest.fixture
def banco_sondavel():
    """Faz a sonda de `/health` passar, sem banco de verdade.

    Desde 2026-08-21 `/health` consulta o banco -- antes devolvia `"ok"` fixo
    e reportava saude com o PostgreSQL fora. A suite continua sem banco (ver
    a docstring deste modulo), entao quem usa essa rota como alvo precisa
    dizer qual dos dois desfechos esta exercitando.
    """
    from unittest import mock

    with mock.patch("financeiro.urls.connection") as conexao:
        conexao.cursor.return_value.__enter__.return_value.execute.return_value = None
        yield conexao


@pytest.fixture
def banco_fora():
    """Faz a sonda de `/health` falhar, para exercitar o ramo do 503."""
    from unittest import mock

    with mock.patch("financeiro.urls.connection") as conexao:
        conexao.cursor.side_effect = RuntimeError("banco inalcancavel")
        yield conexao
