"""Serviços de identidade: catálogo de permissões funcionais e titulares."""
from __future__ import annotations

from typing import Final

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.utils import timezone
from sharedauth.passwords import TAMANHO_SENHA_TEMPORARIA, gerar_senha_temporaria

from core.domain.identity import USER_TYPE_ADMINISTRATOR, USER_TYPE_SUPER_USER, normalize_user_type

from .models import (
    AccountOwner,
    AppPermission,
    AppUser,
    UserAccountVisibility,
    UserOwnerAccess,
    UserPermission,
)
from .password_validators import current_min_length

# Catálogo de permissões funcionais em uso pela aplicação. Cada chave aqui
# precisa existir como linha em AppPermission (ver migration
# 0003_seed_permission_catalog) para que `user.has_perm(chave)` funcione via
# AppPermissionBackend. Mantido enxuto: somente chaves que algum
# @permission_required ou item de menu realmente referencia hoje. Novas
# chaves entram junto com o módulo que passa a usá-las.
PERMISSION_DEFINITIONS: Final[dict[str, str]] = {
    "dashboard.view": "Visualizar Dashboard",
    "transactions.view": "Visualizar lançamentos",
    "transactions.create": "Incluir lançamentos",
    "transactions.update": "Editar lançamentos",
    "transactions.realize": "Marcar lançamentos como realizados",
    "transactions.delete": "Excluir lançamentos",
    "projections.view": "Visualizar Projeções",
    "reports.upcoming_movements.view": "Visualizar Próximos movimentos",
    "reports.account_position.view": "Visualizar Posição por conta",
    "reports.annual_planning.view": "Visualizar Planejamento anual",
    "management.view": "Visualizar Gestão",
    "management.manage": "Gerenciar Gestão (tags, projetos, orçamento)",
    "tables.view": "Visualizar Cadastros",
    "tables.owners.manage": "Gerenciar titulares",
    "tables.institutions.manage": "Gerenciar instituições financeiras",
    "tables.accounts.manage": "Gerenciar contas",
    "tables.categories.manage": "Gerenciar categorias",
    "banking.view": "Visualizar módulo Instituições",
    "banking.import": "Importar extratos bancários",
    "banking.reconcile": "Conciliar extratos bancários",
    "banking.attachments.manage": "Gerenciar comprovantes de lançamentos",
    "operations.view": "Visualizar Operações (lançamentos agrupados)",
    "settings.view": "Visualizar Configurações",
    "settings.theme.update": "Atualizar perfil e visibilidade de contas",
    "settings.audit.view": "Visualizar trilha de auditoria",
    "settings.monthly_close.manage": "Fechar/reabrir mês pela tela de Configurações",
    "settings.database.optimize": "Executar diagnóstico e otimização do banco de dados",
    "settings.projection.manage": "Gerenciar projeção de lançamentos recorrentes",
    "settings.password_policy.manage": "Gerenciar política de senha e bloqueio de login",
    "tables.users.manage": "Gerenciar usuários",
    "permissions.manage": "Gerenciar permissões de usuários",
}

# Permissões que, quando concedidas, implicam outra permissão mais genérica
# (ex.: quem pode gerenciar titulares também pode visualizar Cadastros).
PERMISSION_DEPENDENCIES: Final[dict[str, tuple[str, ...]]] = {
    "transactions.create": ("transactions.view",),
    "transactions.update": ("transactions.view",),
    "transactions.realize": ("transactions.view",),
    "transactions.delete": ("transactions.view",),
    "management.manage": ("management.view",),
    "tables.owners.manage": ("tables.view",),
    "tables.institutions.manage": ("tables.view",),
    "tables.accounts.manage": ("tables.view",),
    "tables.categories.manage": ("tables.view",),
    "banking.import": ("banking.view",),
    "banking.reconcile": ("banking.view",),
    "banking.attachments.manage": ("banking.view",),
    "settings.theme.update": ("settings.view",),
    "settings.audit.view": ("settings.view",),
    "settings.monthly_close.manage": ("settings.view",),
    "settings.database.optimize": ("settings.view",),
    "settings.projection.manage": ("settings.view",),
    "settings.password_policy.manage": ("settings.view",),
}


def _implied_closure(permission_key: str) -> set[str]:
    """Todas as chaves que `permission_key` concede, incluindo ela mesma."""
    closure = {permission_key}
    pending = [permission_key]
    while pending:
        current = pending.pop()
        for implied in PERMISSION_DEPENDENCIES.get(current, ()):
            if implied not in closure:
                closure.add(implied)
                pending.append(implied)
    return closure


def has_function_permission(user, permission_key: str) -> bool:
    """Verifica se `user` possui a permissão funcional (direta ou implícita).

    Administradores (por `user_type`) têm acesso total por regra do sistema,
    sem depender de concessões — é o que impede o sistema de ficar sem ninguém
    capaz de administrar permissões. Usuários comuns dependem de concessões
    registradas em UserPermission, com expansão pelas dependências acima.
    """
    if user is None or not getattr(user, "is_active", False):
        return False
    if getattr(user, "user_type", None) == USER_TYPE_ADMINISTRATOR:
        return True

    granted_keys = set(
        UserPermission.objects.filter(user=user, allowed=True).values_list("permission__name", flat=True)
    )
    return any(permission_key in _implied_closure(key) for key in granted_keys)


def expand_permission_keys(keys) -> set[str]:
    """Expande um conjunto de chaves para incluir tudo que elas implicam."""
    expanded: set[str] = set()
    for key in keys:
        expanded |= _implied_closure(key)
    return expanded


# --- Tela de Permissões (Segurança > Controle de acesso) ---

def list_manageable_users():
    """Usuários exibidos na tela de Permissões.

    Inclui administrators de propósito: eles já têm acesso total por regra do
    sistema, mas precisam aparecer na lista para que a própria conta possa ser
    gerenciada."""
    return AppUser.objects.all().order_by("username")


# Chaves criticas: acoes destrutivas ou administrativas sensiveis, destacadas na matriz
# de permissoes funcionais.
CRITICAL_PERMISSION_KEYS: Final[set[str]] = {
    "transactions.delete",
    "tables.users.manage",
    "settings.database.optimize",
    "settings.password_policy.manage",
    "permissions.manage",
}

# Agrupamento visual das permissões funcionais por item de menu, usado para renderizar a
# matriz de permissões em seções/subgrupos. É só apresentação: a autorização efetiva
# vem de has_function_permission, nunca deste agrupamento.
PERMISSION_MENU_GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("Dashboard", ("dashboard.view",)),
    ("Movimentação · Lançamentos", (
        "transactions.view", "transactions.create", "transactions.update",
        "transactions.realize", "transactions.delete",
    )),
    ("Movimentação · Lançamentos n+1", ("operations.view",)),
    ("Movimentação · Comprovantes", ("banking.attachments.manage",)),
    ("Movimentação · Instituições · Acesso geral", ("banking.view",)),
    ("Movimentação · Importação de extrato", ("banking.import",)),
    ("Movimentação · Conciliação", ("banking.reconcile",)),
    ("Movimentação · Fechamento mensal", ("settings.monthly_close.manage",)),
    ("Relatórios · Próximos movimentos", ("reports.upcoming_movements.view",)),
    ("Relatórios · Projeções", ("projections.view",)),
    ("Relatórios · Posição por conta", ("reports.account_position.view",)),
    ("Relatórios · Planejamento anual", ("reports.annual_planning.view",)),
    ("Relatórios · Controle gerencial", ("management.view", "management.manage")),
    ("Cadastros · Acesso geral", ("tables.view",)),
    ("Cadastros · Titulares", ("tables.owners.manage",)),
    ("Cadastros · Instituições", ("tables.institutions.manage",)),
    ("Cadastros · Contas", ("tables.accounts.manage",)),
    ("Cadastros · Categorias", ("tables.categories.manage",)),
    ("Configurações · Acesso geral", ("settings.view",)),
    ("Configurações · Perfil e tema", ("settings.theme.update",)),
    ("Configurações · Parâmetros", (
        "settings.projection.manage", "settings.password_policy.manage",
    )),
    ("Configurações · Banco de dados", ("settings.database.optimize",)),
    ("Segurança · Auditoria", ("settings.audit.view",)),
    ("Segurança · Permissões · Usuários", ("tables.users.manage",)),
    ("Segurança · Permissões", ("permissions.manage",)),
)
_PERMISSION_GROUP_BY_KEY: Final[dict[str, str]] = {
    key: group_name for group_name, keys in PERMISSION_MENU_GROUPS for key in keys
}
_PERMISSION_ORDER_BY_KEY: Final[dict[str, int]] = {
    key: index
    for index, key in enumerate(key for _name, keys in PERMISSION_MENU_GROUPS for key in keys)
}
_PERMISSION_SUBGROUP_ORDER: Final[dict[str, int]] = {
    name: index for index, (name, _keys) in enumerate(PERMISSION_MENU_GROUPS)
}
_PERMISSION_GROUP_ORDER: Final[dict[str, int]] = {}
for _group_name, _keys in PERMISSION_MENU_GROUPS:
    _top_level = _group_name.split(" · ", 1)[0]
    _PERMISSION_GROUP_ORDER.setdefault(_top_level, len(_PERMISSION_GROUP_ORDER))


def permission_catalog_grouped():
    """Permissões cadastradas, agrupadas por item de menu (nome do grupo -> lista ordenada)."""
    permissions = list(AppPermission.objects.filter(name__in=PERMISSION_DEFINITIONS.keys()))
    groups: dict[str, list] = {}
    for permission in permissions:
        group = _PERMISSION_GROUP_BY_KEY.get(permission.name, "Outras")
        groups.setdefault(group, []).append(permission)
    return [
        {
            "name": group,
            "permissions": sorted(
                groups[group],
                key=lambda p: (_PERMISSION_ORDER_BY_KEY.get(p.name, len(_PERMISSION_ORDER_BY_KEY)), p.name),
            ),
        }
        for group in sorted(
            groups,
            key=lambda name: (
                _PERMISSION_GROUP_ORDER.get(name.split(" · ", 1)[0], len(_PERMISSION_GROUP_ORDER)),
                _PERMISSION_SUBGROUP_ORDER.get(name, len(_PERMISSION_SUBGROUP_ORDER)),
                name,
            ),
        )
    ]


def permission_catalog_sections(permission_groups):
    """Reagrupa `permission_catalog_grouped()` por seção de topo (ex.: "Movimentação"),
    com subgrupos quando o nome do grupo tem "·" (ex.: "Movimentação · Banking · Acesso geral")."""
    sections: list[dict] = []
    section_by_name: dict[str, dict] = {}
    for group in permission_groups:
        parts = group["name"].split(" · ", 1)
        section_name = parts[0]
        subgroup_name = parts[1] if len(parts) > 1 else section_name
        has_subgroup = len(parts) > 1
        section = section_by_name.get(section_name)
        if section is None:
            section = {"name": section_name, "groups": [], "has_subgroups": False}
            section_by_name[section_name] = section
            sections.append(section)
        section["groups"].append({
            "name": subgroup_name,
            "has_subgroup": has_subgroup,
            "permissions": group["permissions"],
        })
        section["has_subgroups"] = section["has_subgroups"] or has_subgroup
    return sections


# Perfis rápidos: conjuntos de permissões pré-definidos para aplicar de uma vez a um
# usuário. São atalhos de cadastro, não um nível de acesso próprio — o que vale é o
# conjunto de UserPermission que o perfil grava.
PROFILE_DEFINITIONS: Final[dict[str, dict]] = {
    "consulta": {
        "label": "Consulta",
        "description": "Acesso de leitura às principais telas, sem inclusão, edição, exclusão ou ações administrativas.",
        "permissions": {
            "dashboard.view", "transactions.view", "projections.view",
            "reports.upcoming_movements.view", "reports.account_position.view", "reports.annual_planning.view",
            "operations.view", "settings.view", "settings.theme.update",
        },
    },
    "operador": {
        "label": "Operador",
        "description": "Consulta, inclusão, edição e realização de lançamentos. Não inclui exclusão nem administração.",
        "permissions": {
            "dashboard.view", "transactions.view", "transactions.create",
            "transactions.update", "transactions.realize", "projections.view",
            "reports.upcoming_movements.view", "reports.account_position.view", "reports.annual_planning.view",
            "operations.view", "settings.view", "settings.theme.update",
        },
    },
    "gestor": {
        "label": "Gestor",
        "description": "Operação financeira completa, cadastros financeiros e rotinas administrativas não destrutivas.",
        "permissions": {
            "dashboard.view", "transactions.view", "transactions.create",
            "transactions.update", "transactions.delete", "transactions.realize",
            "projections.view",
            "reports.upcoming_movements.view", "reports.account_position.view", "reports.annual_planning.view",
            "operations.view", "management.view", "management.manage",
            "banking.view", "banking.import", "banking.reconcile", "banking.attachments.manage",
            "tables.view", "tables.owners.manage", "tables.accounts.manage",
            "tables.institutions.manage", "tables.categories.manage",
            "settings.view", "settings.theme.update",
            "settings.database.optimize", "settings.projection.manage",
        },
    },
    "administrador": {
        "label": "Administrador",
        "description": "Todas as permissões funcionais cadastradas, incluindo permissões críticas.",
        "permissions": set(),
    },
}


def profile_permission_keys(profile_key: str) -> set[str] | None:
    profile = PROFILE_DEFINITIONS.get(profile_key)
    if profile is None:
        return None
    if profile_key == "administrador":
        return set(PERMISSION_DEFINITIONS.keys())
    return expand_permission_keys(set(profile["permissions"]))


def permission_summary(user: AppUser | None) -> dict:
    """Estatísticas exibidas no card de resumo da tela de Permissões."""
    if user is None:
        return {
            "owner_view_count": 0, "owner_create_count": 0, "owner_update_count": 0,
            "owner_delete_count": 0, "function_allowed_count": 0,
            "function_total_count": len(PERMISSION_DEFINITIONS),
            "critical_allowed_count": 0, "critical_total_count": len(CRITICAL_PERMISSION_KEYS),
            "missing_recommended_permissions": [],
        }
    owner_accesses = list(owner_access_map(user).values())
    allowed_keys = expand_permission_keys(allowed_permission_keys(user))
    recommended_keys = profile_permission_keys("operador") or set()
    return {
        "owner_view_count": sum(1 for a in owner_accesses if a.can_view),
        "owner_create_count": sum(1 for a in owner_accesses if a.can_create),
        "owner_update_count": sum(1 for a in owner_accesses if a.can_update),
        "owner_delete_count": sum(1 for a in owner_accesses if a.can_delete),
        "function_allowed_count": len(allowed_keys),
        "function_total_count": len(PERMISSION_DEFINITIONS),
        "critical_allowed_count": len(allowed_keys & CRITICAL_PERMISSION_KEYS),
        "critical_total_count": len(CRITICAL_PERMISSION_KEYS),
        "missing_recommended_permissions": sorted(recommended_keys - allowed_keys),
    }


# --- Gerenciamento de usuários (tela de Permissões) ---

def _is_elevated_user_type(user_type: str | None) -> bool:
    return user_type in {USER_TYPE_ADMINISTRATOR, USER_TYPE_SUPER_USER}


def user_mutation_block_message(
    action: str,
    current_user: AppUser,
    target_user: AppUser | None = None,
    requested_user_type: str | None = None,
) -> str | None:
    """Mensagem de bloqueio se a mutação de usuário não for permitida, ou None se liberada."""
    if action == "delete" and target_user and target_user.id == current_user.id:
        return "Ação bloqueada: você não pode excluir a própria conta."

    if action == "delete" and target_user and target_user.user_type == USER_TYPE_ADMINISTRATOR:
        other_admin_exists = AppUser.objects.filter(
            user_type=USER_TYPE_ADMINISTRATOR
        ).exclude(id=target_user.id).exists()
        if not other_admin_exists:
            return "Ação bloqueada: não é permitido excluir o último administrator."

    if action in ("add", "edit") and _is_elevated_user_type(requested_user_type) and current_user.user_type != USER_TYPE_ADMINISTRATOR:
        return "Acesso negado: somente administrator pode criar ou promover usuários privilegiados."

    if action == "edit" and target_user and _is_elevated_user_type(target_user.user_type) and current_user.user_type != USER_TYPE_ADMINISTRATOR:
        return "Acesso negado: somente administrator pode alterar usuário privilegiado."

    return None


def _username_taken(username: str, *, excluding_user_id: int | None = None) -> bool:
    queryset = AppUser.objects.filter(username=username)
    if excluding_user_id is not None:
        queryset = queryset.exclude(id=excluding_user_id)
    return queryset.exists()


def create_managed_user(username: str, user_type: str, password: str, password_confirm: str) -> AppUser:
    username = (username or "").strip()
    if not username:
        raise ValueError("Nome do usuário é obrigatório.")
    if _username_taken(username):
        raise ValueError("Já existe um usuário com este nome.")
    if password != password_confirm:
        raise ValueError("A confirmação de senha não confere.")

    user = AppUser(username=username, user_type=normalize_user_type(user_type))
    try:
        validate_password(password or "", user=user)
    except ValidationError as exc:
        raise ValueError(" ".join(exc.messages)) from exc

    user.set_password(password)
    user.must_change_password = True
    user.save()
    return user


def update_managed_user(
    user: AppUser,
    username: str,
    user_type: str,
    password: str | None,
    password_confirm: str | None,
    must_change_password: bool,
) -> AppUser:
    username = (username or "").strip()
    if not username:
        raise ValueError("Nome do usuário é obrigatório.")
    if _username_taken(username, excluding_user_id=user.id):
        raise ValueError("Já existe um usuário com este nome.")

    password = (password or "").strip()
    password_confirm = (password_confirm or "").strip()
    password_changed = False
    if password or password_confirm:
        if password != password_confirm:
            raise ValueError("A confirmação de senha não confere.")
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            raise ValueError(" ".join(exc.messages)) from exc
        user.set_password(password)
        user.password_updated_at = timezone.now()
        password_changed = True

    user.username = username
    user.user_type = normalize_user_type(user_type)
    user.must_change_password = True if password_changed else must_change_password
    user.save()
    return user


def reset_managed_user_password(user: AppUser) -> str:
    """Sorteia a senha temporaria de outra conta e obriga a troca.

    Quem administra nao escolhe mais a senha de outra pessoa: uma senha
    escolhida por ele e uma senha que ele conhece e que tende a se repetir
    entre contas. O sistema sorteia, mostra uma vez, e o dono e obrigado a
    trocar no primeiro acesso -- `MustChangePasswordMiddleware` cobra isso em
    toda requisicao, nao so no login.

    O valor devolvido e a **unica copia em texto claro** que vai existir. Quem
    chama mostra e descarta; nao vai para log, auditoria nem coluna.

    O sorteio usa o alfabeto de `sharedauth.passwords`, que exclui `0/O` e
    `1/l/I` -- a senha vai ser ditada -- e **nao tem caractere especial**. O
    tamanho vem da politica em Configuracoes > Parametros, nunca do padrao da
    biblioteca: uma instalacao com minimo de 15 recusaria os 12 do padrao, e a
    redefinicao rejeitaria a propria senha que acabou de sortear. Ja exigencia
    de caractere especial nao tem como o sorteio atender -- ai a recusa e dita
    na hora, em vez de virar um erro obscuro.
    """
    senha = gerar_senha_temporaria(max(TAMANHO_SENHA_TEMPORARIA, current_min_length()))
    try:
        validate_password(senha, user=user)
    except ValidationError as exc:
        raise ValueError(
            "A senha sorteada nao atende a politica configurada em "
            "Configuracoes > Parametros (" + " ".join(exc.messages) + "). "
            "Ajuste a politica ou defina a senha pela edicao do usuario."
        ) from exc

    user.set_password(senha)
    user.password_updated_at = timezone.now()
    user.must_change_password = True
    user.save(update_fields=["password", "password_updated_at", "must_change_password", "updated_at"])
    return senha


def delete_managed_user(user: AppUser) -> None:
    user.delete()


def permission_link_map(user: AppUser) -> dict[int, UserPermission]:
    return {link.permission_id: link for link in UserPermission.objects.filter(user=user).select_related("permission")}


def allowed_permission_keys(user: AppUser, permission_links: dict[int, UserPermission] | None = None) -> set[str]:
    links = permission_links if permission_links is not None else permission_link_map(user)
    return {link.permission.name for link in links.values() if link.allowed}


def save_function_permissions(user: AppUser, allowed_keys: set[str]) -> None:
    """Grava as permissões funcionais marcadas para `user`, expandindo dependências."""
    from accounts.models import AppPermission

    expanded_keys = expand_permission_keys(allowed_keys)
    permissions = AppPermission.objects.filter(name__in=PERMISSION_DEFINITIONS.keys())
    existing = permission_link_map(user)
    for permission in permissions:
        allowed = permission.name in expanded_keys
        link = existing.get(permission.id)
        if link is None:
            UserPermission.objects.create(user=user, permission=permission, allowed=allowed)
        elif link.allowed != allowed:
            link.allowed = allowed
            link.save(update_fields=["allowed", "updated_at"])


def owner_access_map(user: AppUser) -> dict[int, UserOwnerAccess]:
    return {access.owner_id: access for access in UserOwnerAccess.objects.filter(user=user)}


def save_owner_access_matrix(user: AppUser, owner_flags: dict[int, dict[str, bool]]) -> None:
    """`owner_flags`: {owner_id: {'view': bool, 'create': bool, 'update': bool, 'delete': bool}}.

    Marcar create/update/delete implica view automaticamente: conceder escrita
    sem leitura produziria um acesso que a interface não sabe representar.
    """
    existing = owner_access_map(user)
    for owner in AccountOwner.objects.all():
        flags = owner_flags.get(owner.id, {})
        can_create = bool(flags.get("create"))
        can_update = bool(flags.get("update"))
        can_delete = bool(flags.get("delete"))
        can_view = bool(flags.get("view")) or can_create or can_update or can_delete

        access = existing.get(owner.id)
        enabled = can_view or can_create or can_update or can_delete
        if access is None and enabled:
            UserOwnerAccess.objects.create(
                user=user, owner=owner,
                can_view=can_view, can_create=can_create, can_update=can_update, can_delete=can_delete,
            )
        elif access is not None and not enabled:
            access.delete()
        elif access is not None:
            access.can_view = can_view
            access.can_create = can_create
            access.can_update = can_update
            access.can_delete = can_delete
            access.save(update_fields=["can_view", "can_create", "can_update", "can_delete"])


# --- Escopo por titular (usado por Contas, que são vinculadas a um dono) ---

_OWNER_ACTIONS: Final[set[str]] = {"view", "create", "update", "delete"}


def _has_broad_owner_access(user) -> bool:
    """Administradores e super usuários enxergam/gerenciam todos os titulares.

    Escopo de DADOS (esta função) é deliberadamente mais amplo que a permissão
    FUNCIONAL (has_function_permission, só administrator): super users enxergam
    todos os titulares, mas isso não lhes dá o direito de gerir permissões.
    """
    return bool(
        user and getattr(user, "user_type", None) in {USER_TYPE_ADMINISTRATOR, USER_TYPE_SUPER_USER}
    )


def accessible_owner_ids(user, action: str = "view") -> list[int]:
    """IDs de titulares que `user` pode acessar para a ação informada."""
    if user is None:
        return []
    action = action if action in _OWNER_ACTIONS else "view"
    if _has_broad_owner_access(user):
        return list(AccountOwner.objects.values_list("id", flat=True))
    field = f"can_{action}"
    return list(
        UserOwnerAccess.objects.filter(user=user, **{field: True}).values_list("owner_id", flat=True)
    )


def can_access_owner(user, owner_id, action: str = "view") -> bool:
    """True se `user` pode acessar o titular `owner_id` para a ação informada."""
    if user is None or owner_id is None:
        return False
    if _has_broad_owner_access(user):
        return True
    action = action if action in _OWNER_ACTIONS else "view"
    field = f"can_{action}"
    return UserOwnerAccess.objects.filter(user=user, owner_id=owner_id, **{field: True}).exists()


# --- Titulares (AccountOwner) ---

_MAX_OWNER_NAME_LENGTH = 100


def list_owners():
    return AccountOwner.objects.all()


def _clean_owner_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Nome do titular é obrigatório.")
    if len(name) > _MAX_OWNER_NAME_LENGTH:
        raise ValueError(f"Nome do titular não pode exceder {_MAX_OWNER_NAME_LENGTH} caracteres.")
    return name


def create_owner(name: str) -> AccountOwner:
    clean_name = _clean_owner_name(name)
    if AccountOwner.objects.filter(name__iexact=clean_name).exists():
        raise ValueError("Já existe um titular com esse nome.")
    try:
        return AccountOwner.objects.create(name=clean_name)
    except IntegrityError as exc:
        raise ValueError("Já existe um titular com esse nome.") from exc


def update_owner(owner: AccountOwner, name: str) -> AccountOwner:
    clean_name = _clean_owner_name(name)
    if AccountOwner.objects.filter(name__iexact=clean_name).exclude(id=owner.id).exists():
        raise ValueError("Já existe um titular com esse nome.")
    owner.name = clean_name
    try:
        owner.save(update_fields=["name", "updated_at"])
    except IntegrityError as exc:
        raise ValueError("Já existe um titular com esse nome.") from exc
    return owner


def delete_owner(owner: AccountOwner) -> None:
    try:
        owner.delete()
    except ProtectedError as exc:
        raise ValueError(
            "Não é possível excluir este titular: existem contas vinculadas a ele."
        ) from exc


def change_user_password(
    user: AppUser,
    current_password: str | None,
    new_password: str | None,
    confirmation: str | None,
) -> None:
    """Troca a senha do usuário autenticado.

    Exige a senha atual correta (troca sem ela transformaria uma sessão
    sequestrada em tomada de conta), confirmação idêntica e aderência à
    política de complexidade (AUTH_PASSWORD_VALIDATORS)."""
    if not user.check_password(current_password or ""):
        raise ValueError("Senha atual inválida.")
    if new_password != confirmation:
        raise ValueError("A confirmação de senha não confere.")
    if new_password == current_password:
        # O caso que motiva a regra é a senha temporária: quem foi obrigado a
        # trocar redigita a que o administrador acabou de ditar, a marca se
        # apaga, e a senha que um terceiro conhece continua valendo --
        # exatamente o que a obrigação existia para impedir. Mesma regra de
        # `sharedauth.passwords.validar_troca` nos três apps Flask.
        raise ValueError("A nova senha deve ser diferente da senha atual.")
    try:
        validate_password(new_password or "", user=user)
    except ValidationError as exc:
        raise ValueError(" ".join(exc.messages)) from exc

    user.set_password(new_password)
    user.password_updated_at = timezone.now()
    user.must_change_password = False
    user.save(update_fields=["password", "password_updated_at", "must_change_password", "updated_at"])


# --- Visibilidade de contas (Configurações > Visibilidade de contas) ---

def _visible_accounts_for_user(user: AppUser | None):
    from banking.models import FinancialAccount

    if user is None:
        return FinancialAccount.objects.none()
    owner_ids = accessible_owner_ids(user, "view")
    if not owner_ids:
        return FinancialAccount.objects.none()
    return FinancialAccount.objects.select_related("owner", "institution").filter(
        owner_id__in=owner_ids
    ).order_by("owner__name", "institution__institution_name", "account_name")


def account_visibility_options(user: AppUser | None):
    """Lista (account, hide_from_dashboard, hide_from_projections) para o formulário."""
    visibility = {
        row.account_id: row
        for row in UserAccountVisibility.objects.filter(user=user)
    } if user else {}
    return [
        {
            "account": account,
            "hide_from_dashboard": bool(visibility.get(account.id) and visibility[account.id].hide_from_dashboard),
            "hide_from_projections": bool(visibility.get(account.id) and visibility[account.id].hide_from_projections),
        }
        for account in _visible_accounts_for_user(user)
    ]


def hidden_account_ids(user: AppUser | None, scope: str) -> set[int]:
    """IDs de contas ocultas para `user` no escopo informado ('dashboard' ou 'projections')."""
    if user is None or scope not in {"dashboard", "projections"}:
        return set()
    owner_ids = accessible_owner_ids(user, "view")
    if not owner_ids:
        return set()
    field = "hide_from_dashboard" if scope == "dashboard" else "hide_from_projections"
    return set(
        UserAccountVisibility.objects.filter(
            user=user, account__owner_id__in=owner_ids, **{field: True}
        ).values_list("account_id", flat=True)
    )


def update_user_account_visibility(
    user: AppUser,
    *,
    hidden_dashboard_ids: set[int],
    hidden_projection_ids: set[int],
) -> None:
    visible_ids = {account.id for account in _visible_accounts_for_user(user)}
    dashboard_ids = hidden_dashboard_ids & visible_ids
    projection_ids = hidden_projection_ids & visible_ids
    desired_ids = dashboard_ids | projection_ids

    existing = {row.account_id: row for row in UserAccountVisibility.objects.filter(user=user)}
    for account_id, row in existing.items():
        if account_id not in visible_ids:
            continue
        if account_id not in desired_ids:
            row.delete()
            continue
        row.hide_from_dashboard = account_id in dashboard_ids
        row.hide_from_projections = account_id in projection_ids
        row.save(update_fields=["hide_from_dashboard", "hide_from_projections", "updated_at"])

    for account_id in sorted(desired_ids - existing.keys()):
        UserAccountVisibility.objects.create(
            user=user,
            account_id=account_id,
            hide_from_dashboard=account_id in dashboard_ids,
            hide_from_projections=account_id in projection_ids,
        )
