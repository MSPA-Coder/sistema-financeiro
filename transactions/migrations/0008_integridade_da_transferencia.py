"""Impõe no PostgreSQL que transferência interna sempre tenha as duas pontas.

O service já cria a origem e a contraparte na mesma transação, mas a tabela
aceitava uma linha ``internal_transfer`` isolada quando algum caminho futuro,
uma importação ou uma manutenção contornasse o service. A FK ``source_entry``
por si só não resolve: a origem tem a FK nula por definição, e uma FK opcional
não exige que exista a linha que a referencia.

O gatilho é uma *constraint trigger* postergada. Assim, durante a transação o
service pode gravar primeiro a origem e depois o destino; no commit, contudo,
cada origem precisa ter exatamente uma contraparte e cada destino precisa
referenciar a sua origem na mesma operação. A migração só instala a proteção
depois de conferir os dados existentes: ela não inventa nem apaga lançamentos
financeiros históricos.
"""

from django.db import migrations


OPERACAO_TRANSFERENCIA = "internal_transfer"
LIMITE_DA_LISTA = 20


def conferir_transferencias(apps, schema_editor):
    CashFlowEntry = apps.get_model("transactions", "CashFlowEntry")
    BankOperation = apps.get_model("transactions", "BankOperation")

    entries = list(
        CashFlowEntry.objects.filter(operation_type=OPERACAO_TRANSFERENCIA)
        .order_by("id")
        .values("id", "bank_operation_id", "source_entry_id")
    )
    by_id = {entry["id"]: entry for entry in entries}
    destinations_by_source: dict[int, list[dict]] = {}
    for entry in entries:
        if entry["source_entry_id"] is not None:
            destinations_by_source.setdefault(entry["source_entry_id"], []).append(entry)

    operation_ids = {entry["bank_operation_id"] for entry in entries if entry["bank_operation_id"]}
    operations = {
        operation["id"]: operation["operation_type"]
        for operation in BankOperation.objects.filter(id__in=operation_ids).values("id", "operation_type")
    }
    invalidos: list[str] = []
    for entry in entries:
        operation_id = entry["bank_operation_id"]
        if operations.get(operation_id) != OPERACAO_TRANSFERENCIA:
            invalidos.append(f"#{entry['id']}: operação interna ausente ou incompatível")
            continue
        source_id = entry["source_entry_id"]
        if source_id is None:
            if len(destinations_by_source.get(entry["id"], [])) != 1:
                invalidos.append(f"#{entry['id']}: origem sem uma única contraparte")
            continue
        source = by_id.get(source_id)
        if source is None or source["bank_operation_id"] != operation_id:
            invalidos.append(f"#{entry['id']}: contraparte ausente ou de outra operação")

    if not invalidos:
        return
    linhas = "\n".join(invalidos[:LIMITE_DA_LISTA])
    if len(invalidos) > LIMITE_DA_LISTA:
        linhas += f"\n... e mais {len(invalidos) - LIMITE_DA_LISTA}"
    raise RuntimeError(
        "Há transferência(s) interna(s) sem par íntegro:\n"
        + linhas
        + "\nEsta migration não corrige dados. Corrija cada par pela tela ou "
        "por procedimento financeiro auditado e rode o migrate de novo."
    )


SQL = r"""
CREATE FUNCTION assert_internal_transfer_counterparty()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed_entry_id integer := COALESCE(NEW.id, OLD.id);
    new_operation_id integer := CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.bank_operation_id END;
    old_operation_id integer := CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.bank_operation_id END;
    invalid_entry_id integer;
BEGIN
    WITH candidates AS (
        SELECT entry.*
        FROM cash_flow_entry AS entry
        WHERE entry.operation_type = 'internal_transfer'
          AND (
              entry.id = changed_entry_id
              OR entry.source_entry_id = changed_entry_id
              OR entry.bank_operation_id = new_operation_id
              OR entry.bank_operation_id = old_operation_id
          )
    )
    SELECT entry.id
      INTO invalid_entry_id
      FROM candidates AS entry
      LEFT JOIN bank_operation AS operation ON operation.id = entry.bank_operation_id
     WHERE entry.bank_operation_id IS NULL
        OR operation.operation_type IS DISTINCT FROM 'internal_transfer'
        OR (
            entry.source_entry_id IS NULL
            AND (
                SELECT count(*)
                FROM cash_flow_entry AS destination
                WHERE destination.source_entry_id = entry.id
                  AND destination.bank_operation_id = entry.bank_operation_id
                  AND destination.operation_type = 'internal_transfer'
            ) <> 1
        )
        OR (
            entry.source_entry_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1
                FROM cash_flow_entry AS origin
                WHERE origin.id = entry.source_entry_id
                  AND origin.bank_operation_id = entry.bank_operation_id
                  AND origin.operation_type = 'internal_transfer'
            )
        )
     LIMIT 1;

    IF invalid_entry_id IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'ck_internal_transfer_has_counterparty',
            MESSAGE = 'Transferência interna exige uma única contraparte na mesma operação.',
            DETAIL = format('Lançamento inválido: %s.', invalid_entry_id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER ck_internal_transfer_has_counterparty
AFTER INSERT OR UPDATE OR DELETE ON cash_flow_entry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION assert_internal_transfer_counterparty();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS ck_internal_transfer_has_counterparty ON cash_flow_entry;
DROP FUNCTION IF EXISTS assert_internal_transfer_counterparty();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0007_remover_contadores_da_operacao"),
    ]

    operations = [
        migrations.RunPython(conferir_transferencias, migrations.RunPython.noop),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
