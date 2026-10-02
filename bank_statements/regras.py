"""Regras explícitas de linha de extrato (ver `models.StatementRule`).

A regra casa pelo texto da linha (sem acento nem caixa) e, opcionalmente, pela
instituição e pelo sinal. Quando mais de uma casa, vence a mais específica:
a que tem instituição, depois a de padrão mais longo.
"""
from __future__ import annotations

import unicodedata

from banking.models import FinancialAccount

from .models import (
    RULE_ACTION_TRANSFER,
    RULE_SIGN_ANY,
    RULE_SIGN_CREDIT,
    BankStatementLine,
    StatementRule,
)


def normalizar(texto: str) -> str:
    """Maiúsculas, sem acento e com espaços simples: a forma em que o padrão casa."""
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return " ".join(sem_acento.upper().split())


class Regras:
    """As regras ativas, lidas uma vez: um plano consulta uma regra por linha."""

    def __init__(self, regras=None):
        if regras is None:
            regras = StatementRule.objects.filter(active=True).select_related("category", "institution")
        self._regras = sorted(
            ((normalizar(regra.pattern), regra) for regra in regras),
            key=lambda par: (par[1].institution_id is None, -len(par[0]), par[1].id),
        )

    def da_linha(self, linha: BankStatementLine) -> StatementRule | None:
        texto = normalizar(linha.description)
        instituicao_id = linha.account.institution_id
        for padrao, regra in self._regras:
            if regra.institution_id is not None and regra.institution_id != instituicao_id:
                continue
            if regra.sign != RULE_SIGN_ANY and (regra.sign == RULE_SIGN_CREDIT) != (linha.amount > 0):
                continue
            if padrao and padrao in texto:
                return regra
        return None


def conta_de_destino(regra: StatementRule, conta: FinancialAccount) -> FinancialAccount | None:
    """A conta de destino da regra de transferência: mesmo titular, mesma
    instituição, nome informado. `None` quando a regra não é de transferência
    ou a conta ainda não existe."""
    if regra.action != RULE_ACTION_TRANSFER:
        return None
    return (
        FinancialAccount.objects.filter(
            owner_id=conta.owner_id,
            institution_id=conta.institution_id,
            account_name__iexact=regra.destination_account_name.strip(),
        )
        .exclude(id=conta.id)
        .first()
    )
