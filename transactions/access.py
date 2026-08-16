"""Controle de acesso para lançamentos financeiros (Movimentação > Lançamentos).

Uma transferência interna pode sair de uma conta acessível para uma conta de
outro titular: a contraparte não concede acesso geral a essa conta, mas
precisa poder ser exibida e atualizada junto da mesma transferência. Por isso
a autorização das duas pontas é sempre resolvida pela ponta "origem"
(`source_entry_id is None`) — a ponta de destino, isolada, continua sujeita ao
escopo normal do usuário.
"""
from __future__ import annotations

from banking.services import can_access_account as _account_accessible
from core.domain.finance import OPERATION_INTERNAL_TRANSFER

from .models import CashFlowEntry


def _authorization_entry(entry: CashFlowEntry | None) -> CashFlowEntry | None:
    if entry is None or entry.operation_type != OPERATION_INTERNAL_TRANSFER:
        return entry
    if entry.source_entry_id is None:
        return entry
    return entry.source_entry


def can_access_entry(user, entry: CashFlowEntry | None, action: str = "view") -> bool:
    if entry is None:
        return False
    authorized_entry = _authorization_entry(entry)
    if authorized_entry is None:
        return False
    return _account_accessible(user, authorized_entry.account_id, action)


def can_access_account(user, account_id, action: str = "view") -> bool:
    return _account_accessible(user, account_id, action)
