"""Models de transações e fluxo de caixa."""
from django.db import models
from django.utils import timezone

from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INSTALLMENT,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_RECURRING,
    OPERATION_SINGLE,
    STATUS_CANCELED,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VALID_ENTRY_TYPES,
    VALID_STATUSES,
)


class CashFlowCategory(models.Model):
    """Categoria de fluxo de caixa. Categorias internas afetam saldo, mas não totais gerenciais."""
    
    category_name = models.CharField(max_length=100, unique=True)
    is_internal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'cash_flow_category'
        ordering = ['category_name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(category_name__regex=r'^\s*.+\s*$'),
                name='ck_cash_flow_category_name_not_blank',
            ),
        ]
        indexes = [
            models.Index(fields=['is_internal']),
        ]
    
    def __str__(self):
        return self.category_name
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class BankOperation(models.Model):
    """Agrupador lógico de movimentos bancários.
    
    CashFlowEntry permanece como tabela de movimentos, enquanto esta entidade
    agrupa parcelas, recorrências e transferências internas.
    """
    
    operation_key = models.CharField(max_length=80, unique=True)
    legacy_operation_id = models.CharField(max_length=36, blank=True, null=True)
    operation_type = models.CharField(
        max_length=30,
        choices=[
            (OPERATION_SINGLE, 'Única'),
            (OPERATION_INSTALLMENT, 'Parcelado'),
            (OPERATION_RECURRING, 'Recorrente'),
            (OPERATION_INTERNAL_TRANSFER, 'Transferência Interna'),
        ],
        default=OPERATION_SINGLE,
    )
    description = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            (STATUS_PROJECTED, 'A vencer'),
            (STATUS_PENDING, 'Vencidos'),
            (STATUS_REALIZED, 'Realizado'),
            (STATUS_CANCELED, 'Cancelado'),
        ],
        default=STATUS_PROJECTED,
    )
    installment_total = models.IntegerField(default=1)
    first_due_date = models.DateField(null=True, blank=True)
    last_due_date = models.DateField(null=True, blank=True)
    entry_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'bank_operation'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(operation_key__regex=r'^\s*.+\s*$'),
                name='ck_bank_operation_key_not_blank',
            ),
            models.CheckConstraint(
                condition=models.Q(operation_type__in=[OPERATION_SINGLE, OPERATION_INSTALLMENT, OPERATION_RECURRING, OPERATION_INTERNAL_TRANSFER]),
                name='ck_bank_operation_type_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=VALID_STATUSES),
                name='ck_bank_operation_status_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(installment_total__gte=1),
                name='ck_bank_operation_installment_total_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(entry_count__gte=0),
                name='ck_bank_operation_entry_count_non_negative',
            ),
        ]
        indexes = [
            models.Index(fields=['legacy_operation_id']),
            models.Index(fields=['operation_type', 'status']),
        ]
    
    def __str__(self):
        return self.operation_key
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class CashFlowEntry(models.Model):
    """Lançamento financeiro do fluxo de caixa."""
    
    account = models.ForeignKey('banking.FinancialAccount', on_delete=models.PROTECT, related_name='transactions')
    category = models.ForeignKey(CashFlowCategory, on_delete=models.PROTECT, related_name='transactions')
    entry_type = models.CharField(
        max_length=10,
        choices=[
            (ENTRY_TYPE_INCOME, 'Receita'),
            (ENTRY_TYPE_EXPENSE, 'Despesa'),
        ],
    )
    description = models.CharField(max_length=255, blank=True)
    entry_amount = models.DecimalField(max_digits=12, decimal_places=2)
    installments = models.IntegerField(default=1)
    current_installment = models.IntegerField(default=1)
    due_date = models.DateField()
    realized_date = models.DateField(null=True, blank=True)
    realized_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_recurring = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=[
            (STATUS_PROJECTED, 'A vencer'),
            (STATUS_PENDING, 'Vencidos'),
            (STATUS_REALIZED, 'Realizado'),
            (STATUS_CANCELED, 'Cancelado'),
        ],
        default=STATUS_PROJECTED,
    )
    operation_type = models.CharField(
        max_length=30,
        default=OPERATION_SINGLE,
        db_index=True,
    )
    bank_operation = models.ForeignKey(
        BankOperation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='entries',
    )
    source_entry = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_entries',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'cash_flow_entry'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(entry_type__in=VALID_ENTRY_TYPES),
                name='ck_cash_flow_entry_type_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(entry_amount__gt=0),
                name='ck_cash_flow_entry_amount_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(realized_amount__isnull=True) | models.Q(realized_amount__gt=0),
                name='ck_cash_flow_entry_realized_amount_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=VALID_STATUSES),
                name='ck_cash_flow_entry_status_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(operation_type__in=[OPERATION_SINGLE, OPERATION_INSTALLMENT, OPERATION_RECURRING, OPERATION_INTERNAL_TRANSFER]),
                name='ck_cash_flow_entry_operation_type_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(installments__gte=1),
                name='ck_cash_flow_entry_installments_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(current_installment__gte=1),
                name='ck_cash_flow_entry_current_installment_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(current_installment__lte=models.F('installments')),
                name='ck_cash_flow_entry_current_lte_total',
            ),
            models.CheckConstraint(
                condition=models.Q(source_entry__isnull=True) | ~models.Q(source_entry=models.F('id')),
                name='ck_cash_flow_entry_source_not_self',
            ),
        ]
        indexes = [
            models.Index(fields=['account', 'due_date']),
            models.Index(fields=['account', 'realized_date']),
            models.Index(fields=['category', 'due_date']),
            models.Index(fields=['status', 'due_date']),
            models.Index(fields=['entry_type', 'due_date']),
            models.Index(fields=['operation_type']),
            models.Index(fields=['bank_operation']),
            models.Index(fields=['source_entry']),
        ]
    
    def __str__(self):
        return f"{self.description} - {self.entry_amount}"
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class AccountMonthClose(models.Model):
    """Fechamento mensal de uma conta bancária.
    
    Um mês fechado bloqueia alterações em movimentos daquela conta no período,
    preservando o saldo consolidado que foi validado pelo usuário.
    """
    
    account = models.ForeignKey('banking.FinancialAccount', on_delete=models.CASCADE, related_name='month_closes')
    year = models.IntegerField()
    month = models.IntegerField()
    closing_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    closed_at = models.DateTimeField()
    closed_by_user = models.ForeignKey(
        'accounts.AppUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='closed_months',
    )
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by_user = models.ForeignKey(
        'accounts.AppUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='reopened_months',
    )
    reopen_reason = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'account_month_close'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(month__gte=1, month__lte=12),
                name='ck_account_month_close_month_range',
            ),
            models.CheckConstraint(
                condition=models.Q(year__gte=2000, year__lte=2100),
                name='ck_account_month_close_year_range',
            ),
            models.UniqueConstraint(
                fields=['account', 'year', 'month'],
                name='uq_account_month_close_account_period',
            ),
        ]
        indexes = [
            models.Index(fields=['account', 'year', 'month']),
        ]
    
    def __str__(self):
        return f"{self.account} - {self.month}/{self.year}"
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)

