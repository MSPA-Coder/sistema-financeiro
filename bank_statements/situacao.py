"""Situação das Contas: que conta tem dados importados e conciliados em que mês.

Uma matriz conta × mês. O mês de uma célula é o da data da linha do extrato ou
da fatura (`BankStatementLine.statement_date`), não o da importação: importar em
outubro o extrato de setembro preenche a coluna de setembro.

Estados de cada célula, do pior ao melhor:

- **sem importação**: nenhuma linha de extrato ou fatura no mês;
- **com pendências**: há linhas, e alguma ainda está "nova" (nem conciliada nem
  ignorada);
- **conciliado**: há linhas e nenhuma está pendente;
- **saldo informado**: sem linhas, mas o saldo foi atualizado em Saldo Aplicações
  no mês. É o que resta de uma aplicação (CDB, cofrinho) cujo extrato não traz
  linha a linha;
- **não se aplica**: sem linhas e o mês termina antes do saldo inicial da conta.
  Quando há linhas, vale o que elas dizem, mesmo antes do saldo inicial: a fatura
  de cartão anterior a ele é importada e ignorada.

O resumo conta, para um mês de referência, as contas a que o mês se aplica.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Count
from django.db.models.functions import TruncMonth

from banking.models import FinancialAccount
from banking.services import accessible_account_ids
from core.domain.finance import STATUS_REALIZED
from transactions.models import CashFlowEntry

from .models import LINE_STATUS_NEW, BankStatementLine

SEM_IMPORTACAO = "sem_importacao"
COM_PENDENCIAS = "com_pendencias"
CONCILIADO = "conciliado"
SALDO_INFORMADO = "saldo_informado"
NAO_SE_APLICA = "nao_se_aplica"

#: estado -> (ícone, rótulo). O ícone vai junto da cor: o estado não depende dela.
ROTULOS = {
    SEM_IMPORTACAO: ("✖", "Sem importação"),
    COM_PENDENCIAS: ("⚠", "Com pendências"),
    CONCILIADO: ("✔", "Conciliado"),
    SALDO_INFORMADO: ("✔", "Saldo informado"),
    NAO_SE_APLICA: ("—", "Não se aplica"),
}
ESTADOS_EM_ORDEM = (CONCILIADO, SALDO_INFORMADO, COM_PENDENCIAS, SEM_IMPORTACAO, NAO_SE_APLICA)
#: Estados que contam como "em dia" no resumo.
EM_DIA = (CONCILIADO, SALDO_INFORMADO)

PREFIXO_DO_SALDO_INFORMADO = "Atualização de saldo"  # ver `saldo.aplicar`
MESES_PERMITIDOS = (3, 6, 12)
MESES_PADRAO = 6


def classificar(*, linhas: int, novas: int, saldo_informado: bool, antes_do_saldo_inicial: bool) -> str:
    if linhas:
        return COM_PENDENCIAS if novas else CONCILIADO
    if antes_do_saldo_inicial:
        return NAO_SE_APLICA
    return SALDO_INFORMADO if saldo_informado else SEM_IMPORTACAO


def primeiro_dia_do_mes_seguinte(dia: date) -> date:
    return date(dia.year + (dia.month == 12), dia.month % 12 + 1, 1)


def meses_ate(hoje: date, quantos: int) -> list[date]:
    """Os primeiros dias dos `quantos` últimos meses, o mês de `hoje` por último."""
    ano, mes = hoje.year, hoje.month
    meses = []
    for _ in range(quantos):
        meses.append(date(ano, mes, 1))
        ano, mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    return meses[::-1]


def _mes_anterior(hoje: date) -> date:
    return (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)


@dataclass
class Celula:
    mes: date
    estado: str
    linhas: int = 0
    novas: int = 0
    lote_id: int | None = None  # o lote mais recente com linhas no mês
    url: str = ""  # para onde a célula leva; quem monta a tela preenche

    @property
    def icone(self) -> str:
        return ROTULOS[self.estado][0]

    @property
    def rotulo(self) -> str:
        return ROTULOS[self.estado][1]

    @property
    def detalhe(self) -> str:
        if self.estado == COM_PENDENCIAS:
            return f"{self.linhas - self.novas} de {self.linhas} conciliadas"
        if self.estado == CONCILIADO:
            return f"{self.linhas} linha{'s' if self.linhas != 1 else ''}"
        return ""

    @property
    def descricao(self) -> str:
        return f"{self.rotulo}: {self.detalhe}" if self.detalhe else self.rotulo


@dataclass
class Linha:
    conta: FinancialAccount
    celulas: list[Celula]


@dataclass
class Resumo:
    mes: date
    por_estado: dict[str, int] = field(default_factory=dict)

    @property
    def aplicaveis(self) -> int:
        return sum(n for estado, n in self.por_estado.items() if estado != NAO_SE_APLICA)

    @property
    def em_dia(self) -> int:
        return sum(self.por_estado.get(estado, 0) for estado in EM_DIA)

    @property
    def pendentes(self) -> int:
        return self.por_estado.get(COM_PENDENCIAS, 0)

    @property
    def sem_importacao(self) -> int:
        return self.por_estado.get(SEM_IMPORTACAO, 0)

    @property
    def percentual(self) -> int:
        return round(100 * self.em_dia / self.aplicaveis) if self.aplicaveis else 0


@dataclass
class Matriz:
    meses: list[date]
    referencia: date
    hoje: date
    linhas: list[Linha]
    resumo: Resumo


def _linhas_por_conta_e_mes(ids: list[int], inicio: date, fim: date) -> dict[tuple[int, date], dict]:
    celulas: dict[tuple[int, date], dict] = defaultdict(lambda: {"linhas": 0, "novas": 0, "lote": 0})
    consulta = (
        BankStatementLine.objects.filter(account_id__in=ids, statement_date__gte=inicio, statement_date__lt=fim)
        .annotate(mes=TruncMonth("statement_date"))
        .values("account_id", "mes", "import_batch_id", "status")
        .annotate(n=Count("id"))
    )
    for linha in consulta:
        celula = celulas[(linha["account_id"], linha["mes"])]
        celula["linhas"] += linha["n"]
        if linha["status"] == LINE_STATUS_NEW:
            celula["novas"] += linha["n"]
        celula["lote"] = max(celula["lote"], linha["import_batch_id"])
    return celulas


def _saldos_informados(ids: list[int], inicio: date, fim: date) -> set[tuple[int, date]]:
    return {
        (linha["account_id"], linha["mes"])
        for linha in CashFlowEntry.objects.filter(
            account_id__in=ids,
            status=STATUS_REALIZED,
            description__startswith=PREFIXO_DO_SALDO_INFORMADO,
            due_date__gte=inicio,
            due_date__lt=fim,
        )
        .annotate(mes=TruncMonth("due_date"))
        .values("account_id", "mes")
        .distinct()
    }


def montar(user, *, quantos: int = MESES_PADRAO, referencia: date | None = None, hoje: date | None = None) -> Matriz:
    """A matriz das contas de `user` nos últimos `quantos` meses.

    `referencia` (qualquer dia do mês) é o mês que o resumo conta. Sem ela, é o mês
    anterior ao atual: o do corrente ainda está em andamento e os extratos só saem
    depois que ele acaba."""
    hoje = hoje or date.today()
    quantos = quantos if quantos in MESES_PERMITIDOS else MESES_PADRAO
    meses = meses_ate(hoje, quantos)
    referencia = referencia.replace(day=1) if referencia else _mes_anterior(hoje)
    if referencia not in meses:
        referencia = meses[-2] if len(meses) > 1 else meses[-1]

    contas = list(
        FinancialAccount.objects.filter(id__in=accessible_account_ids(user, "view"))
        .select_related("owner", "institution")
        .order_by("owner__name", "institution__institution_name", "account_name")
    )
    ids = [conta.id for conta in contas]
    inicio, fim = meses[0], primeiro_dia_do_mes_seguinte(meses[-1])
    com_linhas = _linhas_por_conta_e_mes(ids, inicio, fim) if ids else {}
    informados = _saldos_informados(ids, inicio, fim) if ids else set()

    linhas = []
    resumo = Resumo(mes=referencia)
    for conta in contas:
        celulas = []
        for mes in meses:
            dados = com_linhas.get((conta.id, mes))
            estado = classificar(
                linhas=dados["linhas"] if dados else 0,
                novas=dados["novas"] if dados else 0,
                saldo_informado=(conta.id, mes) in informados,
                antes_do_saldo_inicial=primeiro_dia_do_mes_seguinte(mes) - timedelta(days=1) < conta.initial_balance_date,
            )
            celulas.append(Celula(
                mes=mes, estado=estado,
                linhas=dados["linhas"] if dados else 0,
                novas=dados["novas"] if dados else 0,
                lote_id=dados["lote"] if dados else None,
            ))
            if mes == referencia:
                resumo.por_estado[estado] = resumo.por_estado.get(estado, 0) + 1
        linhas.append(Linha(conta=conta, celulas=celulas))
    return Matriz(meses=meses, referencia=referencia, hoje=hoje, linhas=linhas, resumo=resumo)
