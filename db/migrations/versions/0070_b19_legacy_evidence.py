"""Retain the original commit contract for immutable pre-0069 previews."""

from alembic import op

revision = "0070_b19_legacy_evidence"
down_revision = "0069_b19_interval_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.f01_b19_assert_snapshot(jsonb)'::regprocedure)"
    ).scalar_one()
    marker = "IF included IS DISTINCT FROM expected_included"
    replacement = """-- Only an immutable commit selection can have legacy missing evidence.
                IF a.purpose='APPOINTMENT_BULK_COMMIT' AND NOT included
                    AND candidate IS NULL AND item->'starts_at'='null'::jsonb
                    AND item->'ends_at'='null'::jsonb
                    AND snapshot IS NOT DISTINCT FROM (selection-'preview_id'-'revision') THEN
                    carried_start:=NULL;
                    carried_end:=NULL;
                END IF;
                IF included IS DISTINCT FROM expected_included"""
    if definition.count(marker) != 1:
        raise RuntimeError("Unexpected B-19 legacy evidence guard")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def downgrade() -> None:
    raise RuntimeError("B-19 legacy evidence migration is forward-only")
