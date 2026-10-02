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
from collections import Counter
from dataclasses import dataclass, replace
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
    # Só a fatura de cartão preenche os campos abaixo (ver `fatura_csv`).
    purchase_date: date | None = None
    installment_current: int | None = None
    installment_total: int | None = None
    card_holder: str = ""
    bank_category: str = ""


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


def numerar_repeticoes(linhas: list[ParsedStatementLine], account_id: int) -> list[ParsedStatementLine]:
    """Duas linhas iguais no mesmo arquivo são dois movimentos, não uma duplicata.

    Dois PIX de R$ 50 à mesma pessoa no mesmo dia têm data, descrição e valor
    idênticos. Com o hash só desses três campos, a segunda linha recebia o hash
    da primeira e a importação a descartava como "duplicada" -- o movimento
    sumia da conciliação. A fatura do cartão já resolvia isso com um contador
    (`fatura_csv.py`); aqui é o mesmo, com uma diferença deliberada: a PRIMEIRA
    ocorrência mantém o hash antigo. Assim, reenviar um arquivo já importado
    acrescenta só as repetições que se perderam, e não duplica o resto.

    O contador segue a ordem do arquivo, então reenviar o mesmo arquivo gera os
    mesmos hashes.
    """
    vistas: Counter[tuple[date, str, Decimal]] = Counter()
    numeradas = []
    for linha in linhas:
        chave = (linha.statement_date, linha.description.strip().lower(), linha.amount)
        vistas[chave] += 1
        if vistas[chave] > 1:
            linha = replace(
                linha,
                line_hash=line_hash(
                    account_id,
                    linha.statement_date,
                    f"{linha.description}|repeticao {vistas[chave]}",
                    linha.amount,
                ),
            )
        numeradas.append(linha)
    return numeradas


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
        return numerar_repeticoes(parsed, account_id)


# --- OFX / OFC / QFX ---
#
# Suporta OFX 1.x (SGML) e OFX 2.x (XML), exportados pela maioria dos bancos
# brasileiros e por softwares de contabilidade pessoal. O parser não depende
# de bibliotecas externas: o formato de texto é suficientemente regular para
# extração simples via regex.

_RE_TAG = re.compile(r"<([A-Z0-9.]+)>\s*([^\n<]*)", re.IGNORECASE)
_RE_XML_STMTTRN_TAG = re.compile(r"</?STMTTRN>", re.IGNORECASE)


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
    """Extrai transações de OFX 2.x com varredura linear das tags de bloco.

    Não use ``<STMTTRN>(.*?)</STMTTRN>`` aqui: em entrada malformada com muitas
    aberturas e nenhum fechamento, o mecanismo de backtracking pode revisitar o
    restante do arquivo para cada abertura. A varredura abaixo avança por cada
    tag uma vez e mantém a tolerância anterior a blocos incompletos.
    """
    txs: list[dict[str, str]] = []
    block_start: int | None = None
    for tag_match in _RE_XML_STMTTRN_TAG.finditer(content):
        tag = tag_match.group(0)
        if not tag.startswith("</"):
            if block_start is None:
                block_start = tag_match.end()
            continue
        if block_start is None:
            continue

        tags: dict[str, str] = {}
        for match in _RE_TAG.finditer(content[block_start : tag_match.start()]):
            tags[match.group(1).upper()] = match.group(2).strip()
        if tags:
            txs.append(tags)
        block_start = None
    return txs


def extract_ofx_account_hint(content: str) -> str | None:
    """Número de conta (`ACCTID`) do cabeçalho OFX, se houver.

    O cabeçalho (`<BANKACCTFROM>`/`<CCACCTFROM>`) vem antes do primeiro
    `<STMTTRN>` e nunca é lido por `_extract_transactions_sgml`/`_xml` (que só
    olham dentro de cada bloco de transação) - usado só para sugerir a conta
    na importação em lote, não na leitura das linhas.
    """
    match = re.search(r"<STMTTRN>", content, re.IGNORECASE)
    header = content[: match.start()] if match else content
    for tag_match in _RE_TAG.finditer(header):
        if tag_match.group(1).upper() == "ACCTID":
            value = tag_match.group(2).strip()
            return value or None
    return None


class OfxStatementAdapter:
    """Adapter para arquivos OFX/OFC/QFX (Open Financial Exchange).

    O hash de deduplicação usa só (conta, data, descrição, valor) - sem o
    FITID que o próprio OFX fornece - pelo mesmo motivo do CSV e dos PDFs
    (ver `numerar_repeticoes`): bancos como o C6 geram um FITID novo a cada
    exportação, mesmo para a transação idêntica, então um hash que dependesse
    dele nunca reconheceria a reimportação de um período já coberto como
    duplicata. Reimportar período sobreposto é rotina aqui (o período de cada
    exportação é escolhido à mão, então vai se sobrepor de vez em quando) -
    por isso todo adapter precisa reconhecer e descartar a duplicata sozinho,
    em vez de depender de um identificador que o banco não garante estável.
    """

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

            if not dtposted or not trnamt:
                continue  # ignora linhas sem data ou valor

            try:
                stmt_date = _parse_ofx_date(dtposted)
                amount = _parse_ofx_amount(trnamt)
            except ValueError:
                continue  # ignora linhas malformadas individualmente

            description = memo[:255]
            lines.append(
                ParsedStatementLine(
                    statement_date=stmt_date,
                    description=description,
                    amount=amount,
                    line_hash=line_hash(account_id, stmt_date, description, amount),
                )
            )

        if not lines:
            raise ValueError("Arquivo OFX não contém transações válidas.")

        return numerar_repeticoes(lines, account_id)


_OFX_EXTENSIONS = (".ofx", ".ofc", ".qfx")
_OFX_MIMETYPES = ("application/x-ofx", "application/ofx", "text/x-ofx")


# --- PDF de corretora homologada ---
#
# Diferente do CSV/OFX, o PDF não tem um formato padrão entre corretoras: cada
# uma exporta o extrato com seu próprio layout de texto. Por isso o dispatch
# não olha só a extensão do arquivo, mas também a instituição da conta - só
# corretoras marcadas como `homologada` (banking.FinancialInstitution) têm um
# adapter de PDF registrado, e o adapter é escolhido pelo nome da instituição.


def extract_pdf_text(raw: bytes) -> str:
    """Texto de todas as páginas do PDF, uma string só separada por `\\n`.

    Compartilhado pelos dois adapters de PDF (e pela detecção de conta da
    importação em lote, que precisa do texto antes de saber a instituição).
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError(
            "Suporte a extrato em PDF indisponível no momento (dependência não instalada)."
        ) from exc

    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        raise ValueError("Não foi possível ler o PDF do extrato.") from exc


_RE_CONTA_LABEL = re.compile(r"Conta:\s*([\d.\-/]+)")


def extract_conta_label(text: str) -> str | None:
    """Número de conta do rótulo "Conta: ..." do cabeçalho/rodapé do PDF.

    Mesmo rótulo nos dois layouts conhecidos (Genial: "Conta: 1234567-8";
    Mercado Pago: "Conta: 11111111111") - usado só para sugerir a conta na
    importação em lote, nunca na leitura das linhas de cada parser.
    """
    match = _RE_CONTA_LABEL.search(text)
    return match.group(1).strip() if match else None


def sniff_pdf_format(text: str) -> str | None:
    """Chave de `_PDF_ADAPTERS` cujo nome de instituição aparece no texto do
    PDF, se exatamente uma bater - usado para decidir o adapter antes de
    saber a conta (e portanto a instituição) na importação em lote.
    """
    lowered = text.lower()
    found = [key for key in _PDF_ADAPTERS if key in lowered]
    return found[0] if len(found) == 1 else None


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
    return numerar_repeticoes(parsed, account_id)


class GenialPdfStatementAdapter:
    """Adapter para o extrato de conta corrente em PDF da Genial Investimentos."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        raw = read_statement_upload(file, label="PDF")
        text = extract_pdf_text(raw)
        return _parse_genial_lines(text, account_id)


# --- PDF do Mercado Pago ---
#
# Layout bem mais simples que o da Genial: cada linha da tabela já sai numa
# única linha de texto extraído, "<data> <descrição> <id da operação> R$
# <valor> R$ <saldo>". A coluna Valor nunca mostra sinal no único extrato
# disponível para desenhar este parser (só tinha crédito nele) - então o
# sinal vem da variação do saldo corrente em vez do texto de Valor, e o
# valor lido só serve de conferência: se divergir, o layout mudou e é
# melhor falhar alto do que arriscar importar um débito como crédito.

_RE_MP_SALDO_INICIAL = re.compile(r"Saldo inicial:\s*R\$\s*([\d.,]+)", re.IGNORECASE)
_RE_MP_SALDO_FINAL = re.compile(r"Saldo final:\s*R\$\s*([\d.,]+)", re.IGNORECASE)
_RE_MP_ROW = re.compile(
    r"^(\d{2})-(\d{2})-(\d{4})\s+(.+)\s+\d+\s+R\$\s*([\d.,]+)\s+R\$\s*([\d.,]+)\s*$"
)


def _parse_mp_decimal(raw: str) -> Decimal:
    return _to_decimal(raw.strip().replace(".", "").replace(",", "."))


def _parse_mercadopago_lines(text: str, account_id: int) -> list[ParsedStatementLine]:
    """Interpreta o texto extraído (via pdfplumber) do extrato de conta da
    Mercado Pago.

    Cada linha de movimento já sai inteira do pdfplumber: data, descrição, id
    da operação e os dois valores em R$ (Valor da linha e Saldo após ela). O
    saldo inicial do cabeçalho ("Saldo inicial: R$ X") é o ponto de partida
    da cadeia de saldos; cada linha seguinte confere `saldo atual - saldo
    anterior` contra o `Valor` lido, e o saldo final do cabeçalho confere a
    cadeia inteira no fim - conferência barata, essencial num formato novo
    validado com um único extrato real.
    """
    saldo_inicial_match = _RE_MP_SALDO_INICIAL.search(text)
    saldo_final_match = _RE_MP_SALDO_FINAL.search(text)
    if not saldo_inicial_match or not saldo_final_match:
        raise ValueError("Não encontrei o saldo inicial/final no extrato Mercado Pago.")

    saldo_anterior = _parse_mp_decimal(saldo_inicial_match.group(1))
    saldo_final_esperado = _parse_mp_decimal(saldo_final_match.group(1))

    parsed: list[ParsedStatementLine] = []
    for raw_line in text.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        if stripped_line.startswith("Data de geração:"):
            break  # rodapé: fim da tabela de movimentos
        match = _RE_MP_ROW.match(stripped_line)
        if not match:
            continue  # cabeçalho, título de tabela repetido na página 2, rodapé
        day, month, year, description, valor_raw, saldo_raw = match.groups()
        statement_date = date(int(year), int(month), int(day))
        saldo_atual = _parse_mp_decimal(saldo_raw)
        valor = _parse_mp_decimal(valor_raw)
        amount = saldo_atual - saldo_anterior
        if abs(abs(amount) - valor) > Decimal("0.01"):
            raise ValueError(
                f"Inconsistência no extrato Mercado Pago em {statement_date:%d/%m/%Y}: "
                f"variação de saldo ({amount}) não bate com o valor lido ({valor_raw})."
            )
        if amount == 0:
            raise ValueError("Valor zerado no extrato não é aceito.")
        description = description.strip()[:255]
        parsed.append(
            ParsedStatementLine(
                statement_date=statement_date,
                description=description,
                amount=amount,
                line_hash=line_hash(account_id, statement_date, description, amount),
            )
        )
        saldo_anterior = saldo_atual

    if not parsed:
        raise ValueError("Nenhum lançamento encontrado no extrato Mercado Pago (PDF).")
    if abs(saldo_anterior - saldo_final_esperado) > Decimal("0.01"):
        raise ValueError(
            "Inconsistência no extrato Mercado Pago: soma dos lançamentos não fecha "
            f"com o saldo final informado ({saldo_final_esperado})."
        )
    max_rows = max_statement_rows()
    if len(parsed) > max_rows:
        raise ValueError(f"Extrato Mercado Pago excede o limite de {max_rows} linha(s).")
    return numerar_repeticoes(parsed, account_id)


class MercadoPagoPdfStatementAdapter:
    """Adapter para o extrato de conta em PDF da Mercado Pago."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        raw = read_statement_upload(file, label="PDF")
        text = extract_pdf_text(raw)
        return _parse_mercadopago_lines(text, account_id)


# --- PDF da XP Investimentos ---
#
# O extrato da conta de investimento da XP é uma tabela (Liq, Mov, Histórico,
# Valor, Saldo) cuja descrição quebra em até três linhas, e o texto corrido do
# PDF mistura essas linhas com as das linhas vizinhas ("TED BCO 336 ..." vem
# antes da data, "DE TED - SPB" depois). Por isso este adapter lê a geometria:
# as linhas da tabela são separadas por filetes horizontais, e cada palavra
# pertence à linha cujo faixa de filetes a contém. Sem filetes, cada palavra
# vai para a data mais próxima.
#
# O mais recente vem primeiro, e a coluna Saldo é o saldo depois da linha. É a
# cadeia de saldos que confere o sinal e o valor de cada linha: o texto marca o
# débito com "-R$", mas o que garante que nenhuma linha foi lida errada é
# `saldo da linha mais velha = saldo da mais nova - valor da mais nova`. Se a
# cadeia não fecha, o layout mudou e é melhor falhar alto.

_RE_XP_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_RE_XP_VALOR = re.compile(r"(-)?\s*R\$\s*(-)?\s*([\d.]+,\d{2})")
_RE_XP_PERIODO_FIM = re.compile(r"At[ée]:\s*(\d{2})/(\d{2})/(\d{4})")
_XP_FILETE_MAX_ALTURA = 2.5
_XP_TOLERANCIA_X = 5


@dataclass(frozen=True)
class _LinhaXp:
    data: date
    descricao: str
    valor: Decimal
    saldo: Decimal


def _xp_data(texto: str) -> date:
    dia, mes, ano = texto.split("/")
    return date(int(ano), int(mes), int(dia))


def _xp_cabecalho(palavras):
    """As posições x das colunas, a partir da primeira linha de cabeçalho da página."""
    por_texto: dict[str, list] = {}
    for palavra in palavras:
        if palavra["text"] in ("Liq", "Mov", "Histórico", "Valor", "Saldo"):
            por_texto.setdefault(palavra["text"], []).append(palavra)
    if not all(nome in por_texto for nome in ("Liq", "Mov", "Histórico", "Valor", "Saldo")):
        return None
    liq = min(por_texto["Liq"], key=lambda p: p["top"])
    mesma_linha = {
        nome: min(
            (p for p in por_texto[nome] if abs(p["top"] - liq["top"]) < 3),
            key=lambda p: p["x0"],
            default=None,
        )
        for nome in por_texto
    }
    if any(valor is None for valor in mesma_linha.values()):
        return None
    return {
        "topo": liq["top"],
        "liq": mesma_linha["Liq"]["x0"],
        "mov": mesma_linha["Mov"]["x0"],
        "historico": mesma_linha["Histórico"]["x0"],
        "valor": mesma_linha["Valor"]["x0"],
    }


def _xp_linhas_da_pagina(pagina, colunas_anteriores=None):
    palavras = pagina.extract_words()
    colunas = _xp_cabecalho(palavras) or colunas_anteriores
    if colunas is None:
        return [], None
    inicio = colunas["topo"] if _xp_cabecalho(palavras) else 0
    fim = float("inf")
    for indice, palavra in enumerate(palavras):
        if palavra["text"] == "Lançamentos" and palavra["top"] > inicio:
            seguinte = palavras[indice + 1]["text"] if indice + 1 < len(palavras) else ""
            if seguinte == "futuros":
                fim = palavra["top"]
                break
    area = [p for p in palavras if inicio + 8 < p["top"] < fim]
    datas = [
        p for p in area
        if _RE_XP_DATA.match(p["text"]) and p["x0"] < colunas["mov"] - 10
    ]
    if not datas:
        return [], colunas
    datas.sort(key=lambda p: p["top"])

    filetes = sorted(
        r["top"] for r in pagina.rects
        if (r["bottom"] - r["top"]) <= _XP_FILETE_MAX_ALTURA
        and colunas["liq"] - 20 <= r["x0"] <= colunas["liq"]
        and inicio < r["top"] < fim
    )
    faixas: dict[int, list] = {indice: [] for indice in range(len(datas))}
    for palavra in area:
        if filetes:
            acima = [f for f in filetes if f <= palavra["top"]]
            abaixo = [f for f in filetes if f > palavra["top"]]
            limite_inferior = max(acima) if acima else None
            limite_superior = min(abaixo) if abaixo else None
            dono = None
            for indice, data in enumerate(datas):
                dentro_de_baixo = limite_inferior is None or data["top"] >= limite_inferior
                dentro_de_cima = limite_superior is None or data["top"] < limite_superior
                if dentro_de_baixo and dentro_de_cima:
                    dono = indice
                    break
            if dono is None:
                continue
        else:
            dono = min(range(len(datas)), key=lambda i: (abs(datas[i]["top"] - palavra["top"]), i))
        faixas[dono].append(palavra)

    linhas = []
    for indice, data in enumerate(datas):
        palavras_da_linha = faixas[indice]
        historico = sorted(
            (p for p in palavras_da_linha
             if colunas["historico"] - _XP_TOLERANCIA_X <= p["x0"] < colunas["valor"] - _XP_TOLERANCIA_X),
            key=lambda p: (round(p["top"]), p["x0"]),
        )
        direita = sorted(
            (p for p in palavras_da_linha if p["x0"] >= colunas["valor"] - _XP_TOLERANCIA_X),
            key=lambda p: p["x0"],
        )
        valores = _RE_XP_VALOR.findall(" ".join(p["text"] for p in direita))
        if len(valores) < 2:
            raise ValueError(
                f"Não consegui ler o valor e o saldo da linha de {data['text']} no extrato XP."
            )
        (s1a, s1b, v1), (s2a, s2b, v2) = valores[0], valores[1]
        linhas.append(
            _LinhaXp(
                data=_xp_data(data["text"]),
                descricao=" ".join(p["text"] for p in historico if p["text"]).strip(),
                valor=_brl(s1a, s1b, v1),
                saldo=_brl(s2a, s2b, v2),
            )
        )
    return linhas, colunas


def _xp_ler(pdf_paginas) -> list[_LinhaXp]:
    """As linhas de todas as páginas, na ordem do arquivo (a mais recente primeiro)."""
    linhas: list[_LinhaXp] = []
    colunas = None
    for pagina in pdf_paginas:
        da_pagina, colunas = _xp_linhas_da_pagina(pagina, colunas)
        linhas.extend(da_pagina)
    return linhas


def _xp_conferir_cadeia(linhas: list[_LinhaXp]) -> None:
    for mais_nova, mais_velha in zip(linhas, linhas[1:], strict=False):
        if abs((mais_nova.saldo - mais_nova.valor) - mais_velha.saldo) > Decimal("0.01"):
            raise ValueError(
                "Inconsistência no extrato XP: o saldo depois da linha de "
                f"{mais_velha.data:%d/%m/%Y} não bate com o valor da linha seguinte "
                f"({mais_nova.data:%d/%m/%Y}). O layout pode ter mudado."
            )


def _xp_abrir(raw: bytes):
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError(
            "Suporte a extrato em PDF indisponível no momento (dependência não instalada)."
        ) from exc
    try:
        return pdfplumber.open(io.BytesIO(raw))
    except Exception as exc:
        raise ValueError("Não foi possível ler o PDF do extrato.") from exc


def _parse_xp_linhas(linhas: list[_LinhaXp], account_id: int) -> list[ParsedStatementLine]:
    if not linhas:
        raise ValueError("Nenhum lançamento encontrado no extrato XP (PDF).")
    _xp_conferir_cadeia(linhas)
    max_rows = max_statement_rows()
    if len(linhas) > max_rows:
        raise ValueError(f"Extrato XP excede o limite de {max_rows} linha(s).")
    parsed = []
    for linha in linhas:
        if linha.valor == 0:
            raise ValueError("Valor zerado no extrato não é aceito.")
        descricao = linha.descricao[:255] or "Movimento XP"
        parsed.append(
            ParsedStatementLine(
                statement_date=linha.data,
                description=descricao,
                amount=linha.valor,
                line_hash=line_hash(account_id, linha.data, descricao, linha.valor),
            )
        )
    return numerar_repeticoes(parsed, account_id)


class XpPdfStatementAdapter:
    """Adapter para o extrato da conta de investimento em PDF da XP Investimentos."""

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        raw = read_statement_upload(file, label="PDF")
        with _xp_abrir(raw) as pdf:
            linhas = _xp_ler(pdf.pages)
        return _parse_xp_linhas(linhas, account_id)


def _saldo_do_xp(raw: bytes, text: str) -> tuple[Decimal, date] | None:
    """Saldo depois da linha mais recente, na data final do período.

    O "Saldo disponível" do cabeçalho é o de quando o extrato foi consultado,
    não o do fim do período; o saldo da primeira linha é o que vale."""
    periodo = _RE_XP_PERIODO_FIM.search(text)
    if periodo is None:
        return None
    with _xp_abrir(raw) as pdf:
        linhas = _xp_ler(pdf.pages)
    if not linhas:
        return None
    fim = date(int(periodo.group(3)), int(periodo.group(2)), int(periodo.group(1)))
    return linhas[0].saldo, fim


_PDF_ADAPTERS = {
    "genial": GenialPdfStatementAdapter,
    "mercado pago": MercadoPagoPdfStatementAdapter,
    "xp investimentos": XpPdfStatementAdapter,
}
# Outros nomes sob os quais a mesma instituição pode estar cadastrada. O "SCP XP
# Investimestos" (sic) é como a conta de investimento da XP foi cadastrada.
_PDF_ALIASES = {
    "xp investimentos": ("scp xp investimentos", "scp xp investimestos"),
}


def pdf_institution_names(format_key: str) -> tuple[str, ...]:
    """Os nomes de instituição (minúsculos) que usam o adapter de `format_key`."""
    return (format_key, *_PDF_ALIASES.get(format_key, ()))


def _normalize_institution_key(name: str) -> str:
    chave = (name or "").strip().lower()
    for formato, apelidos in _PDF_ALIASES.items():
        if chave in apelidos:
            return formato
    return chave


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


# --- Saldo informado pelo próprio arquivo ---
#
# O saldo do extrato serve para conferir o saldo do CB na mesma data: se bate, a
# conta está conciliada; se não, falta ou sobra linha em algum lugar. Fica fora
# de `parse` de propósito: nada aqui entra no hash nem muda uma linha, e um
# arquivo sem saldo (o OFX do C6, o CSV) continua importando como sempre.

_RE_OFX_LEDGERBAL = re.compile(
    r"<LEDGERBAL>.*?<BALAMT>\s*([-+]?[\d.,]+).*?<DTASOF>\s*(\d{8})", re.IGNORECASE | re.DOTALL
)
_RE_GENIAL_SALDO_FINAL = re.compile(r"(-)?\s*R\$\s*(-)?\s*([\d.]+,\d{2})\s*\n\s*Saldo final do per[ií]odo", re.IGNORECASE)
_RE_GENIAL_PERIODO = re.compile(
    r"De\s+\d{1,2}\s+[A-Za-zç]{3}\s+\d{4}\s+a\s+(\d{1,2})\s+([A-Za-zç]{3})\s+(\d{4})", re.IGNORECASE
)
_RE_MP_SALDO_FINAL_COM_SINAL = re.compile(
    r"Saldo final:\s*(-)?\s*R\$\s*(-)?\s*([\d.]+,\d{2})", re.IGNORECASE
)
_RE_MP_PERIODO = re.compile(r"Per[ií]odo:\s*De\s+\d{2}-\d{2}-\d{4}\s+al\s+(\d{2})-(\d{2})-(\d{4})", re.IGNORECASE)


def _brl(sinal_antes: str | None, sinal_depois: str | None, bruto: str) -> Decimal:
    valor = _to_decimal(bruto.replace(".", "").replace(",", "."))
    return -valor if (sinal_antes or sinal_depois) else valor


def _saldo_do_ofx(content: str) -> tuple[Decimal, date] | None:
    achado = _RE_OFX_LEDGERBAL.search(content)
    if achado is None:
        return None
    return _to_decimal(achado.group(1).replace(",", ".")), _parse_ofx_date(achado.group(2))


def _saldo_do_pdf(text: str) -> tuple[Decimal, date] | None:
    formato = sniff_pdf_format(text)
    if formato == "genial":
        saldo, periodo = _RE_GENIAL_SALDO_FINAL.search(text), _RE_GENIAL_PERIODO.search(text)
        if saldo and periodo and periodo.group(2).lower() in _MONTHS_PT:
            dia = date(int(periodo.group(3)), _MONTHS_PT[periodo.group(2).lower()], int(periodo.group(1)))
            return _brl(saldo.group(1), saldo.group(2), saldo.group(3)), dia
    if formato == "mercado pago":
        saldo, periodo = _RE_MP_SALDO_FINAL_COM_SINAL.search(text), _RE_MP_PERIODO.search(text)
        if saldo and periodo:
            dia = date(int(periodo.group(3)), int(periodo.group(2)), int(periodo.group(1)))
            return _brl(saldo.group(1), saldo.group(2), saldo.group(3)), dia
    return None


def extract_statement_balance(file: UploadedFile) -> tuple[Decimal, date] | None:
    """`(saldo, data)` que o arquivo informa, ou `None` quando não informa.

    Nunca levanta: um arquivo que não traz saldo, ou cujo saldo não dá para ler,
    importa do mesmo jeito, só sem a conferência."""
    try:
        filename = (getattr(file, "name", "") or "").lower()
        mimetype = (getattr(file, "content_type", "") or "").lower()
        raw = read_statement_upload(file, label="de extrato")
        if filename.endswith(".pdf") or "pdf" in mimetype:
            texto = extract_pdf_text(raw)
            if sniff_pdf_format(texto) == "xp investimentos":
                return _saldo_do_xp(raw, texto)
            return _saldo_do_pdf(texto)
        if any(filename.endswith(ext) for ext in _OFX_EXTENSIONS) or any(m in mimetype for m in _OFX_MIMETYPES):
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("latin-1")
            return _saldo_do_ofx(content)
    except (ValueError, ArithmeticError):
        return None
    return None


__all__ = [
    "XpPdfStatementAdapter",
    "extract_statement_balance",
    "pdf_institution_names",
    "CsvStatementAdapter",
    "GenialPdfStatementAdapter",
    "MercadoPagoPdfStatementAdapter",
    "OfxStatementAdapter",
    "ParsedStatementLine",
    "StatementAdapter",
    "extract_conta_label",
    "extract_ofx_account_hint",
    "extract_pdf_text",
    "get_statement_adapter",
    "max_statement_rows",
    "max_statement_size_bytes",
    "read_statement_upload",
    "sniff_pdf_format",
]
