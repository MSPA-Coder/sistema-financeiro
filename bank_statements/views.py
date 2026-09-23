"""Views de importação, conciliação e anexos bancários (Bancos)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from accounts.services import has_function_permission
from core.htmx import quer_fragmento
from core.permissions import permission_required
from core.services import audit_request_context

from . import fatura, fatura_projetada, reclassificacao, reconciliation
from .attachments import (
    attachment_download_path,
    attachment_for_download,
    delete_entry_attachment,
    recent_attachments_for_user,
    save_entry_attachment,
)
from .services import (
    accounts_for_import_form,
    import_statement_file,
    statement_import_status,
    statement_imports_for_user,
)


@login_required
@permission_required('banking.view')
@permission_required('banking.import', fallback='dashboard:dashboard')
def imports_view(request):
    """Lista de importações recentes e formulário de novo envio."""
    context = {
        "imports": statement_imports_for_user(request.user),
        "import_accounts": accounts_for_import_form(request.user),
    }
    return render(request, 'banking/imports.html', context)


@login_required
@permission_required('banking.import', fallback='bank_statements:imports_view')
@require_POST
def create_import_view(request):
    """Processa o upload de um extrato (CSV ou OFX/OFC/QFX)."""
    try:
        batch, inserted, skipped = import_statement_file(
            request.user,
            account_id=request.POST.get('account_id', ''),
            uploaded_file=request.FILES.get('statement_file'),
        )
        messages.success(
            request,
            f"Extrato importado. Linhas novas: {inserted}. Duplicadas ignoradas: {skipped}.",
        )
        if batch.account.is_credit_card and inserted:
            # A fatura não para nas linhas: segue para a prévia do processamento.
            destino = reverse('bank_statements:fatura', args=[batch.id])
            if quer_fragmento(request):
                resposta = HttpResponse(status=204)
                resposta['HX-Redirect'] = destino
                return resposta
            return redirect(destino)
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        context = {"imports": statement_imports_for_user(request.user)}
        return render(request, 'banking/_import_table.html', context)
    return redirect('bank_statements:imports_view')


@login_required
@permission_required('banking.view')
def import_status_view(request, batch_id):
    """Status resumido de um lote de importação, em JSON."""
    status = statement_import_status(request.user, batch_id)
    if status is None:
        return JsonResponse({"error": "Lote não encontrado"}, status=404)
    return JsonResponse(status)


@login_required
@permission_required('banking.view')
@permission_required('banking.import', fallback='bank_statements:imports_view')
def fatura_view(request, batch_id):
    """Prévia da fatura importada: o que cada linha vai virar."""
    try:
        lote = fatura.lote_de_fatura(request.user, batch_id)
    except ValueError as exc:
        messages.warning(request, str(exc))
        return redirect('bank_statements:imports_view')
    novas = list(fatura.linhas_novas(lote))
    planos = fatura.planejar(lote.account, novas)
    context = {
        "lote": lote,
        "conta": lote.account,
        "planos": planos,
        "tudo_no_saldo_inicial": bool(planos) and all(p.acao == fatura.SALDO_INICIAL for p in planos),
        "resumo": fatura.resumir(lote.lines.all()),
        "processadas": lote.lines.exclude(status="novo").count(),
        "categorias": list(fatura.categorias_gerenciais()),
        "projetadas": fatura_projetada.planejar(lote.account),
    }
    return render(request, 'banking/fatura.html', context)


@login_required
@permission_required('banking.view')
def faturas_view(request):
    """Banking › Faturas: cada cartão com saldo, faturas importadas e projeção."""
    return render(request, 'banking/faturas.html', {"paineis": fatura_projetada.paineis(request.user)})


@login_required
@permission_required('banking.import', fallback='bank_statements:imports_view')
@permission_required('banking.reconcile', fallback='bank_statements:imports_view')
@require_POST
def processar_fatura_view(request, batch_id):
    """Grava a prévia: lança, concilia e ignora, tudo ou nada."""
    escolhas = {
        chave.removeprefix('categoria_'): valor
        for chave, valor in request.POST.items()
        if chave.startswith('categoria_')
    }
    try:
        contagem = fatura.processar(
            request.user, batch_id, escolhas, audit_context=audit_request_context(request)
        )
        pendentes = contagem.get(fatura.PAGAMENTO_SEM_PAR, 0)
        feitas = sum(contagem.values()) - pendentes
        texto = f"Fatura processada: {feitas} linha(s)."
        if pendentes:
            texto += f" {pendentes} pagamento(s) sem transferência ficaram em Conciliação."
        messages.success(request, texto)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect('bank_statements:fatura', batch_id=batch_id)


def _escolhas_da_reclassificacao(post) -> dict:
    return {
        "entry_ids": post.getlist('entry_ids'),
        "categoria_id": post.get('categoria'),
        "incluir_iguais": post.get('iguais') == '1',
        "parecidas_ids": post.getlist('parecidas'),
        "bancos": post.getlist('bancos'),
    }


@login_required
@permission_required('banking.view')
@permission_required('banking.reclassify', fallback='dashboard:dashboard')
@require_http_methods(["GET", "POST"])
def reclassificacao_view(request):
    """Lista para reclassificar em lote; POST monta a prévia ou aplica."""
    voltar = request.POST.get('voltar', '') if request.method == 'POST' else request.GET.urlencode()
    plano = None
    escolhas = {}
    if request.method == 'POST':
        escolhas = _escolhas_da_reclassificacao(request.POST)
        try:
            if request.POST.get('acao') == 'aplicar':
                alterados = reclassificacao.aplicar(
                    request.user,
                    autorizar_meses=request.POST.get('autorizar_meses') == '1',
                    audit_context=audit_request_context(request),
                    **escolhas,
                )
                messages.success(request, f"{alterados} lançamento(s) reclassificado(s).")
                destino = reverse('bank_statements:reclassificacao')
                return redirect(f"{destino}?{voltar}" if voltar else destino)
            plano = reclassificacao.planejar(request.user, **escolhas)
        except ValueError as exc:
            messages.error(request, str(exc))

    filtros = reclassificacao.Filtros.do_get(QueryDict(voltar))
    context = {
        "filtros": filtros,
        "voltar": voltar,
        "lancamentos": reclassificacao.lancamentos(request.user, filtros),
        "limite": reclassificacao.LIMITE_DA_LISTA,
        "categorias": list(fatura.categorias_gerenciais()),
        "categorias_do_banco": reclassificacao.categorias_do_banco(),
        "contas": accounts_for_import_form(request.user),
        "plano": plano,
        "escolhas": escolhas,
        "pode_reabrir": has_function_permission(request.user, 'settings.monthly_close.manage'),
    }
    return render(request, 'banking/reclassificacao.html', context)


def _reconciliation_context(request, *, target_line_id=None):
    return reconciliation.reconciliation_view_data(request.user, target_line_id)


@login_required
@permission_required('banking.view')
@permission_required('banking.reconcile', fallback='dashboard:dashboard')
def reconciliation_view(request):
    """Linhas pendentes de conciliação e conciliações recentes."""
    target_line_id = request.GET.get('line_id')
    context = _reconciliation_context(request, target_line_id=target_line_id)
    return render(request, 'banking/reconciliation.html', context)


@login_required
@permission_required('banking.view')
def reconciliation_refresh_view(request):
    """Atualização HTMX das tabelas de conciliação.

    Exige apenas banking.view: é uma releitura, não uma conciliação. As ações
    que alteram estado continuam exigindo banking.reconcile."""
    target_line_id = request.GET.get('line_id')
    context = _reconciliation_context(request, target_line_id=target_line_id)
    return render(request, 'banking/_reconciliation_tables.html', context)


@login_required
@permission_required('banking.reconcile', fallback='bank_statements:reconciliation_view')
@require_POST
def reconcile_view(request):
    """Concilia uma linha de extrato com um lançamento."""
    line_id = request.POST.get('line_id')
    try:
        reconciliation.reconcile_line_with_entry(
            request.user,
            line_id=line_id,
            entry_id=request.POST.get('entry_id'),
            audit_context=audit_request_context(request),
        )
        messages.success(request, "Linha conciliada e movimento marcado como realizado.")
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        return render(request, 'banking/_reconciliation_tables.html', _reconciliation_context(request))
    return redirect('bank_statements:reconciliation_view')


@login_required
@permission_required('banking.reconcile', fallback='bank_statements:reconciliation_view')
@require_POST
def create_entry_from_line_view(request):
    """Cria um lançamento novo a partir de uma linha de extrato sem candidato."""
    line_id = request.POST.get('line_id')
    try:
        reconciliation.create_entry_from_line(
            request.user,
            line_id=line_id,
            category_id=request.POST.get('category_id'),
            audit_context=audit_request_context(request),
        )
        messages.success(request, "Lançamento criado e conciliado com a linha do extrato.")
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        return render(request, 'banking/_reconciliation_tables.html', _reconciliation_context(request))
    return redirect('bank_statements:reconciliation_view')


@login_required
@permission_required('banking.reconcile', fallback='bank_statements:reconciliation_view')
@require_POST
def bulk_action_lines_view(request):
    """Concilia, cria lançamentos ou ignora várias linhas de extrato selecionadas de uma vez."""
    action = request.POST.get('bulk_action')
    line_ids = request.POST.getlist('line_ids')
    if not line_ids:
        messages.warning(request, "Selecione ao menos uma linha do extrato.")
    elif action == 'reconcile':
        reconciled, errors = reconciliation.bulk_reconcile_lines(
            request.user,
            line_ids=line_ids,
            audit_context=audit_request_context(request),
        )
        if reconciled:
            messages.success(request, f"{reconciled} linha(s) conciliada(s).")
        if errors:
            messages.error(request, f"{len(errors)} linha(s) não puderam ser conciliadas: {errors[0][1]}")
    elif action == 'create':
        created, errors = reconciliation.bulk_create_entries_from_lines(
            request.user,
            line_ids=line_ids,
            audit_context=audit_request_context(request),
        )
        if created:
            messages.success(request, f"{created} lançamento(s) criado(s) e conciliado(s).")
        if errors:
            messages.error(request, f"{len(errors)} linha(s) não puderam ser processadas: {errors[0][1]}")
    elif action == 'ignore':
        ignored, errors = reconciliation.bulk_ignore_lines(request.user, line_ids=line_ids)
        if ignored:
            messages.success(request, f"{ignored} linha(s) ignorada(s).")
        if errors:
            messages.error(request, f"{len(errors)} linha(s) não puderam ser ignoradas: {errors[0][1]}")
    else:
        messages.warning(request, "Ação em lote inválida.")

    if quer_fragmento(request):
        return render(request, 'banking/_reconciliation_tables.html', _reconciliation_context(request))
    return redirect('bank_statements:reconciliation_view')


@login_required
@permission_required('banking.reconcile', fallback='bank_statements:reconciliation_view')
@require_POST
def undo_reconciliation_view(request):
    """Desfaz a conciliação de uma linha."""
    line_id = request.POST.get('line_id')
    try:
        reconciliation.undo_reconciliation(request.user, line_id=line_id)
        messages.success(
            request,
            "Conciliação desfeita. A linha voltou para Vencidos e o movimento "
            "deixou de estar realizado pela conciliação.",
        )
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        return render(request, 'banking/_reconciliation_tables.html', _reconciliation_context(request))
    return redirect('bank_statements:reconciliation_view')


def _attachments_context(request, *, target_attachment_id=None):
    return {
        "recent_attachments": recent_attachments_for_user(request.user, target_attachment_id),
        "target_attachment_id": target_attachment_id,
    }


@login_required
@permission_required('banking.view')
@permission_required('banking.attachments.manage', fallback='dashboard:dashboard')
def attachments_view(request):
    """Lista de comprovantes recentes e formulário de anexação."""
    target_attachment_id = request.GET.get('attachment_id')
    target_attachment_id = int(target_attachment_id) if target_attachment_id and target_attachment_id.isdigit() else None
    context = _attachments_context(request, target_attachment_id=target_attachment_id)
    return render(request, 'banking/attachments.html', context)


@login_required
@permission_required('banking.attachments.manage', fallback='bank_statements:attachments_view')
@require_POST
def create_attachment_view(request):
    """Anexa um comprovante a um lançamento existente."""
    try:
        attachment = save_entry_attachment(
            request.user,
            request.POST.get('entry_id'),
            request.FILES.get('attachment_file'),
        )
        messages.success(request, f"Anexo salvo: {attachment.original_filename}.")
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        return render(request, 'banking/_attachments_table.html', _attachments_context(request))
    return redirect('bank_statements:attachments_view')


@login_required
@permission_required('banking.attachments.manage', fallback='bank_statements:attachments_view')
def attachment_download_view(request, attachment_id):
    """Baixa o arquivo de um comprovante."""
    attachment = attachment_for_download(request.user, attachment_id)
    if attachment is None:
        messages.warning(request, "Anexo não encontrado.")
        return redirect('bank_statements:attachments_view')
    try:
        path = attachment_download_path(attachment)
    except ValueError as exc:
        messages.warning(request, str(exc))
        return redirect('bank_statements:attachments_view')
    return FileResponse(
        path.open('rb'),
        as_attachment=True,
        filename=attachment.original_filename,
        content_type=attachment.mime_type or 'application/octet-stream',
    )


@login_required
@permission_required('banking.attachments.manage', fallback='bank_statements:attachments_view')
@require_POST
def delete_attachment_view(request, attachment_id):
    """Exclui o metadado e o arquivo físico de um comprovante autorizado."""
    try:
        delete_entry_attachment(request.user, attachment_id=attachment_id)
        messages.success(request, "Comprovante excluído.")
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        return render(request, 'banking/_attachments_table.html', _attachments_context(request))
    return redirect('bank_statements:attachments_view')


@login_required
@permission_required('banking.reconcile', fallback='bank_statements:reconciliation_view')
@require_POST
def ignore_line_view(request):
    """Marca uma linha nova como ignorada."""
    line_id = request.POST.get('line_id')
    try:
        reconciliation.ignore_statement_line(request.user, line_id=line_id)
        messages.success(request, "Linha do extrato marcada como ignorada.")
    except ValueError as exc:
        messages.error(request, str(exc))

    if quer_fragmento(request):
        return render(request, 'banking/_reconciliation_tables.html', _reconciliation_context(request))
    return redirect('bank_statements:reconciliation_view')
