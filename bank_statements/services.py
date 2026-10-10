"""Casos de uso de importação de extrato bancário (Bancos > Importações).

Cada importação cria um lote (`BankStatementImport`) e insere apenas as
linhas cujo hash ainda não existe para a conta, contando quantas duplicadas
foram ignoradas. O hash por conta é o que torna reimportar o mesmo extrato
uma operação segura: reenviar o arquivo não duplica movimentação.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from banking.models import FinancialAccount
from banking.services import accessible_account_ids, can_access_account
from core import regional

from .adapters import (
    extract_statement_balance,
    extract_statement_period_end,
    get_statement_adapter,
    read_statement_upload,
)
from .fatura_csv import CartaoCsvAdapter, formato_da_fatura
from .models import BankStatementImport, BankStatementLine

_MAX_FILENAME_LENGTH = 255
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(raw_name: str | None) -> str:
    """Normaliza o nome do arquivo enviado para um valor seguro de armazenar.

    Remove separadores de caminho e caracteres fora de um allowlist simples,
    de modo que um nome enviado pelo usuário não consiga escapar do diretório
    de destino. Implementado aqui em vez de puxar uma dependência nova.
    """
    name = os.path.basename((raw_name or "").strip()) or "extrato"
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    return name[:_MAX_FILENAME_LENGTH] or "extrato"


def _existing_line_hashes(account_id: int, candidate_hashes: set[str]) -> set[str]:
    """Hashes já presentes para a conta, consultados em lotes de 500."""
    if not candidate_hashes:
        return set()
    existing: set[str] = set()
    hashes = tuple(candidate_hashes)
    for offset in range(0, len(hashes), 500):
        chunk = hashes[offset:offset + 500]
        existing.update(
            BankStatementLine.objects.filter(
                account_id=account_id, line_hash__in=chunk
            ).values_list('line_hash', flat=True)
        )
    return existing


def _clean_account_id(raw_account_id) -> int:
    try:
        account_id = int(raw_account_id)
        if account_id <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Informe a conta e o arquivo do extrato.") from exc
    return account_id


def _adapter_for(account: FinancialAccount, uploaded_file: UploadedFile):
    """Fatura só entra em cartão, e cartão só recebe fatura.

    Os dois erros são de sinal: uma fatura lida como extrato grava compra como
    receita, e um extrato lido como fatura grava o contrário. Por isso o
    cabeçalho é conferido nos dois sentidos, antes de qualquer linha ser gravada.
    """
    name = (uploaded_file.name or "").lower()
    eh_csv = name.endswith(".csv") or "csv" in (getattr(uploaded_file, "content_type", "") or "").lower()
    if account.is_credit_card:
        if not eh_csv:
            raise ValueError("Conta de cartão de crédito recebe a fatura em CSV (C6 ou XP).")
        return CartaoCsvAdapter(account.card_closing_day)
    if eh_csv and formato_da_fatura(read_statement_upload(uploaded_file, label="CSV")) is not None:
        raise ValueError(
            "Este arquivo é uma fatura de cartão de crédito. Importe-o na conta do cartão "
            "(tipo \"Cartão de crédito\" em Cadastros > Contas)."
        )
    return get_statement_adapter(uploaded_file, institution=account.institution)


def import_statement_file(
    user, *, account_id, uploaded_file: UploadedFile | None
) -> tuple[BankStatementImport, int, int]:
    """Importa um arquivo de extrato (CSV ou OFX/OFC/QFX) para uma conta.

    Retorna `(lote, linhas_inseridas, linhas_duplicadas_ignoradas)`.
    Levanta `ValueError` para entrada inválida ou acesso negado.
    """
    if uploaded_file is None:
        raise ValueError("Informe a conta e o arquivo do extrato.")
    clean_account_id = _clean_account_id(account_id)
    if not can_access_account(user, clean_account_id, "create"):
        raise ValueError("Acesso negado para importar extrato nesta conta.")

    try:
        account = FinancialAccount.objects.select_related("institution").get(id=clean_account_id)
    except FinancialAccount.DoesNotExist as exc:
        raise ValueError("Conta não encontrada.") from exc

    parsed = _adapter_for(account, uploaded_file).parse(uploaded_file, clean_account_id)
    # Fatura de cartão não traz saldo; extrato de conta traz, às vezes.
    saldo = None if account.is_credit_card else extract_statement_balance(uploaded_file)
    if not parsed and saldo is None:
        raise ValueError("O extrato não tem movimento nem saldo: não há o que importar.")
    # Sem movimento mas com saldo é um mês sem movimentação de verdade: o lote
    # entra sem linhas, e o saldo dele cobre o mês (Situação das Contas,
    # fechamento) e é conferido com o do CB como qualquer outro.

    filename = _sanitize_filename(uploaded_file.name)

    with transaction.atomic():
        batch = BankStatementImport.objects.create(
            account_id=clean_account_id,
            source_filename=filename,
            row_count=0,
            statement_balance=saldo[0] if saldo else None,
            statement_balance_date=saldo[1] if saldo else None,
            statement_period_end=None if account.is_credit_card else extract_statement_period_end(uploaded_file),
        )
        existing_hashes = _existing_line_hashes(
            clean_account_id, {line.line_hash for line in parsed}
        )
        new_lines = []
        inserted = skipped = 0
        for line in parsed:
            if line.line_hash in existing_hashes:
                skipped += 1
                continue
            new_lines.append(
                BankStatementLine(
                    import_batch=batch,
                    account_id=clean_account_id,
                    statement_date=line.statement_date,
                    description=line.description,
                    amount=line.amount,
                    line_hash=line.line_hash,
                    purchase_date=line.purchase_date,
                    installment_current=line.installment_current,
                    installment_total=line.installment_total,
                    card_holder=line.card_holder,
                    bank_category=line.bank_category,
                    status="novo",
                )
            )
            existing_hashes.add(line.line_hash)
            inserted += 1
        if new_lines:
            BankStatementLine.objects.bulk_create(new_lines)
        batch.row_count = inserted
        batch.save(update_fields=["row_count", "updated_at"])

    return batch, inserted, skipped


@dataclass(frozen=True)
class ConferenciaDeSaldo:
    """O saldo que o extrato informa contra o saldo realizado do CB na mesma data."""

    saldo_do_extrato: Decimal
    data: date
    saldo_no_cb: Decimal

    @property
    def diferenca(self) -> Decimal:
        return (self.saldo_no_cb - self.saldo_do_extrato).quantize(Decimal("0.01"))

    @property
    def bate(self) -> bool:
        return self.diferenca == 0


def conferencia_de_saldo(lote: BankStatementImport) -> ConferenciaDeSaldo | None:
    """Confere o saldo do arquivo com o do CB, ou `None` se o arquivo não traz saldo.

    É o saldo absoluto da conta até a data (lançamentos realizados), não o do
    período importado: por isso uma diferença aponta linha faltando ou sobrando
    em qualquer ponto do histórico, e a conferência vale mesmo com o extrato
    parcial."""
    if lote.statement_balance is None or lote.statement_balance_date is None:
        return None
    from core.domain.finance import VIEW_REALIZED
    from reports.services import decimal_balances_before_by_account

    saldos = decimal_balances_before_by_account(
        [lote.account_id], lote.statement_balance_date + timedelta(days=1), VIEW_REALIZED
    )
    return ConferenciaDeSaldo(
        saldo_do_extrato=lote.statement_balance,
        data=lote.statement_balance_date,
        saldo_no_cb=saldos.get(lote.account_id, Decimal("0.00")),
    )


def statement_imports_for_user(user, limit: int = 20) -> Iterable[BankStatementImport]:
    """Últimos lotes de importação visíveis para `user`, com a conferência de
    saldo (`.conferencia`) em cada um que traz saldo."""
    account_ids = accessible_account_ids(user, "view")
    if not account_ids:
        return BankStatementImport.objects.none()
    lotes = list(
        BankStatementImport.objects.select_related("account__owner", "account__institution").filter(
            account_id__in=account_ids
        )[:limit]
    )
    for lote in lotes:
        lote.conferencia = conferencia_de_saldo(lote)
    return lotes


def statement_import_status(user, batch_id: int) -> dict[str, object] | None:
    """Status resumido de um lote, ou `None` se inexistente/fora de escopo."""
    try:
        batch = BankStatementImport.objects.select_related("account").get(id=batch_id)
    except BankStatementImport.DoesNotExist:
        return None
    if not can_access_account(user, batch.account_id, "view"):
        return None
    return {
        "id": batch.id,
        "row_count": batch.row_count,
        "status": batch.status,
        "created_at": regional.formatar_data_hora(batch.created_at) or None,
    }


def accounts_for_import_form(user) -> Iterable[FinancialAccount]:
    """Contas que `user` pode escolher como destino de uma importação.

    Ordenada por titular: é a lista usada no `<select>` de confirmação, que
    agrupa por titular (`{% regroup %}` exige a sequência já ordenada pela
    chave do grupo) para reduzir o risco de escolher a conta do titular
    errado quando dois titulares têm contas de mesmo nome na mesma
    instituição.
    """
    account_ids = accessible_account_ids(user, "create")
    if not account_ids:
        return FinancialAccount.objects.none()
    return FinancialAccount.objects.select_related("owner", "institution").filter(
        id__in=account_ids
    ).order_by("owner__name", "institution__institution_name", "account_name")
