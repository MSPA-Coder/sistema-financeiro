from django.db import migrations

OLD_DESCRIPTION = "Visualizar módulo Bancos"
NEW_DESCRIPTION = "Visualizar módulo Instituições"


def rename_description(apps, schema_editor):
    AppPermission = apps.get_model("accounts", "AppPermission")
    AppPermission.objects.filter(name="banking.view").update(description=NEW_DESCRIPTION)


def revert_description(apps, schema_editor):
    AppPermission = apps.get_model("accounts", "AppPermission")
    AppPermission.objects.filter(name="banking.view").update(description=OLD_DESCRIPTION)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_seed_permission_catalog"),
    ]

    operations = [
        migrations.RunPython(rename_description, revert_description),
    ]
