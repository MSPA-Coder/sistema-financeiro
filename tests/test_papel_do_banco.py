"""A aplicação recusa atender como superusuário do PostgreSQL.

A suíte conecta com o papel administrativo, que é superusuário -- o mesmo
atributo que a produção tinha no papel da aplicação até 09/2026. Por isso a
recusa pode ser provada contra o servidor de verdade, e não contra um dublê.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from core.papel_do_banco import VARIAVEL, conferir_papel


class _CursorFalso:
    def __init__(self, valor: str) -> None:
        self.valor = valor
        self.consultas: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.consultas.append(sql)

    def fetchone(self):
        return (self.valor,)


class _ConexaoFalsa:
    vendor = "postgresql"
    settings_dict = {"USER": "controle_bancario_app"}

    def __init__(self, valor: str) -> None:
        self.cursor_falso = _CursorFalso(valor)
        self.fechada = False

    def cursor(self):
        return self.cursor_falso

    def close(self) -> None:
        self.fechada = True


@pytest.mark.django_db
def test_superusuario_real_e_recusado_quando_exigido(monkeypatch):
    monkeypatch.setenv(VARIAVEL, "1")
    connection.ensure_connection()
    with connection.cursor() as cursor:
        cursor.execute("SHOW is_superuser")
        assert cursor.fetchone()[0] == "on", "a suíte deveria conectar como superusuário"

    with pytest.raises(ImproperlyConfigured, match="superusuário"):
        conferir_papel(sender=None, connection=connection)


@pytest.mark.django_db
def test_sem_a_variavel_o_papel_administrativo_passa(monkeypatch):
    """O `migrate` usa o superusuário de propósito, sem a variável ligada."""
    monkeypatch.delenv(VARIAVEL, raising=False)
    conferir_papel(sender=None, connection=connection)


def test_papel_restrito_passa_e_a_pergunta_vai_ao_servidor(monkeypatch):
    monkeypatch.setenv(VARIAVEL, "1")
    conexao = _ConexaoFalsa("off")
    conferir_papel(sender=None, connection=conexao)
    assert conexao.cursor_falso.consultas == ["SHOW is_superuser"]
    assert not conexao.fechada


def test_superusuario_fecha_a_conexao_antes_de_recusar(monkeypatch):
    monkeypatch.setenv(VARIAVEL, "1")
    conexao = _ConexaoFalsa("on")
    with pytest.raises(ImproperlyConfigured):
        conferir_papel(sender=None, connection=conexao)
    assert conexao.fechada


def test_o_receptor_esta_ligado_ao_sinal_de_conexao():
    """Sem o `ready()` do core, a trava existiria e nunca rodaria."""
    from django.db.backends.signals import connection_created

    estava_ligado = connection_created.disconnect(dispatch_uid="core.conferir_papel")
    connection_created.connect(conferir_papel, dispatch_uid="core.conferir_papel")
    assert estava_ligado
