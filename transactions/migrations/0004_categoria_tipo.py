"""A categoria passa a dizer o que ela é, não só se é "interna".

`is_internal` respondia uma pergunta de sim ou não, e havia três respostas
possíveis: gerencial (entra em receita ou despesa), transferência (tem outra
ponta neste sistema) e movimentação (sai da conta sem mudar de dono e sem conta
de destino aqui -- a liquidação de bolsa, que vira ação).

O passado é preservado sem perda: toda categoria interna vira transferência,
que é o que "interna" sempre significou neste sistema -- não havia como criar
uma categoria interna sem contraparte. Quem reclassifica alguma delas para
movimentação é a U04c, com relatório, e não esta migration.
"""
from django.db import migrations, models

GERENCIAL = "gerencial"
TRANSFERENCIA = "transferencia"
MOVIMENTACAO = "movimentacao"


def preencher_tipo(apps, schema_editor):
    CashFlowCategory = apps.get_model("transactions", "CashFlowCategory")
    CashFlowCategory.objects.filter(is_internal=True).update(kind=TRANSFERENCIA)
    CashFlowCategory.objects.filter(is_internal=False).update(kind=GERENCIAL)


def reverter_tipo(apps, schema_editor):
    """Volta ao booleano: transferência e movimentação eram ambas "interna"."""
    CashFlowCategory = apps.get_model("transactions", "CashFlowCategory")
    CashFlowCategory.objects.filter(kind__in=(TRANSFERENCIA, MOVIMENTACAO)).update(is_internal=True)
    CashFlowCategory.objects.filter(kind=GERENCIAL).update(is_internal=False)


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0003_bankoperation_responsible_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashflowcategory",
            name="kind",
            field=models.CharField(
                choices=[
                    (GERENCIAL, "Gerencial (receita ou despesa)"),
                    (TRANSFERENCIA, "Transferência entre contas"),
                    (MOVIMENTACAO, "Movimentação (não é receita nem despesa)"),
                ],
                default=GERENCIAL,
                max_length=20,
            ),
        ),
        migrations.RunPython(preencher_tipo, reverter_tipo),
        migrations.RemoveIndex(
            model_name="cashflowcategory",
            name="cash_flow_c_is_inte_ca4eaa_idx",
        ),
        migrations.RemoveField(
            model_name="cashflowcategory",
            name="is_internal",
        ),
        migrations.AddIndex(
            model_name="cashflowcategory",
            index=models.Index(fields=["kind"], name="cash_flow_c_kind_51270d_idx"),
        ),
        migrations.AddConstraint(
            model_name="cashflowcategory",
            constraint=models.CheckConstraint(
                condition=models.Q(kind__in=(GERENCIAL, TRANSFERENCIA, MOVIMENTACAO)),
                name="ck_cash_flow_category_kind_valid",
            ),
        ),
    ]
