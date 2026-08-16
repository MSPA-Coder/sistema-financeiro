"""Models de importação de extrato bancário.

`BankStatementImport` é o lote enviado; `BankStatementLine` é a linha
individual, que pode ser conciliada contra um lançamento.

As FKs para `banking.FinancialAccount` usam PROTECT, não CASCADE: excluir uma
conta que possui histórico de importação deve falhar de forma auditável, em
vez de apagar o histórico junto e em silêncio.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils import timezone

STATUS_IMPORTED = "importado"
STATUS_PROCESSED = "processado"
STATUS_ERROR = "erro"
VALID_IMPORT_STATUSES = (STATUS_IMPORTED, STATUS_PROCESSED, STATUS_ERROR)

LINE_STATUS_NEW = "novo"
LINE_STATUS_RECONCILED = "conciliado"
LINE_STATUS_IGNORED = "ignorado"
VALID_LINE_STATUSES = (LINE_STATUS_NEW, LINE_STATUS_RECONCILED, LINE_STATUS_IGNORED)


class BankStatementImport(models.Model):
    """Lote de importação de extrato bancário."""

    account = models.ForeignKey(
        "banking.FinancialAccount",
        on_delete=models.PROTECT,
        related_name="statement_imports",
    )
    source_filename = models.CharField(max_length=255)
    row_count = models.IntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=[
            (STATUS_IMPORTED, "Importado"),
            (STATUS_PROCESSED, "Processado"),
            (STATUS_ERROR, "Erro"),
        ],
        default=STATUS_IMPORTED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bank_statement_import"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(source_filename__regex=r"^\s*.+\s*$"),
                name="ck_bank_statement_import_filename_not_blank",
            ),
            models.CheckConstraint(
                condition=Q(status__in=VALID_IMPORT_STATUSES),
                name="ck_bank_statement_import_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["account"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.source_filename} ({self.account_id})"

    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class EntryAttachment(models.Model):
    """Comprovante local vinculado a um lançamento (Bancos > Anexos)."""

    entry = models.ForeignKey(
        "transactions.CashFlowEntry",
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    original_filename = models.CharField(max_length=255)
    stored_filename = models.CharField(max_length=255)
    stored_path = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=120, blank=True, null=True)
    file_size = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "entry_attachment"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(original_filename__regex=r"^\s*.+\s*$"),
                name="ck_entry_attachment_filename_not_blank",
            ),
        ]
        indexes = [
            models.Index(fields=["entry"]),
        ]

    def __str__(self):
        return self.original_filename

    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class BankStatementLine(models.Model):
    """Linha importada de extrato, conciliável contra um lançamento."""

    import_batch = models.ForeignKey(
        BankStatementImport,
        on_delete=models.CASCADE,
        related_name="lines",
        db_column="import_id",
    )
    account = models.ForeignKey(
        "banking.FinancialAccount",
        on_delete=models.PROTECT,
        related_name="statement_lines",
    )
    matched_entry = models.ForeignKey(
        "transactions.CashFlowEntry",
        on_delete=models.PROTECT,
        related_name="statement_matches",
        null=True,
        blank=True,
    )
    statement_date = models.DateField()
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    line_hash = models.CharField(max_length=64)
    status = models.CharField(
        max_length=20,
        choices=[
            (LINE_STATUS_NEW, "Novo"),
            (LINE_STATUS_RECONCILED, "Conciliado"),
            (LINE_STATUS_IGNORED, "Ignorado"),
        ],
        default=LINE_STATUS_NEW,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bank_statement_line"
        ordering = ["-statement_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(amount=0),
                name="ck_bank_statement_line_amount_non_zero",
            ),
            models.CheckConstraint(
                condition=Q(status__in=VALID_LINE_STATUSES),
                name="ck_bank_statement_line_status_valid",
            ),
            UniqueConstraint(
                fields=["account", "line_hash"],
                name="uq_bank_statement_line_account_hash",
            ),
            # Índice único parcial: um lançamento só pode estar conciliado com,
            # no máximo, uma linha de extrato ativa. A garantia fica no banco
            # porque a validação em código não sobrevive a concorrência.
            UniqueConstraint(
                fields=["matched_entry"],
                condition=Q(status=LINE_STATUS_RECONCILED, matched_entry__isnull=False),
                name="uq_bank_statement_line_active_matched_entry",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "statement_date"]),
            models.Index(fields=["import_batch"]),
            models.Index(fields=["matched_entry"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.statement_date} - {self.description} ({self.amount})"

    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)
