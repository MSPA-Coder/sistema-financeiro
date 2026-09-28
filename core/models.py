"""Modelos de infraestrutura compartilhados (auditoria e configuração)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


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


class PatrimonioV4ChangeCounter(models.Model):
    """Relógio transacional único para a outbox pública do patrimônio."""

    id = models.SmallIntegerField(primary_key=True)
    value = models.BigIntegerField(default=0)

    class Meta:
        db_table = "patrimonio_v4_change_counter"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="ck_patrimonio_v4_counter_singleton"),
            models.CheckConstraint(condition=models.Q(value__gte=0), name="ck_patrimonio_v4_counter_non_negative"),
        ]


class PatrimonioV4Outbox(models.Model):
    """Invalidação transacional; o consumidor volta ao snapshot v4."""

    cursor = models.BigIntegerField(primary_key=True)
    resource = models.CharField(max_length=32)
    source_record_id = models.BigIntegerField()
    operation = models.CharField(max_length=8)
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "patrimonio_v4_outbox"
        indexes = [models.Index(fields=["cursor"], name="ix_patrimonio_v4_outbox_cursor")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(resource__in=["account", "category", "cash_entry", "transfer"]),
                name="ck_patrimonio_v4_outbox_resource",
            ),
            models.CheckConstraint(
                condition=models.Q(operation__in=["upsert", "delete"]),
                name="ck_patrimonio_v4_outbox_operation",
            ),
        ]
