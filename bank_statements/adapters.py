"""Adapters de importação bancária: convertem um arquivo externo (CSV,
OFX/OFC/QFX ou PDF de corretora homologada) em uma lista de linhas
normalizadas (`ParsedStatementLine`).

Cada formato tem seu adapter; todos entregam a mesma estrutura normalizada,
de modo que importação, detecção de duplicidade e conciliação não precisam
saber de qual formato a linha veio.

Erros de validação são sinalizados com `ValueError`, a convenção usada pelos
services do projeto — a view traduz para mensagem de tela.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile


@dataclass(frozen=True)
class ParsedStatementLine:
    """Linha de extrato já normalizada para o domínio interno."""

    statement_date: date
    description: str
    amount: Decimal
    line_hash: str


@runtime_checkable
class StatementAdapter(Protocol):
    """Adapter para converter um formato externo em linhas normalizadas."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        """Lê o arquivo externo e devolve linhas normalizadas."""


def max_statement_size_bytes() -> int:
    return int(getattr(settings, "MAX_BANK_STATEMENT_SIZE_BYTES", 5 * 1024 * 1024))


def max_statement_rows() -> int:
    return int(getattr(settings, "MAX_BANK_STATEMENT_ROWS", 5000))


def _human_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB".replace(".0 MB", " MB")
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB".replace(".0 KB", " KB")
    return f"{size_bytes} bytes"


def read_statement_upload(file: UploadedFile, *, label: str) -> bytes:
    """Lê o upload inteiro respeitando o limite de tamanho configurado.

    Lê até `max_size + 1` bytes para detectar excesso sem carregar um
    arquivo arbitrariamente grande na memória.
    """
    max_size = max_statement_size_bytes()
    file.seek(0)
    raw = file.read(max_size + 1)
    if not raw:
        raise ValueError(f"Arquivo {label} vazio.")
    if len(raw) > max_size:
        raise ValueError(f"Arquivo {label} excede o limite de {_human_size(max_size)}.")
    return raw


def _to_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Valor numérico inválido: {value!r}.") from exc


# --- CSV genérico ---

DATE_KEYS = ("data", "date", "dt", "data movimento", "data_movimento")
DESCRIPTION_KEYS = (
    "descricao",
    "descrição",
    "historico",
    "histórico",
    "description",
    "memo",
    "lancamento",
    "lançamento",
)
AMOUNT_KEYS = ("valor", "amount", "vlr", "value")


def _norm_key(value: str) -> str:
    return (value or "").strip().lower()


def _first(row: dict[str, str], keys: tuple[str, ...]) -> str:
    normalized = {_norm_key(k): v for k, v in row.items()}
    for key in keys:
        if key in normalized:
            return (normalized[key] or "").strip()
    return ""


def _parse_date(raw: str) -> date:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Data inválida no extrato: {raw or '(vazia)'}.")


def _parse_amount(raw: str) -> Decimal:
    value = (raw or "").strip().replace("R$", "").replace(" ", "")
    if not value:
        raise ValueError("Valor vazio no extrato.")
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    amount = _to_decimal(value)
    if amount == 0:
        raise ValueError("Valor zerado no extrato não é aceito.")
    return amount


def line_hash(account_id: int, statement_date: date, description: str, amount: Decimal) -> str:
    source = f"{account_id}|{statement_date.isoformat()}|{description.strip().lower()}|{amount}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class CsvStatementAdapter:
    """Adapter padrão para arquivos CSV de extrato."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        raw = read_statement_upload(file, label="CSV")
        text = raw.decode("utf-8-sig", errors="replace")
        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("CSV sem cabeçalho.")

        parsed: list[ParsedStatementLine] = []
        max_rows = max_statement_rows()
        for index, row in enumerate(reader, 1):
            if index > max_rows:
                raise ValueError(f"CSV excede o limite de {max_rows} linha(s).")
            if not any((cell or "").strip() for cell in row.values()):
                continue
            statement_date = _parse_date(_first(row, DATE_KEYS))
            description = _first(row, DESCRIPTION_KEYS)[:255]
            amount = _parse_amount(_first(row, AMOUNT_KEYS))
            if not description:
                description = "Movimento importado"
            parsed.append(
                ParsedStatementLine(
                    statement_date=statement_date,
                    description=description,
                    amount=amount,
                    line_hash=line_hash(account_id, statement_date, description, amount),
                )
            )
        if not parsed:
            raise ValueError("Nenhuma linha válida encontrada no CSV.")
        return parsed


# --- OFX / OFC / QFX ---
#
# Suporta OFX 1.x (SGML) e OFX 2.x (XML), exportados pela maioria dos bancos
# brasileiros e por softwares de contabilidade pessoal. O parser não depende
# de bibliotecas externas: o formato de texto é suficientemente regular para
# extração simples via regex.

_RE_TAG = re.compile(r"<([A-Z0-9.]+)>\s*([^\n<]*)", re.IGNORECASE)
_RE_XML_STMTTRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.DOTALL | re.IGNORECASE)


def _parse_ofx_date(raw: str) -> date:
    """Converte data OFX (YYYYMMDD[HHMMSS[.mmm][TZ]]) para date."""
    raw = (raw or "").strip()[:8]
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Data OFX inválida: {raw!r}.") from exc


def _parse_ofx_amount(raw: str) -> Decimal:
    """Converte valor OFX para Decimal. OFX usa ponto como separador decimal."""
    value = (raw or "").strip().replace(",", ".")
    amount = _to_decimal(value)
    if amount == 0:
        raise ValueError("Valor zerado no extrato não é aceito.")
    return amount


def _make_ofx_hash(account_id: int, stmt_date: date, amount: Decimal, description: str, fitid: str) -> str:
    key = f"ofx:{account_id}:{stmt_date.isoformat()}:{amount}:{description}:{fitid}"
    return hashlib.sha256(key.encode()).hexdigest()


def _extract_transactions_sgml(content: str) -> list[dict[str, str]]:
    """Extrai transações de OFX 1.x (SGML sem tags de fechamento)."""
    txs: list[dict[str, str]] = []
    blocks = re.split(r"<STMTTRN>", content, flags=re.IGNORECASE)
    for block in blocks[1:]:
        tags: dict[str, str] = {}
        for match in _RE_TAG.finditer(block):
            tags[match.group(1).upper()] = match.group(2).strip()
        if tags:
            txs.append(tags)
    return txs


def _extract_transactions_xml(content: str) -> list[dict[str, str]]:
    """Extrai transações de OFX 2.x (XML com tags de fechamento)."""
    txs: list[dict[str, str]] = []
    for block_match in _RE_XML_STMTTRN.finditer(content):
        tags: dict[str, str] = {}
        for match in _RE_TAG.finditer(block_match.group(1)):
            tags[match.group(1).upper()] = match.group(2).strip()
        if tags:
            txs.append(tags)
    return txs


class OfxStatementAdapter:
    """Adapter para arquivos OFX/OFC/QFX (Open Financial Exchange)."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        raw = read_statement_upload(file, label="OFX")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1")

        is_xml = bool(re.search(r"<\?xml", content, re.IGNORECASE)) or bool(
            re.search(r"</STMTTRN>", content, re.IGNORECASE)
        )

        raw_txs = _extract_transactions_xml(content) if is_xml else _extract_transactions_sgml(content)

        if not raw_txs:
            raise ValueError("Nenhuma transação encontrada no arquivo OFX.")
        max_rows = max_statement_rows()
        if len(raw_txs) > max_rows:
            raise ValueError(f"OFX excede o limite de {max_rows} transação(ões).")

        lines: list[ParsedStatementLine] = []
        for tags in raw_txs:
            dtposted = tags.get("DTPOSTED") or tags.get("DTUSER", "")
            memo = tags.get("MEMO") or tags.get("NAME") or tags.get("TRNTYPE", "Sem descrição")
            trnamt = tags.get("TRNAMT", "")
            fitid = tags.get("FITID", "")

            if not dtposted or not trnamt:
                continue  # ignora linhas sem data ou valor

            try:
                stmt_date = _parse_ofx_date(dtposted)
                amount = _parse_ofx_amount(trnamt)
            except ValueError:
                continue  # ignora linhas malformadas individualmente

            lines.append(
                ParsedStatementLine(
                    statement_date=stmt_date,
                    description=memo[:255],
                    amount=amount,
                    line_hash=_make_ofx_hash(account_id, stmt_date, amount, memo, fitid),
                )
            )

        if not lines:
            raise ValueError("Arquivo OFX não contém transações válidas.")

        return lines


_OFX_EXTENSIONS = (".ofx", ".ofc", ".qfx")
_OFX_MIMETYPES = ("application/x-ofx", "application/ofx", "text/x-ofx")


# --- PDF de corretora homologada ---
#
# Diferente do CSV/OFX, o PDF não tem um formato padrão entre corretoras: cada
# uma exporta o extrato com seu próprio layout de texto. Por isso o dispatch
# não olha só a extensão do arquivo, mas também a instituição da conta - só
# corretoras marcadas como `homologada` (banking.FinancialInstitution) têm um
# adapter de PDF registrado, e o adapter é escolhido pelo nome da instituição.

_MONTHS_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

_RE_GENIAL_ENTRY = re.compile(
    r"^(?:(?:Seg|Ter|Qua|Qui|Sex|S[aá]b|Dom)\s+(\d{1,2})\s+([A-Za-zç]{3})\s+(\d{4})\s+)?"
    r"(.+?)\s+([+-])\s*R\$\s*([\d.,]+)\s*$",
    re.IGNORECASE,
)


def _parse_genial_amount(sign: str, raw: str) -> Decimal:
    value = raw.strip().replace(".", "").replace(",", ".")
    amount = _to_decimal(value)
    if sign == "-":
        amount = -amount
    if amount == 0:
        raise ValueError("Valor zerado no extrato não é aceito.")
    return amount


def _parse_genial_lines(text: str, account_id: int) -> list[ParsedStatementLine]:
    """Interpreta o texto já extraído (via pdfplumber) do extrato Genial.

    Layout fixo do template "Extrato de conta corrente" da Genial: cada
    lançamento é uma linha "<categoria> <sinal> R$ <valor>", com dia da
    semana e data prefixados apenas no primeiro lançamento de cada dia. As
    linhas seguintes até o próximo lançamento (ou até o rodapé "Nome: ...",
    que encerra a leitura) são a descrição, que pode quebrar em mais de uma
    linha de texto extraído.
    """
    parsed: list[ParsedStatementLine] = []
    current: dict | None = None
    current_date: date | None = None
    started = False

    def flush() -> None:
        if current is None:
            return
        joined = " ".join(current["desc_lines"]).strip() or current["category"]
        description = f"{current['category']} - {joined}"[:255]
        parsed.append(
            ParsedStatementLine(
                statement_date=current["date"],
                description=description,
                amount=current["amount"],
                line_hash=line_hash(account_id, current["date"], description, current["amount"]),
            )
        )

    for raw_line in text.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        if stripped_line.startswith("Nome:"):
            break  # rodapé do template Genial: fim da tabela de lançamentos
        match = _RE_GENIAL_ENTRY.match(stripped_line)
        # Sem prefixo de data, só é um lançamento se já estivermos dentro da
        # tabela (`started`) - do contrário é ruído do cabeçalho, como
        # "Total de entradas + R$ 754,68", que também bate no formato
        # "<texto> <sinal> R$ <valor>".
        if match and (match.group(1) or started):
            day, mon, year, category, sign, amount_raw = match.groups()
            if day:
                month = _MONTHS_PT.get(mon.lower()[:3])
                if month is None:
                    raise ValueError(f"Mês inválido no extrato Genial: {mon!r}.")
                current_date = date(int(year), month, int(day))
            flush()
            current = {
                "date": current_date,
                "category": category.strip(),
                "amount": _parse_genial_amount(sign, amount_raw),
                "desc_lines": [],
            }
            started = True
            continue
        if started and current is not None:
            current["desc_lines"].append(stripped_line)
    flush()

    if not parsed:
        raise ValueError("Nenhum lançamento encontrado no extrato Genial (PDF).")
    max_rows = max_statement_rows()
    if len(parsed) > max_rows:
        raise ValueError(f"Extrato Genial excede o limite de {max_rows} linha(s).")
    return parsed


class GenialPdfStatementAdapter:
    """Adapter para o extrato de conta corrente em PDF da Genial Investimentos."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        raw = read_statement_upload(file, label="PDF")
        try:
            import pdfplumber
        except ImportError as exc:
            raise ValueError(
                "Suporte a extrato em PDF indisponível no momento (dependência não instalada)."
            ) from exc

        try:
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as exc:
            raise ValueError("Não foi possível ler o PDF do extrato.") from exc

        return _parse_genial_lines(text, account_id)


_PDF_ADAPTERS = {
    "genial": GenialPdfStatementAdapter,
}


def _normalize_institution_key(name: str) -> str:
    return (name or "").strip().lower()


def get_statement_adapter(file: UploadedFile | None, *, institution=None) -> StatementAdapter:
    """Seleciona o adapter adequado para o arquivo informado.

    Formatos suportados:
    - CSV genérico (.csv) - separador detectado automaticamente
    - OFX / OFC / QFX (.ofx, .ofc, .qfx) - OFX 1.x (SGML) e 2.x (XML)
    - PDF de corretora homologada (.pdf) - `institution` precisa ter
      `homologada=True` e um adapter registrado em `_PDF_ADAPTERS`
    """
    filename = (getattr(file, "name", "") or "").lower()
    mimetype = (getattr(file, "content_type", "") or "").lower()

    if filename.endswith(".pdf") or "pdf" in mimetype:
        if institution is None or not getattr(institution, "homologada", False):
            raise ValueError(
                "Importação em PDF só é permitida para corretoras homologadas. "
                "Verifique o cadastro da instituição da conta."
            )
        adapter_cls = _PDF_ADAPTERS.get(_normalize_institution_key(getattr(institution, "institution_name", "")))
        if adapter_cls is None:
            raise ValueError(
                f"Não há suporte a importação em PDF para a corretora "
                f"'{getattr(institution, 'institution_name', '')}'."
            )
        return adapter_cls()
    if any(filename.endswith(ext) for ext in _OFX_EXTENSIONS) or any(m in mimetype for m in _OFX_MIMETYPES):
        return OfxStatementAdapter()
    if filename.endswith(".csv") or "csv" in mimetype or not filename:
        return CsvStatementAdapter()
    raise ValueError(
        "Formato de extrato não suportado. Use CSV (.csv), OFX/OFC (.ofx, .ofc, .qfx) "
        "ou PDF de corretora homologada."
    )


__all__ = [
    "CsvStatementAdapter",
    "GenialPdfStatementAdapter",
    "OfxStatementAdapter",
    "ParsedStatementLine",
    "StatementAdapter",
    "get_statement_adapter",
    "max_statement_rows",
    "max_statement_size_bytes",
    "read_statement_upload",
]
