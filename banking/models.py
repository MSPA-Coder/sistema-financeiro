from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.timezone import localdate

from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_OPTIONS,
    ACCOUNT_KIND_REGULAR,
    CARD_DAY_MAX,
    CARD_DAY_MIN,
    CURRENCY_BRL,
    CURRENCY_OPTIONS,
    VALID_ACCOUNT_KINDS,
    VALID_CURRENCIES,
)


class FinancialInstitution(models.Model):
    """Instituição financeira: banco ou corretora."""
    
    INSTITUTION_TYPE_CHOICES = [
        ('Banco', 'Banco'),
        ('Corretora', 'Corretora'),
    ]
    
    institution_name = models.CharField(max_length=100)
    institution_type = models.CharField(
        max_length=20,
        choices=INSTITUTION_TYPE_CHOICES,
        default='Banco',
    )
    homologada = models.BooleanField(
        default=False,
        help_text=(
            'Corretora homologada para importação de extrato em PDF na rotina '
            'de Bancos > Importações. Fora do CRUD: só é alterado via migração/shell.'
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'financial_institution'
        ordering = ['institution_name']
        constraints = [
            models.UniqueConstraint(Lower('institution_name'), name='uq_financial_institution_name_ci'),
            models.CheckConstraint(
                condition=models.Q(institution_name__regex=r'^\s*.+\s*$'),
                name='ck_financial_institution_name_not_blank',
            ),
            models.CheckConstraint(
                condition=models.Q(institution_type__in=['Banco', 'Corretora']),
                name='ck_financial_institution_type_valid',
            ),
        ]
        indexes = [
            models.Index(fields=['institution_type']),
        ]
    
    def __str__(self):
        return self.institution_name
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class FinancialAccount(models.Model):
    """Conta financeira vinculada a um dono e a uma instituição financeira."""
    
    owner = models.ForeignKey('accounts.AccountOwner', on_delete=models.PROTECT, related_name='accounts')
    institution = models.ForeignKey(FinancialInstitution, on_delete=models.PROTECT, related_name='accounts')
    account_name = models.CharField(max_length=100)
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_OPTIONS,
        default=CURRENCY_BRL,
        help_text=(
            'Moeda de todos os valores da conta. Os lançamentos não guardam '
            'moeda própria: herdam a daqui.'
        ),
    )
    initial_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    initial_balance_date = models.DateField(
        default=localdate,
        help_text='Data a que o saldo inicial se refere.',
    )
    is_default = models.BooleanField(default=False)
    # Cartão de crédito: saldo negativo é a fatura em aberto. Os dias guiam a
    # fatura projetada; o pagamento em si é uma ou mais transferências, de
    # quantas contas forem, e a conta padrão só serve para projetar.
    account_kind = models.CharField(
        max_length=20,
        choices=ACCOUNT_KIND_OPTIONS,
        default=ACCOUNT_KIND_REGULAR,
    )
    card_closing_day = models.PositiveSmallIntegerField(null=True, blank=True)
    card_due_day = models.PositiveSmallIntegerField(null=True, blank=True)
    card_payment_account = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cards_paid',
    )
    # Gasto novo por fatura fixado à mão. Vazio, a fatura projetada usa a
    # mediana das últimas faturas importadas (`bank_statements/fatura_projetada.py`).
    card_estimated_spend = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'financial_account'
        ordering = ['account_name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(account_name__regex=r'^\s*.+\s*$'),
                name='ck_financial_account_name_not_blank',
            ),
            models.CheckConstraint(
                condition=models.Q(currency__in=VALID_CURRENCIES),
                name='ck_financial_account_currency_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(account_kind__in=VALID_ACCOUNT_KINDS),
                name='ck_financial_account_kind_valid',
            ),
            # Cartão tem os dois dias; conta comum não tem nenhum dado de cartão.
            # O `isnull=False` é necessário: no CHECK, `NULL >= 1` não é falso,
            # é desconhecido, e o PostgreSQL deixaria passar um cartão sem dias.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        account_kind=ACCOUNT_KIND_CREDIT_CARD,
                        card_closing_day__isnull=False,
                        card_due_day__isnull=False,
                        card_closing_day__gte=CARD_DAY_MIN,
                        card_closing_day__lte=CARD_DAY_MAX,
                        card_due_day__gte=CARD_DAY_MIN,
                        card_due_day__lte=CARD_DAY_MAX,
                    )
                    & (models.Q(card_estimated_spend__isnull=True) | models.Q(card_estimated_spend__gte=0))
                    | models.Q(
                        account_kind=ACCOUNT_KIND_REGULAR,
                        card_closing_day__isnull=True,
                        card_due_day__isnull=True,
                        card_payment_account__isnull=True,
                        card_estimated_spend__isnull=True,
                    )
                ),
                name='ck_financial_account_card_fields',
            ),
        ]
        indexes = [
            models.Index(fields=['owner']),
            models.Index(fields=['institution']),
            models.Index(fields=['is_default']),
        ]
    
    def __str__(self):
        return self.account_name

    @property
    def is_credit_card(self) -> bool:
        return self.account_kind == ACCOUNT_KIND_CREDIT_CARD
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)
