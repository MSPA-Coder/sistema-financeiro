"""Filtro request-scoped de grupos de conta: bancos, corretoras, cartões,
aplicações e administradas.

Os grupos são uma PARTIÇÃO das contas: cada conta cai em exatamente um.
É isso que deixa uma seleção múltipla legível -- marcar "Bancos" e desmarcar
"Cartões" não deixa dúvida sobre o cartão emitido por um banco. Por isso a
finalidade vence o tipo da conta, e o tipo da conta vence o tipo da instituição:

- conta administrada (dinheiro de terceiros que o titular só gere) ->
  Administradas, seja qual for o tipo;
- cartão de crédito -> Cartões, seja qual for a instituição;
- aplicação -> Aplicações, idem;
- conta comum -> Bancos ou Corretoras, pelo tipo da instituição.

Como a moeda, o filtro vive na URL (`grupos=bancos,cartoes`) e não é gravado.
Ausente, valem todos **menos Administradas**: o dinheiro de terceiros não entra
no fluxo pessoal sem que o usuário peça. Marcar só "Administradas" mostra o
resultado delas sozinho, e marcar todas mostra tudo junto.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from django.core.exceptions import SuspiciousOperation
from django.db.models import Q

from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    ACCOUNT_KIND_REGULAR,
    ACCOUNT_PURPOSE_ADMINISTERED,
    ACCOUNT_PURPOSE_PERSONAL,
)

GROUPS_PARAM = "grupos"

GROUP_BANKS = "bancos"
GROUP_BROKERS = "corretoras"
GROUP_CARDS = "cartoes"
GROUP_INVESTMENTS = "aplicacoes"
GROUP_ADMINISTERED = "administradas"
# A ordem é a da tela.
ACCOUNT_GROUP_OPTIONS = (
    (GROUP_BANKS, "Bancos"),
    (GROUP_BROKERS, "Corretoras"),
    (GROUP_CARDS, "Cartões"),
    (GROUP_INVESTMENTS, "Aplicações"),
    (GROUP_ADMINISTERED, "Administradas"),
)
ALL_ACCOUNT_GROUPS = frozenset(code for code, _label in ACCOUNT_GROUP_OPTIONS)
# O que vale quando a URL não diz nada: tudo, menos o dinheiro de terceiros.
DEFAULT_ACCOUNT_GROUPS = ALL_ACCOUNT_GROUPS - {GROUP_ADMINISTERED}

# Valores de `FinancialInstitution.institution_type`.
_INSTITUTION_TYPE_BY_GROUP = {GROUP_BANKS: "Banco", GROUP_BROKERS: "Corretora"}


def parse_account_groups(params: Mapping[str, str]) -> frozenset[str]:
    """Normaliza ``grupos``; entrada inválida resulta em resposta HTTP 400.

    Ausente ou vazio vale o padrão (todos menos Administradas). Seleção vazia
    não é um estado útil -- seria uma tela sempre zerada -- e a tela não deixa
    desmarcar o último grupo.
    """
    raw = params.get(GROUPS_PARAM)
    if raw is None or not raw.strip():
        return DEFAULT_ACCOUNT_GROUPS
    groups = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    if not groups or not groups <= ALL_ACCOUNT_GROUPS:
        raise SuspiciousOperation("Filtro de grupos de conta inválido.")
    return groups


def is_filtering(groups: frozenset[str]) -> bool:
    """Se a seleção difere do padrão (e portanto precisa viajar na URL)."""
    return groups != DEFAULT_ACCOUNT_GROUPS


def account_group(account_kind: str, institution_type: str, purpose: str = ACCOUNT_PURPOSE_PERSONAL) -> str:
    """O grupo de uma conta. A finalidade vence o tipo da conta, que vence o da instituição."""
    if purpose == ACCOUNT_PURPOSE_ADMINISTERED:
        return GROUP_ADMINISTERED
    if account_kind == ACCOUNT_KIND_CREDIT_CARD:
        return GROUP_CARDS
    if account_kind == ACCOUNT_KIND_INVESTMENT:
        return GROUP_INVESTMENTS
    return GROUP_BROKERS if institution_type == "Corretora" else GROUP_BANKS


def account_group_q(groups: Iterable[str], prefix: str = "") -> Q:
    """`Q` das contas dos grupos pedidos; ``prefix`` é o caminho até a conta
    (``"account__"`` para filtrar lançamentos). Todos os grupos rendem ``Q()``.
    """
    groups = frozenset(groups)
    if groups >= ALL_ACCOUNT_GROUPS:
        return Q()
    condition = Q(pk__in=[])
    personal = Q(**{f"{prefix}purpose": ACCOUNT_PURPOSE_PERSONAL})
    for group in groups:
        if group == GROUP_ADMINISTERED:
            condition |= Q(**{f"{prefix}purpose": ACCOUNT_PURPOSE_ADMINISTERED})
        elif group == GROUP_CARDS:
            condition |= personal & Q(**{f"{prefix}account_kind": ACCOUNT_KIND_CREDIT_CARD})
        elif group == GROUP_INVESTMENTS:
            condition |= personal & Q(**{f"{prefix}account_kind": ACCOUNT_KIND_INVESTMENT})
        else:
            condition |= personal & Q(**{
                f"{prefix}account_kind": ACCOUNT_KIND_REGULAR,
                f"{prefix}institution__institution_type": _INSTITUTION_TYPE_BY_GROUP[group],
            })
    return condition
