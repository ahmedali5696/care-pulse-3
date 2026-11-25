"""Correct Routine proof parsing and expose only its signed source completion."""

from alembic import op

revision = "0052_b11_routine_scope"
down_revision = "0051_b11_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT CREATE ON SCHEMA public TO carepulse_identity_owner")
    op.execute("SET LOCAL ROLE carepulse_identity_owner")
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.f01_register_proof(bytea,text,bytea,bytea)'::regprocedure)"
    ).scalar_one()
    marker = (
        "(command->'new_booking_date_exception' - 'selected_service_date' - 'reason')"
    )
    if definition.count(marker) != 1:
        raise RuntimeError("Missing unique Routine proof marker")
    connection.exec_driver_sql(
        definition.replace(
            marker,
            "((command->'new_booking_date_exception') - 'selected_service_date' - 'reason')",
        ),
        execution_options={"no_parameters": True},
    )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner")
    op.execute("""
        CREATE POLICY f01_api_intake_source_completion ON public.appointment_history
            FOR SELECT TO carepulse_api USING (
                new_status = 'Completed' AND EXISTS (
                    SELECT 1 FROM public.f01_intake_authority() i
                    JOIN public.visits v ON v.organization_id = i.organization_id
                        AND v.id = (i.command->>'source_visit_id')::uuid
                        AND v.patient_id = i.patient_id
                    WHERE i.stage = 'PRIMARY' AND i.purpose = 'INTAKE_ROUTINE'
                        AND i.organization_id = appointment_history.organization_id
                        AND v.appointment_id = appointment_history.appointment_id
                )
            );
        ALTER POLICY f01_api_intake_select ON public.idempotency_receipts
            USING (EXISTS (
                SELECT 1 FROM public.f01_intake_authority() i
                WHERE i.organization_id = idempotency_receipts.organization_id
                    AND idempotency_receipts.scope_kind = 'TENANT'
                    AND idempotency_receipts.actor_kind = 'STAFF'
                    AND idempotency_receipts.staff_id = i.staff_id
                    AND idempotency_receipts.actor_id = i.staff_id
                    AND idempotency_receipts.operation = i.operation
                    AND idempotency_receipts.idempotency_key = i.idempotency_key
                    AND idempotency_receipts.scope_id = i.organization_id
                    AND idempotency_receipts.result_projection->>'patient_id' = i.patient_id::text
            ));
    """)


def downgrade() -> None:
    raise RuntimeError("B-11 Routine scope migration is forward-only")
