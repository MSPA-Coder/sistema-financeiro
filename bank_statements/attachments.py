"""Casos de uso de comprovantes/anexos bancários (Bancos > Anexos).

Upload é superfície hostil, então a validação é em camadas: extensão
permitida, assinatura do arquivo conferida nos primeiros bytes (extensão
sozinha é só um nome) e limite de tamanho (`MAX_ATTACHMENT_SIZE_BYTES`).

O acesso é resolvido pela conta do lançamento
(`banking.services.can_access_account`, action="update"): anexar comprovante
é uma escrita, e quem não pode alterar o lançamento não pode anexar a ele.
Os arquivos ficam sob `MEDIA_ROOT`, em volume operacional separado do banco.
"""
from __future__ import annotations

import os
import re
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction as db_transaction

from banking.services import accessible_account_ids, can_access_account
from transactions.models import CashFlowEntry

from .models import EntryAttachment

ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".csv", ".txt"}
ATTACHMENT_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".csv": "text/csv",
    ".txt": "text/plain",
}
_MAX_FILENAME_LENGTH = 255
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SAFE_STEM_LENGTH = 180


def attachment_storage_dir() -> Path:
    root = Path(settings.MEDIA_ROOT) / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_attachment_size_bytes() -> int:
    return int(getattr(settings, "MAX_ATTACHMENT_SIZE_BYTES", 10 * 1024 * 1024))


def _sanitize_filename(raw_name: str | None) -> str:
    name = os.path.basename((raw_name or "").strip()) or "anexo"
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    return name[:_MAX_FILENAME_LENGTH] or "anexo"


def _validate_attachment_signature(extension: str, header: bytes) -> None:
    if extension == ".pdf" and not header.startswith(b"%PDF"):
        raise ValueError("Arquivo PDF inválido.")
    if extension == ".png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Arquivo PNG inválido.")
    if extension in {".jpg", ".jpeg"} and not header.startswith(b"\xff\xd8\xff"):
        raise ValueError("Arquivo JPEG inválido.")
    if extension == ".webp" and not (header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
        raise ValueError("Arquivo WEBP inválido.")
    if extension in {".csv", ".txt"} and b"\x00" in header:
        raise ValueError("Arquivo texto inválido.")


def _attachment_path(attachment: EntryAttachment) -> Path:
    path = (Path(settings.MEDIA_ROOT) / attachment.stored_path).resolve()
    root = attachment_storage_dir().resolve()
    if root not in path.parents:
        raise ValueError("Arquivo de anexo não encontrado.")
    return path


def _attachment_removal_path(attachment: EntryAttachment) -> Path:
    """Valida o caminho lexical e não aceita symlink para remoção."""
    candidate = Path(settings.MEDIA_ROOT) / attachment.stored_path
    root = attachment_storage_dir().resolve()
    resolved = candidate.resolve()
    if root not in resolved.parents or candidate == root or candidate.is_symlink():
        raise ValueError("Arquivo de anexo fora do armazenamento permitido.")
    return candidate


def _unlink_attachment_file(path: Path) -> None:
    """Remove somente um arquivo regular dentro da árvore de anexos."""
    root = attachment_storage_dir().resolve()
    candidate = Path(path)
    resolved = candidate.resolve()
    if root not in resolved.parents or candidate == root:
        raise ValueError("Arquivo de anexo fora do armazenamento permitido.")
    # Não siga um symlink durante uma remoção: mesmo que o destino atual esteja
    # dentro da árvore, a entrada física pode ser trocada entre as verificações.
    if candidate.is_symlink():
        raise ValueError("Arquivo de anexo inválido.")
    candidate.unlink(missing_ok=True)


def attachment_download_path(attachment: EntryAttachment) -> Path:
    path = _attachment_path(attachment)
    if not path.is_file():
        raise ValueError("Arquivo de anexo não encontrado.")
    return path


def can_access_entry(user, entry: CashFlowEntry | None, action: str = "update") -> bool:
    return entry is not None and can_access_account(user, entry.account_id, action)


def save_entry_attachment(user, entry_id, uploaded_file: UploadedFile | None) -> EntryAttachment:
    if not entry_id or uploaded_file is None:
        raise ValueError("Informe o movimento e o arquivo do anexo.")
    try:
        entry = CashFlowEntry.objects.get(id=entry_id)
    except (CashFlowEntry.DoesNotExist, ValueError, TypeError):
        raise ValueError("Movimento não encontrado.") from None
    if not can_access_entry(user, entry, "update"):
        raise ValueError("Acesso negado para gerenciar comprovante deste movimento.")

    safe_original = _sanitize_filename(uploaded_file.name)
    extension = Path(safe_original).suffix.lower()
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValueError("Tipo de arquivo não permitido para anexo.")
    stem = Path(safe_original).stem or "anexo"
    original = f"{stem[:_MAX_SAFE_STEM_LENGTH - len(extension)]}{extension}"
    stored = f"{entry.id}_{uuid4().hex}_{original}"
    path = attachment_storage_dir() / stored

    max_size = max_attachment_size_bytes()
    size = 0
    header = b""
    try:
        with path.open("wb") as destination:
            for chunk in uploaded_file.chunks():
                if not header:
                    header = chunk[:16]
                size += len(chunk)
                if size > max_size:
                    raise ValueError("Arquivo excede o limite permitido para anexos.")
                destination.write(chunk)
        if size == 0:
            raise ValueError("Arquivo vazio.")
        _validate_attachment_signature(extension, header)
    except Exception:
        with suppress(OSError, ValueError):
            _unlink_attachment_file(path)
        raise

    try:
        return EntryAttachment.objects.create(
            entry=entry,
            original_filename=original,
            stored_filename=stored,
            stored_path=str(Path("attachments") / stored),
            mime_type=ATTACHMENT_MIME_TYPES.get(extension) or getattr(uploaded_file, "content_type", None),
            file_size=size,
        )
    except Exception:
        # O arquivo já existe, mas o registro relacional pode falhar (por
        # exemplo, por indisponibilidade do banco). Não deixe um órfão físico.
        with suppress(OSError, ValueError):
            _unlink_attachment_file(path)
        raise


@db_transaction.atomic
def delete_entry_attachment(user, *, attachment_id) -> None:
    """Exclui um comprovante autorizado e seu arquivo após o commit.

    O caminho é validado antes de apagar o registro e o unlink é agendado com
    ``on_commit``. Assim, uma falha no banco não remove um arquivo cujo
    metadado ainda possa permanecer, e um rollback não cria um novo órfão.
    """
    if not attachment_id:
        raise ValueError("Anexo inválido.")
    try:
        attachment = EntryAttachment.objects.select_related("entry").get(id=attachment_id)
    except (EntryAttachment.DoesNotExist, ValueError, TypeError):
        raise ValueError("Anexo não encontrado.") from None
    if not can_access_entry(user, attachment.entry, "update"):
        raise ValueError("Acesso negado para gerenciar este comprovante.")

    path = _attachment_removal_path(attachment)
    attachment.delete()
    db_transaction.on_commit(lambda: _unlink_attachment_file(path))


def recent_attachments_for_user(user, target_attachment_id: int | None = None, limit: int = 50):
    account_ids = accessible_account_ids(user, "view")
    if not account_ids:
        return []
    attachments = list(
        EntryAttachment.objects.select_related(
            "entry__account__owner", "entry__account__institution", "entry__category"
        ).filter(entry__account_id__in=account_ids)[:limit]
    )
    if target_attachment_id:
        target = EntryAttachment.objects.select_related(
            "entry__account__owner", "entry__account__institution"
        ).filter(id=target_attachment_id, entry__account_id__in=account_ids).first()
        if target and all(attachment.id != target.id for attachment in attachments):
            attachments = [target, *attachments]
    return attachments


def attachment_for_download(user, attachment_id: int | None) -> EntryAttachment | None:
    if not attachment_id:
        return None
    try:
        attachment = EntryAttachment.objects.select_related("entry").get(id=attachment_id)
    except (EntryAttachment.DoesNotExist, ValueError, TypeError):
        return None
    if not can_access_entry(user, attachment.entry, "update"):
        return None
    return attachment
