"""Operação recorrente passa a guardar a data em que a série foi encerrada.

Excluir uma recorrência "deste registro em diante" apagava a cauda, mas a
projeção recriava tudo na execução seguinte, porque as ocorrências que
restaram continuavam recorrentes. O campo registra o corte; a projeção ignora
a operação que o tiver preenchido.

ESTA MIGRATION NÃO CORRIGE DADOS. As operações 162 e 164 de produção foram
contornadas em 23/09/2026 desligando `is_recurring` nas linhas restantes, e
esse estado já fica fora da projeção sem data de encerramento. Não há como
distinguir, pelo banco, outra série já truncada de uma série viva.

A reversão remove a coluna.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0005_realizado_exige_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="bankoperation",
            name="recurrence_ended_on",
            field=models.DateField(blank=True, null=True),
        ),
    ]
