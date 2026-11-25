"""Recognize a conflicting intake key without widening receipt visibility."""

from alembic import op

revision = "0053_b11_receipt_conflict"
down_revision = "0052_b11_routine_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_intake_key_conflict() RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.f01_intake_authority() i
                JOIN public.patients requested ON requested.organization_id = i.organization_id
                    AND requested.id = i.patient_id
                JOIN public.idempotency_receipts r ON r.organization_id = i.organization_id
                    AND r.scope_id = i.organization_id AND r.scope_kind = 'TENANT'
                    AND r.actor_kind = 'STAFF' AND r.staff_id = i.staff_id AND r.actor_id = i.staff_id
                    AND r.idempotency_key = i.idempotency_key AND r.operation = i.operation
                JOIN public.appointments a ON a.organization_id = i.organization_id
                    AND a.id = CASE WHEN pg_input_is_valid(r.result_projection->>'id', 'uuid')
                        THEN (r.result_projection->>'id')::uuid END
                    AND a.patient_id::text = r.result_projection->>'patient_id'
                    AND a.doctor_staff_id::text = r.result_projection->>'doctor_staff_id'
                    AND a.service_type = r.result_projection->>'service_type'
                    AND a.classification = i.classification
                    AND a.classification = r.result_projection->>'classification'
                JOIN public.patients original ON original.organization_id = i.organization_id
                    AND original.id = a.patient_id
                JOIN public.queue_entries e ON e.organization_id = i.organization_id
                    AND e.id = CASE WHEN pg_input_is_valid(r.result_projection->>'queue_entry_id', 'uuid')
                        THEN (r.result_projection->>'queue_entry_id')::uuid END
                    AND e.appointment_id = a.id AND e.patient_id = original.id
                    AND e.attested_by_staff_id = i.staff_id
                WHERE original.id <> requested.id AND r.result_revision = 0
                    AND r.request_digest <> i.digest
            );
        $$;
        REVOKE ALL ON FUNCTION public.f01_intake_key_conflict() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_intake_key_conflict() TO carepulse_api;
    """)


def downgrade() -> None:
    raise RuntimeError("B-11 receipt conflict migration is forward-only")
