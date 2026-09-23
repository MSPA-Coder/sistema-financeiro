"""Reclassificar categorias em lote (Banking > Reclassificação).

A tela existe para a limpeza que a importação de faturas deixa: centenas de
compras em "Outros". Marcar várias e dar a mesma categoria resolve o grosso; o
resto vem de três ampliações, todas mostradas na prévia antes de gravar:

- **iguais**: a mesma descrição depois de tirar o que varia de uma compra
  para outra (portador, valor em dólar, números de pedido). Entram junto, já
  marcadas -- "MERCADOLIVRE*2PRODUTOS" e "MERCADOLIVRE*5PRODUTOS" são a mesma
  loja;
- **parecidas**: começam pela mesma palavra, mas não são iguais ("UBER TRIP" e
  "UBER EATS"). Aparecem para marcar, nunca entram sozinhas;
- **mesma categoria do banco**: a fatura da C6 classifica cada compra
  ("Assistência médica..."). Dizer que uma delas é Saúde pode valer para todas.

Compra parcelada ou recorrente muda inteira: a categoria é da compra, não da
parcela.

Só receita e despesa gerenciais entram. Transferência e movimentação têm outra
ponta ou outro significado, e mudar isso não é trocar de categoria.

Trocar a categoria não move saldo, mas o CB bloqueia qualquer alteração em mês
fechado. Com autorização explícita (e a permissão de fechamento mensal), cada
mês afetado é reaberto com motivo, alterado e fechado de novo; o saldo de
fechamento é recalculado e tem de ser o mesmo, ou nada é gravado. Reabertura,
fechamento e cada troca de categoria ficam na auditoria.

Não há tabela de regras: o importador aprende pelo histórico
(`fatura._categoria_sugerida`), então a reclassificação feita aqui já é a
sugestão das próximas faturas.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction as db_transaction
from django.db.models import Q

from accounts.services import has_function_permission
from banking.models import FinancialAccount
from banking.services import accessible_account_ids
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    OPERATION_INSTALLMENT,
    OPERATION_RECURRING,
)
from reports.services import VIEW_REALIZED, decimal_balance_before
from transactions.models import AccountMonthClose, CashFlowCategory, CashFlowEntry
from transactions.services import close_month, reopen_month

from .models import LINE_STATUS_RECONCILED, BankStatementLine

LIMITE_DA_LISTA = 500
LIMITE_DE_PARECIDAS = 50
MOTIVO_DA_REABERTURA = "Reclassificação de categorias em lote"

_PORTADOR = re.compile(r"\s+·\s+[^·]+$")
_DOLAR = re.compile(r"\(US\$[^)]*\)")
_SEPARADOR = re.compile(r"[^A-Z0-9]+")
_RUIDO = {"PARC", "PARCELA", "PARCELADO", "PARCELAS"}
# Intermediadores de pagamento: a primeira palavra é deles, não da loja, e
# juntaria como "parecidas" compras que não têm nada em comum.
_PREFIXOS_GENERICOS = {"PAGSEGURO", "MERCADOPAGO", "SUMUP", "PAYPAL", "EBANX", "STONE"}


def chave_da_descricao(texto: str) -> str:
    """A descrição sem o que muda de uma compra para outra da mesma loja."""
    limpo = _DOLAR.sub(" ", _PORTADOR.sub("", texto or ""))
    ascii_ = unicodedata.normalize("NFKD", limpo).encode("ascii", "ignore").decode("ascii").upper()
    palavras = [
        p for p in _SEPARADOR.split(ascii_)
        if p and p not in _RUIDO and not any(c.isdigit() for c in p)
    ]
    return " ".join(palavras) or ascii_.strip()


def primeira_palavra(chave: str) -> str | None:
    """A palavra que define "parecida", quando ela diz alguma coisa."""
    palavra = chave.split(" ", 1)[0] if chave else ""
    if len(palavra) < 4 or palavra in _PREFIXOS_GENERICOS:
        return None
    return palavra


def _gerenciais(user):
    return CashFlowEntry.objects.filter(
        account_id__in=accessible_account_ids(user, "update"),
        category__kind=CATEGORY_KIND_MANAGERIAL,
    )


def _importados() -> Q:
    """Lançamento conciliado com linha importada, ou parcela de compra que tem uma."""
    return Q(statement_matches__status=LINE_STATUS_RECONCILED) | Q(
        bank_operation__entries__statement_matches__status=LINE_STATUS_RECONCILED
    )


@dataclass
class Filtros:
    conta_id: int | None = None
    de: date | None = None
    ate: date | None = None
    categoria_id: int | None = None
    texto: str = ""
    categoria_do_banco: str = ""
    so_importados: bool = True

    @classmethod
    def do_get(cls, get) -> Filtros:
        def inteiro(nome):
            valor = (get.get(nome) or "").strip()
            return int(valor) if valor.isdigit() else None

        def data(nome):
            try:
                return date.fromisoformat((get.get(nome) or "").strip())
            except ValueError:
                return None

        return cls(
            conta_id=inteiro("conta"),
            de=data("de"),
            ate=data("ate"),
            categoria_id=inteiro("categoria"),
            texto=(get.get("texto") or "").strip()[:100],
            categoria_do_banco=(get.get("banco") or "").strip()[:100],
            # Checkbox desmarcado não vem no GET; a primeira abertura (sem
            # filtro algum) mostra só importados.
            so_importados=get.get("importados") == "1" or not get,
        )


def categorias_do_banco() -> list[str]:
    return list(
        BankStatementLine.objects.exclude(bank_category="")
        .values_list("bank_category", flat=True).distinct().order_by("bank_category")
    )


def _categoria_do_banco_por_lancamento(ids) -> dict[int, str]:
    """A categoria do banco de cada lançamento; a parcela futura herda a da compra."""
    entradas = dict(CashFlowEntry.objects.filter(id__in=ids).values_list("id", "bank_operation_id"))
    operacoes = {op for op in entradas.values() if op}
    linhas = (
        BankStatementLine.objects.filter(status=LINE_STATUS_RECONCILED)
        .exclude(bank_category="")
        .filter(Q(matched_entry_id__in=entradas) | Q(matched_entry__bank_operation_id__in=operacoes))
        .values_list("matched_entry_id", "matched_entry__bank_operation_id", "bank_category")
    )
    por_lancamento: dict[int, str] = {}
    por_operacao: dict[int, str] = {}
    for entry_id, operacao_id, categoria in linhas:
        por_lancamento[entry_id] = categoria
        if operacao_id:
            por_operacao[operacao_id] = categoria
    return {i: por_lancamento.get(i) or por_operacao.get(op, "") for i, op in entradas.items()}


def lancamentos(user, filtros: Filtros) -> list[CashFlowEntry]:
    """A lista da tela: uma linha por compra (parcelado aparece uma vez)."""
    qs = _gerenciais(user)
    if filtros.conta_id:
        qs = qs.filter(account_id=filtros.conta_id)
    if filtros.de:
        qs = qs.filter(due_date__gte=filtros.de)
    if filtros.ate:
        qs = qs.filter(due_date__lte=filtros.ate)
    if filtros.categoria_id:
        qs = qs.filter(category_id=filtros.categoria_id)
    if filtros.texto:
        qs = qs.filter(description__icontains=filtros.texto)
    if filtros.so_importados:
        qs = qs.filter(_importados())
    if filtros.categoria_do_banco:
        qs = qs.filter(
            Q(statement_matches__bank_category=filtros.categoria_do_banco)
            | Q(bank_operation__entries__statement_matches__bank_category=filtros.categoria_do_banco)
        )
    vistos: set[int] = set()
    lista: list[CashFlowEntry] = []
    for entrada in (
        qs.distinct().select_related("account__owner", "account__institution", "category")
        .order_by("description", "due_date", "id")
    ):
        agrupa = entrada.operation_type in (OPERATION_INSTALLMENT, OPERATION_RECURRING) and entrada.bank_operation_id
        if agrupa:
            if entrada.bank_operation_id in vistos:
                continue
            vistos.add(entrada.bank_operation_id)
        lista.append(entrada)
        if len(lista) >= LIMITE_DA_LISTA:
            break
    bancos = _categoria_do_banco_por_lancamento([e.id for e in lista])
    for entrada in lista:
        entrada.chave = chave_da_descricao(entrada.description)
        entrada.categoria_do_banco = bancos.get(entrada.id, "")
    return lista


@dataclass
class Plano:
    destino: CashFlowCategory
    alvo: list[CashFlowEntry]
    selecionados: int
    iguais: int
    parecidas: list[CashFlowEntry]
    do_banco: dict[str, int]
    meses_fechados: list[tuple[FinancialAccount, int, int]]
    por_chave: list[tuple[str, int]] = field(default_factory=list)


def _ids(valores) -> list[int]:
    return [int(v) for v in valores if str(v).strip().isdigit()]


def _operacoes_inteiras(entradas) -> set[int]:
    ids = {e.id for e in entradas}
    operacoes = {
        e.bank_operation_id for e in entradas
        if e.bank_operation_id and e.operation_type in (OPERATION_INSTALLMENT, OPERATION_RECURRING)
    }
    if operacoes:
        ids |= set(CashFlowEntry.objects.filter(bank_operation_id__in=operacoes).values_list("id", flat=True))
    return ids


def planejar(user, *, entry_ids, categoria_id, incluir_iguais=True, parecidas_ids=(), bancos=()) -> Plano:
    """O que a reclassificação vai mudar, sem gravar nada."""
    try:
        destino = CashFlowCategory.objects.get(id=int(categoria_id), kind=CATEGORY_KIND_MANAGERIAL)
    except (CashFlowCategory.DoesNotExist, TypeError, ValueError) as exc:
        raise ValueError("Escolha a categoria de destino (receita ou despesa).") from exc

    base = _gerenciais(user)
    selecionados = list(base.filter(id__in=_ids(entry_ids)))
    if not selecionados:
        raise ValueError("Selecione ao menos um lançamento.")

    chaves = {chave_da_descricao(e.description) for e in selecionados}
    palavras = {p for p in (primeira_palavra(c) for c in chaves) if p}
    # O banco filtra por um pedaço do texto; a comparação de verdade é pela
    # chave, em Python. Chave sem palavra forte (curta, ou de intermediador)
    # ainda procura iguais pelo primeiro termo.
    termos = palavras | {c.split(" ", 1)[0] for c in chaves if c}
    filtro = Q()
    for termo in termos:
        filtro |= Q(description__icontains=termo)
    candidatos = list(base.filter(filtro).exclude(category=destino)) if termos else []

    iguais = {e.id for e in candidatos if incluir_iguais and chave_da_descricao(e.description) in chaves}
    marcadas = set(_ids(parecidas_ids))
    parecidas_vistas: dict[int, CashFlowEntry] = {}
    for e in candidatos:
        chave = chave_da_descricao(e.description)
        if chave not in chaves and primeira_palavra(chave) in palavras:
            parecidas_vistas.setdefault(e.id, e)

    do_banco_escolhido = {b for b in bancos if b}
    ids_do_banco: set[int] = set()
    if do_banco_escolhido:
        ids_do_banco = set(base.filter(
            Q(statement_matches__bank_category__in=do_banco_escolhido, statement_matches__status=LINE_STATUS_RECONCILED)
        ).values_list("id", flat=True))

    sementes = list(base.filter(
        id__in={e.id for e in selecionados} | iguais | (marcadas & set(parecidas_vistas)) | ids_do_banco
    ))
    alvo = list(
        base.filter(id__in=_operacoes_inteiras(sementes)).exclude(category=destino)
        .select_related("account", "category").order_by("description", "due_date", "id")
    )

    bancos_dos_selecionados = _categoria_do_banco_por_lancamento([e.id for e in selecionados])
    do_banco: dict[str, int] = {}
    for categoria in sorted({c for c in bancos_dos_selecionados.values() if c}):
        do_banco[categoria] = base.filter(
            statement_matches__bank_category=categoria, statement_matches__status=LINE_STATUS_RECONCILED
        ).exclude(category=destino).count()

    meses = sorted({
        (e.account_id, d.year, d.month) for e in alvo for d in (e.due_date, e.realized_date) if d
    })
    fechados = {
        (f.account_id, f.year, f.month): f.account
        for f in AccountMonthClose.objects.filter(active=True, account_id__in={m[0] for m in meses}).select_related("account")
    }
    por_chave: dict[str, int] = {}
    for e in alvo:
        chave = chave_da_descricao(e.description)
        por_chave[chave] = por_chave.get(chave, 0) + 1

    parecidas = sorted(
        (e for i, e in parecidas_vistas.items() if i not in {x.id for x in alvo} or i in marcadas),
        key=lambda e: (e.description, e.due_date),
    )[:LIMITE_DE_PARECIDAS]
    for e in parecidas:
        e.marcada = e.id in marcadas
    return Plano(
        destino=destino,
        alvo=alvo,
        selecionados=len(selecionados),
        iguais=len(iguais - {e.id for e in selecionados}),
        parecidas=parecidas,
        do_banco=do_banco,
        meses_fechados=[(fechados[m], m[1], m[2]) for m in meses if m in fechados],
        por_chave=sorted(por_chave.items(), key=lambda item: (-item[1], item[0])),
    )


def aplicar(user, *, autorizar_meses=False, audit_context=None, **escolhas) -> int:
    """Grava o plano; tudo ou nada. Devolve quantos lançamentos mudaram."""
    from core.services import log_audit_event

    with db_transaction.atomic():
        plano = planejar(user, **escolhas)
        if plano.meses_fechados:
            if not autorizar_meses:
                raise ValueError(
                    "Há meses fechados entre os lançamentos: autorize a reabertura e o novo fechamento."
                )
            if not has_function_permission(user, "settings.monthly_close.manage"):
                raise ValueError("Reabrir mês fechado exige a permissão de fechamento mensal.")
        saldos = {}
        for conta, ano, mes in plano.meses_fechados:
            fechamento = AccountMonthClose.objects.get(account=conta, year=ano, month=mes, active=True)
            saldos[(conta.id, ano, mes)] = (conta, fechamento.closing_balance)
            reopen_month(conta, ano, mes, MOTIVO_DA_REABERTURA, user, audit_context=audit_context)

        for entrada in plano.alvo:
            anterior = entrada.category
            entrada.category = plano.destino
            entrada.save(update_fields=["category", "updated_at"])
            log_audit_event(
                "cash_flow_entry", entrada.id, "update",
                old_values={"category": anterior.category_name},
                new_values={"category": plano.destino.category_name},
                user=user, request_context=audit_context,
                summary="Categoria reclassificada em lote.",
            )

        for (conta_id, ano, mes), (conta, anterior) in saldos.items():
            fim = date(ano + (mes == 12), mes % 12 + 1, 1)
            novo = decimal_balance_before([conta_id], fim, VIEW_REALIZED)
            if novo != anterior:
                raise ValueError(
                    f"O saldo de fechamento de {mes:02d}/{ano} ({conta}) mudaria de {anterior} para {novo}; "
                    "nada foi gravado."
                )
            close_month(conta, ano, mes, novo, user, audit_context=audit_context)
    return len(plano.alvo)
