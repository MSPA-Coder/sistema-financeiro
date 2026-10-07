"""Casos de uso do upload em lote de extratos (Bancos > Importações).

Fluxo: `stage_uploaded_files` salva cada arquivo e tenta detectar a conta a
partir do próprio conteúdo; nada vira `BankStatementLine` ainda. Só
`resolve_pending_uploads`, chamado depois que o usuário confirma (ou corrige)
a conta de cada arquivo na tela seguinte, chama `import_statement_file` -
exatamente a mesma função que o formulário de um arquivo só sempre chamou,
sem nenhuma mudança nela.

Upload é superfície hostil, então a validação segue a mesma disciplina de
`bank_statements/attachments.py`: extensão permitida, tamanho limitado (reusa
`max_statement_size_bytes`/`read_statement_upload`, os mesmos limites que já
valiam para o formulário de um arquivo só) e arquivo gravado sob
`MEDIA_ROOT`, em subpasta própria, separado do banco.
"""
from __future__ import annotations

import os
import re
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile
from django.db import transaction as db_transaction

from banking.models import FinancialAccount, FinancialInstitution

from .adapters import (
    extract_conta_label,
    extract_ofx_account_hint,
    extract_pdf_text,
    get_statement_adapter,
    pdf_institution_names,
    read_statement_upload,
    sniff_pdf_format,
)
from .fatura_csv import FORMATO_C6, FORMATO_XP, formato_da_fatura
from .models import BankStatementImport, PendingStatementUpload
from .services import accounts_for_import_form, import_statement_file

ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".ofx", ".ofc", ".qfx", ".pdf"}
_MAX_FILENAME_LENGTH = 255
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SAFE_STEM_LENGTH = 180
_RE_NON_DIGIT = re.compile(r"\D+")
_FATURA_LABELS = {FORMATO_C6: "Fatura C6", FORMATO_XP: "Fatura XP"}


def pending_storage_dir() -> Path:
    root = Path(settings.MEDIA_ROOT) / "pending_imports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sanitize_filename(raw_name: str | None) -> str:
    name = os.path.basename((raw_name or "").strip()) or "extrato"
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    return name[:_MAX_FILENAME_LENGTH] or "extrato"


def _only_digits(value: str) -> str:
    return _RE_NON_DIGIT.sub("", value or "")


def _pending_removal_path(upload: PendingStatementUpload) -> Path:
    """Valida o caminho lexicalmente e não aceita symlink para remoção."""
    candidate = Path(settings.MEDIA_ROOT) / upload.stored_path
    root = pending_storage_dir().resolve()
    resolved = candidate.resolve()
    if root not in resolved.parents or candidate == root or candidate.is_symlink():
        raise ValueError("Arquivo pendente fora do armazenamento permitido.")
    return candidate


def _unlink_pending_file(path: Path) -> None:
    root = pending_storage_dir().resolve()
    candidate = Path(path)
    resolved = candidate.resolve()
    if root not in resolved.parents or candidate == root:
        raise ValueError("Arquivo pendente fora do armazenamento permitido.")
    if candidate.is_symlink():
        raise ValueError("Arquivo pendente inválido.")
    candidate.unlink(missing_ok=True)


def _account_lookup(user) -> dict[str, list[FinancialAccount]]:
    """`statement_identifier` só-dígitos -> contas que o têm, entre as que
    `user` pode escolher como destino de importação."""
    lookup: dict[str, list[FinancialAccount]] = {}
    for account in accounts_for_import_form(user):
        digits = _only_digits(account.statement_identifier)
        if digits:
            lookup.setdefault(digits, []).append(account)
    return lookup


def detect_account(
    user, *, filename: str, content_type: str, raw: bytes
) -> tuple[FinancialAccount | None, str, str]:
    """Tenta achar a conta do arquivo pelo conteúdo.

    Devolve `(conta_detectada_ou_None, rotulo, erro_de_leitura)`. `rotulo` é
    o texto mostrado na tela de confirmação mesmo sem conta detectada (ex.
    instituição reconhecida mas nenhuma conta com esse identificador no
    cadastro). `erro_de_leitura` só vem preenchido quando o arquivo em si já
    se mostra ilegível (PDF corrompido, OFX sem transação) - descoberto já no
    upload, não só depois de confirmar.
    """
    lookup = _account_lookup(user)
    lowered_name = (filename or "").lower()
    lowered_type = (content_type or "").lower()
    file_like = SimpleUploadedFile(filename or "extrato", raw, content_type=content_type or "")

    if lowered_name.endswith(".pdf") or "pdf" in lowered_type:
        try:
            text = extract_pdf_text(raw)
        except ValueError as exc:
            return None, "", str(exc)

        format_key = sniff_pdf_format(text)
        institution = None
        if format_key:
            # O mesmo adapter pode servir a instituições cadastradas sob mais
            # de um nome (ex. "SCP XP Investimestos" para a XP).
            for nome in pdf_institution_names(format_key):
                institution = FinancialInstitution.objects.filter(
                    homologada=True, institution_name__iexact=nome
                ).first()
                if institution is not None:
                    break
        conta = extract_conta_label(text)
        label = institution.institution_name if institution else "PDF não reconhecido"
        if conta:
            label = f"{label} · conta {conta}"
        if institution is not None:
            try:
                get_statement_adapter(file_like, institution=institution).parse(file_like, account_id=0)
            except ValueError as exc:
                return None, label, str(exc)
        digits = _only_digits(conta) if conta else ""
        matches = lookup.get(digits, []) if digits else []
        if len(matches) > 1 and institution is not None:
            # O mesmo número em duas contas (a conta digital e a de investimento
            # da XP são ambas 323220): a instituição do PDF desempata.
            matches = [m for m in matches if m.institution_id == institution.id]
        return (matches[0] if len(matches) == 1 else None), label, ""

    is_ofx = lowered_name.endswith((".ofx", ".ofc", ".qfx")) or any(
        marker in lowered_type for marker in ("x-ofx", "ofx")
    )
    if is_ofx:
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1")
        hint = extract_ofx_account_hint(content)
        label = f"OFX · conta {hint}" if hint else "OFX"
        try:
            get_statement_adapter(file_like).parse(file_like, account_id=0)
        except ValueError as exc:
            return None, label, str(exc)
        digits = _only_digits(hint) if hint else ""
        matches = lookup.get(digits, []) if digits else []
        return (matches[0] if len(matches) == 1 else None), label, ""

    if lowered_name.endswith(".csv") or "csv" in lowered_type:
        formato = formato_da_fatura(raw)
        label = _FATURA_LABELS.get(formato, "CSV de extrato")
        return None, label, ""

    return None, "", ""


def _failed_upload(user, uploaded_file: UploadedFile, safe_original: str, message: str) -> PendingStatementUpload:
    return PendingStatementUpload.objects.create(
        uploaded_by=user,
        original_filename=safe_original,
        stored_filename="",
        stored_path="",
        mime_type=getattr(uploaded_file, "content_type", "") or None,
        file_size=0,
        detection_error=message,
    )


def stage_uploaded_files(user, files: list[UploadedFile]) -> list[PendingStatementUpload]:
    """Grava cada arquivo e tenta detectar a conta. Nada em `BankStatementLine`
    ainda - só depois de `resolve_pending_uploads`, quando o usuário confirmar."""
    staged: list[PendingStatementUpload] = []
    for uploaded_file in files:
        if uploaded_file is None:
            continue
        safe_original = _sanitize_filename(uploaded_file.name)
        extension = Path(safe_original).suffix.lower()
        if extension and extension not in ALLOWED_UPLOAD_EXTENSIONS:
            staged.append(
                _failed_upload(user, uploaded_file, safe_original, "Tipo de arquivo não permitido para importação.")
            )
            continue

        try:
            raw = read_statement_upload(uploaded_file, label="de importação")
        except ValueError as exc:
            staged.append(_failed_upload(user, uploaded_file, safe_original, str(exc)))
            continue

        stem = Path(safe_original).stem or "extrato"
        original = f"{stem[:_MAX_SAFE_STEM_LENGTH - len(extension)]}{extension}"
        stored = f"{user.id}_{uuid4().hex}_{original}"
        path = pending_storage_dir() / stored
        with path.open("wb") as destination:
            destination.write(raw)

        try:
            account, label, error = detect_account(
                user,
                filename=original,
                content_type=getattr(uploaded_file, "content_type", "") or "",
                raw=raw,
            )
            staged.append(
                PendingStatementUpload.objects.create(
                    uploaded_by=user,
                    original_filename=original,
                    stored_filename=stored,
                    stored_path=str(Path("pending_imports") / stored),
                    mime_type=getattr(uploaded_file, "content_type", "") or None,
                    file_size=len(raw),
                    detected_account=account,
                    detected_label=label,
                    detection_error=error,
                )
            )
        except Exception:
            with suppress(OSError, ValueError):
                _unlink_pending_file(path)
            raise
    return staged


def pending_uploads_for_user(user):
    return PendingStatementUpload.objects.filter(uploaded_by=user).select_related(
        "detected_account__owner", "detected_account__institution"
    )


def _delete_pending(upload: PendingStatementUpload) -> None:
    path = None
    if upload.stored_path:
        with suppress(ValueError):
            path = _pending_removal_path(upload)
    upload.delete()
    if path is not None:
        db_transaction.on_commit(lambda: _unlink_pending_file(path))


def resolve_pending_uploads(
    user, choices: dict
) -> tuple[list[tuple[str, int, int]], list[tuple[str, str]], list[BankStatementImport]]:
    """Confirma os uploads pendentes: importa os escolhidos, descarta o resto.

    `choices` mapeia `pending_id -> account_id` (string) ou `None`/vazio para
    "não importar". Cada arquivo passa pela própria chamada a
    `import_statement_file` - igual ao formulário de um arquivo só -, então
    uma falha isolada não impede os outros. A linha pendente some depois, com
    sucesso ou erro: não fica lixo acumulando para o usuário limpar.
    """
    resultados: list[tuple[str, int, int]] = []
    erros: list[tuple[str, str]] = []
    lotes: list[BankStatementImport] = []

    for pending_id, account_id in choices.items():
        try:
            upload = PendingStatementUpload.objects.get(id=pending_id, uploaded_by=user)
        except (PendingStatementUpload.DoesNotExist, ValueError, TypeError):
            continue

        if upload.detection_error:
            erros.append((upload.original_filename, upload.detection_error))
            _delete_pending(upload)
            continue

        if not account_id:
            _delete_pending(upload)
            continue

        path = _pending_removal_path(upload)
        raw = path.read_bytes()
        file_like = SimpleUploadedFile(upload.original_filename, raw, content_type=upload.mime_type or "")
        try:
            batch, inserted, skipped = import_statement_file(
                user, account_id=account_id, uploaded_file=file_like
            )
            resultados.append((upload.original_filename, inserted, skipped))
            lotes.append(batch)
        except ValueError as exc:
            erros.append((upload.original_filename, str(exc)))
        _delete_pending(upload)

    return resultados, erros, lotes
