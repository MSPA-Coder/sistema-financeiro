"""Filtro request-scoped de grupos de conta: bancos, corretoras, cartões, aplicações.

Os quatro grupos são uma PARTIÇÃO das contas: cada conta cai em exatamente um.
É isso que deixa uma seleção múltipla legível -- marcar "Bancos" e desmarcar
"Cartões" não deixa dúvida sobre o cartão emitido por um banco. Por isso o tipo
da conta vence o tipo da instituição:

- cartão de crédito -> Cartões, seja qual for a instituição;
- aplicação -> Aplicações, idem;
- conta comum -> Bancos ou Corretoras, pelo tipo da instituição.

Como a moeda, o filtro vive na URL (`grupos=bancos,cartoes`) e não é gravado:
ausente, valem todos, e as telas ficam como sempre foram. Não se confunde com
Configurações > Contas em análises, que é preferência gravada por usuário.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from django.core.exceptions import SuspiciousOperation
from django.db.models import Q

from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    ACCOUNT_KIND_REGULAR,
)

GROUPS_PARAM = "grupos"

GROUP_BANKS = "bancos"
GROUP_BROKERS = "corretoras"
GROUP_CARDS = "cartoes"
GROUP_INVESTMENTS = "aplicacoes"
# A ordem é a da tela.
ACCOUNT_GROUP_OPTIONS = (
    (GROUP_BANKS, "Bancos"),
    (GROUP_BROKERS, "Corretoras"),
    (GROUP_CARDS, "Cartões"),
    (GROUP_INVESTMENTS, "Aplicações"),
)
ALL_ACCOUNT_GROUPS = frozenset(code for code, _label in ACCOUNT_GROUP_OPTIONS)

# Valores de `FinancialInstitution.institution_type`.
_INSTITUTION_TYPE_BY_GROUP = {GROUP_BANKS: "Banco", GROUP_BROKERS: "Corretora"}


def parse_account_groups(params: Mapping[str, str]) -> frozenset[str]:
    """Normaliza ``grupos``; entrada inválida resulta em resposta HTTP 400.

    Ausente ou vazio vale todos. Seleção vazia não é um estado útil -- seria
    uma tela sempre zerada -- e a tela não deixa desmarcar o último grupo.
    """
    raw = params.get(GROUPS_PARAM)
    if raw is None or not raw.strip():
        return ALL_ACCOUNT_GROUPS
    groups = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    if not groups or not groups <= ALL_ACCOUNT_GROUPS:
        raise SuspiciousOperation("Filtro de grupos de conta inválido.")
    return groups


def is_filtering(groups: frozenset[str]) -> bool:
    return groups != ALL_ACCOUNT_GROUPS


def account_group(account_kind: str, institution_type: str) -> str:
    """O grupo de uma conta. O tipo da conta vence o da instituição."""
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
    for group in groups:
        if group == GROUP_CARDS:
            condition |= Q(**{f"{prefix}account_kind": ACCOUNT_KIND_CREDIT_CARD})
        elif group == GROUP_INVESTMENTS:
            condition |= Q(**{f"{prefix}account_kind": ACCOUNT_KIND_INVESTMENT})
        else:
            condition |= Q(**{
                f"{prefix}account_kind": ACCOUNT_KIND_REGULAR,
                f"{prefix}institution__institution_type": _INSTITUTION_TYPE_BY_GROUP[group],
            })
    return condition
