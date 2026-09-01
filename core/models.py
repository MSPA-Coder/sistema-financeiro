"""Modelos de infraestrutura compartilhados (auditoria e configuração)."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Trilha de auditoria para operações relevantes em dados financeiros."""

    entity_name = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=80, blank=True, null=True)
    action = models.CharField(max_length=80)
    old_values_json = models.JSONField(blank=True, null=True)
    new_values_json = models.JSONField(blank=True, null=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    # A FK pode ficar nula quando uma conta e removida. Estes campos preservam
    # quem executou a acao sem depender do ciclo de vida do usuario.
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_name = models.CharField(max_length=150, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    proxy_ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    result = models.CharField(max_length=16, default="success")
    summary = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(fields=["entity_name", "entity_id"], name="ix_audit_log_entity"),
            models.Index(fields=["created_at"], name="ix_audit_log_created_at"),
            models.Index(fields=["user"], name="ix_audit_log_user"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(entity_name=""),
                name="ck_audit_log_entity_name_not_blank",
            ),
            models.CheckConstraint(
                condition=~models.Q(action=""),
                name="ck_audit_log_action_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.entity_name}#{self.entity_id}"


class AppSetting(models.Model):
    """Configuração simples da aplicação em formato chave/valor."""

    setting_key = models.CharField(max_length=50, unique=True)
    setting_value = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "app_setting"

    def __str__(self) -> str:
        return self.setting_key
