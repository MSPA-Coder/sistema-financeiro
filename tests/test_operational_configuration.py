"""Controles de configuração que não acessam o banco operacional.

Esses testes protegem contratos de subida e de isolamento. Eles leem somente a
configuração e executam um processo Python isolado quando necessário; não criam
tabelas, migrations ou conexões com PostgreSQL.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.db import connection

ROOT_DO_PROJETO = Path(__file__).resolve().parents[1]
COMPOSE = ROOT_DO_PROJETO / "compose.yaml"
DOCKERIGNORE = ROOT_DO_PROJETO / ".dockerignore"


def _import_settings(ambiente: dict[str, str], code: str = "import financeiro.settings"):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT_DO_PROJETO,
        env=ambiente,
        capture_output=True,
        text=True,
        check=False,
    )


def test_settings_falha_sem_chave_secreta_ou_arquivo():
    """Uma importação limpa não pode aceitar segredo ausente por acidente."""
    ambiente = os.environ.copy()
    ambiente.pop("DJANGO_SECRET_KEY", None)
    ambiente.pop("DJANGO_SECRET_KEY_FILE", None)
    ambiente["REQUIRE_FILE_SECRETS"] = "true"

    resultado = _import_settings(ambiente)

    assert resultado.returncode != 0
    assert "DJANGO_SECRET_KEY" in resultado.stderr


def test_settings_le_segredos_dos_arquivos_no_modo_operacional(tmp_path):
    """O processo usa os dois arquivos sem recorrer às variáveis diretas."""
    chave = tmp_path / "django_secret_key"
    senha = tmp_path / "postgres_password"
    chave.write_text("chave-de-teste", encoding="utf-8")
    senha.write_text("senha-de-teste", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente.pop("DJANGO_SECRET_KEY", None)
    ambiente.pop("POSTGRES_PASSWORD", None)
    ambiente["DJANGO_SECRET_KEY_FILE"] = str(chave)
    ambiente["POSTGRES_PASSWORD_FILE"] = str(senha)
    ambiente["REQUIRE_FILE_SECRETS"] = "true"

    resultado = _import_settings(
        ambiente,
        "from financeiro.settings import DATABASES, SECRET_KEY; "
        "assert SECRET_KEY == 'chave-de-teste'; "
        "assert DATABASES['default']['PASSWORD'] == 'senha-de-teste'",
    )

    assert resultado.returncode == 0


def test_settings_recusa_arquivo_secreto_vazio_no_modo_operacional(tmp_path):
    arquivo_vazio = tmp_path / "django_secret_key"
    arquivo_vazio.write_text("\n", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente.pop("DJANGO_SECRET_KEY", None)
    ambiente["DJANGO_SECRET_KEY_FILE"] = str(arquivo_vazio)
    ambiente["REQUIRE_FILE_SECRETS"] = "true"

    resultado = _import_settings(ambiente)

    assert resultado.returncode != 0
    # A asserção prova o que importa -- a subida falha e a mensagem nomeia a
    # variável quebrada -- sem depender da grafia exata. A mensagem agora vem
    # de `sharedauth.secrets`, compartilhada com os tres apps Flask, e casar
    # com o texto literal amarraria este teste ao acento de outro repositorio.
    assert "vazio" in resultado.stderr
    assert "DJANGO_SECRET_KEY_FILE" in resultado.stderr


def test_settings_aceita_variaveis_diretas_apenas_no_modo_local():
    ambiente = os.environ.copy()
    ambiente.pop("DJANGO_SECRET_KEY_FILE", None)
    ambiente.pop("POSTGRES_PASSWORD_FILE", None)
    ambiente["DJANGO_SECRET_KEY"] = "chave-local-explicita"
    ambiente["POSTGRES_PASSWORD"] = "senha-local-explicita"
    ambiente["REQUIRE_FILE_SECRETS"] = "false"

    resultado = _import_settings(ambiente)

    assert resultado.returncode == 0


def test_settings_recusa_conectar_como_postgres(tmp_path):
    """POSTGRES_USER=postgres é o superusuário do cluster, não uma conta de app (CB-05)."""
    senha = tmp_path / "postgres_password"
    senha.write_text("senha-de-teste", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente["POSTGRES_USER"] = "postgres"
    ambiente.pop("POSTGRES_PASSWORD", None)
    ambiente["POSTGRES_PASSWORD_FILE"] = str(senha)
    ambiente["REQUIRE_FILE_SECRETS"] = "true"

    resultado = _import_settings(ambiente)

    assert resultado.returncode != 0
    assert "postgres" in resultado.stderr
    assert "POSTGRES_USER" in resultado.stderr


def test_settings_aceita_postgres_user_dedicado(tmp_path):
    senha = tmp_path / "postgres_password"
    senha.write_text("senha-de-teste", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente["POSTGRES_USER"] = "controle_bancario"
    ambiente.pop("POSTGRES_PASSWORD", None)
    ambiente["POSTGRES_PASSWORD_FILE"] = str(senha)
    ambiente["REQUIRE_FILE_SECRETS"] = "true"

    resultado = _import_settings(ambiente)

    assert resultado.returncode == 0


def test_default_de_postgres_user_nao_e_o_superusuario():
    """Sem a variável definida, o padrão não pode voltar a ser 'postgres'."""
    ambiente = os.environ.copy()
    ambiente.pop("POSTGRES_USER", None)
    ambiente.pop("POSTGRES_PASSWORD", None)
    ambiente["POSTGRES_PASSWORD"] = "senha-de-teste"
    ambiente["REQUIRE_FILE_SECRETS"] = "false"

    resultado = _import_settings(
        ambiente, "from financeiro.settings import DATABASES; print(DATABASES['default']['USER'])"
    )

    assert resultado.returncode == 0
    assert resultado.stdout.strip() == "controle_bancario"


def test_compose_nao_usa_postgres_como_padrao_de_postgres_user():
    conteudo = COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_USER:-postgres}" not in conteudo
    assert conteudo.count("POSTGRES_USER:-controle_bancario}") == 3


def test_compose_exige_os_segredos_operacionais():
    """O caminho suportado monta arquivos e não passa segredos no ambiente."""
    conteudo = COMPOSE.read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in conteudo
    assert "DJANGO_SECRET_KEY_FILE: /run/secrets/django_secret_key" in conteudo
    assert "REQUIRE_FILE_SECRETS: \"true\"" in conteudo
    assert "POSTGRES_PASSWORD: ${" not in conteudo
    assert "DJANGO_SECRET_KEY: ${" not in conteudo


def test_contexto_de_build_exclui_segredos_e_estado_local():
    """O contexto nao pode entregar arquivos locais a uma instrucao COPY."""
    regras = set(DOCKERIGNORE.read_text(encoding="utf-8").splitlines())

    for regra in {
        ".env",
        ".env.*",
        ".certs",
        ".secrets",
        "backups",
        "logs",
        "media",
        "staticfiles",
    }:
        assert regra in regras


def test_postgres_tem_hardening_equivalente_ao_runtime():
    """O banco pode gravar PGDATA, mas nao precisa de privilegios extras."""
    conteudo = COMPOSE.read_text(encoding="utf-8")
    inicio = conteudo.index("x-postgres-hardening:")
    fim = conteudo.index("services:")
    bloco = conteudo[inicio:fim]

    assert 'user: "postgres"' in bloco
    assert "read_only: true" in bloco
    assert "- ALL" in bloco
    assert "- no-new-privileges:true" in bloco
    assert "/tmp:mode=1777,rw,noexec,nosuid,size=64m" in bloco
    assert "/var/run/postgresql:mode=1777,rw,noexec,nosuid,size=16m" in bloco
    assert "pids_limit: 256" in bloco


def test_bootstrap_de_migrations_antecipa_o_web():
    """O serviço web só pode iniciar após a etapa controlada de migrations."""
    conteudo = COMPOSE.read_text(encoding="utf-8")
    inicio_migrate = conteudo.index("  migrate:")
    inicio_web = conteudo.index("  web:")
    bloco_migrate = conteudo[inicio_migrate:inicio_web]
    bloco_web = conteudo[inicio_web:conteudo.index("  quality:")]

    assert "python manage.py migrate --noinput" in bloco_migrate
    assert "python manage.py collectstatic --noinput --clear" in bloco_migrate
    assert "migrate:\n        condition: service_completed_successfully" in bloco_web


def test_nome_do_banco_de_teste_difere_do_operacional():
    """Se um teste precisar de banco, Django deriva um banco de teste próprio."""
    nome_operacional = connection.settings_dict["NAME"]
    nome_de_teste = connection.creation._get_test_db_name()

    assert nome_de_teste != nome_operacional
    assert nome_de_teste.startswith("test_")


def test_pytest_bloqueia_acesso_ao_banco_sem_marcador(django_db_blocker):
    """A suíte focada não abre conexão por acidente nem reutiliza dados reais."""
    with pytest.raises(RuntimeError, match="Database access not allowed"), django_db_blocker.block():
        connection.cursor()
