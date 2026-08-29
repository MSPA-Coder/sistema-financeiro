"""Context processors globais para shell de navegacao."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class MenuItem:
    label: str
    url: str
    icon: str
    active_prefix: str
    required_permission: str = ""
    requires_staff: bool = False
    exact_match: bool = False
    children: tuple[MenuItem, ...] = ()


def _build_menu_items() -> list[MenuItem]:
    return [
        MenuItem("Dashboard", "/dashboard/", "\U0001F4CA", "/dashboard/", required_permission="dashboard.view"),
        MenuItem(
            "Movimentação",
            "/transactions/",
            "\U0001F4B8",
            "/transactions/",
            children=(
                MenuItem(
                    "Lançamentos",
                    "/transactions/",
                    "\U0001F9FE",
                    "/transactions/",
                    required_permission="transactions.view",
                ),
                MenuItem(
                    "Lançamentos n+1",
                    "/operations/",
                    "\U0001F9E9",
                    "/operations/",
                    required_permission="operations.view",
                ),
                MenuItem(
                    "Banking",
                    "/banking/attachments/",
                    "\U0001F3E6",
                    "/banking/",
                    children=(
                        MenuItem(
                            "Importação de extrato",
                            "/banking/imports/",
                            "\U0001F4E5",
                            "/banking/imports/",
                            required_permission="banking.import",
                        ),
                        MenuItem(
                            "Conciliação",
                            "/banking/reconciliation/",
                            "\U0001F517",
                            "/banking/reconciliation/",
                            required_permission="banking.reconcile",
                        ),
                        MenuItem(
                            "Comprovantes",
                            "/banking/attachments/",
                            "\U0001F4CE",
                            "/banking/attachments/",
                            required_permission="banking.attachments.manage",
                        ),
                        MenuItem(
                            "Fechamento mensal",
                            "/settings/monthly-close/",
                            "\U0001F4C5",
                            "/settings/monthly-close/",
                            required_permission="settings.monthly_close.manage",
                        ),
                    ),
                ),
            ),
        ),
        MenuItem(
            "Relatórios",
            "/reports/upcoming-movements/",
            "\U0001F4C8",
            "/reports/",
            children=(
                MenuItem("Próximos movimentos", "/reports/upcoming-movements/", "\U0001F4CC", "/reports/upcoming-movements/"),
                MenuItem("Projeções", "/reports/projections/", "\U0001F4C9", "/reports/projections/", required_permission="projections.view"),
                MenuItem("Posição por conta", "/reports/account-position/", "\U0001F9EE", "/reports/account-position/", required_permission="reports.account_position.view"),
                MenuItem(
                    "Planejamento anual",
                    "/reports/annual-planning/",
                    "\U0001F4C5",
                    "/reports/annual-planning/",
                    required_permission="reports.annual_planning.view",
                ),
                MenuItem(
                    "Controle gerencial",
                    "/management/",
                    "\U0001F5C2",
                    "/management/",
                    required_permission="management.view",
                ),
            ),
        ),
        MenuItem(
            "Cadastros",
            "/tables/owners/",
            "\U0001F9F1",
            "/tables/",
            required_permission="tables.view",
            children=(
                MenuItem("Titulares", "/tables/owners/", "\U0001F464", "/tables/owners/", required_permission="tables.owners.manage"),
                MenuItem("Instituições", "/tables/banks/", "\U0001F3DB", "/tables/banks/", required_permission="tables.institutions.manage"),
                MenuItem("Contas", "/tables/accounts/", "\U0001F4BC", "/tables/accounts/", required_permission="tables.accounts.manage"),
                MenuItem("Categorias", "/tables/categories/", "\U0001F3F7", "/tables/categories/", required_permission="tables.categories.manage"),
            ),
        ),
        MenuItem(
            "Configurações",
            "/settings/profile/",
            "⚙",
            "/settings/profile/",
            required_permission="settings.view",
            children=(
                MenuItem("Perfil e tema", "/settings/profile/", "\U0001F3A8", "/settings/profile/"),
                MenuItem("Contas em análises", "/settings/account-visibility/", "\U0001F3E6", "/settings/account-visibility/"),
                MenuItem("Parâmetros", "/settings/", "\U0001F6E0", "/settings/", exact_match=True),
                MenuItem("Banco de dados", "/settings/database/", "\U0001F5C4", "/settings/database/"),
            ),
        ),
        MenuItem(
            "Segurança",
            "/change-password/",
            "\U0001F6E1",
            "/change-password/",
            children=(
                MenuItem("Alterar senha", "/change-password/", "\U0001F511", "/change-password/"),
                MenuItem("Permissões", "/permissions/", "\U0001F510", "/permissions/", required_permission="permissions.manage"),
                MenuItem("Trilha de auditoria", "/settings/audit-log/", "\U0001F9FE", "/settings/audit-log/", required_permission="settings.audit.view"),
            ),
        ),
        MenuItem("Administração", "/admin/", "#", "/admin/", requires_staff=True),
    ]


def _is_allowed(item: MenuItem, user) -> bool:
    if item.requires_staff and not (user and getattr(user, "is_staff", False)):
        return False
    return not (
        item.required_permission and not (user and user.has_perm(item.required_permission))
    )


def _serialize_menu_item(item: MenuItem, user, path: str, level: int = 0):
    if not _is_allowed(item, user):
        return None

    child_items = []
    for child in item.children:
        serialized = _serialize_menu_item(child, user, path, level + 1)
        if serialized is not None:
            child_items.append(serialized)

    is_active = path == item.active_prefix if item.exact_match else path.startswith(item.active_prefix)
    if not is_active:
        is_active = any(child.get("active", False) for child in child_items)

    return {
        "label": item.label,
        "url": item.url,
        "icon": item.icon,
        "active": is_active,
        "level": level,
        "children": child_items,
    }


def _serialize_menu(items: Iterable[MenuItem], user, path: str):
    serialized_items = []
    for item in items:
        serialized = _serialize_menu_item(item, user, path, level=0)
        if serialized is not None:
            serialized_items.append(serialized)
    return serialized_items


def app_shell(request):
    """Expoe dados comuns de layout para templates Django."""
    user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
    menu_items = _serialize_menu(_build_menu_items(), user, request.path)

    return {
        "ui_theme": getattr(user, "ui_theme", "light") if user else "light",
        "table_scroll_rows": getattr(user, "table_scroll_rows", 15) if user else 15,
        "current_active_user": user,
        "app_menu_items": menu_items,
    }
