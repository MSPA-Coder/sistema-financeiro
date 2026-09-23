"""Fatura projetada do cartão: parcelas contratadas mais o gasto novo estimado.

As parcelas futuras já existem como lançamentos na conta do cartão (a
importação da fatura as cria). O que falta é o que ainda não foi comprado.
Esse "gasto novo" é medido nas faturas importadas, de um jeito que não conta
de novo as parcelas: para cada fatura passada, soma-se o que ainda não era
conhecido `k` fechamentos antes dela. Uma parcela N/M passa a ser conhecida
na data em que a 1ª entrou na fatura (a da linha menos N-1 meses); compra à
vista, na data dela. Por isso:

- para a próxima fatura (`k = 1`), o gasto novo é o que foi comprado dentro
  do ciclo -- à vista e 1ª parcelas;
- para faturas mais distantes, entram também as 2ª, 3ª... parcelas de
  compras feitas depois do corte, e o valor cresce com `k`. Uma média única
  subestimaria as faturas mais longe.

A estimativa de cada `k` é a mediana das últimas `JANELA_FATURAS` faturas,
para um mês atípico não puxar o valor. Estornos e pagamentos ficam de fora, e
a anuidade (que vem com a data do lançamento) é tratada como parcela antiga.
`FinancialAccount.card_estimated_spend`, quando preenchido, substitui a
mediana em todos os `k`.

A fatura projetada vira lançamentos, para o fluxo de caixa e a Projeção do
NetWorth enxergarem: uma despesa "Gasto estimado" no cartão, na data do
fechamento, e a transferência do pagamento, da conta de pagamento padrão, no
vencimento. As duas coisas são refeitas inteiras a cada atualização, e são
reconhecidas pelo `operation_key` da `BankOperation` (`PREFIXO_*`). Vão até o
horizonte de projeção de Parâmetros, como as recorrências. Um pagamento que o
usuário já agendou (transferência para o cartão perto do vencimento) é
respeitado: a fatura não ganha outro.
"""

from __future__ import annotations

import calendar
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from banking.models import FinancialAccount
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from reports.services import add_months
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry

from .fatura import PREFIXO_ESTIMATIVA, PREFIXO_PAGAMENTO, eh_pagamento
from .fatura_csv import _fechamento
from .models import BankStatementLine

JANELA_FATURAS = 6
MINIMO_DE_FATURAS = 3
CATEGORIA_ESTIMATIVA = "Cartão de Crédito"
# Um pagamento agendado pelo usuário conta para a fatura se cair entre o
# fechamento e alguns dias depois do vencimento.
_TOLERANCIA_PAGAMENTO = timedelta(days=10)
_CENTAVO = Decimal("0.01")
_ZERO = Decimal("0.00")


def _no_dia(ano: int, mes: int, dia: int) -> date:
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def _fechamento_seguinte(fechamento: date, dia: int) -> date:
    proximo = add_months(date(fechamento.year, fechamento.month, 1), 1)
    return _no_dia(proximo.year, proximo.month, dia)


def _fechamento_anterior(fechamento: date, dia: int) -> date:
    anterior = add_months(date(fechamento.year, fechamento.month, 1), -1)
    return _no_dia(anterior.year, anterior.month, dia)


def vencimento_da_fatura(fechamento: date, dia_de_vencimento: int) -> date:
    """O primeiro dia de vencimento depois do fechamento."""
    no_mes = _no_dia(fechamento.year, fechamento.month, dia_de_vencimento)
    if no_mes > fechamento:
        return no_mes
    proximo = add_months(date(fechamento.year, fechamento.month, 1), 1)
    return _no_dia(proximo.year, proximo.month, dia_de_vencimento)


def _conhecida_desde(linha: BankStatementLine) -> date:
    if linha.installment_current:
        return add_months(linha.statement_date, -(linha.installment_current - 1))
    return linha.statement_date


def gasto_novo(linhas, fechamento: date, k: int) -> Decimal:
    """O que a fatura cobrou e ainda não era conhecido `k` fechamentos antes."""
    corte = add_months(fechamento, -k)
    total = _ZERO
    for linha in linhas:
        if linha.amount < 0 and not eh_pagamento(linha) and _conhecida_desde(linha) > corte:
            total -= linha.amount
    return total


@dataclass(frozen=True)
class FaturaImportada:
    fechamento: date
    linhas: tuple


def faturas_importadas(conta: FinancialAccount) -> list[FaturaImportada]:
    """As faturas do cartão, da mais recente para a mais antiga.

    Cada importação é uma fatura; o fechamento sai das linhas do mesmo jeito
    que na leitura do arquivo. Duas importações com o mesmo fechamento (a
    fatura enviada em partes) são uma fatura só.
    """
    por_lote: dict[int, list[BankStatementLine]] = {}
    for linha in BankStatementLine.objects.filter(account=conta).order_by("import_batch_id", "id"):
        por_lote.setdefault(linha.import_batch_id, []).append(linha)
    por_fechamento: dict[date, list[BankStatementLine]] = {}
    for linhas in por_lote.values():
        lancadas = [
            linha.statement_date for linha in linhas
            if linha.installment_current in (None, 1)
        ]
        if not lancadas:
            continue
        fechamento = _fechamento(max(lancadas), conta.card_closing_day)
        por_fechamento.setdefault(fechamento, []).extend(linhas)
    return [
        FaturaImportada(fechamento, tuple(por_fechamento[fechamento]))
        for fechamento in sorted(por_fechamento, reverse=True)
    ]


@dataclass(frozen=True)
class Estimativa:
    valor: Decimal
    origem: str  # "fixado", "mediana" ou "" (sem base)
    faturas: int


def estimar(conta: FinancialAccount, k: int, faturas: list[FaturaImportada]) -> Estimativa:
    if conta.card_estimated_spend is not None:
        return Estimativa(conta.card_estimated_spend, "fixado", 0)
    base = faturas[:JANELA_FATURAS]
    if len(base) < MINIMO_DE_FATURAS:
        return Estimativa(_ZERO, "", len(base))
    valores = [gasto_novo(fatura.linhas, fatura.fechamento, k) for fatura in base]
    return Estimativa(Decimal(statistics.median(valores)).quantize(_CENTAVO), "mediana", len(base))


@dataclass
class FaturaProjetada:
    k: int
    fechamento: date
    vencimento: date
    lancado: Decimal
    estimativa: Estimativa
    agendado: Decimal
    conta_de_pagamento: FinancialAccount | None

    @property
    def total(self) -> Decimal:
        return self.lancado + self.estimativa.valor

    @property
    def pagamento(self) -> Decimal:
        """O pagamento que a projeção cria; zero quando o usuário já agendou."""
        if self.agendado or self.conta_de_pagamento is None or self.total <= 0:
            return _ZERO
        return self.total


def _projetadas() -> Q:
    """Os lançamentos que a própria projeção criou e que ela vai refazer.

    O realizado não entra: marcado como pago, ele virou fato (ver
    `_apagar_projecao_anterior`) e conta como qualquer outro lançamento.
    """
    return (
        Q(bank_operation__operation_key__startswith=f"{PREFIXO_ESTIMATIVA}:")
        | Q(bank_operation__operation_key__startswith=f"{PREFIXO_PAGAMENTO}:")
    ) & ~Q(status=STATUS_REALIZED)


def _lancado_no_ciclo(conta: FinancialAccount, inicio: date, fim: date) -> Decimal:
    total = _ZERO
    linhas = (
        CashFlowEntry.objects.filter(account=conta, due_date__gt=inicio, due_date__lte=fim)
        .exclude(category__kind=CATEGORY_KIND_TRANSFER)
        .exclude(_projetadas())
        .values_list("entry_type", "entry_amount", "status", "realized_amount")
    )
    for tipo, valor, status, realizado in linhas:
        efetivo = realizado if status == STATUS_REALIZED and realizado is not None else valor
        total += efetivo if tipo == ENTRY_TYPE_EXPENSE else -efetivo
    return total


def _agendado(conta: FinancialAccount, fechamento: date, vencimento: date) -> Decimal:
    total = _ZERO
    for valor in (
        CashFlowEntry.objects.filter(
            account=conta,
            entry_type=ENTRY_TYPE_INCOME,
            category__kind=CATEGORY_KIND_TRANSFER,
            due_date__gt=fechamento,
            due_date__lte=vencimento + _TOLERANCIA_PAGAMENTO,
        )
        .exclude(_projetadas())
        .values_list("entry_amount", flat=True)
    ):
        total += valor
    return total


def planejar(conta: FinancialAccount, *, hoje: date | None = None, fim: date | None = None) -> list[FaturaProjetada]:
    """As faturas que ainda vão vencer, até o horizonte. Não grava nada.

    A contagem de `k` parte da última fatura importada: uma fatura já fechada
    e ainda não importada também é estimada, porque as compras dela ainda
    não estão no sistema.
    """
    from transactions.recurring_projection import recurring_projection_horizon_end

    if not conta.is_credit_card:
        return []
    hoje = hoje or date.today()
    fim = fim or recurring_projection_horizon_end(hoje)
    dia = conta.card_closing_day
    faturas = faturas_importadas(conta)
    base = faturas[0].fechamento if faturas else _fechamento_anterior(_fechamento(hoje, dia), dia)

    planos = []
    anterior, k = base, 1
    while True:
        fechamento = _fechamento_seguinte(anterior, dia)
        vencimento = vencimento_da_fatura(fechamento, conta.card_due_day)
        if vencimento > fim:
            break
        if vencimento >= hoje:
            planos.append(FaturaProjetada(
                k=k,
                fechamento=fechamento,
                vencimento=vencimento,
                lancado=_lancado_no_ciclo(conta, anterior, fechamento),
                estimativa=estimar(conta, k, faturas),
                agendado=_agendado(conta, fechamento, vencimento),
                conta_de_pagamento=conta.card_payment_account,
            ))
        anterior, k = fechamento, k + 1
    return planos


def _categoria_de_transferencia(conta: FinancialAccount) -> CashFlowCategory | None:
    """A categoria dos pagamentos que já chegaram ao cartão; senão, a primeira."""
    usada = (
        CashFlowEntry.objects.filter(account=conta, category__kind=CATEGORY_KIND_TRANSFER)
        .order_by("-due_date")
        .values_list("category_id", flat=True)
        .first()
    )
    if usada:
        return CashFlowCategory.objects.get(id=usada)
    return CashFlowCategory.objects.filter(kind=CATEGORY_KIND_TRANSFER).order_by("id").first()


def _categoria_da_estimativa() -> CashFlowCategory:
    categoria = CashFlowCategory.objects.filter(category_name=CATEGORIA_ESTIMATIVA).first()
    if categoria is None:
        categoria = CashFlowCategory.objects.create(
            category_name=CATEGORIA_ESTIMATIVA, kind=CATEGORY_KIND_MANAGERIAL
        )
    return categoria


def _operacao(chave: str, tipo: str, descricao: str, vencimento: date, quantidade: int) -> BankOperation:
    return BankOperation.objects.create(
        operation_key=chave,
        operation_type=tipo,
        description=descricao[:255],
        status=STATUS_PROJECTED,
        installment_total=1,
        first_due_date=vencimento,
        last_due_date=vencimento,
        entry_count=quantidade,
    )


def _apagar_projecao_anterior(conta: FinancialAccount) -> None:
    operacoes = BankOperation.objects.filter(
        Q(operation_key__startswith=f"{PREFIXO_ESTIMATIVA}:{conta.id}:")
        | Q(operation_key__startswith=f"{PREFIXO_PAGAMENTO}:{conta.id}:")
    )
    # Ocorrência realizada (a pessoa marcou o pagamento projetado como pago)
    # deixa de ser projeção: fica, e a operação com ela.
    linhas = CashFlowEntry.objects.filter(bank_operation__in=operacoes).exclude(status=STATUS_REALIZED)
    linhas.filter(source_entry__isnull=False).delete()
    linhas.delete()
    operacoes.filter(entries__isnull=True).delete()
    # A que sobrou tem ocorrência realizada e deixa de ser da projeção: sai do
    # prefixo, para a próxima atualização não a apagar nem colidir com a chave.
    for operacao in BankOperation.objects.filter(
        Q(operation_key__startswith=f"{PREFIXO_ESTIMATIVA}:{conta.id}:")
        | Q(operation_key__startswith=f"{PREFIXO_PAGAMENTO}:{conta.id}:")
    ):
        operacao.operation_key = f"realizado-{operacao.operation_key}"[:80]
        operacao.entry_count = operacao.entries.count()
        operacao.save(update_fields=["operation_key", "entry_count", "updated_at"])


@transaction.atomic
def atualizar(conta: FinancialAccount, *, hoje: date | None = None, fim: date | None = None) -> list[FaturaProjetada]:
    """Refaz os lançamentos da fatura projetada do cartão."""
    from transactions.services import _account_label

    conta = FinancialAccount.objects.select_for_update(of=("self",)).select_related(
        "card_payment_account__owner", "card_payment_account__institution", "owner", "institution"
    ).get(id=conta.id)
    _apagar_projecao_anterior(conta)
    if not conta.is_credit_card:
        return []
    hoje = hoje or date.today()
    planos = planejar(conta, hoje=hoje, fim=fim)
    if not planos:
        return planos

    categoria = _categoria_da_estimativa()
    transferencia = _categoria_de_transferencia(conta)
    pagadora = conta.card_payment_account
    for plano in planos:
        if plano.estimativa.valor > 0:
            quando = max(plano.fechamento, hoje)
            descricao = (
                "Gasto estimado (valor fixado no cartão)"
                if plano.estimativa.origem == "fixado"
                else f"Gasto estimado (mediana de {plano.estimativa.faturas} faturas)"
            )
            operacao = _operacao(
                f"{PREFIXO_ESTIMATIVA}:{conta.id}:{plano.fechamento.isoformat()}",
                OPERATION_SINGLE, descricao, quando, 1,
            )
            CashFlowEntry.objects.create(
                account=conta, category=categoria, entry_type=ENTRY_TYPE_EXPENSE,
                description=descricao, entry_amount=plano.estimativa.valor,
                due_date=quando, status=STATUS_PROJECTED,
                operation_type=OPERATION_SINGLE, bank_operation=operacao,
            )
        if plano.pagamento > 0 and transferencia is not None:
            operacao = _operacao(
                f"{PREFIXO_PAGAMENTO}:{conta.id}:{plano.vencimento.isoformat()}",
                OPERATION_INTERNAL_TRANSFER,
                f"Fatura projetada {conta.account_name} {plano.vencimento:%d/%m/%Y}",
                plano.vencimento, 2,
            )
            origem = CashFlowEntry.objects.create(
                account=pagadora, category=transferencia, entry_type=ENTRY_TYPE_EXPENSE,
                description=f"Conta Destino: {_account_label(conta)}"[:255],
                entry_amount=plano.pagamento, due_date=plano.vencimento, status=STATUS_PROJECTED,
                operation_type=OPERATION_INTERNAL_TRANSFER, bank_operation=operacao,
            )
            CashFlowEntry.objects.create(
                account=conta, category=transferencia, entry_type=ENTRY_TYPE_INCOME,
                description=f"Conta Origem: {_account_label(pagadora)}"[:255],
                entry_amount=plano.pagamento, due_date=plano.vencimento, status=STATUS_PROJECTED,
                operation_type=OPERATION_INTERNAL_TRANSFER, bank_operation=operacao,
                source_entry=origem,
            )
    return planos


def atualizar_todos(*, hoje: date | None = None, fim: date | None = None) -> int:
    """Refaz a fatura projetada de todos os cartões; devolve quantos."""
    cartoes = list(FinancialAccount.objects.filter(account_kind=ACCOUNT_KIND_CREDIT_CARD).values_list("id", flat=True))
    for cartao_id in cartoes:
        atualizar(FinancialAccount(id=cartao_id), hoje=hoje, fim=fim)
    return len(cartoes)
