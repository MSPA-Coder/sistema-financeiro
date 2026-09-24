"""Controles de configuração que não acessam o banco operacional.

Esses testes protegem contratos de subida e de isolamento. Eles leem somente a
configuração e executam um processo Python isolado quando necessário; não criam
tabelas, migrations ou conexões com PostgreSQL.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
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
    # `REQUIRE_FILE_SECRETS` recusa a chave vinda de variavel, entao o
    # arquivo tem de ser fornecido aqui. Sem isso o processo morre na
    # chave secreta e nunca chega a verificar o POSTGRES_USER, que e o
    # assunto do teste -- passava so onde o ambiente ja tinha a variavel.
    chave = tmp_path / "django_secret_key"
    chave.write_text("chave-de-teste", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente.pop("DJANGO_SECRET_KEY", None)
    ambiente["DJANGO_SECRET_KEY_FILE"] = str(chave)
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
    # `REQUIRE_FILE_SECRETS` recusa a chave vinda de variavel, entao o
    # arquivo tem de ser fornecido aqui. Sem isso o processo morre na
    # chave secreta e nunca chega a verificar o POSTGRES_USER, que e o
    # assunto do teste -- passava so onde o ambiente ja tinha a variavel.
    chave = tmp_path / "django_secret_key"
    chave.write_text("chave-de-teste", encoding="utf-8")
    ambiente = os.environ.copy()
    ambiente.pop("DJANGO_SECRET_KEY", None)
    ambiente["DJANGO_SECRET_KEY_FILE"] = str(chave)
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
    """Toda interpolação de POSTGRES_USER traz o padrão seguro.

    A versão anterior deste teste contava as ocorrências e exigia `== 3`. O
    número quebrava ao acrescentar um serviço legítimo -- foi o que aconteceu
    quando o `postgres-teste` entrou -- e, pior, não media a propriedade que
    interessa: um `${POSTGRES_USER}` seco, SEM padrão nenhum, passava pela
    contagem sem ser notado, e é justamente ele que faria a conexão cair no
    superusuário do cluster.

    Verificar cada interpolação cobre os dois casos e não depende de quantos
    serviços o arquivo tem.
    """
    conteudo = COMPOSE.read_text(encoding="utf-8")

    interpolacoes = re.findall(r"\$\{POSTGRES_USER(:-[^}]*)?\}", conteudo)
    assert interpolacoes, "nenhuma interpolação de POSTGRES_USER encontrada"

    sem_padrao_seguro = [
        padrao or "(sem padrão)"
        for padrao in interpolacoes
        if padrao != ":-controle_bancario"
    ]
    assert not sem_padrao_seguro, (
        "interpolações de POSTGRES_USER sem o padrão seguro: "
        f"{sem_padrao_seguro}"
    )


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


def _servicos() -> dict:
    """Serviços do compose com as âncoras (`<<: *...`) já resolvidas.

    Conferir a propriedade no documento carregado, e não um trecho do texto,
    é o que deixa reordenar chaves ou trocar uma âncora sem reprovar nada -- e
    o que faz um serviço novo sem endurecimento aparecer pelo nome.
    """
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_todo_servico_roda_sem_privilegios_extras():
    """Nenhum contêiner ganha escrita na raiz, capability ou escalada.

    O banco pode gravar PGDATA pelo volume, e só; o resto grava em tmpfs.
    """
    fora = {
        nome: campo
        for nome, servico in _servicos().items()
        for campo, ok in (
            ("read_only", servico.get("read_only") is True),
            ("cap_drop", "ALL" in servico.get("cap_drop", [])),
            ("security_opt", "no-new-privileges:true" in servico.get("security_opt", [])),
            ("pids_limit", bool(servico.get("pids_limit"))),
        )
        if not ok
    }
    assert not fora, f"serviços sem endurecimento: {fora}"


def test_segredos_chegam_por_arquivo_montado_e_nunca_pelo_ambiente():
    """O caminho suportado monta arquivos; segredo no ambiente vaza em `inspect`."""
    diretos = {"POSTGRES_PASSWORD", "DJANGO_SECRET_KEY", "PATRIMONIO_TOKEN"}
    problemas = []
    for nome, servico in _servicos().items():
        ambiente = servico.get("environment") or {}
        montados = set(servico.get("secrets") or [])
        problemas += [f"{nome}: {chave} no ambiente" for chave in diretos & set(ambiente)]
        for chave, valor in ambiente.items():
            segredo = valor.removeprefix("/run/secrets/")
            if chave.endswith("_FILE") and segredo != valor and segredo not in montados:
                problemas.append(f"{nome}: {chave} aponta para segredo não montado")
        if "DJANGO_SECRET_KEY_FILE" in ambiente and ambiente.get("REQUIRE_FILE_SECRETS") != "true":
            problemas.append(f"{nome}: sem REQUIRE_FILE_SECRETS")
    assert not problemas, problemas


def test_web_conecta_com_o_papel_restrito_e_o_migrate_com_o_administrativo():
    servicos = _servicos()

    web = servicos["web"]["environment"]
    migrate = servicos["migrate"]["environment"]
    assert web["POSTGRES_PASSWORD_FILE"] == "/run/secrets/postgres_app_password"
    assert web["DB_EXIGIR_PAPEL_RESTRITO"] == "1"
    assert migrate["POSTGRES_PASSWORD_FILE"] == "/run/secrets/postgres_password"


def test_quality_nao_recebe_segredo_de_producao():
    """A suíte usa valores sintéticos próprios, nunca os montados em web/migrate."""
    servicos = _servicos()

    for nome in ("quality", "postgres-teste"):
        segredos = servicos[nome].get("secrets") or []
        assert segredos
        assert all(segredo.startswith("quality_") for segredo in segredos), (nome, segredos)


def test_bootstrap_de_migrations_antecipa_o_web():
    """O serviço web só pode iniciar após a etapa controlada de migrations."""
    servicos = _servicos()
    comando = " ".join(servicos["migrate"]["command"])

    assert servicos["web"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "manage.py migrate --noinput" in comando
    assert "manage.py collectstatic --noinput --clear" in comando


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
