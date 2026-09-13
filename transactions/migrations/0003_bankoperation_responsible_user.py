from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("transactions", "0002_remover_status_cancelado"), ("accounts", "0008_accountowner_name_ci_unique")]

    operations = [
        migrations.AddField(
            model_name="bankoperation",
            name="responsible_user",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="responsible_bank_operations", to=settings.AUTH_USER_MODEL),
        ),
    ]
