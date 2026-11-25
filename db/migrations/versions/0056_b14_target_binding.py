"""Qualify the signed B-14 target in proof registration."""

from alembic import op

revision = "0056_b14_target_binding"
down_revision = "0055_b14_admission_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT CREATE ON SCHEMA public TO carepulse_identity_owner")
    op.execute("SET LOCAL ROLE carepulse_identity_owner")
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.f01_register_proof(bytea,text,bytea,bytea)'::regprocedure)"
    ).scalar_one()
    declaration = "DECLARE\n            request_id bigint;"
    target = "b14_target_id = target_id WHERE r.id = request_id;"
    if definition.count(declaration) != 1 or definition.count(target) != 1:
        raise RuntimeError("Missing unique B-14 proof target binding markers")
    connection.exec_driver_sql(
        definition.replace(
            declaration, "<<b14_proof>>\n        " + declaration
        ).replace(
            target, "b14_target_id = b14_proof.target_id WHERE r.id = request_id;"
        ),
        execution_options={"no_parameters": True},
    )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner")


def downgrade() -> None:
    raise RuntimeError("B-14 target binding migration is forward-only")
