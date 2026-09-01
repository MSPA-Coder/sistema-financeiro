"""Views de transações com HTMX (Movimentação > Lançamentos)."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.domain.finance import (
    CALC_REPEAT,
    OPERATION_SCOPE_SINGLE,
    STATUS_FILTER_OPTIONS,
    STATUS_OPTIONS,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VALID_OPERATION_SCOPES,
)
from core.htmx import quer_fragmento
from core.permissions import permission_required
from core.services import audit_request_context, log_audit_event
from transactions import access
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.operations import OPERATION_LABELS, operations_page_for_user
from transactions.services import (
    TransactionRequest,
    build_transactions_view_context,
    create_category,
    create_transaction_batch,
    delete_category,
    delete_transaction_or_operation,
    list_categories,
    possible_duplicates_for_created_entries,
    realize_transaction,
    supports_operation_scope,
    transactions_query_params,
    update_category,
    update_transaction_operation,
)


def _parse_int(value, default=1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_decimal(raw, *, default=None):
    if raw in (None, ""):
        return default
    try:
        return Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Valor monetário inválido: {raw}") from exc


def _parse_date(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _operation_scope_from_request(tx: CashFlowEntry, raw: str | None) -> str:
    """Valida o escopo antes de uma mutação de grupo.

    Um lançamento simples não oferece seletor de escopo; nesse caso o único
    comportamento explícito é operar sobre ele próprio. Para grupos, porém,
    ausência ou valor desconhecido nunca pode virar ``all`` por fallback.
    """
    if raw in (None, ""):
        if not supports_operation_scope(tx):
            return OPERATION_SCOPE_SINGLE
        raise ValueError("Escolha o escopo da operação antes de confirmar.")
    if raw not in VALID_OPERATION_SCOPES:
        raise ValueError("Escopo de operação inválido.")
    if not supports_operation_scope(tx):
        return OPERATION_SCOPE_SINGLE
    return raw


def _invalid_operation_scope_response(exc: ValueError) -> HttpResponseBadRequest:
    """Falha fechada para uma intenção destrutiva ambígua ou adulterada."""
    return HttpResponseBadRequest(str(exc))


def _redirect_to_transactions(request, extra_params=None):
    params = transactions_query_params(request.GET)
    if extra_params:
        params.update({key: value for key, value in extra_params.items() if value})
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = "/transactions/"
    if query:
        url = f"{url}?{query}"
    return redirect(url)


def _log_failed_transaction_action(request, entry_id, action: str) -> None:
    """Registra a tentativa sem armazenar a mensagem ou os dados enviados."""
    log_audit_event(
        "cash_flow_entry",
        entry_id,
        action,
        request_context=audit_request_context(request),
        result="failure",
        summary="Operação recusada pela validação do servidor.",
    )


def _transaction_request_from_post(post) -> TransactionRequest:
    account_id = _parse_int(post.get("account_id"), default=None)
    category_id = _parse_int(post.get("category_id"), default=None)
    counterparty_account_id = _parse_int(post.get("counterparty_account_id"), default=None)
    entry_type = post.get("entry_type")
    due_date = _parse_date(post.get("due_date"))

    if not all([account_id, category_id, entry_type, post.get("entry_amount"), due_date]):
        raise ValueError("Preencha todos os campos obrigatórios.")

    entry_amount = _to_decimal(post.get("entry_amount"))
    if entry_amount is None:
        raise ValueError("Valor do lançamento é obrigatório.")

    installments = _parse_int(post.get("installments"), default=1)
    status = post.get("status", STATUS_PROJECTED)
    realized_date = _parse_date(post.get("realized_date")) if status == STATUS_REALIZED else None
    realized_amount = _to_decimal(post.get("realized_amount")) if status == STATUS_REALIZED else None

    description = (post.get("description") or "").strip()

    return TransactionRequest(
        account_id=account_id,
        category_id=category_id,
        entry_type=entry_type,
        description=description,
        entry_amount=entry_amount,
        installments=installments,
        due_date=due_date,
        calc_mode=post.get("calc_mode", CALC_REPEAT),
        is_recurring=post.get("is_recurring") == "on",
        status=status,
        realized_date=realized_date,
        realized_amount=realized_amount,
        counterparty_account_id=counterparty_account_id,
    )


@login_required
@permission_required("transactions.view")
def transactions_view(request):
    """Lista de transações com filtros, saldo corrente e resumo (HTMX)."""
    context = build_transactions_view_context(
        request.user, request.GET, request.session, request=request
    )
    show_balance_column = not context["dashboard_drilldown"]
    show_actions_column = not context["dashboard_drilldown"]
    table_columns = 6 + (1 if context["view_mode"] == STATUS_REALIZED else 0) \
        + (1 if show_balance_column else 0) + (1 if show_actions_column else 0)
    context.update({
        "status_options": STATUS_OPTIONS,
        "can_create_transactions": request.user.has_perm("transactions.create"),
        "can_update_transactions": request.user.has_perm("transactions.update"),
        "can_delete_transactions": request.user.has_perm("transactions.delete"),
        "can_realize_transactions": request.user.has_perm("transactions.realize"),
        "can_view_operations": request.user.has_perm("operations.view"),
        "show_balance_column": show_balance_column,
        "show_actions_column": show_actions_column,
        "table_columns": table_columns,
        "filter_actions_colspan": table_columns - 3,
    })

    if quer_fragmento(request):
        # _table_body.html inclui uma copia OOB (out-of-band) dos cards de resumo
        # para o HTMX atualizar os totais fora da area trocada pelo hx-swap normal.
        # So faz sentido quando este template e a resposta HTMX inteira -- quando
        # index.html o inclui no carregamento normal da pagina, essa copia extra
        # duplicaria os cards visivelmente (o navegador nao entende hx-swap-oob).
        context["render_oob_summary"] = True
        return render(request, "transactions/_table_body.html", context)
    return render(request, "transactions/index.html", context)


@login_required
@permission_required("transactions.realize", fallback="transactions:transactions_view")
@require_POST
def mark_realized(request, tx_id):
    """Marca uma transação (e a contraparte de uma transferência interna) como realizada."""
    entry = CashFlowEntry.objects.filter(id=tx_id).select_related("account", "source_entry").first()
    if entry is None:
        _log_failed_transaction_action(request, tx_id, "realize")
        messages.warning(request, "Lançamento não encontrado.")
        return _redirect_to_transactions(request)
    if not access.can_access_entry(request.user, entry, "update"):
        _log_failed_transaction_action(request, entry.id, "realize")
        messages.warning(request, "Acesso negado: usuário sem permissão para realizar este lançamento.")
        return _redirect_to_transactions(request)

    realized_date = _parse_date(request.POST.get("realized_date"))
    try:
        realized_amount = _to_decimal(request.POST.get("realized_amount"))
    except ValueError as exc:
        _log_failed_transaction_action(request, entry.id, "realize")
        messages.error(request, str(exc))
        return _redirect_to_transactions(request)

    try:
        realize_transaction(
            entry,
            realized_date,
            realized_amount,
            audit_context=audit_request_context(request),
        )
        messages.success(request, "Lançamento marcado como realizado.")
    except ValueError as e:
        _log_failed_transaction_action(request, entry.id, "realize")
        messages.error(request, str(e))

    if quer_fragmento(request):
        response = HttpResponse(status=204)
        response.headers["HX-Trigger"] = "tableRefresh"
        return response
    return _redirect_to_transactions(request)


@login_required
@permission_required("transactions.create", fallback="transactions:transactions_view")
def transaction_new(request):
    """Renderiza o formulário; a gravação delega a uma função POST-only."""
    if request.method == "POST":
        return _transaction_new_post(request)
    return _redirect_to_transactions(request, {"new_entry_open": "1"})


@require_POST
def _transaction_new_post(request):
    try:
        req = _transaction_request_from_post(request.POST)
    except ValueError as exc:
        _log_failed_transaction_action(request, None, "create")
        messages.error(request, str(exc))
        return _redirect_to_transactions(request)

    if not access.can_access_account(request.user, req.account_id, "create"):
        _log_failed_transaction_action(request, None, "create")
        messages.warning(request, "Acesso negado: usuário sem permissão para criar lançamentos nesta conta.")
        return _redirect_to_transactions(request)

    try:
        entries = create_transaction_batch(req, audit_context=audit_request_context(request))
        messages.success(request, "Lançamento(s) criado(s) com sucesso.")
        duplicates = possible_duplicates_for_created_entries(req, entries)
        if duplicates:
            ids = ", ".join(f"#{entry.id}" for entry in duplicates[:5])
            messages.warning(
                request,
                f"Atenção: existem movimentos semelhantes na mesma conta, data e valor: {ids}.",
            )
    except ValueError as e:
        _log_failed_transaction_action(request, None, "create")
        messages.error(request, str(e))
        return _redirect_to_transactions(request)

    if request.POST.get("keep_entry_form_open") == "1":
        return _redirect_to_transactions(request, {
            "new_entry_open": "1",
            "new_account_id": str(req.account_id),
            "new_entry_type": req.entry_type,
            "new_status": req.status,
            "new_due_date": req.due_date.isoformat(),
        })

    if quer_fragmento(request):
        response = HttpResponse(status=204)
        response.headers["HX-Trigger"] = "tableRefresh"
        return response
    return _redirect_to_transactions(request)


@login_required
@permission_required("transactions.update", fallback="transactions:transactions_view")
def transaction_edit(request, tx_id):
    """Edita um lançamento (ou o grupo/bloco escolhido pelo escopo de operação)."""
    tx = CashFlowEntry.objects.filter(id=tx_id).select_related("account", "source_entry").first()
    if tx is None:
        messages.warning(request, "Lançamento não encontrado.")
        return _redirect_to_transactions(request)
    if not access.can_access_entry(request.user, tx, "update"):
        messages.warning(request, "Acesso negado: usuário sem permissão para editar lançamentos nesta conta.")
        return _redirect_to_transactions(request)

    if request.method == "POST":
        return _transaction_edit_post(request, tx)
    return _redirect_to_transactions(request)


@require_POST
def _transaction_edit_post(request, tx):
    try:
        req = _transaction_request_from_post(request.POST)
    except ValueError as exc:
        _log_failed_transaction_action(request, tx.id, "update")
        messages.error(request, str(exc))
        return _redirect_to_transactions(request)

    if not access.can_access_account(request.user, req.account_id, "update"):
        _log_failed_transaction_action(request, tx.id, "update")
        messages.warning(request, "Acesso negado: usuário sem permissão para editar lançamentos nesta conta.")
        return _redirect_to_transactions(request)

    try:
        scope = _operation_scope_from_request(tx, request.POST.get("operation_scope"))
    except ValueError as exc:
        _log_failed_transaction_action(request, tx.id, "update")
        return _invalid_operation_scope_response(exc)
    try:
        update_transaction_operation(
            tx,
            req,
            scope,
            request.POST.get("current_future_confirmation_token"),
            audit_context=audit_request_context(request),
        )
        messages.success(request, "Lançamento(s) atualizado(s) com sucesso.")
    except ValueError as e:
        _log_failed_transaction_action(request, tx.id, "update")
        messages.error(request, str(e))
        return _redirect_to_transactions(request)

    if quer_fragmento(request):
        response = HttpResponse(status=204)
        response.headers["HX-Trigger"] = "tableRefresh"
        return response
    return _redirect_to_transactions(request)


@login_required
@permission_required("transactions.delete", fallback="transactions:transactions_view")
@require_POST
def transaction_delete(request, tx_id):
    """Exclui um lançamento (ou o grupo/bloco escolhido pelo escopo de operação)."""
    tx = CashFlowEntry.objects.filter(id=tx_id).select_related("account", "source_entry").first()
    if tx is None:
        _log_failed_transaction_action(request, tx_id, "delete")
        messages.warning(request, "Lançamento não encontrado.")
        return _redirect_to_transactions(request)
    if not access.can_access_entry(request.user, tx, "delete"):
        _log_failed_transaction_action(request, tx.id, "delete")
        messages.warning(request, "Acesso negado: usuário sem permissão para excluir lançamentos nesta conta.")
        return _redirect_to_transactions(request)

    try:
        scope = _operation_scope_from_request(tx, request.POST.get("operation_scope"))
    except ValueError as exc:
        _log_failed_transaction_action(request, tx.id, "delete")
        return _invalid_operation_scope_response(exc)
    try:
        delete_transaction_or_operation(
            tx,
            scope,
            request.POST.get("current_future_confirmation_token"),
            audit_context=audit_request_context(request),
        )
        messages.success(request, "Lançamento excluído com sucesso.")
    except ValueError as e:
        _log_failed_transaction_action(request, tx.id, "delete")
        messages.error(request, str(e))

    if quer_fragmento(request):
        response = HttpResponse(status=204)
        response.headers["HX-Trigger"] = "tableRefresh"
        return response
    return _redirect_to_transactions(request)


# --- Cadastros: Categorias ---

@login_required
@permission_required('tables.view')
@permission_required('tables.categories.manage')
def categories_view(request):
    """Lista e cadastro de categorias, com suporte a HTMX."""
    current_filter_type = request.GET.get('filter_type') or ''
    context = {
        "categories": list_categories(current_filter_type or None),
        "current_filter_type": current_filter_type,
    }
    if quer_fragmento(request):
        return render(request, 'tables/_categories_table.html', context)
    return render(request, 'tables/categories.html', context)


@login_required
@permission_required('tables.view', fallback='transactions:categories_view')
@permission_required('tables.categories.manage', fallback='transactions:categories_view')
@require_POST
def create_category_view(request):
    try:
        create_category(request.POST.get('category_name', ''), request.POST.get('is_internal') == 'on')
        messages.success(request, "Categoria cadastrada com sucesso.")
    except ValueError as e:
        messages.error(request, str(e))
    return _respond_categories(request)


@login_required
@permission_required('tables.view', fallback='transactions:categories_view')
@permission_required('tables.categories.manage', fallback='transactions:categories_view')
@require_POST
def update_category_view(request, category_id):
    category = get_object_or_404(CashFlowCategory, id=category_id)
    try:
        update_category(category, request.POST.get('category_name', ''), request.POST.get('is_internal') == 'on')
        messages.success(request, "Categoria atualizada com sucesso.")
    except ValueError as e:
        messages.error(request, str(e))
    return _respond_categories(request)


@login_required
@permission_required('tables.view', fallback='transactions:categories_view')
@permission_required('tables.categories.manage', fallback='transactions:categories_view')
@require_POST
def delete_category_view(request, category_id):
    category = get_object_or_404(CashFlowCategory, id=category_id)
    try:
        delete_category(category)
        messages.success(request, "Categoria excluída com sucesso.")
    except ValueError as e:
        messages.error(request, str(e))
    return _respond_categories(request)


def _respond_categories(request):
    if quer_fragmento(request):
        response = HttpResponse(status=200)
        response.headers['HX-Redirect'] = reverse('transactions:categories_view')
        return response
    return redirect('transactions:categories_view')


@login_required
@permission_required('operations.view')
def operations_view(request):
    """Movimentação > Lançamentos n+1: agrupa parcelas, recorrências e
    pares de transferência interna por operation_id, com suporte a HTMX."""
    operation_type = request.GET.get('operation_type', '')
    status = request.GET.get('status', '')
    start = request.GET.get('start', '')
    end = request.GET.get('end', '')
    operation_id = request.GET.get('operation_id', '')
    show_entries = request.GET.get('show_entries', '')
    page = max(_parse_int(request.GET.get('page'), 1), 1)
    page_size = max(_parse_int(request.GET.get('page_size'), 20), 1)

    result = operations_page_for_user(
        request.user,
        operation_type=operation_type,
        status=status,
        start=start,
        end=end,
        operation_id=operation_id,
        page=page,
        page_size=page_size,
    )

    context = {
        'operations': result.operations,
        'total_operations': result.total_operations,
        'operation_type': operation_type,
        'status': status,
        'start': start,
        'end': end,
        'operation_id': operation_id,
        'show_entries': show_entries,
        'page': page,
        'page_size': page_size,
        'operation_labels': OPERATION_LABELS,
        'statuses': [value for value, _label in STATUS_FILTER_OPTIONS],
        'system_start_date': result.system_start_date.isoformat() if result.system_start_date else '',
        'has_next_page': (page * page_size) < result.total_operations,
    }

    if quer_fragmento(request):
        return render(request, 'transactions/_operations_table.html', context)
    return render(request, 'transactions/operations.html', context)
