"""Retira `cancelado` do dominio de `status` nas duas tabelas.

Cancelar lancamento nunca chegou a existir para o usuario: a unica rota que
gravava esse status (`transactions:cancel_entry`) nao era acionada por tela
nenhuma e foi removida em 2026-08-22, junto com outras duas orfas.

Seguro porque foi conferido no banco de PRODUCAO antes de escrever esta
migracao -- zero linhas com `cancelado` em `cash_flow_entry` e em
`bank_operation`:

    cash_flow_entry:  realizado 534, a_vencer 183
    bank_operation:   realizado 298, a_vencer 9

O `AddConstraint` valida as linhas existentes ao subir; com nenhuma no estado
removido, nao ha o que reprovar. Se um dia esta migracao rodar num banco que
tenha linhas `cancelado`, ela FALHA -- e falhar e o comportamento certo:
significaria que a premissa acima nao vale ali, e a decisao precisa ser
retomada em vez de os dados serem convertidos as cegas.

Ordem importa: as restricoes saem antes de o campo mudar e voltam depois. Sem
isso o banco recusaria o estado intermediario.

Verificado em PostgreSQL 17 vazio antes de commitar, como TESTING.md exige para
mudanca de esquema, e nos quatro caminhos:

- bootstrap do zero: todas as migracoes aplicam, e as duas restricoes ficam
  sem `cancelado`;
- reversao: `migrate transactions 0001` devolve `cancelado` a restricao, e
  `migrate accounts 0004` recria as tres permissoes;
- com uma linha `cancelado` presente: **falha**, com
  `IntegrityError: check constraint ... is violated by some row`, e a
  restricao antiga fica INTACTA -- a migracao e atomica, nao deixa meio
  estado;
- com dados validos: aplica e preserva as linhas.
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
