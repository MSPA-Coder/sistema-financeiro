from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_accountowner_name_ci_unique"), ("banking", "0003_financialinstitution_name_ci_unique")]

    operations = [
        migrations.CreateModel(
            name="UserTransferDestinationAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("destination_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="transfer_destination_accesses", to="banking.financialaccount")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="transfer_destination_accesses", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "user_transfer_destination_access"},
        ),
        migrations.AddConstraint(model_name="usertransferdestinationaccess", constraint=models.UniqueConstraint(fields=("user", "destination_account"), name="uq_user_transfer_destination_access")),
        migrations.AddIndex(model_name="usertransferdestinationaccess", index=models.Index(fields=["user", "destination_account"], name="user_transf_user_id_b19060_idx")),
    ]
