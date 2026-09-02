"""`.secrets/` e `.certs/` não podem entrar no ZIP de distribuição (CB-03).

`scripts/package_clean_zip.py` não é um app Django nem um pacote (sem
`__init__.py`); carregado por caminho de arquivo, como o próprio script
prevê ao ser chamado por `python scripts/package_clean_zip.py`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_CAMINHO = Path(__file__).resolve().parents[1] / "scripts" / "package_clean_zip.py"
_SPEC = importlib.util.spec_from_file_location("package_clean_zip", _CAMINHO)
package_clean_zip = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(package_clean_zip)

PROJECT_ROOT = package_clean_zip.PROJECT_ROOT
_is_excluded = package_clean_zip._is_excluded


def test_secrets_directory_e_excluida() -> None:
    # O incidente que o achado descreve: um ZIP "limpo" que carrega os
    # segredos operacionais em texto claro.
    assert _is_excluded(PROJECT_ROOT / ".secrets" / "postgres_password")
    assert _is_excluded(PROJECT_ROOT / ".secrets" / "django_secret_key")


def test_certs_directory_e_excluida() -> None:
    assert _is_excluded(PROJECT_ROOT / ".certs" / "localhost.pem")


def test_env_docker_continua_excluido() -> None:
    # Regressão: a exclusão de .env* já existia antes deste achado.
    assert _is_excluded(PROJECT_ROOT / ".env.docker")


def test_arquivo_comum_do_projeto_nao_e_excluido() -> None:
    # Sanidade: a correção não pode virar uma exclusão ampla demais.
    assert not _is_excluded(PROJECT_ROOT / "manage.py")
