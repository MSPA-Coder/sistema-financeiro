"""Views do shell Django: Permissões e Configurações."""

from __future__ import annotations

from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from accounts.models import AccountOwner, AppUser
from accounts.services import (
    CRITICAL_PERMISSION_KEYS,
    PERMISSION_DEFINITIONS,
    PERMISSION_DEPENDENCIES,
    PROFILE_DEFINITIONS,
    account_visibility_options,
    allowed_permission_keys,
    create_managed_user,
    delete_managed_user,
    list_manageable_users,
    owner_access_map,
    permission_catalog_grouped,
    permission_catalog_sections,
    permission_summary,
    profile_permission_keys,
    reset_managed_user_password,
    save_function_permissions,
    save_owner_access_matrix,
    update_managed_user,
    update_user_account_visibility,
    user_mutation_block_message,
)
from core.context_processors import primeira_tela_permitida
from core.domain.identity import USER_TYPE_ADMINISTRATOR, USER_TYPE_LABELS
from core.domain.settings import (
    APP_SETTING_LAST_OPTIMIZE_INFO,
    UI_THEME_LABELS,
)
from core.permissions import permission_required
from core.services import (
    audit_filter_options,
    audit_request_context,
    available_inspection_tables,
    filtered_recent_audit_logs,
    format_last_optimize_info,
    format_last_projection_run,
    get_app_setting,
    get_login_lockout_policy_settings,
    get_password_policy_settings,
    get_recurring_projection_settings,
    inspect_table,
    log_audit_event,
    optimize_database,
    run_database_health_check,
    system_start_date,
    update_login_lockout_policy_settings,
    update_password_policy_settings,
    update_recurring_projection_settings,
    update_system_start_date,
    update_user_table_scroll_rows,
    update_user_ui_theme,
)
from transactions.models import AccountMonthClose
from transactions.recurring_projection import ensure_recurring_projection_horizon
from transactions.services import close_month, reopen_month

# --- Permissões (tela) ---

def _user_audit_snapshot(user: AppUser) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "user_type": user.user_type,
        "must_change_password": bool(user.must_change_password),
    }


@login_required
def inicio_view(request):
    """Manda a pessoa para a primeira tela a que ela tem acesso.

    E o destino do login (`LOGIN_REDIRECT_URL`) e o destino padrao de uma
    negacao de permissao. Ser derivado, e nao fixo, e o que permite que TODA
    tela do sistema exija a sua permissao: nao existe mais uma tela obrigada a
    ficar sem controle so por ser o lugar onde as pessoas caem.

    Quem nao tem permissao para tela nenhuma cai em "Alterar senha", que e
    aberta a qualquer autenticado e e a unica coisa que essa pessoa de fato
    pode fazer. Verificado: com todas as permissoes negadas, a varredura do
    menu ja para ali sozinha -- o `or` e a rede de seguranca para o caso de o
    menu mudar, nao o caminho normal.
    """
    return redirect(primeira_tela_permitida(request.user) or reverse("change_password"))


@login_required
@permission_required('permissions.manage')
def permissions_view(request):
    users = list_manageable_users()
    selected_user_id = request.GET.get('user_id') or request.POST.get('user_id')
    selected_user = users.filter(id=selected_user_id).first() if selected_user_id else users.first()
    can_manage_users = request.user.has_perm('tables.users.manage')

    if request.method == 'POST' and request.POST.get('table') == 'users':
        if not can_manage_users:
            messages.warning(request, "Acesso negado: usuário sem permissão para gerenciar usuários.")
            return redirect(f"/permissions/?user_id={selected_user.id}" if selected_user else "/permissions/")

        action = request.POST.get('action')
        target_id = request.POST.get('id')
        target_user = users.filter(id=target_id).first() if target_id else None
        requested_user_type = request.POST.get('user_type')

        if action == 'delete':
            if target_user is None:
                messages.warning(request, "Usuário não encontrado para exclusão.")
            else:
                block_message = user_mutation_block_message('delete', request.user, target_user=target_user)
                if block_message:
                    messages.warning(request, block_message)
                else:
                    log_audit_event("app_user", target_user.id, "delete", old_values=_user_audit_snapshot(target_user), user=request.user)
                    delete_managed_user(target_user)
                    if selected_user and selected_user.id == target_user.id:
                        selected_user = None
                    messages.success(request, "Usuário excluído.")
        elif action == 'edit':
            if target_user is None:
                messages.warning(request, "Usuário não encontrado para edição.")
            else:
                block_message = user_mutation_block_message('edit', request.user, target_user=target_user, requested_user_type=requested_user_type)
                if block_message:
                    messages.warning(request, block_message)
                else:
                    old_values = _user_audit_snapshot(target_user)
                    try:
                        update_managed_user(
                            target_user,
                            username=request.POST.get('username', ''),
                            user_type=requested_user_type,
                            password=request.POST.get('password'),
                            password_confirm=request.POST.get('password_confirm'),
                            must_change_password=request.POST.get('must_change_password') == 'on',
                        )
                        new_values = _user_audit_snapshot(target_user)
                        log_audit_event("app_user", target_user.id, "update", old_values=old_values, new_values=new_values, user=request.user)
                        messages.success(request, "Usuário atualizado.")
                        selected_user = target_user
                    except ValueError as exc:
                        messages.error(request, str(exc))
        elif action == 'reset_password':
            if target_user is None:
                messages.warning(request, "Usuário não encontrado para redefinição de senha.")
            else:
                block_message = user_mutation_block_message(
                    'edit', request.user, target_user=target_user,
                    requested_user_type=target_user.user_type,
                )
                if block_message:
                    messages.warning(request, block_message)
                else:
                    old_values = _user_audit_snapshot(target_user)
                    try:
                        senha_temporaria = reset_managed_user_password(target_user)
                    except ValueError as exc:
                        messages.error(request, str(exc))
                    else:
                        # A senha NAO entra na auditoria, nem redigida: nao ha
                        # pergunta que ela responda e ha muitas que ela abre.
                        log_audit_event(
                            "app_user", target_user.id, "password_reset",
                            old_values=old_values,
                            new_values=_user_audit_snapshot(target_user),
                            user=request.user,
                        )
                        # Responde RENDERIZANDO, e nao com o redirect que as
                        # demais acoes usam: a senha temporaria e a unica copia
                        # em texto claro que vai existir, e um redirect a
                        # perderia no caminho. Tambem nao vai por `messages`,
                        # que a guardaria na sessao.
                        return _render_permissions(
                            request, users, target_user, can_manage_users,
                            senha_temporaria=senha_temporaria,
                            senha_de=target_user.username,
                        )
        elif action == 'add':
            block_message = user_mutation_block_message('add', request.user, requested_user_type=requested_user_type)
            if block_message:
                messages.warning(request, block_message)
            else:
                try:
                    new_user = create_managed_user(
                        username=request.POST.get('username', ''),
                        user_type=requested_user_type,
                        password=request.POST.get('password', ''),
                        password_confirm=request.POST.get('password_confirm', ''),
                    )
                    log_audit_event("app_user", new_user.id, "create", new_values=_user_audit_snapshot(new_user), user=request.user)
                    messages.success(request, "Usuário cadastrado.")
                    selected_user = new_user
                except ValueError as exc:
                    messages.error(request, str(exc))
        return redirect(f"/permissions/?user_id={selected_user.id}" if selected_user else "/permissions/")

    if request.method == 'POST' and selected_user:
        # As tres acoes abaixo CONCEDEM privilegio -- permissao funcional,
        # acesso por titular e o perfil rapido que grava as duas. Conceder
        # privilegio e ato administrativo, entao exigem `administrator` e nao
        # apenas `permissions.manage`.
        #
        # Sem esta trava, quem recebesse `permissions.manage` sem ser
        # administrador se selecionava em `?user_id=<o proprio id>`, marcava
        # todas as caixas e todos os titulares, e passava a enxergar e alterar
        # o dado financeiro inteiro. A unica protecao que existia aqui e a de
        # `save_function_permissions`, logo abaixo, e ela impede REMOVER a
        # propria `permissions.manage` -- nao impede ACRESCENTAR o resto.
        #
        # A trava equivalente para o TIPO de usuario ja existia, em
        # `accounts.services.user_mutation_block_message`: "somente
        # administrator pode criar ou promover usuarios privilegiados". Esta e
        # a metade que faltava, a do escopo de dados.
        #
        # `permissions.manage` continua valendo para ABRIR a tela e ler a
        # matriz -- e o que a torna util para quem audita sem administrar.
        if request.user.user_type != USER_TYPE_ADMINISTRATOR:
            messages.warning(
                request,
                "Acesso negado: somente administrator pode alterar permissões "
                "e acessos por titular.",
            )
            return redirect(f"/permissions/?user_id={selected_user.id}")

        action = request.POST.get('action')
        if action == 'save_function_permissions':
            old_keys = sorted(allowed_permission_keys(selected_user))
            allowed_keys = {
                key for key in PERMISSION_DEFINITIONS
                if request.POST.get(f'permission_{key}') == 'on'
            }
            if selected_user.id == request.user.id and 'permissions.manage' not in allowed_keys:
                allowed_keys.add('permissions.manage')
                messages.warning(request, "Proteção aplicada: você não pode remover o próprio acesso a permissions.manage.")
            save_function_permissions(selected_user, allowed_keys)
            log_audit_event(
                "app_user_permissions", selected_user.id, "update",
                old_values={"permission_keys": old_keys},
                new_values={"permission_keys": sorted(allowed_permission_keys(selected_user))},
                user=request.user,
            )
            messages.success(request, "Permissões funcionais atualizadas.")
        elif action == 'save_owner_access':
            old_access = [
                {"owner_id": a.owner_id, "view": a.can_view, "create": a.can_create, "update": a.can_update, "delete": a.can_delete}
                for a in owner_access_map(selected_user).values()
            ]
            owner_flags: dict[int, dict[str, bool]] = {}
            for owner in AccountOwner.objects.all():
                owner_flags[owner.id] = {
                    'view': request.POST.get(f'owner_{owner.id}_view') == 'on',
                    'create': request.POST.get(f'owner_{owner.id}_create') == 'on',
                    'update': request.POST.get(f'owner_{owner.id}_update') == 'on',
                    'delete': request.POST.get(f'owner_{owner.id}_delete') == 'on',
                }
            save_owner_access_matrix(selected_user, owner_flags)
            new_access = [
                {"owner_id": a.owner_id, "view": a.can_view, "create": a.can_create, "update": a.can_update, "delete": a.can_delete}
                for a in owner_access_map(selected_user).values()
            ]
            log_audit_event(
                "app_user_owner_access", selected_user.id, "update",
                old_values={"access": old_access}, new_values={"access": new_access},
                user=request.user,
            )
            messages.success(request, "Acessos por titular atualizados.")
        elif action == 'apply_profile':
            profile_key = request.POST.get('profile_key', '')
            allowed_keys = profile_permission_keys(profile_key)
            if allowed_keys is None:
                messages.warning(request, "Perfil rápido inválido.")
            else:
                old_keys = sorted(allowed_permission_keys(selected_user))
                if selected_user.id == request.user.id:
                    allowed_keys = allowed_keys | {'permissions.manage'}
                save_function_permissions(selected_user, allowed_keys)
                log_audit_event(
                    "app_user_permissions", selected_user.id, "profile_apply",
                    old_values={"permission_keys": old_keys},
                    new_values={"profile_key": profile_key, "permission_keys": sorted(allowed_permission_keys(selected_user))},
                    user=request.user,
                )
                profile_label = PROFILE_DEFINITIONS[profile_key]["label"]
                messages.success(request, f"Perfil rápido aplicado: {profile_label}.")
        return redirect(f"/permissions/?user_id={selected_user.id}")

    return _render_permissions(request, users, selected_user, can_manage_users)


def _render_permissions(request, users, selected_user, can_manage_users, **extra):
    """Monta e renderiza a tela de permissões.

    Extraída para que a redefinição de senha possa responder renderizando, em
    vez do redirect que as demais ações usam -- ver o comentário lá.
    """
    permission_groups = permission_catalog_grouped()
    context = {
        'users': users,
        'owners': AccountOwner.objects.all(),
        'selected_user': selected_user,
        'permission_definitions': PERMISSION_DEFINITIONS,
        'allowed_permission_keys': allowed_permission_keys(selected_user) if selected_user else set(),
        'owner_access_by_owner_id': owner_access_map(selected_user) if selected_user else {},
        'protect_self_permissions_manage': bool(selected_user and selected_user.id == request.user.id),
        'permission_sections': permission_catalog_sections(permission_groups),
        'summary': permission_summary(selected_user),
        'critical_permission_keys': CRITICAL_PERMISSION_KEYS,
        'permission_dependency_keys': PERMISSION_DEPENDENCIES,
        'profile_definitions': PROFILE_DEFINITIONS,
        'user_type_options': list(USER_TYPE_LABELS.values()),
        'can_manage_users': can_manage_users,
        # Esconde os tres botoes que CONCEDEM privilegio. E apresentacao: o
        # controle esta na propria `permissions_view`, que recusa o POST. Existe
        # para a tela nao oferecer um botao que o servidor vai negar -- quem tem
        # `permissions.manage` sem ser administrador continua lendo a matriz,
        # que e para o que a permissao serve agora.
        'pode_conceder': request.user.user_type == USER_TYPE_ADMINISTRATOR,
        **extra,
    }
    return render(request, "permissions/index.html", context)


# --- Configurações > Perfil ---

@login_required
@permission_required('settings.view')
def settings_profile_view(request):
    from core.domain.settings import UI_THEME_DESCRIPTIONS
    return render(request, "settings/profile.html", {
        "theme_options": UI_THEME_LABELS,
        "theme_descriptions": UI_THEME_DESCRIPTIONS,
        "current_theme": request.user.ui_theme,
        "current_table_scroll_rows": request.user.table_scroll_rows,
    })


@login_required
@permission_required('settings.theme.update')
@require_POST
def settings_update_theme_view(request):
    if request.method == 'POST':
        new_theme = update_user_ui_theme(request.user, request.POST.get('theme', ''))
        messages.success(request, f"Tema alterado para: {UI_THEME_LABELS.get(new_theme, 'Light')}")
    return redirect('core:settings_profile')


@login_required
@permission_required('settings.theme.update')
@require_POST
def settings_update_table_scroll_view(request):
    if request.method == 'POST':
        try:
            rows = update_user_table_scroll_rows(request.user, request.POST.get('table_scroll_rows'))
            messages.success(request, f"Scroll de tabelas ajustado para mais de {rows} registro(s).")
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('core:settings_profile')


# --- Configurações > Visibilidade de contas ---

@login_required
@permission_required('settings.view')
def settings_visibility_view(request):
    if request.method == 'POST':
        hidden_dashboard_ids = {int(v) for v in request.POST.getlist('hide_from_dashboard')}
        hidden_projection_ids = {int(v) for v in request.POST.getlist('hide_from_projections')}
        update_user_account_visibility(
            request.user,
            hidden_dashboard_ids=hidden_dashboard_ids,
            hidden_projection_ids=hidden_projection_ids,
        )
        messages.success(request, "Preferências de contas atualizadas.")
        return redirect('core:settings_visibility')

    return render(request, "settings/account_visibility.html", {
        "account_visibility_options": account_visibility_options(request.user),
    })


# --- Configurações > Fechamento mensal ---

_MONTHLY_CLOSE_FILTER_NAMES = ("filter_account_id", "filter_year", "filter_month", "filter_status")


def _monthly_close_filters(request, account_ids: list[int]) -> dict[str, int | str | None]:
    """Normaliza os filtros da lista de fechamentos no escopo do usuário."""
    filters: dict[str, int | str | None] = {
        "account_id": None,
        "year": None,
        "month": None,
        "status": (request.GET.get("filter_status") or "").strip(),
    }

    try:
        account_id = int(request.GET.get("filter_account_id") or "")
    except (TypeError, ValueError):
        account_id = None
    if account_id in account_ids:
        filters["account_id"] = account_id

    try:
        year = int(request.GET.get("filter_year") or "")
    except (TypeError, ValueError):
        year = None
    if year and 2000 <= year <= 2100:
        filters["year"] = year

    try:
        month = int(request.GET.get("filter_month") or "")
    except (TypeError, ValueError):
        month = None
    if month and 1 <= month <= 12:
        filters["month"] = month

    if filters["status"] not in {"", "active", "reopened"}:
        filters["status"] = ""
    return filters


def _monthly_close_redirect(request):
    """Retorna à lista preservando somente os filtros vindos da própria tela."""
    query = {
        name: request.POST.get(name, "")
        for name in _MONTHLY_CLOSE_FILTER_NAMES
        if request.POST.get(name, "")
    }
    url = reverse("core:settings_monthly_close")
    return redirect(f"{url}?{urlencode(query)}" if query else url)

@login_required
@permission_required('settings.monthly_close.manage')
def settings_monthly_close_view(request):
    from banking.models import FinancialAccount
    from banking.services import accessible_account_ids

    close_accounts = FinancialAccount.objects.select_related("owner", "institution").filter(
        id__in=accessible_account_ids(request.user, "update")
    )
    account_ids = [account.id for account in close_accounts]
    filters = _monthly_close_filters(request, account_ids)
    recent_closes = AccountMonthClose.objects.select_related(
        'account__owner', 'account__institution'
    ).filter(account_id__in=account_ids)
    if filters["account_id"]:
        recent_closes = recent_closes.filter(account_id=filters["account_id"])
    if filters["year"]:
        recent_closes = recent_closes.filter(year=filters["year"])
    if filters["month"]:
        recent_closes = recent_closes.filter(month=filters["month"])
    if filters["status"] == "active":
        recent_closes = recent_closes.filter(active=True)
    elif filters["status"] == "reopened":
        recent_closes = recent_closes.filter(active=False)
    recent_closes = recent_closes.order_by('-year', '-month', '-id')[:240]
    previous_month = date.today().replace(day=1) - timedelta(days=1)

    return render(request, "settings/monthly_close.html", {
        "close_accounts": close_accounts,
        "recent_month_closes": recent_closes,
        "monthly_close_year_options": range(2000, 2101),
        "monthly_close_month_options": range(1, 13),
        "monthly_close_filters": filters,
        "monthly_close_default_month": previous_month.month,
        "monthly_close_default_year": previous_month.year,
    })


@login_required
@permission_required('settings.monthly_close.manage')
@require_POST
def settings_close_month_view(request):
    from banking.models import FinancialAccount
    from banking.services import can_access_account
    from core.domain.finance import VIEW_REALIZED
    from reports.services import decimal_balance_before, month_bounds

    try:
        account_id = int(request.POST.get('account_id'))
        year = int(request.POST.get('year'))
        month = int(request.POST.get('month'))
        account = FinancialAccount.objects.get(id=account_id)
        if not can_access_account(request.user, account.id, "update"):
            raise ValueError("Acesso negado: usuário sem permissão para fechar este mês.")
        _start, end_exclusive = month_bounds(year, month)
        closing_balance = decimal_balance_before([account.id], end_exclusive, VIEW_REALIZED)
        closed = close_month(
            account,
            year,
            month,
            closing_balance,
            request.user,
            audit_context=audit_request_context(request),
        )
        messages.success(request, f"Mês {closed.month:02d}/{closed.year} fechado para a conta #{closed.account_id}.")
    except (ValueError, TypeError, FinancialAccount.DoesNotExist) as exc:
        log_audit_event(
            "account_month_close",
            request.POST.get("account_id"),
            "close",
            request_context=audit_request_context(request),
            result="failure",
            summary="Fechamento mensal recusado pela validação do servidor.",
        )
        messages.error(request, str(exc) or "Dados inválidos para fechamento mensal.")
    return _monthly_close_redirect(request)


@login_required
@permission_required('settings.monthly_close.manage')
@require_POST
def settings_reopen_month_view(request):
    from banking.models import FinancialAccount
    from banking.services import can_access_account

    try:
        account_id = int(request.POST.get('account_id'))
        year = int(request.POST.get('year'))
        month = int(request.POST.get('month'))
        account = FinancialAccount.objects.get(id=account_id)
        if not can_access_account(request.user, account.id, "update"):
            raise ValueError("Acesso negado: usuário sem permissão para reabrir este mês.")
        reopened = reopen_month(
            account,
            year,
            month,
            request.POST.get('reason', ''),
            request.user,
            audit_context=audit_request_context(request),
        )
        messages.success(request, f"Mês {reopened.month:02d}/{reopened.year} reaberto para a conta #{reopened.account_id}.")
    except (ValueError, TypeError, FinancialAccount.DoesNotExist) as exc:
        log_audit_event(
            "account_month_close",
            request.POST.get("account_id"),
            "reopen",
            request_context=audit_request_context(request),
            result="failure",
            summary="Reabertura mensal recusada pela validação do servidor.",
        )
        messages.error(request, str(exc) or "Fechamento mensal inválido.")
    return _monthly_close_redirect(request)


# --- Configurações > Banco de dados ---

@login_required
@permission_required('settings.view')
def settings_database_view(request):
    selected_table = request.GET.get('table_name') or ''
    table_columns, table_rows = inspect_table(selected_table)
    return render(request, "settings/database.html", {
        "last_optimize_info": format_last_optimize_info(get_app_setting(APP_SETTING_LAST_OPTIMIZE_INFO)),
        "available_tables": available_inspection_tables(),
        "selected_table": selected_table,
        "table_columns": table_columns,
        "table_rows": table_rows,
    })


@login_required
@permission_required('settings.database.optimize')
@require_POST
def settings_health_check_view(request):
    if request.method == 'POST':
        result = run_database_health_check()
        if result.ok:
            messages.success(request, f"Health check concluído. {result.summary}")
        else:
            messages.warning(request, f"Health check encontrou inconsistência(s). {result.summary}")
            for label, count in result.orphan_checks:
                if count:
                    messages.warning(request, f"{label}: {count} registro(s) órfão(s).")
    return redirect('core:settings_database')


@login_required
@permission_required('settings.database.optimize')
@require_POST
def settings_optimize_view(request):
    if request.method == 'POST':
        summary = optimize_database()
        messages.success(request, f"Otimização concluída. {summary}")
    return redirect('core:settings_database')


# --- Configurações > Auditoria ---

@login_required
@permission_required('settings.audit.view')
def settings_audit_log_view(request):
    logs, filters = filtered_recent_audit_logs(
        created_on=request.GET.get('created_on'),
        user_name=request.GET.get('user_name'),
        entity_name=request.GET.get('entity_name'),
        entity_id=request.GET.get('entity_id'),
        action=request.GET.get('action'),
    )
    return render(request, "settings/audit_log.html", {
        "recent_audit_logs": logs,
        "audit_filters": filters,
        "audit_filter_options": audit_filter_options(),
    })


# --- Configurações > Parâmetros ---

@login_required
@permission_required('settings.view')
def settings_home_view(request):
    projection_settings = get_recurring_projection_settings()
    return render(request, "settings/index.html", {
        "projection_settings": projection_settings,
        "last_projection_run": format_last_projection_run(projection_settings.last_projection_run),
        "password_policy_settings": get_password_policy_settings(),
        "login_lockout_policy_settings": get_login_lockout_policy_settings(),
        "current_system_start_date": system_start_date(),
    })


@login_required
@permission_required('settings.password_policy.manage')
@require_POST
def settings_update_password_policy_view(request):
    if request.method == 'POST':
        try:
            settings = update_password_policy_settings(
                min_length=request.POST.get('min_length'),
                min_uppercase=request.POST.get('min_uppercase'),
                min_numbers=request.POST.get('min_numbers'),
                min_special=request.POST.get('min_special'),
            )
            messages.success(
                request,
                f"Política de senha atualizada: {settings.min_length} caractere(s), "
                f"{settings.min_uppercase} maiúscula(s), {settings.min_numbers} número(s), "
                f"{settings.min_special} especial(is).",
            )
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('core:settings_home')


@login_required
@permission_required('settings.password_policy.manage')
@require_POST
def settings_update_login_lockout_view(request):
    if request.method == 'POST':
        try:
            settings = update_login_lockout_policy_settings(
                max_failures=request.POST.get('max_failures'),
                lock_minutes=request.POST.get('lock_minutes'),
            )
            messages.success(
                request,
                f"Bloqueio de login atualizado: {settings.max_failures} tentativa(s), "
                f"{settings.lock_minutes} minuto(s).",
            )
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('core:settings_home')


@login_required
@permission_required('settings.projection.manage')
@require_POST
def settings_update_recurring_projection_view(request):
    if request.method == 'POST':
        try:
            settings = update_recurring_projection_settings(
                horizon_months=request.POST.get('horizon_months'),
                run_day=request.POST.get('run_day'),
            )
            update_system_start_date(request.POST.get('system_start_date'))
            messages.success(
                request,
                f"Projeção de recorrências ajustada para {settings.horizon_months} mês(es), "
                f"execução no dia {settings.run_day}.",
            )
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect('core:settings_home')


@login_required
@permission_required('settings.projection.manage')
@require_POST
def settings_run_recurring_projection_view(request):
    # Nao ha guarda de "ja executou este mes", e isso e deliberado -- ver
    # `transactions/recurring_projection.py`. A projecao preenche ate o
    # horizonte e so olha para frente: reexecutar no mesmo mes gera zero. O
    # numero na mensagem diz exatamente isso ao usuario.
    if request.method == 'POST':
        result = ensure_recurring_projection_horizon(update_last_run=True)
        messages.success(
            request,
            f"Projeção de recorrências executada. {result.generated_count} lançamento(s) "
            f"gerado(s) até {result.horizon_end.strftime('%d/%m/%Y')}.",
        )
    return redirect('core:settings_home')
