from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


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
    initial_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    is_default = models.BooleanField(default=False)
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
        ]
        indexes = [
            models.Index(fields=['owner']),
            models.Index(fields=['institution']),
            models.Index(fields=['is_default']),
        ]
    
    def __str__(self):
        return self.account_name
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)
