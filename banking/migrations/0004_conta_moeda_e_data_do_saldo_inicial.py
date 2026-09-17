"""Moeda e data do saldo inicial na conta financeira.

Aditiva: nenhuma conta muda de comportamento. Toda conta existente nasce em
`BRL`, que é o que o sistema sempre assumiu sem dizer, e com o saldo inicial
datado de 31/12/2025 -- o corte a partir do qual os lançamentos existem
(o primeiro é de 02/01/2026).

O padrão do campo no modelo é `localdate`, para que uma conta criada amanhã não
nasça com data de 2025. Por isso o `AddField` preenche com a data de hoje e o
`RunPython` logo abaixo corrige o passado: são duas operações, e não um padrão
só, porque a data que serve para o histórico não é a que serve para o futuro.
"""

import datetime

import django.utils.timezone
from django.db import migrations, models

# Mesma data de corte das demais contas, escrita aqui porque migração não
# depende de constante do código: ela precisa continuar valendo daqui a anos.
DATA_DE_CORTE = datetime.date(2025, 12, 31)


def datar_saldo_inicial_existente(apps, schema_editor):
    FinancialAccount = apps.get_model('banking', 'FinancialAccount')
    FinancialAccount.objects.update(initial_balance_date=DATA_DE_CORTE)


def reverter_data_do_saldo_inicial(apps, schema_editor):
    """Nada a desfazer: a coluna inteira sai no reverso do `AddField`."""


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_user_transfer_destination_access'),
        ('banking', '0003_financialinstitution_name_ci_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='financialaccount',
            name='currency',
            field=models.CharField(
                choices=[('BRL', 'Real (R$)'), ('USD', 'Dólar (US$)')],
                default='BRL',
                help_text=(
                    'Moeda de todos os valores da conta. Os lançamentos não guardam '
                    'moeda própria: herdam a daqui.'
                ),
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name='financialaccount',
            name='initial_balance_date',
            field=models.DateField(
                default=django.utils.timezone.localdate,
                help_text='Data a que o saldo inicial se refere.',
            ),
        ),
        migrations.RunPython(datar_saldo_inicial_existente, reverter_data_do_saldo_inicial),
        migrations.AddConstraint(
            model_name='financialaccount',
            constraint=models.CheckConstraint(
                condition=models.Q(('currency__in', ('BRL', 'USD'))),
                name='ck_financial_account_currency_valid',
            ),
        ),
    ]
