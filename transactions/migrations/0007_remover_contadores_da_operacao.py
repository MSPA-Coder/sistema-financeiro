"""Remove `entry_count`, `first_due_date` e `last_due_date` de `bank_operation`.

Eram cópias do que os lançamentos da operação já dizem, gravadas em vários
caminhos e lidas por nenhum. Divergiam da realidade (em 24/09/2026, 6 e 9 das
397 operações de produção) porque a projeção recorrente acrescenta ocorrências
sem atualizá-las -- e ficavam expostas ao SQL livre do FinancasMCP, onde um
número errado pareceria verdade. Quantidade e datas saem de `cash_flow_entry`.

Sem volta útil: o `reverse` recria as colunas vazias (0 e nulo), não os valores.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0006_encerramento_da_recorrencia"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="bankoperation",
            name="ck_bank_operation_entry_count_non_negative",
        ),
        migrations.RemoveField(model_name="bankoperation", name="entry_count"),
        migrations.RemoveField(model_name="bankoperation", name="first_due_date"),
        migrations.RemoveField(model_name="bankoperation", name="last_due_date"),
    ]
