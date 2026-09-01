"""Models de gestão gerencial: tags, projetos e orçamentos."""
from django.db import models
from django.utils import timezone


class ManagementTag(models.Model):
    """Tag livre para classificar movimentos sem alterar a categoria principal."""
    
    tag_name = models.CharField(max_length=80, unique=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'management_tag'
        ordering = ['tag_name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tag_name__regex=r'^\s*.+\s*$'),
                name='ck_management_tag_name_not_blank',
            ),
        ]
        indexes = [models.Index(fields=['active'])]
    
    def __str__(self):
        return self.tag_name
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class CashFlowEntryTag(models.Model):
    """Vínculo N:N entre movimento e tag gerencial."""
    
    entry = models.ForeignKey('transactions.CashFlowEntry', on_delete=models.CASCADE, related_name='tag_links')
    tag = models.ForeignKey(ManagementTag, on_delete=models.CASCADE, related_name='entry_links')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'cash_flow_entry_tag'
        constraints = [
            models.UniqueConstraint(
                fields=['entry', 'tag'],
                name='uq_cash_flow_entry_tag_entry_tag',
            ),
        ]
        indexes = [
            models.Index(fields=['entry']),
            models.Index(fields=['tag']),
        ]
    
    def __str__(self):
        return f"{self.entry} - {self.tag}"
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class ManagementProject(models.Model):
    """Projeto ou centro de custo para análise gerencial leve."""
    
    project_name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'management_project'
        ordering = ['project_name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(project_name__regex=r'^\s*.+\s*$'),
                name='ck_management_project_name_not_blank',
            ),
        ]
        indexes = [
            models.Index(fields=['active']),
        ]
    
    def __str__(self):
        return self.project_name
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class CashFlowEntryProject(models.Model):
    """Vínculo opcional 1:N de movimento para projeto/centro de custo."""
    
    entry = models.ForeignKey('transactions.CashFlowEntry', on_delete=models.CASCADE, related_name='project_link')
    project = models.ForeignKey(ManagementProject, on_delete=models.CASCADE, related_name='entry_links')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'cash_flow_entry_project'
        constraints = [
            models.UniqueConstraint(
                fields=['entry'],
                name='uq_cash_flow_entry_project_entry',
            ),
        ]
        indexes = [
            models.Index(fields=['entry']),
            models.Index(fields=['project']),
        ]
    
    def __str__(self):
        return f"{self.entry} - {self.project}"
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class MonthlyBudget(models.Model):
    """Orçamento mensal simples por dono e categoria."""
    
    owner = models.ForeignKey('accounts.AccountOwner', on_delete=models.CASCADE, related_name='budgets')
    category = models.ForeignKey('transactions.CashFlowCategory', on_delete=models.CASCADE, related_name='budgets')
    year = models.IntegerField()
    month = models.IntegerField()
    # O realizado nao e armazenado: e somado a partir dos lancamentos por
    # `actual_amount_for_budget`. Guardar uma copia so criaria a chance de ela
    # divergir do que os lancamentos dizem.
    planned_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'monthly_budget'
        ordering = ['owner', 'year', 'month']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(month__gte=1, month__lte=12),
                name='ck_monthly_budget_month_range',
            ),
            models.CheckConstraint(
                condition=models.Q(year__gte=2000, year__lte=2100),
                name='ck_monthly_budget_year_range',
            ),
            models.CheckConstraint(
                condition=models.Q(planned_amount__gte=0),
                name='ck_monthly_budget_amount_non_negative',
            ),
            models.UniqueConstraint(
                fields=['owner', 'category', 'year', 'month'],
                name='uq_monthly_budget_owner_category_period',
            ),
        ]
        indexes = [
            models.Index(fields=['owner', 'year', 'month']),
            models.Index(fields=['active']),
        ]
    
    def __str__(self):
        return f"{self.owner} - {self.category} - {self.month}/{self.year}"
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)
