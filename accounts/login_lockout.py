"""Bloqueio persistente de tentativas de login (Configurações > Parâmetros).

A identidade bloqueada combina usuário normalizado + IP remoto: bloquear só
por usuário deixa um atacante varrer contas a partir do mesmo host, e bloquear
só por IP pune todo mundo atrás de um NAT. Após N falhas dentro da janela
configurada em Configurações > Parâmetros > Bloqueio de login, novas
tentativas são recusadas até o bloqueio expirar.

O estado vive em `LoginLockout`, não na sessão: sessão é descartável pelo
próprio atacante e não vale como controle em um app multiusuário.
"""
from __future__ import annotations

import time

from django.utils import timezone

from .models import LoginLockout


class LoginThrottledError(Exception):
    def __init__(self, wait_seconds: int):
        self.wait_seconds = wait_seconds
        super().__init__(f"Muitas tentativas de login. Aguarde {wait_seconds} segundo(s) antes de tentar novamente.")


def _normalize_username(username: str | None) -> str:
    return (username or "").strip().lower()[:100]


def _identity_key(username: str | None, remote_addr: str | None) -> str:
    return f"{_normalize_username(username) or '-'}|{(remote_addr or '-')[:80]}"


def assert_login_not_throttled(username: str | None, remote_addr: str | None) -> None:
    row = LoginLockout.objects.filter(identity_key=_identity_key(username, remote_addr)).first()
    if row and row.locked_until_ts:
        now = int(time.time())
        if row.locked_until_ts > now:
            raise LoginThrottledError(max(1, row.locked_until_ts - now))


def register_failed_login_attempt(username: str | None, remote_addr: str | None) -> tuple[int, int | None]:
    """Registra uma falha; retorna (tentativas_restantes, segundos_de_bloqueio_ou_none)."""
    from core.services import get_login_lockout_policy_settings

    policy = get_login_lockout_policy_settings()
    identity_key = _identity_key(username, remote_addr)
    normalized = _normalize_username(username)

    row, _created = LoginLockout.objects.get_or_create(
        identity_key=identity_key,
        defaults={"normalized_user_name": normalized or None, "remote_addr": (remote_addr or "")[:80] or None},
    )
    row.failure_count = (row.failure_count or 0) + 1
    row.normalized_user_name = normalized or None
    row.remote_addr = (remote_addr or "")[:80] or None
    row.last_failed_at = timezone.now()

    wait_seconds = None
    if row.failure_count >= policy.max_failures:
        wait_seconds = policy.lock_seconds
        row.locked_until_ts = int(time.time()) + wait_seconds
        row.failure_count = 0
    row.save(update_fields=["failure_count", "normalized_user_name", "remote_addr", "locked_until_ts", "last_failed_at", "updated_at"])

    attempts_remaining = max(0, policy.max_failures - row.failure_count) if wait_seconds is None else 0
    return attempts_remaining, wait_seconds


def clear_login_failures(username: str | None, remote_addr: str | None) -> None:
    LoginLockout.objects.filter(identity_key=_identity_key(username, remote_addr)).update(
        failure_count=0, locked_until_ts=None
    )


def failed_login_message(attempts_remaining: int, wait_seconds: int | None) -> str:
    message = f"Usuário ou senha inválidos. Tentativas restantes até o bloqueio: {attempts_remaining}."
    if wait_seconds is not None:
        message += f" Muitas tentativas de login. Aguarde {wait_seconds} segundo(s) antes de tentar novamente."
    return message
