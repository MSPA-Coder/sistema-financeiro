"""Inclui a permissão do relatório de planejamento anual."""

from django.db import migrations


PERMISSION_NAME = "reports.annual_planning.view"
PERMISSION_DESCRIPTION = "Visualizar Planejamento anual"


def add_permission(apps, _schema_editor):
    permission = apps.get_model("accounts", "AppPermission")
    permission.objects.update_or_create(
        name=PERMISSION_NAME,
        defaults={"description": PERMISSION_DESCRIPTION},
    )


def remove_permission(apps, _schema_editor):
    apps.get_model("accounts", "AppPermission").objects.filter(name=PERMISSION_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_remover_permissoes_orfas")]

    operations = [migrations.RunPython(add_permission, remove_permission)]
