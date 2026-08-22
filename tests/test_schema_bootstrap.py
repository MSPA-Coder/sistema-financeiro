"""O grafo de migracoes esta integro e cada app tem uma folha so.

Nao aplica migracoes: isso e verificacao manual obrigatoria contra PostgreSQL
vazio, conforme `docs/development.md`. Este arquivo detecta rapidamente duas
folhas no mesmo app e dependencias que apontam para migracoes inexistentes.

`MigrationLoader(None)` le os arquivos sem abrir conexao, entao a suite
continua sem banco.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db.migrations.loader import MigrationLoader

APPS_DO_PROJETO = [
    "accounts",
    "banking",
    "bank_statements",
    "core",
    "management",
    "transactions",
]


@pytest.fixture(scope="module")
def loader() -> MigrationLoader:
    return MigrationLoader(None, ignore_no_migrations=True)


def test_nenhum_conflito_de_folhas(loader):
    conflitos = loader.detect_conflicts()
    assert not conflitos, f"apps com mais de uma folha: {conflitos}"


def test_grafo_carrega_sem_dependencia_quebrada(loader):
    # `build_graph` no construtor ja levantaria NodeNotFoundError; este teste
    # torna o motivo explicito quando acontecer.
    assert loader.graph.nodes


@pytest.mark.parametrize("app_label", APPS_DO_PROJETO)
def test_app_do_projeto_tem_migracoes(loader, app_label):
    migracoes = [n for n in loader.graph.nodes if n[0] == app_label]
    assert migracoes, f"{app_label} nao tem migracoes; o schema dele nao seria criado"


@pytest.mark.parametrize("app_label", APPS_DO_PROJETO)
def test_app_do_projeto_tem_uma_unica_folha(loader, app_label):
    folhas = [n for n in loader.graph.leaf_nodes() if n[0] == app_label]
    assert len(folhas) == 1, f"{app_label} tem folhas demais: {folhas}"


def test_todo_app_instalado_com_modelo_aparece_no_grafo(loader):
    # Um app com modelo e sem migracao criaria tabela nenhuma no bootstrap, e o
    # erro so apareceria na primeira consulta em producao.
    for config in apps.get_app_configs():
        if config.label not in APPS_DO_PROJETO:
            continue
        if not list(config.get_models()):
            continue
        assert any(n[0] == config.label for n in loader.graph.nodes)
