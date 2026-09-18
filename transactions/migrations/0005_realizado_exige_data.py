"""Lançamento realizado passa a exigir data e valor de realização no banco.

O saldo realizado é filtrado por `realized_date`: um realizado sem data não
cai em intervalo nenhum e some de todos os saldos, sem aviso. O valor vazio era
lido de dois jeitos -- o saldo usava o previsto, o planejamento anual usava
zero. Desde esta versão o serviço recusa a data vazia e grava o previsto
quando o valor vem vazio; esta migration põe o piso no banco.

ESTA MIGRATION NÃO CORRIGE DADOS. O `AddConstraint` valida as linhas
existentes, e cada realizado incompleto pede uma decisão que o código não tem
como tomar: o #1236 de produção, pelo extrato e pelo fechamento de junho, é
duplicata de outro lançamento, e preencher a data dele descontaria a mesma
corretagem duas vezes.
Por isso `conferir_realizados` roda antes e, se achar alguma linha, para com a
lista dos ids em vez do `IntegrityError` cru. A operação é atômica: nada fica
aplicado pela metade.

A reversão remove a constraint; a conferência não tem o que desfazer.
"""

from django.db import migrations, models

REALIZADO = "realizado"
LIMITE_DA_LISTA = 20


def conferir_realizados(apps, schema_editor):
    CashFlowEntry = apps.get_model("transactions", "CashFlowEntry")
    incompletos = list(
        CashFlowEntry.objects.filter(status=REALIZADO)
        .filter(models.Q(realized_date__isnull=True) | models.Q(realized_amount__isnull=True))
        .order_by("id")
        .values_list("id", "realized_date", "realized_amount")
    )
    if not incompletos:
        return

    linhas = []
    for entry_id, data, valor in incompletos[:LIMITE_DA_LISTA]:
        faltas = [nome for nome, campo in (("data", data), ("valor", valor)) if campo is None]
        linhas.append(f"  #{entry_id}: sem {' e sem '.join(faltas)}")
    if len(incompletos) > LIMITE_DA_LISTA:
        linhas.append(f"  ... e mais {len(incompletos) - LIMITE_DA_LISTA}")

    raise RuntimeError(
        f"{len(incompletos)} lançamento(s) realizado(s) sem data ou sem valor de "
        "realização:\n"
        + "\n".join(linhas)
        + "\nEsta migration não corrige dados. Para cada um, decida se é duplicata "
        "(excluir), se falta a data e o valor reais (editar) ou se não foi "
        "realizado (voltar a ficar em aberto). Corrija pela tela, reabrindo o mês "
        "se ele estiver fechado, e rode o migrate de novo."
    )


class Migration(migrations.Migration):

    dependencies = [
        ("banking", "0004_conta_moeda_e_data_do_saldo_inicial"),
        ("transactions", "0004_categoria_tipo"),
    ]

    operations = [
        migrations.RunPython(conferir_realizados, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="cashflowentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", REALIZADO), _negated=True),
                    models.Q(("realized_amount__isnull", False), ("realized_date__isnull", False)),
                    _connector="OR",
                ),
                name="ck_cash_flow_entry_realized_has_date_and_amount",
            ),
        ),
    ]
