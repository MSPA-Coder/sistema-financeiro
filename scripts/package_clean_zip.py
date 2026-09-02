"""Gera um ZIP limpo sem metadados locais, caches, bancos ou temporarios.

Uso, a partir da raiz do projeto:
    python scripts/package_clean_zip.py
    python scripts/package_clean_zip.py --output ../ControleBancario_clean.zip
"""
from __future__ import annotations

import argparse
import fnmatch
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}_clean.zip"

EXCLUDED_DIRS = {
    ".agents",
    ".certs",
    ".codex",
    ".dropbox.cache",
    ".git",
    ".hypothesis",
    ".idea",
    ".mypy_cache",
    ".nox",
    ".pyre",
    ".pytest_cache",
    ".pytype",
    ".ruff_cache",
    ".runtime-worktree",
    ".secrets",
    ".tox",
    ".venv",
    "__pycache__",
    "__pypackages__",
    "backups",
    "build",
    "dist",
    "env",
    "htmlcov",
    "instance",
    "logs",
    "tmp",
    "temp",
    "uploads",
    "venv",
}

EXCLUDED_PATTERNS = (
    "*.bak",
    "*.db",
    "*.db-*",
    "*.db-journal",
    "*.db-shm",
    "*.db-wal",
    "*.egg",
    "*.egg-info",
    "*.log",
    "*.mdc",
    "*.pyc",
    "*.pyo",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.swp",
    "*.swo",
    "*.tmp",
    "*.zip",
    ".coverage",
    ".coverage.*",
    ".DS_Store",
    ".dropbox",
    ".dropbox.attr",
    ".env",
    ".env.*",
    "Thumbs.db",
    "desktop.ini",
    "pip-wheel-metadata",
)


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(PROJECT_ROOT)
    rel_parts = rel.parts
    if any(part in EXCLUDED_DIRS for part in rel_parts):
        return True
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_PATTERNS)


def build_zip(output: Path) -> Path:
    output = output.resolve()
    if output.exists():
        output.unlink()

    files = [
        path
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file() and path.resolve() != output and not _is_excluded(path)
    ]

    with ZipFile(output, "w", ZIP_DEFLATED) as zf:
        for path in sorted(files):
            arcname = PROJECT_ROOT.name / path.relative_to(PROJECT_ROOT)
            zf.write(path, arcname.as_posix())

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera ZIP limpo do projeto.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Caminho do ZIP de saida. Padrao: {DEFAULT_OUTPUT}",
    )
    args = parser.parse_args()

    output = build_zip(args.output)
    print(output)


if __name__ == "__main__":
    main()
