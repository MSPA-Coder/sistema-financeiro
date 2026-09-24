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


def test_smoke_da_ci_sobe_o_runtime_endurecido() -> None:
    """A CI exercita a imagem com as mesmas restrições do VPS.

    O endurecimento de cada serviço do compose é conferido em
    `test_operational_configuration.py`; aqui fica só o que a CI acrescenta:
    rodar a imagem sem escrita, sem capability e sem escalada.
    """
    ci = CI.read_text(encoding="utf-8")

    assert "/health/" in ci
    assert "--read-only" in ci
    assert "--cap-drop=ALL" in ci
    assert "--security-opt=no-new-privileges:true" in ci


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
