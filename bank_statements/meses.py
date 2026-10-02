"""Reabrir meses fechados para uma mudança que não pode mexer em saldo.

Converter categoria, natureza ou vínculo de um lançamento num mês fechado exige
reabrir o mês. O que torna isso seguro é o saldo de fechamento: ele é gravado
antes, e depois da mudança precisa ser exatamente o mesmo, ou nada é gravado.
É o mesmo contrato da Reclassificação (`reclassificacao.aplicar`), aqui como
bloco reutilizável.
"""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import date

from accounts.services import has_function_permission
from core.domain.finance import VIEW_REALIZED
from reports.services import decimal_balance_before
from transactions.models import AccountMonthClose
from transactions.services import close_month, reopen_month


def meses_fechados(meses: Iterable[tuple[int, int, int]]) -> dict[tuple[int, int, int], object]:
    """Dos meses-conta `(conta, ano, mês)` dados, os que estão fechados (com a conta)."""
    pedidos = set(meses)
    if not pedidos:
        return {}
    return {
        (f.account_id, f.year, f.month): f.account
        for f in AccountMonthClose.objects.filter(
            active=True, account_id__in={m[0] for m in pedidos}
        ).select_related("account")
        if (f.account_id, f.year, f.month) in pedidos
    }


@contextmanager
def meses_reabertos(user, meses: Iterable[tuple[int, int, int]], *, autorizar: bool, motivo: str, audit_context=None):
    """Reabre os meses fechados dentro do bloco e fecha de novo no fim.

    Sem mês fechado, não faz nada. Com mês fechado exige `autorizar` e a permissão
    de fechamento mensal, e no fim o saldo de cada mês tem de ser o de antes. O
    chamador deve estar numa transação para que a recusa desfaça tudo."""
    fechados = meses_fechados(meses)
    saldos = {}
    if fechados:
        if not autorizar:
            raise ValueError("Há meses fechados entre os lançamentos: autorize a reabertura e o novo fechamento.")
        if not has_function_permission(user, "settings.monthly_close.manage"):
            raise ValueError("Reabrir mês fechado exige a permissão de fechamento mensal.")
        for (conta_id, ano, mes), conta in fechados.items():
            fechamento = AccountMonthClose.objects.get(account=conta, year=ano, month=mes, active=True)
            saldos[(conta_id, ano, mes)] = (conta, fechamento.closing_balance)
            reopen_month(conta, ano, mes, motivo, user, audit_context=audit_context)
    yield
    for (conta_id, ano, mes), (conta, anterior) in saldos.items():
        fim = date(ano + (mes == 12), mes % 12 + 1, 1)
        novo = decimal_balance_before([conta_id], fim, VIEW_REALIZED)
        if novo != anterior:
            raise ValueError(
                f"O saldo de fechamento de {mes:02d}/{ano} ({conta}) mudaria de {anterior} para {novo}; "
                "nada foi gravado."
            )
        close_month(conta, ano, mes, novo, user, audit_context=audit_context)
