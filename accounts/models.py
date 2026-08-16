"""Models de autenticação e usuários."""
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from core.domain.identity import (
    USER_TYPE_ADMINISTRATOR,
    USER_TYPE_SUPER_USER,
    USER_TYPE_USER,
    VALID_USER_TYPES,
)
from core.domain.settings import (
    UI_THEME_LIGHT,
    VALID_UI_THEMES,
)


class AppUser(AbstractUser):
    """Usuário da aplicação, responsável por perfil de acesso e preferências."""
    
    user_type = models.CharField(
        max_length=30,
        choices=[
            (USER_TYPE_ADMINISTRATOR, USER_TYPE_ADMINISTRATOR),
            (USER_TYPE_SUPER_USER, USER_TYPE_SUPER_USER),
            (USER_TYPE_USER, USER_TYPE_USER),
        ],
        default=USER_TYPE_ADMINISTRATOR,
    )
    password_updated_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)
    ui_theme = models.CharField(
        max_length=30,
        choices=[(theme, theme) for theme in VALID_UI_THEMES],
        default=UI_THEME_LIGHT,
    )
    table_scroll_rows = models.IntegerField(default=15)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'app_user'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(user_type__in=VALID_USER_TYPES),
                name='ck_app_user_user_type_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(ui_theme__in=VALID_UI_THEMES),
                name='ck_app_user_ui_theme_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(table_scroll_rows__gte=5, table_scroll_rows__lte=200),
                name='ck_app_user_table_scroll_rows_range',
            ),
        ]
        indexes = [
            models.Index(fields=['user_type']),
        ]
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class AccountOwner(models.Model):
    """Titular/owner de dados financeiros."""
    
    # A coluna no banco chama-se `owner_name`; o atributo fica `name` para
    # ficar igual aos demais cadastros no código.
    name = models.CharField(max_length=100, db_column='owner_name')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'account_owner'
        ordering = ['name']
    
    def __str__(self):
        return self.name


class UserOwnerAccess(models.Model):
    """Permissão de um usuário para acessar dados de um dono."""
    
    user = models.ForeignKey(AppUser, on_delete=models.CASCADE)
    owner = models.ForeignKey(AccountOwner, on_delete=models.CASCADE)
    can_view = models.BooleanField(default=True)
    can_create = models.BooleanField(default=True)
    can_update = models.BooleanField(default=True)
    can_delete = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'user_owner_access'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'owner'],
                name='uq_user_owner_access_user_owner',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'owner']),
        ]


class AppPermission(models.Model):
    """Permissões específicas da aplicação."""
    
    name = models.CharField(max_length=100, unique=True, db_column='permission_key')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'app_permission'
    
    def __str__(self):
        return self.name


class UserPermission(models.Model):
    """Permissão funcional atribuída diretamente a um usuário."""

    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name='permissions')
    permission = models.ForeignKey(AppPermission, on_delete=models.CASCADE, related_name='user_links')
    allowed = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_permission'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'permission'],
                name='uq_user_permission_user_permission',
            ),
        ]
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['permission']),
        ]


class UserAccountVisibility(models.Model):
    """Preferência analítica por usuário para ocultar contas em visões agregadas."""

    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name='account_visibility_preferences')
    account = models.ForeignKey('banking.FinancialAccount', on_delete=models.CASCADE, related_name='user_visibility_preferences')
    hide_from_dashboard = models.BooleanField(default=False)
    hide_from_projections = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_account_visibility'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'account'],
                name='uq_user_account_visibility_user_account',
            ),
        ]
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['account']),
        ]


class LoginLockout(models.Model):
    """Controle persistente de tentativas de login por usuário/IP."""

    identity_key = models.CharField(max_length=255)
    normalized_user_name = models.CharField(max_length=100, blank=True, null=True)
    remote_addr = models.CharField(max_length=80, blank=True, null=True)
    failure_count = models.IntegerField(default=0)
    locked_until_ts = models.IntegerField(blank=True, null=True)
    last_failed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'login_lockout'
        constraints = [
            models.UniqueConstraint(fields=['identity_key'], name='uq_login_lockout_identity_key'),
        ]
        indexes = [
            models.Index(fields=['locked_until_ts']),
        ]
