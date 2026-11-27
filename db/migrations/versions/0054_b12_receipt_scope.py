"""Bind check-in receipt reads and conflict recognition to signed targets."""

from alembic import op

revision = "0054_b12_receipt_scope"
down_revision = "0053_b11_receipt_conflict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The existing writer authority deliberately excludes receipt recovery.
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        CREATE FUNCTION public.f01_checkin_replay_authority()
        RETURNS TABLE (organization_id uuid, staff_id uuid, target_id uuid,
                       idempotency_key text, digest text, expected_revision bigint)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT r.organization_id, r.staff_id, r.target_id, r.idempotency_key,
                   r.checkin_digest, r.checkin_expected_revision
            FROM public.f01_context() c
            JOIN f01_private.requests r ON r.id = c.request_id
            WHERE c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
              AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
              AND c.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
              AND r.checkin_digest IS NOT NULL;
        $$;
        REVOKE ALL ON FUNCTION public.f01_checkin_replay_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_checkin_replay_authority() TO carepulse_migration;
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)
    op.execute("""
        CREATE FUNCTION public.f01_checkin_target_visible() RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.f01_checkin_replay_authority() c
                JOIN public.appointments a ON a.organization_id = c.organization_id
                    AND a.id = c.target_id
            );
        $$;

        CREATE POLICY f01_api_checkin_receipt_target ON public.idempotency_receipts
            AS RESTRICTIVE FOR SELECT TO carepulse_api
            USING (operation <> 'appointments.check_in' OR EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = idempotency_receipts.organization_id::text
                  AND idempotency_receipts.scope_id = idempotency_receipts.organization_id
                  AND idempotency_receipts.result_projection->>'appointment_id' = c.target_id::text
                  AND public.f01_checkin_target_visible()
            ));

        -- Historical facts survive later admission, cancellation, rank shifts and rescheduling.
        CREATE FUNCTION public.f01_checkin_receipt_witness()
        RETURNS TABLE (target_matches boolean, digest_matches boolean)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT a.id = c.target_id, r.request_digest = c.digest
            FROM public.f01_checkin_replay_authority() c
            JOIN public.appointments requested ON requested.organization_id = c.organization_id
                AND requested.id = c.target_id
            JOIN public.idempotency_receipts r ON r.organization_id = c.organization_id
                AND r.scope_id = c.organization_id AND r.scope_kind = 'TENANT'
                AND r.actor_kind = 'STAFF' AND r.staff_id = c.staff_id AND r.actor_id = c.staff_id
                AND r.operation = 'appointments.check_in' AND r.idempotency_key = c.idempotency_key
            JOIN public.appointments a ON a.organization_id = c.organization_id
                AND a.id = CASE WHEN pg_input_is_valid(r.result_projection->>'appointment_id', 'uuid')
                    THEN (r.result_projection->>'appointment_id')::uuid END
                AND to_jsonb(a.id) = r.result_projection->'appointment_id'
                AND to_jsonb(a.patient_id) = r.result_projection->'patient_id'
                AND to_jsonb(a.doctor_staff_id) = r.result_projection->'doctor_staff_id'
                AND to_jsonb(a.classification) = r.result_projection->'classification'
            JOIN public.queue_entries e ON e.organization_id = a.organization_id
                AND e.id = CASE WHEN pg_input_is_valid(r.result_projection->>'queue_entry_id', 'uuid')
                    THEN (r.result_projection->>'queue_entry_id')::uuid END
                AND e.appointment_id = a.id AND e.patient_id = a.patient_id
                AND e.doctor_staff_id = a.doctor_staff_id AND e.attested_by_staff_id = c.staff_id
                AND to_jsonb(e.id) = r.result_projection->'queue_entry_id'
                AND to_jsonb(e.service_date) = r.result_projection->'service_date'
                AND to_jsonb(e.lane) = r.result_projection->'lane'
                AND to_jsonb(e.arrival_sequence) = r.result_projection->'arrival_sequence'
                AND to_jsonb(e.attested_by_staff_id) = r.result_projection->'attested_by_staff_id'
                AND e.attested_at = CASE WHEN pg_input_is_valid(
                    r.result_projection->>'attested_at', 'timestamp with time zone')
                    THEN (r.result_projection->>'attested_at')::timestamptz END
            JOIN public.queue_days q ON q.organization_id = e.organization_id AND q.id = e.queue_day_id
                AND to_jsonb(q.id) = r.result_projection->'queue_day_id'
                AND to_jsonb(q.queue_policy_version) = r.result_projection->'queue_policy_version'
            JOIN public.appointment_history h ON h.organization_id = a.organization_id
                AND h.appointment_id = a.id AND h.appointment_revision = r.result_revision
                AND h.action = 'CHECKED_IN' AND h.old_status = 'Booked' AND h.new_status = 'Waiting'
                AND h.actor_staff_id = c.staff_id AND h.old_service_date = e.service_date
                AND h.new_service_date = e.service_date AND h.old_number = h.new_number
                AND to_jsonb(h.new_number) = r.result_projection->'booking_number'
            WHERE r.result_revision > 0
                AND to_jsonb(r.result_revision) = r.result_projection->'appointment_revision'
                AND r.result_projection->'status' = to_jsonb('Waiting'::text)
                AND (r.request_digest <> c.digest OR r.result_revision - 1 = c.expected_revision)
                AND jsonb_typeof(r.result_projection->'queue_day_revision') = 'number'
                AND r.result_projection->'queue_day_revision' <= to_jsonb(q.revision)
                AND jsonb_typeof(r.result_projection->'routine_cursor') = 'number'
                AND r.result_projection->'routine_cursor' <= to_jsonb(q.routine_cursor)
                AND r.result_projection->'queue_entry_revision' = to_jsonb(1)
                AND EXISTS (
                    SELECT 1 FROM public.sensitive_audit s
                    WHERE s.organization_id = a.organization_id AND s.action = r.operation
                      AND s.correlation_id = a.id::text AND s.actor_staff_id = h.actor_staff_id
                      AND s.actor_role = h.actor_role
                      AND s.before_state = jsonb_build_object(
                          'appointment_id', a.id, 'status', 'Booked', 'revision', r.result_revision - 1)
                      AND s.after_state = jsonb_build_object(
                          'appointment_id', a.id, 'queue_entry_id', e.id, 'status', 'Waiting',
                          'revision', r.result_revision, 'service_date', e.service_date,
                          'lane', e.lane, 'arrival_sequence', e.arrival_sequence)
                );
        $$;

        CREATE FUNCTION public.f01_checkin_receipt_matches() RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_checkin_receipt_witness() w
                           WHERE w.target_matches AND w.digest_matches);
        $$;

        CREATE FUNCTION public.f01_checkin_key_conflict() RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_checkin_receipt_witness() w
                           WHERE NOT w.target_matches AND NOT w.digest_matches);
        $$;

        REVOKE ALL ON FUNCTION public.f01_checkin_target_visible(),
            public.f01_checkin_receipt_witness(), public.f01_checkin_receipt_matches(),
            public.f01_checkin_key_conflict() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_checkin_target_visible(),
            public.f01_checkin_receipt_matches(), public.f01_checkin_key_conflict() TO carepulse_api;
    """)


def downgrade() -> None:
    raise RuntimeError("B-12 receipt scope migration is forward-only")
