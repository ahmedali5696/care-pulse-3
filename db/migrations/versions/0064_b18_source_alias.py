"""Disambiguate B-18 scope from the D-08 source-state row variable."""

from alembic import op

revision = "0064_b18_source_alias"
down_revision = "0063_b18_reschedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.d08_context(uuid,uuid,jsonb)'::regprocedure)"
    ).scalar_one()
    marker = """SELECT 1 FROM public.f01_b18_primary_scope() s
                        JOIN public.followup_claims cl ON cl.organization_id=s.organization_id
                            AND cl.appointment_id=s.target_id
                        WHERE s.organization_id=org AND s.prepared AND cl.source_visit_id=visit"""
    replacement = """SELECT 1 FROM public.f01_b18_primary_scope() b18_scope
                        JOIN public.followup_claims cl ON cl.organization_id=b18_scope.organization_id
                            AND cl.appointment_id=b18_scope.target_id
                        WHERE b18_scope.organization_id=org AND b18_scope.prepared
                            AND cl.source_visit_id=visit"""
    if definition.count(marker) != 1:
        raise RuntimeError("Missing unique B-18 source scope marker")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def downgrade() -> None:
    raise RuntimeError("B-18 source alias migration is forward-only")
