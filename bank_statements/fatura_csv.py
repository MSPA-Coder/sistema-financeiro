"""Leitura da fatura de cartão de crédito exportada em CSV (C6 e XP).

A fatura não é um extrato e não pode passar pelo `CsvStatementAdapter`:

- o sinal é o contrário. No extrato, valor positivo é dinheiro entrando; na
  fatura, positivo é compra -- dívida aumentando. Aqui o sinal é invertido na
  leitura, e daí em diante a linha segue a convenção da conta: compra é
  negativa (despesa no cartão), pagamento e estorno são positivos;
- a compra parcelada volta em toda fatura, com a data da compra original e o
  número da parcela ("3/10" na C6, "3 de 10" na XP). A linha recebe a data em
  que a parcela entra na fatura (a da compra mais N-1 meses) e guarda N e o
  total, para o processamento casar a parcela com a que já foi lançada;
- a fatura traz quem comprou (titular ou adicional) e, na C6, a categoria do
  banco e o valor em dólar das compras no exterior.

Os formatos são reconhecidos pelo cabeçalho, não pelo nome da instituição:
quem decide é o arquivo. `formato_da_fatura` também serve para recusar uma
fatura enviada para uma conta que não é cartão.
"""
from __future__ import annotations

import calendar
import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from core.domain.finance import MAX_TRANSACTION_INSTALLMENTS

from .adapters import (
    ParsedStatementLine,
    _parse_amount,
    _parse_date,
    line_hash,
    max_statement_rows,
    read_statement_upload,
)

FORMATO_C6 = "c6"
FORMATO_XP = "xp"

_COLUNAS_C6 = {"data de compra", "nome no cartao", "descricao", "parcela", "valor (em r$)"}
_COLUNAS_XP = {"data", "estabelecimento", "portador", "valor", "parcela"}

_PARCELA_C6 = re.compile(r"^(\d+)\s*/\s*(\d+)$")
_PARCELA_XP = re.compile(r"^(\d+)\s+de\s+(\d+)$", re.IGNORECASE)


def _chave(texto: str) -> str:
    """Cabeçalho comparável: minúsculo, sem acento e sem espaço nas pontas."""
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return sem_acento.strip().lower()


def _texto(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="replace")


def _cabecalho(texto: str) -> set[str]:
    primeira = texto.splitlines()[0] if texto else ""
    return {_chave(coluna) for coluna in primeira.split(";")}


def formato_da_fatura(raw: bytes) -> str | None:
    """`"c6"`, `"xp"` ou `None` quando o cabeçalho não é de fatura conhecida."""
    colunas = _cabecalho(_texto(raw))
    if colunas >= _COLUNAS_C6:
        return FORMATO_C6
    if colunas >= _COLUNAS_XP:
        return FORMATO_XP
    return None


@dataclass(frozen=True)
class _Lida:
    data_da_compra: date
    descricao: str
    valor_na_fatura: Decimal
    parcela: tuple[int, int] | None
    portador: str
    categoria_do_banco: str
    dolar: str


def _parcela(raw: str, padrao: re.Pattern) -> tuple[int, int] | None:
    raw = (raw or "").strip()
    if raw in ("", "-") or _chave(raw) == "unica":
        return None
    achado = padrao.match(raw)
    if not achado:
        raise ValueError(f"Parcela não reconhecida na fatura: {raw!r}.")
    atual, total = int(achado.group(1)), int(achado.group(2))
    if not 1 <= atual <= total <= MAX_TRANSACTION_INSTALLMENTS:
        raise ValueError(f"Parcela fora do intervalo aceito: {raw!r}.")
    return (None if total == 1 else (atual, total))


def _valor(raw: str) -> Decimal | None:
    """Valor da fatura; `None` para linha zerada, que não é movimento."""
    try:
        return _parse_amount(raw)
    except ValueError as exc:
        if "zerado" in str(exc):
            return None
        raise


def _primeiro_nome(nome: str) -> str:
    partes = (nome or "").split()
    return partes[0].title() if partes else ""


def _linhas_c6(texto: str) -> list[_Lida]:
    lidas = []
    for row in csv.DictReader(io.StringIO(texto), delimiter=";"):
        linha = {_chave(k): (v or "").strip() for k, v in row.items() if k is not None}
        if not any(linha.values()):
            continue
        valor = _valor(linha.get("valor (em r$)", ""))
        if valor is None:
            continue
        dolar = linha.get("valor (em us$)", "")
        try:
            tem_dolar = bool(dolar) and Decimal(dolar.replace(",", ".")) != 0
        except ArithmeticError:
            tem_dolar = False
        categoria = linha.get("categoria", "")
        lidas.append(_Lida(
            data_da_compra=_parse_date(linha.get("data de compra", "")),
            descricao=linha.get("descricao", "") or "Compra no cartão",
            valor_na_fatura=valor,
            parcela=_parcela(linha.get("parcela", ""), _PARCELA_C6),
            portador=_primeiro_nome(linha.get("nome no cartao", "")),
            categoria_do_banco="" if categoria == "-" else categoria,
            dolar=dolar if tem_dolar else "",
        ))
    return lidas


def _linhas_xp(texto: str) -> list[_Lida]:
    lidas = []
    for row in csv.DictReader(io.StringIO(texto), delimiter=";"):
        linha = {_chave(k): (v or "").strip() for k, v in row.items() if k is not None}
        if not any(linha.values()):
            continue
        valor = _valor(linha.get("valor", ""))
        if valor is None:
            continue
        lidas.append(_Lida(
            data_da_compra=_parse_date(linha.get("data", "")),
            descricao=linha.get("estabelecimento", "") or "Compra no cartão",
            valor_na_fatura=valor,
            parcela=_parcela(linha.get("parcela", ""), _PARCELA_XP),
            portador=_primeiro_nome(linha.get("portador", "")),
            categoria_do_banco="",
            dolar="",
        ))
    return lidas


def _fechamento(ultima_data: date, dia_de_fechamento: int | None) -> date:
    """O fechamento desta fatura: o primeiro dia de fechamento a partir da
    última data de lançamento. Sem o dia, a própria data serve."""
    if not dia_de_fechamento:
        return ultima_data
    ano, mes = ultima_data.year, ultima_data.month
    for _ in range(2):
        candidato = date(ano, mes, min(dia_de_fechamento, calendar.monthrange(ano, mes)[1]))
        if candidato >= ultima_data:
            return candidato
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return ultima_data


def _data_da_parcela(data: date, parcela: tuple[int, int] | None, fechamento: date) -> date:
    """Quando a parcela entra na fatura.

    A regra é a data da compra mais N-1 meses: é assim que a compra parcelada
    aparece. Mas nem toda parcela traz a data da compra -- a anuidade da C6
    ("Anuidade Diferenciada 4/12") vem com a data do próprio lançamento. O
    sinal é a conta passar do fechamento desta fatura, o que uma parcela
    cobrada nela não pode fazer; nesse caso, a data do arquivo já é a certa.
    """
    from reports.services import add_months

    if parcela is None:
        return data
    deslocada = add_months(data, parcela[0] - 1)
    return deslocada if deslocada <= fechamento else data


def ler_fatura(raw: bytes, account_id: int, dia_de_fechamento: int | None = None) -> list[ParsedStatementLine]:
    """Converte a fatura em linhas na convenção de sinal da conta-cartão."""
    formato = formato_da_fatura(raw)
    if formato is None:
        raise ValueError(
            "Formato de fatura não reconhecido. São aceitos os CSV de fatura da C6 e da XP, "
            "sem alterar o cabeçalho."
        )
    texto = _texto(raw)
    lidas = _linhas_c6(texto) if formato == FORMATO_C6 else _linhas_xp(texto)
    if len(lidas) > max_statement_rows():
        raise ValueError(f"Fatura excede o limite de {max_statement_rows()} linha(s).")
    if not lidas:
        raise ValueError("Nenhuma linha válida encontrada na fatura.")

    # Quem comprou só vai para a descrição quando a fatura tem mais de um
    # portador: com um só, repetir o nome em toda linha é ruído.
    varios_portadores = len({lida.portador for lida in lidas if lida.portador}) > 1
    # A referência do fechamento é a última data que com certeza é de
    # lançamento: compra à vista, primeira parcela ou pagamento. A data de uma
    # parcela seguinte é a da compra original, e numa fatura só de parcelas
    # antigas (comum num cartão pouco usado) ela puxaria o fechamento para o
    # passado. Sem nenhuma data de lançamento, vale hoje.
    lancadas = [lida.data_da_compra for lida in lidas if lida.parcela is None or lida.parcela[0] == 1]
    fechamento = _fechamento(max(lancadas, default=timezone.localdate()), dia_de_fechamento)
    repeticoes: Counter[tuple] = Counter()
    linhas = []
    for lida in lidas:
        descricao = lida.descricao
        if lida.dolar:
            descricao = f"{descricao} (US$ {lida.dolar})"
        if varios_portadores and lida.portador:
            descricao = f"{descricao} · {lida.portador}"
        descricao = descricao[:255]
        data = _data_da_parcela(lida.data_da_compra, lida.parcela, fechamento)
        valor = -lida.valor_na_fatura
        parcela = f"{lida.parcela[0]}/{lida.parcela[1]}" if lida.parcela else ""
        # Duas compras idênticas no mesmo dia (dois cafés) são duas linhas.
        # O contador entra no hash para as duas sobreviverem, e continua
        # estável: reenviar o mesmo arquivo gera os mesmos hashes.
        chave = (data, descricao, valor, parcela)
        repeticoes[chave] += 1
        identidade = f"{descricao}|{parcela}|{repeticoes[chave]}"
        linhas.append(ParsedStatementLine(
            statement_date=data,
            description=descricao,
            amount=valor,
            line_hash=line_hash(account_id, data, identidade, valor),
            purchase_date=lida.data_da_compra,
            installment_current=lida.parcela[0] if lida.parcela else None,
            installment_total=lida.parcela[1] if lida.parcela else None,
            card_holder=lida.portador[:60],
            bank_category=lida.categoria_do_banco[:100],
        ))
    return linhas


class CartaoCsvAdapter:
    """Adapter das faturas em CSV; o formato vem do cabeçalho."""

    def __init__(self, dia_de_fechamento: int | None = None) -> None:
        self.dia_de_fechamento = dia_de_fechamento

    def parse(self, file: UploadedFile, account_id: int) -> list[ParsedStatementLine]:
        return ler_fatura(read_statement_upload(file, label="CSV"), account_id, self.dia_de_fechamento)
