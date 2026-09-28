"""Outbox transacional de invalidação para patrimônio v4."""

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


OUTBOX_FUNCTION = """
CREATE FUNCTION patrimonio_v4_outbox_append(
    event_resource text,
    event_record bigint,
    event_operation text
) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    next_cursor bigint;
BEGIN
    INSERT INTO patrimonio_v4_change_counter (id, value)`n    VALUES (1, 0)`n    ON CONFLICT (id) DO NOTHING;`n`n    UPDATE patrimonio_v4_change_counter`n    SET value = value + 1
    WHERE id = 1
    RETURNING value INTO next_cursor;

    INSERT INTO patrimonio_v4_outbox (
        cursor, resource, source_record_id, operation, changed_at
    ) VALUES (
        next_cursor, event_resource, event_record, event_operation, clock_timestamp()
    );
END;
$$;
"""

OUTBOX_TRIGGER = """
CREATE FUNCTION patrimonio_v4_outbox_emit() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_data jsonb;
    event_operation text;
BEGIN
    row_data := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    event_operation := CASE WHEN TG_OP = 'DELETE' THEN 'delete' ELSE 'upsert' END;

    IF TG_ARGV[0] = 'transfer' THEN
        IF TG_OP = 'UPDATE' AND (to_jsonb(OLD) ->> 'operation_type') = 'internal_transfer'
           AND (to_jsonb(NEW) ->> 'operation_type') <> 'internal_transfer' THEN
            row_data := to_jsonb(OLD);
            event_operation := 'delete';
        ELSIF row_data ->> 'operation_type' <> 'internal_transfer' THEN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END IF;
    END IF;

    PERFORM patrimonio_v4_outbox_append(
        TG_ARGV[0],
        (row_data ->> TG_ARGV[1])::bigint,
        event_operation
    );

    IF TG_ARGV[0] = 'cash_entry'
       AND row_data ->> 'operation_type' = 'internal_transfer'
       AND row_data ->> 'bank_operation_id' IS NOT NULL THEN
        PERFORM patrimonio_v4_outbox_append(
            'transfer',
            (row_data ->> 'bank_operation_id')::bigint,
            event_operation
        );
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
"""

RELATED_ACCOUNT_TRIGGER = """
CREATE FUNCTION patrimonio_v4_outbox_emit_related_accounts() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_data jsonb;
    account_row record;
BEGIN
    row_data := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    FOR account_row IN
        SELECT id FROM financial_account
        WHERE (CASE TG_ARGV[0]
            WHEN 'owner_id' THEN owner_id
            WHEN 'institution_id' THEN institution_id
        END) = (row_data ->> 'id')::bigint
    LOOP
        PERFORM patrimonio_v4_outbox_append('account', account_row.id, 'upsert');
    END LOOP;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
"""

TRIGGERS = (
    ("financial_account", "account", "id"),
    ("cash_flow_category", "category", "id"),
    ("cash_flow_entry", "cash_entry", "id"),
    ("bank_operation", "transfer", "id"),
)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_remover_contas_em_analises"),
        ("banking", "0007_conta_tipo_aplicacao"),
        ("core", "0002_auditlog_request_context"),
        ("transactions", "0007_remover_contadores_da_operacao"),
    ]

    operations = [
        migrations.CreateModel(
            name="PatrimonioV4ChangeCounter",
            fields=[
                ("id", models.SmallIntegerField(primary_key=True, serialize=False)),
                ("value", models.BigIntegerField(default=0)),
            ],
            options={"db_table": "patrimonio_v4_change_counter"},
        ),
        migrations.AddConstraint(
            model_name="patrimoniov4changecounter",
            constraint=models.CheckConstraint(condition=Q(("id", 1)), name="ck_patrimonio_v4_counter_singleton"),
        ),
        migrations.AddConstraint(
            model_name="patrimoniov4changecounter",
            constraint=models.CheckConstraint(condition=Q(("value__gte", 0)), name="ck_patrimonio_v4_counter_non_negative"),
        ),
        migrations.CreateModel(
            name="PatrimonioV4Outbox",
            fields=[
                ("cursor", models.BigIntegerField(primary_key=True, serialize=False)),
                ("resource", models.CharField(max_length=32)),
                ("source_record_id", models.BigIntegerField()),
                ("operation", models.CharField(max_length=8)),
                ("changed_at", models.DateTimeField(default=timezone.now)),
            ],
            options={"db_table": "patrimonio_v4_outbox"},
        ),
        migrations.AddConstraint(
            model_name="patrimoniov4outbox",
            constraint=models.CheckConstraint(
                condition=Q(("resource__in", ["account", "category", "cash_entry", "transfer"])),
                name="ck_patrimonio_v4_outbox_resource",
            ),
        ),
        migrations.AddConstraint(
            model_name="patrimoniov4outbox",
            constraint=models.CheckConstraint(
                condition=Q(("operation__in", ["upsert", "delete"])),
                name="ck_patrimonio_v4_outbox_operation",
            ),
        ),
        migrations.AddIndex(
            model_name="patrimoniov4outbox",
            index=models.Index(fields=["cursor"], name="ix_patrimonio_v4_outbox_cursor"),
        ),
        migrations.RunSQL(
            "INSERT INTO patrimonio_v4_change_counter (id, value) VALUES (1, 0)",
            "DELETE FROM patrimonio_v4_change_counter WHERE id = 1",
        ),
        migrations.RunSQL(OUTBOX_FUNCTION, "DROP FUNCTION patrimonio_v4_outbox_append(text, bigint, text)"),
        migrations.RunSQL(OUTBOX_TRIGGER, "DROP FUNCTION patrimonio_v4_outbox_emit()"),
        migrations.RunSQL(
            RELATED_ACCOUNT_TRIGGER,
            "DROP FUNCTION patrimonio_v4_outbox_emit_related_accounts()",
        ),
        *[
            migrations.RunSQL(
                f"CREATE TRIGGER patrimonio_v4_outbox_{table} "
                f"AFTER INSERT OR UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION patrimonio_v4_outbox_emit('{resource}', '{record}')",
                f"DROP TRIGGER patrimonio_v4_outbox_{table} ON {table}",
            )
            for table, resource, record in TRIGGERS
        ],
        migrations.RunSQL(
            "CREATE TRIGGER patrimonio_v4_outbox_account_owner "
            "AFTER INSERT OR UPDATE OR DELETE ON account_owner "
            "FOR EACH ROW EXECUTE FUNCTION patrimonio_v4_outbox_emit_related_accounts('owner_id')",
            "DROP TRIGGER patrimonio_v4_outbox_account_owner ON account_owner",
        ),
        migrations.RunSQL(
            "CREATE TRIGGER patrimonio_v4_outbox_financial_institution "
            "AFTER INSERT OR UPDATE OR DELETE ON financial_institution "
            "FOR EACH ROW EXECUTE FUNCTION patrimonio_v4_outbox_emit_related_accounts('institution_id')",
            "DROP TRIGGER patrimonio_v4_outbox_financial_institution ON financial_institution",
        ),
    ]
