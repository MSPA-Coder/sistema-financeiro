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
