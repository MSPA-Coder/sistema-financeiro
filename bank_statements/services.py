"""Casos de uso de importação de extrato bancário (Bancos > Importações).

Cada importação cria um lote (`BankStatementImport`) e insere apenas as
linhas cujo hash ainda não existe para a conta, contando quantas duplicadas
foram ignoradas. O hash por conta é o que torna reimportar o mesmo extrato
uma operação segura: reenviar o arquivo não duplica movimentação.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from banking.models import FinancialAccount
from banking.services import accessible_account_ids, can_access_account

from .adapters import get_statement_adapter
from .models import BankStatementImport, BankStatementLine

_MAX_FILENAME_LENGTH = 255
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(raw_name: str | None) -> str:
    """Normaliza o nome do arquivo enviado para um valor seguro de armazenar.

    Remove separadores de caminho e caracteres fora de um allowlist simples,
    de modo que um nome enviado pelo usuário não consiga escapar do diretório
    de destino. Implementado aqui em vez de puxar uma dependência nova.
    """
    name = os.path.basename((raw_name or "").strip()) or "extrato"
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    return name[:_MAX_FILENAME_LENGTH] or "extrato"


def _existing_line_hashes(account_id: int, candidate_hashes: set[str]) -> set[str]:
    """Hashes já presentes para a conta, consultados em lotes de 500."""
    if not candidate_hashes:
        return set()
    existing: set[str] = set()
    hashes = tuple(candidate_hashes)
    for offset in range(0, len(hashes), 500):
        chunk = hashes[offset:offset + 500]
        existing.update(
            BankStatementLine.objects.filter(
                account_id=account_id, line_hash__in=chunk
            ).values_list('line_hash', flat=True)
        )
    return existing


def _clean_account_id(raw_account_id) -> int:
    try:
        account_id = int(raw_account_id)
        if account_id <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Informe a conta e o arquivo do extrato.") from exc
    return account_id


def import_statement_file(
    user, *, account_id, uploaded_file: UploadedFile | None
) -> tuple[BankStatementImport, int, int]:
    """Importa um arquivo de extrato (CSV ou OFX/OFC/QFX) para uma conta.

    Retorna `(lote, linhas_inseridas, linhas_duplicadas_ignoradas)`.
    Levanta `ValueError` para entrada inválida ou acesso negado.
    """
    if uploaded_file is None:
        raise ValueError("Informe a conta e o arquivo do extrato.")
    clean_account_id = _clean_account_id(account_id)
    if not can_access_account(user, clean_account_id, "create"):
        raise ValueError("Acesso negado para importar extrato nesta conta.")

    try:
        account = FinancialAccount.objects.select_related("institution").get(id=clean_account_id)
    except FinancialAccount.DoesNotExist as exc:
        raise ValueError("Conta não encontrada.") from exc

    parsed = get_statement_adapter(uploaded_file, institution=account.institution).parse(
        uploaded_file, clean_account_id
    )
    if not parsed:
        raise ValueError("Nenhuma linha válida encontrada no extrato.")

    filename = _sanitize_filename(uploaded_file.name)

    with transaction.atomic():
        batch = BankStatementImport.objects.create(
            account_id=clean_account_id, source_filename=filename, row_count=0
        )
        existing_hashes = _existing_line_hashes(
            clean_account_id, {line.line_hash for line in parsed}
        )
        new_lines = []
        inserted = skipped = 0
        for line in parsed:
            if line.line_hash in existing_hashes:
                skipped += 1
                continue
            new_lines.append(
                BankStatementLine(
                    import_batch=batch,
                    account_id=clean_account_id,
                    statement_date=line.statement_date,
                    description=line.description,
                    amount=line.amount,
                    line_hash=line.line_hash,
                    status="novo",
                )
            )
            existing_hashes.add(line.line_hash)
            inserted += 1
        if new_lines:
            BankStatementLine.objects.bulk_create(new_lines)
        batch.row_count = inserted
        batch.save(update_fields=["row_count", "updated_at"])

    return batch, inserted, skipped


def statement_imports_for_user(user, limit: int = 20) -> Iterable[BankStatementImport]:
    """Últimos lotes de importação visíveis para `user`."""
    account_ids = accessible_account_ids(user, "view")
    if not account_ids:
        return BankStatementImport.objects.none()
    return BankStatementImport.objects.select_related(
        "account__owner", "account__institution"
    ).filter(account_id__in=account_ids)[:limit]


def statement_import_status(user, batch_id: int) -> dict[str, object] | None:
    """Status resumido de um lote, ou `None` se inexistente/fora de escopo."""
    try:
        batch = BankStatementImport.objects.select_related("account").get(id=batch_id)
    except BankStatementImport.DoesNotExist:
        return None
    if not can_access_account(user, batch.account_id, "view"):
        return None
    return {
        "id": batch.id,
        "row_count": batch.row_count,
        "status": batch.status,
        "created_at": batch.created_at.strftime("%d/%m/%Y %H:%M") if batch.created_at else None,
    }


def accounts_for_import_form(user) -> Iterable[FinancialAccount]:
    """Contas que `user` pode escolher como destino de uma importação."""
    account_ids = accessible_account_ids(user, "create")
    if not account_ids:
        return FinancialAccount.objects.none()
    return FinancialAccount.objects.select_related("owner", "institution").filter(
        id__in=account_ids
    )
