"""Processar a fatura de cartão importada (Bancos > Importações > fatura).

A importação só grava as linhas da fatura (`fatura_csv`); nenhum lançamento
nasce nela. Esta etapa decide o que cada linha vira e mostra a decisão numa
prévia antes de gravar. A mesma função (`planejar`) monta a prévia e o
processamento, então o que a tela mostra é o que acontece.

O que cada linha vira:

- **já no saldo inicial**: linha com data anterior à do saldo inicial do
  cartão. O saldo do CB soma todos os lançamentos, inclusive os anteriores a
  essa data; lançar a linha contaria a dívida duas vezes. Ela é ignorada. Se
  for uma parcela, as parcelas seguintes ainda não estão no saldo e são
  criadas como "a vencer";
- **pagamento**: crédito cuja descrição é de pagamento de fatura. Casa com a
  transferência que chega ao cartão (mesmo valor, até 10 dias de distância) e
  a realiza, com a ponta da conta que pagou. Sem transferência, a linha fica
  pendente em Bancos > Conciliação: o pagamento sai de uma conta que só você
  sabe qual é, e pode ter sido dividido;
- **parcela já lançada**: parcela de uma compra que já tem parcelado no
  cartão. Casa pela descrição e pela data (até 5 dias), não pelo número: o CB
  renumera as parcelas quando o grupo é editado ou tem uma parcela excluída;
- **compra parcelada nova**: cria as parcelas N a M e realiza a N;
- **compra já lançada**: despesa avulsa lançada à mão no cartão, com o mesmo
  valor e até 3 dias de distância. É conciliada em vez de duplicada;
- **compra** e **estorno**: criam a despesa ou a receita, já realizadas.

A categoria sugerida vem, nesta ordem, da última compra com a mesma descrição
no cartão, da categoria do banco quando ela tem o nome de uma categoria
cadastrada, e de "Outros". A prévia deixa trocar cada uma.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Q

from banking.models import FinancialAccount
from banking.services import can_access_account
from core.domain.finance import (
    CALC_REPEAT,
    CATEGORY_KIND_MANAGERIAL,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INSTALLMENT,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_SINGLE,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import (
    TransactionRequest,
    assert_entry_period_open,
    create_transaction_batch,
    realize_transaction,
)

from .models import (
    LINE_STATUS_IGNORED,
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    STATUS_PROCESSED,
    BankStatementImport,
    BankStatementLine,
)
from .reconciliation import _create_entry_from_locked_line

SALDO_INICIAL = "saldo_inicial"
PAGAMENTO = "pagamento"
PAGAMENTO_SEM_PAR = "pagamento_sem_par"
PARCELA_LANCADA = "parcela_lancada"
PARCELADO_NOVO = "parcelado_novo"
COMPRA_LANCADA = "compra_lancada"
COMPRA = "compra"
ESTORNO = "estorno"

_CRIAM_LANCAMENTO = (PARCELADO_NOVO, COMPRA, ESTORNO)

_JANELA_PAGAMENTO = timedelta(days=10)
_JANELA_PARCELA = timedelta(days=5)
_JANELA_COMPRA = timedelta(days=3)

# "Pag Fatura Boleto" (C6), "Pagamentos Validos Normais" (XP). A fronteira de
# palavra impede "Pague Menos" de virar pagamento.
_PAGAMENTO = re.compile(r"^pag(amentos?)?\b", re.IGNORECASE)
_CATEGORIA_PADRAO = "Outros"


@dataclass
class Plano:
    """O que uma linha da fatura vira."""

    linha: BankStatementLine
    acao: str
    lancamento: CashFlowEntry | None = None
    categoria: CashFlowCategory | None = None
    # Linha já no saldo inicial que é parcela: a partir de qual parcela criar.
    futuras_a_partir_de: int | None = None
    futuras_vencimento: date | None = None

    @property
    def cria_lancamento(self) -> bool:
        return self.acao in _CRIAM_LANCAMENTO

    @property
    def rotulo(self) -> str:
        linha = self.linha
        if self.acao == SALDO_INICIAL:
            if self.futuras_a_partir_de:
                return (
                    f"Já está no saldo inicial; cria as parcelas {self.futuras_a_partir_de} "
                    f"a {linha.installment_total} a vencer"
                )
            return "Já está no saldo inicial"
        if self.acao == PAGAMENTO:
            return f"Pagamento: concilia com a transferência de {self.lancamento.due_date:%d/%m/%Y}"
        if self.acao == PAGAMENTO_SEM_PAR:
            return "Pagamento sem transferência correspondente: fica pendente em Conciliação"
        if self.acao == PARCELA_LANCADA:
            return f"Parcela já lançada (vencimento {self.lancamento.due_date:%d/%m/%Y})"
        if self.acao == PARCELADO_NOVO:
            return (
                f"Compra parcelada nova: cria as parcelas {linha.installment_current} "
                f"a {linha.installment_total}"
            )
        if self.acao == COMPRA_LANCADA:
            return f"Compra já lançada em {self.lancamento.due_date:%d/%m/%Y}: concilia"
        if self.acao == ESTORNO:
            return "Estorno ou crédito"
        return "Compra"


def eh_pagamento(linha: BankStatementLine) -> bool:
    return linha.amount > 0 and bool(_PAGAMENTO.match(linha.description.strip()))


def _descricoes(linha: BankStatementLine) -> list[str]:
    """A descrição com e sem o portador: a fatura de outro mês pode ter um só."""
    sufixo = f" · {linha.card_holder}" if linha.card_holder else ""
    base = linha.description.removesuffix(sufixo) if sufixo else linha.description
    return [base, f"{base}{sufixo}"] if sufixo else [base]


def _conciliados() -> Q:
    """Lançamentos que já têm linha de extrato conciliada."""
    return Q(statement_matches__status=LINE_STATUS_RECONCILED)


def _mais_perto(candidatos, data: date, usados: set[int], preferir=None) -> CashFlowEntry | None:
    livres = [c for c in candidatos if c.id not in usados]
    if not livres:
        return None
    return min(livres, key=lambda c: (0 if preferir and preferir(c) else 1, abs((c.due_date - data).days), c.id))


def _transferencia(conta, linha, usados) -> CashFlowEntry | None:
    # Lançamento realizado à mão também serve: a conciliação só o vincula.
    candidatos = CashFlowEntry.objects.filter(
        account=conta,
        entry_type=ENTRY_TYPE_INCOME,
        operation_type=OPERATION_INTERNAL_TRANSFER,
        entry_amount=abs(linha.amount),
        due_date__range=(linha.statement_date - _JANELA_PAGAMENTO, linha.statement_date + _JANELA_PAGAMENTO),
    ).exclude(_conciliados())
    return _mais_perto(candidatos, linha.statement_date, usados)


def _parcela(conta, linha, data, usados, *, livres=True) -> CashFlowEntry | None:
    candidatos = CashFlowEntry.objects.filter(
        account=conta,
        entry_type=ENTRY_TYPE_EXPENSE,
        operation_type=OPERATION_INSTALLMENT,
        description__in=_descricoes(linha),
        due_date__range=(data - _JANELA_PARCELA, data + _JANELA_PARCELA),
    )
    if livres:
        candidatos = candidatos.exclude(_conciliados())
    valor = abs(linha.amount)
    return _mais_perto(candidatos, data, usados, preferir=lambda c: c.entry_amount == valor)


def _compra_lancada(conta, linha, usados) -> CashFlowEntry | None:
    candidatos = CashFlowEntry.objects.filter(
        account=conta,
        entry_type=ENTRY_TYPE_EXPENSE if linha.amount < 0 else ENTRY_TYPE_INCOME,
        operation_type=OPERATION_SINGLE,
        is_recurring=False,
        entry_amount=abs(linha.amount),
        due_date__range=(linha.statement_date - _JANELA_COMPRA, linha.statement_date + _JANELA_COMPRA),
    ).exclude(_conciliados())
    return _mais_perto(candidatos, linha.statement_date, usados)


def _categoria_sugerida(conta, linha) -> CashFlowCategory | None:
    anterior = (
        CashFlowEntry.objects.filter(
            account=conta,
            description__in=_descricoes(linha),
            category__kind=CATEGORY_KIND_MANAGERIAL,
        )
        .select_related("category")
        .order_by("-due_date", "-id")
        .first()
    )
    if anterior is not None:
        return anterior.category
    gerenciais = CashFlowCategory.objects.filter(kind=CATEGORY_KIND_MANAGERIAL)
    if linha.bank_category:
        do_banco = gerenciais.filter(category_name__iexact=linha.bank_category).first()
        if do_banco is not None:
            return do_banco
    return gerenciais.filter(category_name__iexact=_CATEGORIA_PADRAO).first()


def planejar(conta: FinancialAccount, linhas) -> list[Plano]:
    """Decide o destino de cada linha nova, sem gravar nada."""
    from reports.services import add_months

    planos: list[Plano] = []
    usados: set[int] = set()
    for linha in sorted(linhas, key=lambda item: (item.statement_date, item.id)):
        plano = Plano(linha=linha, acao=COMPRA)
        antes_do_saldo = linha.statement_date < conta.initial_balance_date
        if antes_do_saldo:
            plano.acao = SALDO_INICIAL
            atual, total = linha.installment_current, linha.installment_total
            # Só as parcelas de depois do saldo inicial: as de antes estão em
            # faturas que ele já cobre.
            primeira = None
            if atual and atual < total:
                primeira = next(
                    (
                        k for k in range(atual + 1, total + 1)
                        if add_months(linha.statement_date, k - atual) >= conta.initial_balance_date
                    ),
                    None,
                )
            if primeira is not None:
                vencimento = add_months(linha.statement_date, primeira - atual)
                if _parcela(conta, linha, vencimento, usados, livres=False) is None:
                    plano.futuras_a_partir_de = primeira
                    plano.futuras_vencimento = vencimento
                    plano.categoria = _categoria_sugerida(conta, linha)
        elif eh_pagamento(linha):
            plano.lancamento = _transferencia(conta, linha, usados)
            plano.acao = PAGAMENTO if plano.lancamento else PAGAMENTO_SEM_PAR
        elif linha.installment_current and linha.amount < 0:
            plano.lancamento = _parcela(conta, linha, linha.statement_date, usados)
            plano.acao = PARCELA_LANCADA if plano.lancamento else PARCELADO_NOVO
        else:
            plano.lancamento = _compra_lancada(conta, linha, usados)
            if plano.lancamento:
                plano.acao = COMPRA_LANCADA
            else:
                plano.acao = ESTORNO if linha.amount > 0 else COMPRA
        if plano.cria_lancamento:
            plano.categoria = _categoria_sugerida(conta, linha)
        if plano.lancamento is not None:
            usados.add(plano.lancamento.id)
        planos.append(plano)
    return planos


@dataclass
class Resumo:
    compras: Decimal
    creditos: Decimal
    pagamentos: Decimal

    @property
    def total_da_fatura(self) -> Decimal:
        """O que a fatura cobra: compras menos estornos, sem os pagamentos."""
        return -(self.compras + self.creditos)


def resumir(linhas) -> Resumo:
    compras = creditos = pagamentos = Decimal("0.00")
    for linha in linhas:
        if linha.amount < 0:
            compras += linha.amount
        elif eh_pagamento(linha):
            pagamentos += linha.amount
        else:
            creditos += linha.amount
    return Resumo(compras=compras, creditos=creditos, pagamentos=pagamentos)


def lote_de_fatura(user, batch_id, action: str = "view") -> BankStatementImport:
    """O lote, se for de cartão e estiver ao alcance de `user`."""
    try:
        lote = BankStatementImport.objects.select_related("account__owner", "account__institution").get(id=batch_id)
    except (BankStatementImport.DoesNotExist, ValueError, TypeError) as exc:
        raise ValueError("Importação não encontrada.") from exc
    if not can_access_account(user, lote.account_id, action):
        raise ValueError("Importação não encontrada.")
    if not lote.account.is_credit_card:
        raise ValueError("Esta importação não é de um cartão de crédito.")
    return lote


def linhas_novas(lote: BankStatementImport):
    return lote.lines.filter(status=LINE_STATUS_NEW).order_by("statement_date", "id")


def categorias_gerenciais():
    return CashFlowCategory.objects.filter(kind=CATEGORY_KIND_MANAGERIAL).order_by("category_name")


def _categoria_escolhida(plano: Plano, escolhas: dict) -> CashFlowCategory:
    escolhida = escolhas.get(str(plano.linha.id)) or escolhas.get(plano.linha.id)
    if escolhida:
        try:
            return CashFlowCategory.objects.get(id=int(escolhida), kind=CATEGORY_KIND_MANAGERIAL)
        except (CashFlowCategory.DoesNotExist, ValueError, TypeError) as exc:
            raise ValueError(f"Categoria inválida para \"{plano.linha.description}\".") from exc
    if plano.categoria is None:
        raise ValueError(
            f"Sem categoria para \"{plano.linha.description}\": escolha uma ou cadastre \"Outros\"."
        )
    return plano.categoria


def _conciliar(user, linha: BankStatementLine, lancamento: CashFlowEntry, audit_context) -> None:
    assert_entry_period_open(lancamento, action_label="conciliar")
    if lancamento.status != STATUS_REALIZED:
        realize_transaction(
            lancamento,
            realized_date=linha.statement_date,
            realized_amount=abs(linha.amount),
            audit_context=audit_context,
            user=user,
        )
    linha.matched_entry = lancamento
    linha.status = LINE_STATUS_RECONCILED
    linha.save(update_fields=["matched_entry", "status", "updated_at"])


def _criar_parcelas(user, conta, linha, categoria, *, primeira: int, vencimento: date,
                    realizar: bool, audit_context) -> list[CashFlowEntry]:
    valor = abs(linha.amount)
    return create_transaction_batch(
        TransactionRequest(
            account_id=conta.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description=linha.description,
            entry_amount=valor,
            installments=linha.installment_total,
            due_date=vencimento,
            calc_mode=CALC_REPEAT,
            status=STATUS_REALIZED if realizar else STATUS_PROJECTED,
            realized_date=linha.statement_date if realizar else None,
            realized_amount=valor if realizar else None,
            first_installment=primeira,
        ),
        audit_context=audit_context,
        user=user,
    )


def processar(user, batch_id, escolhas: dict, audit_context=None) -> dict[str, int]:
    """Aplica o plano às linhas novas do lote, tudo ou nada.

    `escolhas` mapeia o id da linha para o id da categoria escolhida na prévia;
    linha sem escolha usa a sugerida. Devolve a contagem por ação.
    """
    lote = lote_de_fatura(user, batch_id, "update")
    conta = lote.account
    if not can_access_account(user, conta.id, "create"):
        raise ValueError("Acesso negado para lançar neste cartão.")

    contagem: dict[str, int] = {}
    with db_transaction.atomic():
        linhas = list(linhas_novas(lote).select_for_update())
        if not linhas:
            raise ValueError("Esta fatura não tem linhas pendentes.")
        for plano in planejar(conta, linhas):
            linha = plano.linha
            if plano.acao == SALDO_INICIAL:
                if plano.futuras_a_partir_de:
                    _criar_parcelas(
                        user, conta, linha, _categoria_escolhida(plano, escolhas),
                        primeira=plano.futuras_a_partir_de,
                        vencimento=plano.futuras_vencimento,
                        realizar=False, audit_context=audit_context,
                    )
                linha.status = LINE_STATUS_IGNORED
                linha.save(update_fields=["status", "updated_at"])
            elif plano.acao in (PAGAMENTO, PARCELA_LANCADA, COMPRA_LANCADA):
                _conciliar(user, linha, plano.lancamento, audit_context)
            elif plano.acao == PARCELADO_NOVO:
                criadas = _criar_parcelas(
                    user, conta, linha, _categoria_escolhida(plano, escolhas),
                    primeira=linha.installment_current, vencimento=linha.statement_date,
                    realizar=True, audit_context=audit_context,
                )
                linha.matched_entry = criadas[0]
                linha.status = LINE_STATUS_RECONCILED
                linha.save(update_fields=["matched_entry", "status", "updated_at"])
            elif plano.acao in (COMPRA, ESTORNO):
                _create_entry_from_locked_line(
                    user, line=linha, category_id=_categoria_escolhida(plano, escolhas).id,
                    audit_context=audit_context,
                )
            contagem[plano.acao] = contagem.get(plano.acao, 0) + 1

        if not linhas_novas(lote).exists():
            lote.status = STATUS_PROCESSED
            lote.save(update_fields=["status", "updated_at"])
    return contagem
