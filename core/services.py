"""Serviços de infraestrutura compartilhada: configurações chave/valor da
aplicação e as telas de Configurações (Perfil, Auditoria, Fechamento
mensal, Banco de dados, Parâmetros)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from ipaddress import ip_address, ip_network
from uuid import uuid4

from django.conf import settings
from django.db import connection

from core.domain.settings import (
    APP_SETTING_LAST_OPTIMIZE_INFO,
    APP_SETTING_LAST_PROJECTION_RUN,
    APP_SETTING_LOGIN_LOCK_MAX_FAILURES,
    APP_SETTING_LOGIN_LOCK_MINUTES,
    APP_SETTING_PASSWORD_MIN_LENGTH,
    APP_SETTING_PASSWORD_MIN_NUMBERS,
    APP_SETTING_PASSWORD_MIN_SPECIAL,
    APP_SETTING_PASSWORD_MIN_UPPERCASE,
    APP_SETTING_PROJECTION_HORIZON_MONTHS,
    APP_SETTING_PROJECTION_RUN_DAY,
    APP_SETTING_SYSTEM_START_DATE,
    normalize_table_scroll_rows,
    normalize_ui_theme,
)
from core.models import AppSetting


def get_app_setting(setting_key: str, default: str | None = None) -> str | None:
    value = AppSetting.objects.filter(setting_key=setting_key).values_list("setting_value", flat=True).first()
    if value in (None, ""):
        return default
    return value


def upsert_app_setting(setting_key: str, setting_value: str) -> AppSetting:
    setting, _created = AppSetting.objects.update_or_create(
        setting_key=setting_key,
        defaults={"setting_value": setting_value},
    )
    return setting


def parse_system_start_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("Data inicial do sistema inválida.") from exc


def system_start_date() -> date | None:
    return parse_system_start_date(get_app_setting(APP_SETTING_SYSTEM_START_DATE))


def update_system_start_date(value: object) -> date | None:
    parsed = parse_system_start_date(value)
    upsert_app_setting(APP_SETTING_SYSTEM_START_DATE, parsed.isoformat() if parsed else "")
    return parsed


# --- Perfil: tema e grade ---

def update_user_ui_theme(user, theme: str) -> str:
    normalized = normalize_ui_theme(theme)
    user.ui_theme = normalized
    user.save(update_fields=["ui_theme", "updated_at"])
    return normalized


def update_user_table_scroll_rows(user, rows: object) -> int:
    normalized = normalize_table_scroll_rows(rows, default=user.table_scroll_rows or 15)
    user.table_scroll_rows = normalized
    user.save(update_fields=["table_scroll_rows", "updated_at"])
    return normalized


# --- Auditoria ---

def audit_filter_options() -> dict[str, list[str]]:
    from core.models import AuditLog

    def distinct(field):
        return [
            value
            for value in AuditLog.objects.exclude(**{f"{field}__isnull": True}).exclude(**{field: ""})
            .order_by(field).values_list(field, flat=True).distinct()
            if value
        ]

    dates = sorted(
        {
            created_at.date().isoformat()
            for created_at in AuditLog.objects.exclude(created_at__isnull=True).values_list("created_at", flat=True)
        },
        reverse=True,
    )
    user_names = sorted(
        {
            username
            for username in AuditLog.objects.exclude(user__isnull=True)
            .values_list("user__username", flat=True)
            if username
        }
    )
    return {
        "created_on": dates,
        "user_name": user_names,
        "entity_name": distinct("entity_name"),
        "entity_id": distinct("entity_id"),
        "action": distinct("action"),
    }


def filtered_recent_audit_logs(
    *,
    created_on: str | None = None,
    user_name: str | None = None,
    entity_name: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
):
    from core.models import AuditLog

    filters = {
        "created_on": (created_on or "").strip(),
        "user_name": (user_name or "").strip(),
        "entity_name": (entity_name or "").strip(),
        "entity_id": (entity_id or "").strip(),
        "action": (action or "").strip(),
    }
    qs = AuditLog.objects.select_related("user")
    if filters["created_on"]:
        try:
            parsed_date = date.fromisoformat(filters["created_on"])
            qs = qs.filter(created_at__date=parsed_date)
        except ValueError:
            filters["created_on"] = ""
    if filters["user_name"]:
        qs = qs.filter(user__username=filters["user_name"])
    if filters["entity_name"]:
        qs = qs.filter(entity_name=filters["entity_name"])
    if filters["entity_id"]:
        qs = qs.filter(entity_id=filters["entity_id"])
    if filters["action"]:
        qs = qs.filter(action=filters["action"])
    logs = list(qs.order_by("-created_at", "-id")[:limit])
    return logs, filters


@dataclass(frozen=True)
class AuditRequestContext:
    """Dados nao sensiveis da requisicao, capturados na borda HTTP."""

    user: object | None
    client_ip: str | None
    proxy_ip: str | None
    request_id: str


def _valid_ip(value: str | None) -> str | None:
    try:
        return str(ip_address((value or "").strip()))
    except ValueError:
        return None


def _trusted_proxy(remote_addr: str | None) -> bool:
    remote_ip = _valid_ip(remote_addr)
    if remote_ip is None:
        return False
    for cidr in settings.AUDIT_TRUSTED_PROXY_CIDRS:
        try:
            if ip_address(remote_ip) in ip_network(cidr):
                return True
        except ValueError:
            # Configuracao invalida nao pode tornar um cabecalho confiavel.
            continue
    return False


def audit_request_context(request) -> AuditRequestContext:
    """Extrai a origem sem confiar em cabecalhos enviados diretamente pelo cliente.

    X-Forwarded-For so e aceito quando a conexao TCP vem de uma rede de proxy
    explicitamente configurada. Sem essa configuracao, REMOTE_ADDR e o cliente.
    """
    remote_addr = _valid_ip(request.META.get("REMOTE_ADDR"))
    client_ip = remote_addr
    proxy_ip = None
    if remote_addr and _trusted_proxy(remote_addr):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        forwarded_ips = [_valid_ip(value) for value in forwarded]
        if forwarded_ips and all(forwarded_ips):
            client_ip = forwarded_ips[0]
            proxy_ip = remote_addr

    supplied_request_id = (request.META.get("HTTP_X_REQUEST_ID") or "").strip()
    request_id = supplied_request_id if supplied_request_id.isascii() and 1 <= len(supplied_request_id) <= 64 else uuid4().hex
    return AuditRequestContext(
        user=getattr(request, "user", None),
        client_ip=client_ip,
        proxy_ip=proxy_ip,
        request_id=request_id,
    )


def log_audit_event(
    entity_name: str,
    entity_id,
    action: str,
    *,
    old_values=None,
    new_values=None,
    user=None,
    request_context: AuditRequestContext | None = None,
    result: str = "success",
    summary: str = "",
):
    from core.models import AuditLog

    actor = request_context.user if request_context is not None else user
    return AuditLog.objects.create(
        entity_name=entity_name,
        entity_id=str(entity_id) if entity_id is not None else None,
        action=action,
        old_values_json=old_values,
        new_values_json=new_values,
        user=actor if getattr(actor, "is_authenticated", False) else None,
        actor_id=getattr(actor, "id", None),
        actor_name=(getattr(actor, "get_username", lambda: "")() or "")[:150],
        client_ip=request_context.client_ip if request_context else None,
        proxy_ip=request_context.proxy_ip if request_context else None,
        request_id=request_context.request_id if request_context else "",
        result=result,
        summary=summary[:255],
    )


# --- Parâmetros: política de senha ---

_PASSWORD_MIN_LENGTH_FLOOR = 8
_PASSWORD_MAX_FAILURES_CEILING = 1000


@dataclass(frozen=True)
class PasswordPolicySettings:
    min_length: int
    min_uppercase: int
    min_numbers: int
    min_special: int


def _parse_bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Valor deve ser um número inteiro.") from exc
    if parsed < minimum:
        raise ValueError(f"Valor deve ser pelo menos {minimum}.")
    if parsed > maximum:
        raise ValueError(f"Valor deve ser no máximo {maximum}.")
    return parsed


def get_password_policy_settings() -> PasswordPolicySettings:
    def _get(key, default):
        try:
            return int(get_app_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    return PasswordPolicySettings(
        min_length=max(_get(APP_SETTING_PASSWORD_MIN_LENGTH, _PASSWORD_MIN_LENGTH_FLOOR), _PASSWORD_MIN_LENGTH_FLOOR),
        min_uppercase=max(_get(APP_SETTING_PASSWORD_MIN_UPPERCASE, 0), 0),
        min_numbers=max(_get(APP_SETTING_PASSWORD_MIN_NUMBERS, 0), 0),
        min_special=max(_get(APP_SETTING_PASSWORD_MIN_SPECIAL, 0), 0),
    )


def update_password_policy_settings(*, min_length, min_uppercase, min_numbers, min_special) -> PasswordPolicySettings:
    settings = PasswordPolicySettings(
        min_length=_parse_bounded_int(min_length, default=_PASSWORD_MIN_LENGTH_FLOOR, minimum=_PASSWORD_MIN_LENGTH_FLOOR, maximum=256),
        min_uppercase=_parse_bounded_int(min_uppercase, default=0, minimum=0, maximum=128),
        min_numbers=_parse_bounded_int(min_numbers, default=0, minimum=0, maximum=128),
        min_special=_parse_bounded_int(min_special, default=0, minimum=0, maximum=128),
    )
    upsert_app_setting(APP_SETTING_PASSWORD_MIN_LENGTH, str(settings.min_length))
    upsert_app_setting(APP_SETTING_PASSWORD_MIN_UPPERCASE, str(settings.min_uppercase))
    upsert_app_setting(APP_SETTING_PASSWORD_MIN_NUMBERS, str(settings.min_numbers))
    upsert_app_setting(APP_SETTING_PASSWORD_MIN_SPECIAL, str(settings.min_special))
    return settings


# --- Parâmetros: bloqueio de login ---

@dataclass(frozen=True)
class LoginLockoutPolicySettings:
    max_failures: int
    lock_minutes: int

    @property
    def lock_seconds(self) -> int:
        return self.lock_minutes * 60


DEFAULT_LOGIN_LOCK_MAX_FAILURES = 5
DEFAULT_LOGIN_LOCK_MINUTES = 1


def get_login_lockout_policy_settings() -> LoginLockoutPolicySettings:
    def _get(key, default):
        try:
            return int(get_app_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    return LoginLockoutPolicySettings(
        max_failures=max(_get(APP_SETTING_LOGIN_LOCK_MAX_FAILURES, DEFAULT_LOGIN_LOCK_MAX_FAILURES), 1),
        lock_minutes=max(_get(APP_SETTING_LOGIN_LOCK_MINUTES, DEFAULT_LOGIN_LOCK_MINUTES), 1),
    )


def update_login_lockout_policy_settings(*, max_failures, lock_minutes) -> LoginLockoutPolicySettings:
    settings = LoginLockoutPolicySettings(
        max_failures=_parse_bounded_int(max_failures, default=DEFAULT_LOGIN_LOCK_MAX_FAILURES, minimum=1, maximum=_PASSWORD_MAX_FAILURES_CEILING),
        lock_minutes=_parse_bounded_int(lock_minutes, default=DEFAULT_LOGIN_LOCK_MINUTES, minimum=1, maximum=525_600),
    )
    upsert_app_setting(APP_SETTING_LOGIN_LOCK_MAX_FAILURES, str(settings.max_failures))
    upsert_app_setting(APP_SETTING_LOGIN_LOCK_MINUTES, str(settings.lock_minutes))
    return settings


# --- Parâmetros: projeção de lançamentos recorrentes ---

DEFAULT_PROJECTION_HORIZON_MONTHS = 6
DEFAULT_PROJECTION_RUN_DAY = 31
MAX_PROJECTION_HORIZON_MONTHS = 36


@dataclass(frozen=True)
class RecurringProjectionSettings:
    horizon_months: int
    run_day: int
    last_projection_run: str | None


def _clamp_projection_horizon(value, default=DEFAULT_PROJECTION_HORIZON_MONTHS) -> int:
    try:
        months = int(value)
    except (TypeError, ValueError):
        months = default
    return max(1, min(months, MAX_PROJECTION_HORIZON_MONTHS))


def _clamp_projection_run_day(value, default=DEFAULT_PROJECTION_RUN_DAY) -> int:
    try:
        day = int(value)
    except (TypeError, ValueError):
        day = default
    return max(1, min(day, 31))


def get_recurring_projection_settings() -> RecurringProjectionSettings:
    return RecurringProjectionSettings(
        horizon_months=_clamp_projection_horizon(get_app_setting(APP_SETTING_PROJECTION_HORIZON_MONTHS, str(DEFAULT_PROJECTION_HORIZON_MONTHS))),
        run_day=_clamp_projection_run_day(get_app_setting(APP_SETTING_PROJECTION_RUN_DAY, str(DEFAULT_PROJECTION_RUN_DAY))),
        last_projection_run=get_app_setting(APP_SETTING_LAST_PROJECTION_RUN),
    )


def update_recurring_projection_settings(*, horizon_months, run_day) -> RecurringProjectionSettings:
    parsed_horizon = _clamp_projection_horizon(horizon_months)
    parsed_run_day = _clamp_projection_run_day(run_day)
    upsert_app_setting(APP_SETTING_PROJECTION_HORIZON_MONTHS, str(parsed_horizon))
    upsert_app_setting(APP_SETTING_PROJECTION_RUN_DAY, str(parsed_run_day))
    return RecurringProjectionSettings(
        horizon_months=parsed_horizon,
        run_day=parsed_run_day,
        last_projection_run=get_app_setting(APP_SETTING_LAST_PROJECTION_RUN),
    )


def format_last_projection_run(setting_value: str | None) -> str:
    if not setting_value:
        return "Nenhuma execução registrada"
    try:
        return datetime.fromisoformat(setting_value).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return setting_value


# --- Banco de dados: diagnóstico e otimização (somente leitura / seguro) ---

@dataclass(frozen=True)
class DatabaseHealthResult:
    ok: bool
    summary: str
    table_counts: dict[str, int]
    orphan_checks: list[tuple[str, int]]


_HEALTH_CHECK_TABLES = (
    "app_user", "account_owner", "financial_institution", "financial_account",
    "cash_flow_category", "cash_flow_entry", "bank_operation",
    "account_month_close", "audit_log", "bank_statement_import",
    "bank_statement_line", "entry_attachment",
)

_ORPHAN_CHECKS = (
    ("cash_flow_entry -> financial_account", "SELECT count(*) FROM cash_flow_entry e LEFT JOIN financial_account a ON a.id = e.account_id WHERE a.id IS NULL"),
    ("cash_flow_entry -> cash_flow_category", "SELECT count(*) FROM cash_flow_entry e LEFT JOIN cash_flow_category c ON c.id = e.category_id WHERE c.id IS NULL"),
    ("financial_account -> account_owner", "SELECT count(*) FROM financial_account a LEFT JOIN account_owner o ON o.id = a.owner_id WHERE o.id IS NULL"),
    ("bank_statement_line -> cash_flow_entry (conciliado)", "SELECT count(*) FROM bank_statement_line l LEFT JOIN cash_flow_entry e ON e.id = l.matched_entry_id WHERE l.status = 'conciliado' AND l.matched_entry_id IS NOT NULL AND e.id IS NULL"),
)


def run_database_health_check() -> DatabaseHealthResult:
    table_counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in _HEALTH_CHECK_TABLES:
            cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - nomes fixos, não vêm de entrada do usuário
            table_counts[table] = cursor.fetchone()[0]

        orphan_checks: list[tuple[str, int]] = []
        for label, sql in _ORPHAN_CHECKS:
            cursor.execute(sql)
            orphan_checks.append((label, cursor.fetchone()[0]))

    total_orphans = sum(count for _label, count in orphan_checks)
    ok = total_orphans == 0
    summary = (
        f"{sum(table_counts.values())} registro(s) em {len(table_counts)} tabela(s) verificada(s), 0 inconsistência(s)."
        if ok
        else f"{total_orphans} inconsistência(s) encontrada(s) — veja os detalhes."
    )
    return DatabaseHealthResult(ok=ok, summary=summary, table_counts=table_counts, orphan_checks=orphan_checks)


def optimize_database() -> str:
    """VACUUM ANALYZE em todas as tabelas do app. Operação segura, não destrutiva.

    VACUUM não roda dentro de uma transação; usa autocommit explicitamente.
    """
    with connection.cursor() as cursor:
        previous_autocommit = connection.get_autocommit()
        connection.set_autocommit(True)
        try:
            for table in _HEALTH_CHECK_TABLES:
                cursor.execute(f"VACUUM ANALYZE {table}")  # noqa: S608 - nomes fixos, não vêm de entrada do usuário
        finally:
            connection.set_autocommit(previous_autocommit)
    summary = f"VACUUM ANALYZE executado em {len(_HEALTH_CHECK_TABLES)} tabela(s)."
    upsert_app_setting(
        APP_SETTING_LAST_OPTIMIZE_INFO,
        f"{datetime.now().isoformat(timespec='seconds')}|{summary}",
    )
    return summary


def format_last_optimize_info(setting_value: str | None) -> str:
    if not setting_value:
        return "Nenhuma otimização registrada"
    timestamp, _, summary = setting_value.partition("|")
    try:
        formatted = datetime.fromisoformat(timestamp).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return setting_value
    return f"{formatted} — {summary}" if summary else formatted


# --- Banco de dados: inspeção de tabelas (somente leitura, tabelas fixas) ---

def available_inspection_tables() -> tuple[str, ...]:
    return tuple(sorted(_HEALTH_CHECK_TABLES))


def inspect_table(table_name: str | None) -> tuple[list[str], list[tuple]]:
    """Colunas e até 50 linhas de `table_name`, validado contra a whitelist fixa
    de tabelas do app (nunca interpola nome de tabela vindo direto do usuário)."""
    if not table_name or table_name not in _HEALTH_CHECK_TABLES:
        return [], []
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {table_name} LIMIT 50")  # noqa: S608 - table_name validado contra whitelist acima
        columns = [col.name for col in cursor.description]
        rows = cursor.fetchall()
    return columns, rows
