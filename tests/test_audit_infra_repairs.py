"""Contratos das correções de empacotamento, CI e operação.

Estes testes são intencionalmente estáticos: não sobem containers, não leem
segredos e não dependem de um daemon Docker. O portão oficial continua sendo o
Compose; aqui ficam apenas as regressões pequenas que seriam fáceis de perder
numa revisão de infraestrutura.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DROPBOXIGNORE = ROOT / ".dropboxignore"
GITIGNORE = ROOT / ".gitignore"
PROVISION = ROOT / "scripts" / "provision_compose_secrets.ps1"

_SPEC = importlib.util.spec_from_file_location(
    "package_clean_zip",
    ROOT / "scripts" / "package_clean_zip.py",
)
assert _SPEC and _SPEC.loader
package_clean_zip = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(package_clean_zip)


def test_dados_de_runtime_ficam_fora_do_zip_e_do_dropbox() -> None:
    assert "media" in package_clean_zip.EXCLUDED_DIRS
    assert "staticfiles" in package_clean_zip.EXCLUDED_DIRS
    assert package_clean_zip._is_excluded(ROOT / "media" / "comprovante.pdf")
    assert package_clean_zip._is_excluded(ROOT / "staticfiles" / "admin.css")

    regras_dropbox = set(DROPBOXIGNORE.read_text(encoding="utf-8").splitlines())
    assert "media/" in regras_dropbox
    assert "staticfiles/" in regras_dropbox

    regras_git = set(GITIGNORE.read_text(encoding="utf-8").splitlines())
    assert "media/" in regras_git
    assert "staticfiles/" in regras_git


def test_quality_tem_segredos_sinteticos_separados() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "/run/secrets/quality_django_secret_key" in compose
    assert "/run/secrets/quality_postgres_password" in compose
    assert "/run/secrets/quality_patrimonio_token" in compose
    assert "- quality_postgres_password" in compose
    assert "quality_django_secret_key:" in compose
    assert "quality_patrimonio_token:" in compose

    # A quality não pode herdar os nomes dos segredos montados por web/migrate.
    quality_bloco = compose[compose.index("  quality:") :]
    assert "*app-secrets" not in quality_bloco
    assert "*app-environment" not in quality_bloco


def test_quality_e_runner_tem_fronteiras_de_privilégio_e_readiness() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    ci = CI.read_text(encoding="utf-8")

    inicio_quality = compose.index("  quality:")
    fim_quality = compose.index("\nvolumes:\n", inicio_quality)
    quality_bloco = compose[inicio_quality:fim_quality]
    hardening = compose[compose.index("x-quality-hardening:") : compose.index("x-service-logging:")]
    assert "*quality-hardening" in quality_bloco
    assert "read_only: true" in hardening
    assert "- ALL" in hardening
    assert "no-new-privileges:true" in hardening
    assert "/tmp:mode=1777,rw,noexec,nosuid" in hardening

    assert "docker compose up --build --detach --wait --wait-timeout 120 web" in ci
    assert "http://127.0.0.1:5201/health/" in ci
    assert "--read-only" in ci
    assert "--cap-drop=ALL" in ci
    assert "--security-opt=no-new-privileges:true" in ci


def test_ci_nao_reutiliza_valores_sinteticos_entre_runtime_e_quality() -> None:
    ci = CI.read_text(encoding="utf-8")

    assert "ci-runtime-django-secret-not-for-production" in ci
    assert "ci-quality-django-secret-not-for-production" in ci
    assert "ci-runtime-postgres-password-not-for-production" in ci
    assert "ci-quality-postgres-password-not-for-production" in ci
    assert "chmod 0644 .secrets/*" in ci


def test_provisionamento_valida_antes_de_escrever_e_preserva_existentes() -> None:
    provision = PROVISION.read_text(encoding="utf-8")

    preflight = provision.index("$destinations =")
    escrita = provision.index("New-Item -ItemType Directory")
    assert preflight < escrita
    assert 'Assert-SecretValue -Name $secretSources[$fileName]' in provision
    assert 'New-UrlSafeSecret' in provision
    assert '"QUALITY_DJANGO_SECRET_KEY"' in provision
    assert '"QUALITY_POSTGRES_PASSWORD"' in provision
    assert '"QUALITY_PATRIMONIO_TOKEN"' in provision
    assert "valor-placeholder" in provision
