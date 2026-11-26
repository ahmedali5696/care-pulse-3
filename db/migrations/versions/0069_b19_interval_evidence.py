"""Validate carried-clock evidence independently of B-19 eligibility."""

from alembic import op

revision = "0069_b19_interval_evidence"
down_revision = "0068_b19_capacity_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.f01_b19_assert_snapshot(jsonb)'::regprocedure)"
    ).scalar_one()
    replacements = (
        (
            "conflicts uuid[]; destination date;",
            "conflicts uuid[]; carried_start timestamptz; carried_end timestamptz; destination date;",
        ),
        (
            "candidate:=candidates->(row_item->>'id');",
            """candidate:=candidates->(row_item->>'id');
                carried_start:=NULL;
                carried_end:=NULL;
                IF row_item->>'booking_mode'='EXACT_TIME' THEN
                    carried_start:=public.f01_b19_carried(destination,((row_item->>'starts_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time);
                    carried_end:=public.f01_b19_carried(destination,((row_item->>'ends_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time);
                    IF carried_start IS NULL OR carried_end IS NULL OR carried_start>=carried_end THEN
                        carried_start:=NULL;
                        carried_end:=NULL;
                    END IF;
                END IF;""",
        ),
        ("(candidate->>'starts_at')::timestamptz", "carried_start"),
        ("(candidate->>'ends_at')::timestamptz", "carried_end"),
    )
    for marker, replacement in replacements:
        if definition.count(marker) != 1:
            raise RuntimeError(f"Unexpected B-19 interval evidence guard: {marker}")
        definition = definition.replace(marker, replacement)
    connection.exec_driver_sql(definition, execution_options={"no_parameters": True})


def downgrade() -> None:
    raise RuntimeError("B-19 interval evidence migration is forward-only")
