"""Reclassificação das famílias de lançamento determinísticas, por regra.

Onde a Reclassificação da tela exige marcar lançamento a lançamento, aqui cada
família é descrita uma vez (um trecho do texto, a categoria em que ela está hoje
e a categoria para onde vai) e a simulação mostra quantos lançamentos, quanto
dinheiro e quais meses fechados cada uma toca. A aplicação reaproveita
`reclassificacao.aplicar`: tudo ou nada, mês fechado só com autorização e com o
saldo de fechamento idêntico, auditoria de cada troca.

Duas travas valem para todas as famílias:

- só muda lançamento que está **na categoria de origem** da família. Quem o
  usuário já pôs em outra categoria não é tocado; e a origem "Outros" é o lugar
  de quem ainda não foi classificado;
- o texto casa por **palavra inteira** ("RAIA" não casa com "PRAIA").

A família descreve texto e categoria por nome, nunca ids, para rodar igual em
cada ambiente. A categoria de destino que não existe é criada, já no grupo
indicado, só na aplicação.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db import transaction as db_transaction

from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
)
from transactions.models import (
    AccountMonthClose,
    CashFlowCategory,
    CashFlowCategoryGroup,
    CashFlowEntry,
)

from . import reclassificacao
from .regras import normalizar

_PALAVRAS = re.compile(r"[A-Z0-9]+")


@dataclass(frozen=True)
class Familia:
    nome: str
    # Cada padrão é uma sequência de palavras que precisa aparecer, inteira e
    # em ordem, na descrição; basta um deles. `exato` exige que seja a descrição toda.
    padroes: tuple[str, ...]
    de: tuple[str, ...]
    tipo: str
    para: str
    grupo: str
    exato: bool = False


FAMILIAS: tuple[Familia, ...] = (
    Familia("IRRF das operações de investimento", ("IRRF S",), ("Impostos e Tributos",),
            ENTRY_TYPE_EXPENSE, "IRRF sobre investimentos", "Investimentos"),
    Familia("VA/VR: saída no mercado", ("VALE ALIMENTACAO VALE REFEICAO",), ("Supermercado",),
            ENTRY_TYPE_EXPENSE, "Mercado (VA)", "Alimentação"),
    Familia("VA/VR: entrada na DBR", ("VALE ALIMENTACAO VALE REFEICAO",), ("DBR",),
            ENTRY_TYPE_INCOME, "Benefícios (VA/VR)", "DBR"),
    Familia("Salário", ("SALARIO",), ("DBR",), ENTRY_TYPE_INCOME, "Salário", "DBR", exato=True),
    Familia("Férias e 13º salário", ("PAGAMENTO FERIAS", "FERIAS", "13O SALARIO", "DECIMO TERCEIRO"),
            ("DBR",), ENTRY_TYPE_INCOME, "13º e férias", "DBR"),
    Familia("Reembolsos da DBR", ("REEMBOLSO",), ("DBR",), ENTRY_TYPE_INCOME, "Reembolsos", "DBR"),
    Familia("Despesas reembolsáveis (Uber)", ("UBERRIDES", "UBER TRIP", "UBER UBER"), ("DBR",),
            ENTRY_TYPE_EXPENSE, "Despesas reembolsáveis", "DBR"),
    Familia("Plano de saúde", ("PREVENT",), ("Saúde",), ENTRY_TYPE_EXPENSE, "Plano de saúde", "Saúde"),
    Familia("Farmácia", ("ULTRAFARMA", "DROGARIA", "DROGASIL", "RAIA", "DROGA RAIA"), ("Saúde",),
            ENTRY_TYPE_EXPENSE, "Farmácia", "Saúde"),
    Familia("Bem-estar", ("PILATES",), ("Saúde",), ENTRY_TYPE_EXPENSE, "Bem-estar", "Saúde"),
    Familia("Aluguel recebido do Butantã", ("ALUGUEL BUTANTA",), ("Casa",),
            ENTRY_TYPE_INCOME, "Aluguel Butantã", "Imóvel Butantã"),
    Familia("Remuneração do aluguel de ações", ("TAXA DE REMUNERACAO EMPRESTIMO",), ("Rendimentos",),
            ENTRY_TYPE_INCOME, "Aluguel de Açoes / Dividendos / JCP", "Investimentos"),
    Familia("JCP creditado", ("JCP",), ("Rendimentos",),
            ENTRY_TYPE_INCOME, "Aluguel de Açoes / Dividendos / JCP", "Investimentos"),
    Familia("Compras online (Mercado Livre)", ("MERCADOLIVRE",), ("Outros",),
            ENTRY_TYPE_EXPENSE, "Compras online", "Compras"),
)


def _palavras(texto: str) -> list[str]:
    return _PALAVRAS.findall(normalizar(texto))


def casa(familia: Familia, descricao: str) -> bool:
    """Se a descrição pertence à família (palavra inteira; ver `Familia.padroes`)."""
    palavras = _palavras(descricao)
    for padrao in familia.padroes:
        alvo = _palavras(padrao)
        if familia.exato:
            if palavras == alvo:
                return True
            continue
        tamanho = len(alvo)
        if tamanho and any(palavras[i:i + tamanho] == alvo for i in range(len(palavras) - tamanho + 1)):
            return True
    return False


@dataclass
class Efeito:
    familia: Familia
    entradas: list[CashFlowEntry] = field(default_factory=list)
    categoria_nova: bool = False
    grupo_novo: bool = False
    meses_fechados: int = 0

    @property
    def total(self):
        return sum((e.realized_amount or e.entry_amount for e in self.entradas), 0)


def _gerenciais(user):
    return reclassificacao._gerenciais(user)


def simular(user, familias=FAMILIAS) -> list[Efeito]:
    """O que cada família tocaria, sem gravar nada."""
    efeitos: list[Efeito] = []
    for familia in familias:
        existentes = CashFlowCategory.objects.filter(category_name__iexact=familia.para).first()
        candidatos = (
            _gerenciais(user)
            .filter(category__category_name__in=familia.de, entry_type=familia.tipo)
            .exclude(category__category_name__iexact=familia.para)
            .select_related("category", "account")
            .order_by("due_date", "id")
        )
        entradas = [e for e in candidatos if casa(familia, e.description)]
        efeitos.append(
            Efeito(
                familia=familia,
                entradas=entradas,
                categoria_nova=existentes is None,
                grupo_novo=not CashFlowCategoryGroup.objects.filter(group_name=familia.grupo).exists(),
                meses_fechados=_meses_fechados(entradas),
            )
        )
    return efeitos


def _meses_fechados(entradas) -> int:
    """Quantos meses-conta fechados as entradas tocam (a reclassificação os reabre)."""
    meses = {
        (e.account_id, d.year, d.month) for e in entradas for d in (e.due_date, e.realized_date) if d
    }
    if not meses:
        return 0
    fechados = AccountMonthClose.objects.filter(
        active=True, account_id__in={m[0] for m in meses}
    ).values_list("account_id", "year", "month")
    return len(meses & set(fechados))


def _garantir_categoria(familia: Familia) -> CashFlowCategory:
    grupo, _ = CashFlowCategoryGroup.objects.get_or_create(group_name=familia.grupo)
    categoria = CashFlowCategory.objects.filter(category_name__iexact=familia.para).first()
    if categoria is None:
        categoria = CashFlowCategory.objects.create(
            category_name=familia.para, kind=CATEGORY_KIND_MANAGERIAL, group=grupo
        )
    elif categoria.group_id is None:
        categoria.group = grupo
        categoria.save(update_fields=["group", "updated_at"])
    return categoria


def aplicar(user, familias=FAMILIAS, *, autorizar_meses: bool = False, audit_context=None) -> dict[str, int]:
    """Aplica as famílias, tudo ou nada. Devolve `{nome da família: lançamentos}`."""
    resultado: dict[str, int] = {}
    with db_transaction.atomic():
        for familia in familias:
            categoria = None
            candidatos = (
                _gerenciais(user)
                .filter(category__category_name__in=familia.de, entry_type=familia.tipo)
                .exclude(category__category_name__iexact=familia.para)
                .order_by("due_date", "id")
            )
            ids = [e.id for e in candidatos if casa(familia, e.description)]
            if not ids:
                continue
            categoria = _garantir_categoria(familia)
            resultado[familia.nome] = reclassificacao.aplicar(
                user,
                entry_ids=ids,
                categoria_id=categoria.id,
                incluir_iguais=False,
                autorizar_meses=autorizar_meses,
                audit_context=audit_context,
            )
    return resultado
