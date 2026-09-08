"""Fixtures da suite.

A suite tem DUAS CAMADAS, e a distincao importa na hora de escrever teste
novo.

A CAMADA SEM BANCO e a maioria dos arquivos, e continua sendo desenho e nao
limitacao: cabecalhos, negacao por padrao, CSRF, autorizacao e integridade do
grafo de migracoes sao decididos antes de qualquer consulta. Sem banco, essa
parte roda em segundos e nao precisa de infraestrutura. As fixtures
`banco_sondavel` e `banco_fora` abaixo servem a ela, dublando a conexao que
`/health` consulta.

A CAMADA COM BANCO e o que a fase F1 do LEVANTAMENTO_2026-09.md acrescentou:
`test_invariantes_persistidos.py` e `test_migracoes_aplicadas.py`, marcados com
`pytest.mark.django_db`. Ela existe porque tres garantias que o AGENTS.md
declara eram, por construcao, impossiveis de verificar sem PostgreSQL:

- que `@transaction.atomic` desfaz de verdade -- o teste que existia chamava
  `close_month.__wrapped__`, que desembrulha o decorador;
- que as `CheckConstraint` e a `UniqueConstraint` chegaram ao banco e recusam
  o que prometem recusar;
- que uma operacao composta que falha no meio nao deixa metade gravada.

O banco dessa camada e o servico `postgres-teste` do Compose: efemero, em
tmpfs, e deliberadamente NAO e o `postgres` com dados reais. O runner do Django
cria e destroi `test_controle_bancario` sozinho, mas faz isso no servidor que a
configuracao apontar -- e um DROP DATABASE sempre executa onde mandaram.

CONSEQUENCIA PRATICA: o bootstrap do schema em PostgreSQL vazio deixou de ser
verificacao manual. Toda execucao da suite aplica a cadeia inteira de migracoes
a um banco vazio, porque e assim que o pytest-django constroi o banco de teste.
Uma migracao que falha ao executar agora reprova na CI, e nao mais no
`deploy.sh` -- que reverte codigo e imagem, mas nao reverte migracao.
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

    `/health` consulta o banco. Como esta suite nao abre uma conexao real,
    cada teste da rota declara por fixture se exercita sucesso ou falha.
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
