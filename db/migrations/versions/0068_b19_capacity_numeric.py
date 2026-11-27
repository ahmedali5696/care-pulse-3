"""Preserve retained integral JSON capacity values in the B-19 snapshot guard."""

from alembic import op

revision = "0068_b19_capacity_numeric"
down_revision = "0067_b19_exclusion_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.f01_b19_assert_snapshot(jsonb)'::regprocedure)"
    ).scalar_one()
    marker = "(v.payload->>'capacity')::integer"
    if definition.count(marker) != 1:
        raise RuntimeError("Unexpected B-19 capacity guard source")
    connection.exec_driver_sql(
        definition.replace(marker, "((v.payload->>'capacity')::numeric)::integer"),
        execution_options={"no_parameters": True},
    )


def downgrade() -> None:
    raise RuntimeError("B-19 capacity numeric migration is forward-only")
