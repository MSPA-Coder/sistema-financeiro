"""Retira `cancelado` do dominio de `status` nas duas tabelas.

O `AddConstraint` valida as linhas existentes. Se esta migracao encontrar uma
linha `cancelado`, ela deve falhar em vez de converter dados silenciosamente;
o tratamento desses registros exige uma decisao explicita do mantenedor.

Ordem importa: as restricoes saem antes de o campo mudar e voltam depois. Sem
isso o banco recusaria o estado intermediario.

A operacao e atomica: uma falha de validacao preserva a restricao anterior.
Com dados validos, aplica as novas restricoes e preserva as linhas. A reversao
para `transactions 0001` restaura `cancelado` no dominio.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('banking', '0002_financialinstitution_homologada'),
        ('transactions', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='bankoperation',
            name='ck_bank_operation_status_valid',
        ),
        migrations.RemoveConstraint(
            model_name='cashflowentry',
            name='ck_cash_flow_entry_status_valid',
        ),
        migrations.AlterField(
            model_name='bankoperation',
            name='status',
            field=models.CharField(choices=[('a_vencer', 'A vencer'), ('vencidos', 'Vencidos'), ('realizado', 'Realizado')], default='a_vencer', max_length=20),
        ),
        migrations.AlterField(
            model_name='cashflowentry',
            name='status',
            field=models.CharField(choices=[('a_vencer', 'A vencer'), ('vencidos', 'Vencidos'), ('realizado', 'Realizado')], default='a_vencer', max_length=20),
        ),
        migrations.AddConstraint(
            model_name='bankoperation',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ('a_vencer', 'vencidos', 'realizado'))), name='ck_bank_operation_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='cashflowentry',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ('a_vencer', 'vencidos', 'realizado'))), name='ck_cash_flow_entry_status_valid'),
        ),
    ]
