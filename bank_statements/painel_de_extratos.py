"""Extratos importados: o que Faturas é para os cartões, para as demais contas.

Por conta que não é cartão (corrente, aplicação), mostra o saldo realizado, os
últimos extratos importados e, em cada um, quantas linhas já foram conciliadas
e o saldo que o arquivo informa contra o do CB. O detalhe de um extrato lista as
linhas do lote com o status de cada uma. Só leitura: importar, conciliar e
desfazer continuam em Importar extratos e faturas e em Conciliação.

Não há "extrato projetado": a fatura projetada existe porque o cartão tem
fechamento e vencimento conhecidos, e a conta corrente não tem nada parecido.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count

from banking.models import FinancialAccount
from banking.services import accessible_account_ids, can_access_account
from core.domain.finance import NON_CARD_ACCOUNT_KINDS, VIEW_REALIZED
from reports.services import decimal_balances_before_by_account

from .models import (
    LINE_STATUS_IGNORED,
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    BankStatementImport,
    BankStatementLine,
)
from .services import ConferenciaDeSaldo

#: Linhas listadas no detalhe de um extrato; acima disso a tela avisa.
LIMITE_DE_LINHAS = 500


@dataclass
class ExtratoImportado:
    """Um lote de extrato com a contagem das linhas por status."""

    lote: BankStatementImport
    novas: int = 0
    conciliadas: int = 0
    ignoradas: int = 0
    conferencia: ConferenciaDeSaldo | None = None

    @property
    def total(self) -> int:
        return self.novas + self.conciliadas + self.ignoradas

    @property
    def em_dia(self) -> bool:
        return self.total > 0 and self.novas == 0


@dataclass
class PainelDaConta:
    """O que Extratos importados mostra de uma conta."""

    conta: FinancialAccount
    saldo: Decimal
    extratos: list[ExtratoImportado]


def _contagens(lote_ids: list[int]) -> dict[int, dict[str, int]]:
    contagens: dict[int, dict[str, int]] = defaultdict(dict)
    for linha in (
        BankStatementLine.objects.filter(import_batch_id__in=lote_ids)
        .values("import_batch_id", "status")
        .annotate(n=Count("id"))
    ):
        contagens[linha["import_batch_id"]][linha["status"]] = linha["n"]
    return contagens


def _conferencias(lotes: list[BankStatementImport]) -> dict[int, ConferenciaDeSaldo]:
    """A conferência de cada lote que traz saldo, com uma consulta por data distinta.

    Os extratos costumam informar o saldo do fim do mês, então as datas se
    repetem entre contas e o custo não cresce com o número de contas."""
    por_data: dict[date, list[BankStatementImport]] = defaultdict(list)
    for lote in lotes:
        if lote.statement_balance is not None and lote.statement_balance_date is not None:
            por_data[lote.statement_balance_date].append(lote)
    conferencias = {}
    for data, do_dia in por_data.items():
        saldos = decimal_balances_before_by_account(
            list({lote.account_id for lote in do_dia}), data + timedelta(days=1), VIEW_REALIZED
        )
        for lote in do_dia:
            conferencias[lote.id] = ConferenciaDeSaldo(
                saldo_do_extrato=lote.statement_balance,
                data=data,
                saldo_no_cb=saldos.get(lote.account_id, Decimal("0.00")),
            )
    return conferencias


def _importados(lotes: list[BankStatementImport]) -> dict[int, ExtratoImportado]:
    contagens = _contagens([lote.id for lote in lotes])
    conferencias = _conferencias(lotes)
    return {
        lote.id: ExtratoImportado(
            lote=lote,
            novas=contagens[lote.id].get(LINE_STATUS_NEW, 0),
            conciliadas=contagens[lote.id].get(LINE_STATUS_RECONCILED, 0),
            ignoradas=contagens[lote.id].get(LINE_STATUS_IGNORED, 0),
            conferencia=conferencias.get(lote.id),
        )
        for lote in lotes
    }


def paineis(user, *, hoje: date | None = None, extratos: int = 6) -> list[PainelDaConta]:
    """As contas (que não são cartão) que `user` enxerga, com saldo e últimos extratos."""
    hoje = hoje or date.today()
    contas = list(
        FinancialAccount.objects.filter(
            id__in=accessible_account_ids(user, "view"), account_kind__in=NON_CARD_ACCOUNT_KINDS
        )
        .select_related("owner", "institution")
        .order_by("owner__name", "institution__institution_name", "account_name")
    )
    if not contas:
        return []
    saldos = decimal_balances_before_by_account(
        [conta.id for conta in contas], hoje + timedelta(days=1), VIEW_REALIZED
    )
    por_conta: dict[int, list[BankStatementImport]] = defaultdict(list)
    for lote in BankStatementImport.objects.filter(account_id__in=[c.id for c in contas]).order_by("-id"):
        if len(por_conta[lote.account_id]) < extratos:
            por_conta[lote.account_id].append(lote)
    importados = _importados([lote for lotes in por_conta.values() for lote in lotes])
    return [
        PainelDaConta(
            conta=conta,
            saldo=saldos.get(conta.id, Decimal("0.00")),
            extratos=[importados[lote.id] for lote in por_conta[conta.id]],
        )
        for conta in contas
    ]


def extrato_do_lote(user, batch_id) -> ExtratoImportado:
    """O lote, se não for de cartão e estiver ao alcance de `user`."""
    try:
        lote = BankStatementImport.objects.select_related("account__owner", "account__institution").get(id=batch_id)
    except (BankStatementImport.DoesNotExist, ValueError, TypeError) as exc:
        raise ValueError("Importação não encontrada.") from exc
    if not can_access_account(user, lote.account_id, "view"):
        raise ValueError("Importação não encontrada.")
    if lote.account.is_credit_card:
        raise ValueError("Esta importação é de um cartão de crédito: veja em Faturas.")
    return _importados([lote])[lote.id]


def linhas_do_extrato(lote: BankStatementImport) -> list[BankStatementLine]:
    return list(lote.lines.order_by("statement_date", "id")[:LIMITE_DE_LINHAS])
