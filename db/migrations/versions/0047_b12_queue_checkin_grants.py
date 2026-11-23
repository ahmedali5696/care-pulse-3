"""B-12: F-01 route purposes, least-privilege grants and RLS for check-in and queue read."""

from alembic import op

revision = "0047_b12_queue_checkin_grants"
down_revision = "0046_f01_audit_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Extend F-01 route_purpose and appointment_history audit guard under carepulse_identity_owner.
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
    """)
    op.execute(r"""
        CREATE OR REPLACE FUNCTION f01_private.route_purpose(p_method text, p_path text) RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
        DECLARE uuid_pattern constant text := '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}';
        BEGIN
            RETURN CASE
                WHEN p_method = 'POST' AND p_path = '/v1/provisioning/owner-activate'
                    THEN 'OWNER_ACTIVATION'
                WHEN p_method = 'POST' AND p_path = '/v1/staff/receptionists/invite-accept'
                    THEN 'RECEPTIONIST_ACCEPT'
                WHEN p_method = 'POST' AND p_path IN ('/v1/staff/recovery/reserve',
                    '/v1/staff/recovery/finalize') THEN 'OWNER_RECOVERY'
                WHEN (p_method IN ('GET', 'POST') AND p_path = '/v1/staff/receptionists')
                    OR (p_method = 'GET' AND p_path ~ ('^/v1/staff/receptionists/' || uuid_pattern || '$'))
                    OR (p_method = 'POST' AND p_path ~ ('^/v1/staff/receptionists/' || uuid_pattern ||
                        '/(enable|disable|invite|revoke-invite)$'))
                    OR (p_method = 'PATCH' AND p_path ~ ('^/v1/staff/receptionists/' || uuid_pattern ||
                        '/permissions$')) THEN 'STAFF_MANAGEMENT'
                WHEN (p_method IN ('GET', 'POST') AND p_path = '/v1/patients')
                    OR (p_method IN ('GET', 'PATCH') AND p_path ~ ('^/v1/patients/' || uuid_pattern || '$'))
                    OR (p_method = 'POST' AND p_path ~ ('^/v1/patients/' || uuid_pattern ||
                        '/(archive|unarchive)$')) THEN 'PATIENT_REGISTRY'
                WHEN (p_method IN ('GET', 'POST') AND p_path = '/v1/appointments')
                    OR (p_method = 'GET' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '$'))
                    THEN 'APPOINTMENT_BOOKING'
                WHEN p_method = 'POST' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '/check-in$')
                    THEN 'QUEUE_CHECK_IN'
                WHEN p_method = 'GET' AND p_path = '/v1/queue'
                    THEN 'QUEUE_READ'
                WHEN p_method IN ('GET', 'PATCH') AND p_path = '/v1/settings/pricing'
                    THEN 'SETTINGS_PRICING'
                WHEN (p_method IN ('GET', 'PATCH') AND p_path = '/v1/settings/schedule')
                    OR (p_method = 'POST' AND p_path IN ('/v1/settings/schedule/preview',
                        '/v1/settings/schedule/activate')) THEN 'SETTINGS_SCHEDULE'
                WHEN p_method = 'GET' AND p_path = '/v1/slots' THEN 'SLOTS'
                WHEN (p_method = 'PATCH' AND p_path ~ ('^/v1/visits/' || uuid_pattern || '$'))
                    OR (p_method = 'POST' AND p_path ~ ('^/v1/visits/' || uuid_pattern ||
                        '/extend-followup$')) THEN 'VISIT_FOLLOWUP'
            END;
        END $$;

        CREATE OR REPLACE FUNCTION f01_private.guard_audit_appointment_history() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_context() c
                LEFT JOIN f01_private.activations a ON a.request_id = c.request_id AND a.stage = c.stage
                WHERE c.stage = 'PRIMARY'
                  AND c.organization_id = NEW.organization_id::text
                  AND c.staff_id = NEW.actor_staff_id::text
                  AND c.role = NEW.actor_role
                  AND NEW.price_snapshot_appointment_id = NEW.appointment_id
                  AND NEW.replacement_appointment_id IS NULL
                  AND NEW.reason IS NULL
                  AND (
                      (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments'
                          AND a.audit_appointment_id = NEW.appointment_id
                          AND NEW.action = 'CREATED' AND NEW.appointment_revision = 0)
                      OR (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                          AND c.target_id = NEW.appointment_id
                          AND NEW.action = 'CHECKED_IN' AND NEW.appointment_revision > 0
                          AND NEW.old_status = 'Booked' AND NEW.new_status = 'Waiting'
                          AND NEW.old_service_date = NEW.new_service_date
                          AND NEW.old_number = NEW.new_number)
                  )
            ) THEN
                RAISE EXCEPTION 'Appointment history scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)

    # 2) Least-privilege column/table grants for B-12 check-in and dated queue read.
    op.execute("""
        GRANT UPDATE (status, revision) ON public.appointments TO carepulse_api;
        GRANT SELECT, INSERT ON public.queue_days TO carepulse_api;
        GRANT UPDATE (revision, arrival_counter) ON public.queue_days TO carepulse_api;
        GRANT SELECT ON public.queue_entries TO carepulse_api;
        GRANT INSERT (id, organization_id, queue_day_id, appointment_id,
                      patient_id, doctor_staff_id, service_date, lane,
                      routine_rank, disposition)
            ON public.queue_entries TO carepulse_api;
        GRANT UPDATE (routine_rank, revision) ON public.queue_entries TO carepulse_api;
        GRANT SELECT ON public.doctor_controls TO carepulse_api;
    """)

    # 3) Queue tables RLS policies for carepulse_api.
    op.execute(r"""
        CREATE POLICY f01_api_read ON public.queue_days FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_days.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose IN ('QUEUE_CHECK_IN', 'QUEUE_READ')
            ));
        CREATE POLICY f01_api_insert ON public.queue_days FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_days.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
            ));
        CREATE POLICY f01_api_update ON public.queue_days FOR UPDATE TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_days.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_days.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
            ));

        CREATE POLICY f01_api_read ON public.queue_entries FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_entries.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose IN ('QUEUE_CHECK_IN', 'QUEUE_READ')
            ));
        CREATE POLICY f01_api_insert ON public.queue_entries FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_entries.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                  AND queue_entries.appointment_id = c.target_id
                  AND queue_entries.disposition = 'ACTIVE'
            ));
        CREATE POLICY f01_api_update ON public.queue_entries FOR UPDATE TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_entries.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                  AND queue_entries.disposition = 'ACTIVE'
                  AND queue_entries.routine_rank IS NOT NULL
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = queue_entries.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                  AND queue_entries.disposition = 'ACTIVE'
                  AND queue_entries.routine_rank IS NOT NULL
            ));

        CREATE POLICY f01_api_read ON public.doctor_controls FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = doctor_controls.organization_id::text
                  AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_READ' AND c.method = 'GET'
                  AND c.path = '/v1/queue'
            ));
    """)

    # 4) Extend existing policies on patients, appointments, appointment_history,
    #    policy tables, receipts, audit, and outbox for QUEUE_CHECK_IN / QUEUE_READ.
    op.execute(r"""
        DROP POLICY f01_api_read ON public.patients;
        CREATE POLICY f01_api_read ON public.patients FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                LEFT JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = patients.organization_id::text
                  AND (
                      (c.purpose = 'PATIENT_REGISTRY'
                          AND (c.role = 'DOCTOR_OWNER' OR NOT patients.archived)
                          AND (
                              (c.method = 'GET' AND c.path = '/v1/patients' AND c.stage = 'PRIMARY')
                              OR (c.target_id IS NOT NULL AND patients.id = c.target_id AND c.stage = 'PRIMARY')
                              OR (c.method = 'POST' AND c.path = '/v1/patients' AND (
                                  a.patient_id = patients.id
                                  OR EXISTS (
                                      SELECT 1 FROM public.idempotency_receipts r
                                      WHERE r.organization_id = patients.organization_id
                                        AND r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
                                        AND r.staff_id::text = c.staff_id AND r.actor_id::text = c.staff_id
                                        AND r.operation = 'patients.create'
                                        AND r.idempotency_key = c.idempotency_key
                                        AND r.result_projection->>'id' = patients.id::text
                                  )
                              ))
                          ))
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments' AND patients.id = c.patient_id)
                      OR (c.purpose = 'VISIT_FOLLOWUP' AND c.stage = 'PRIMARY'
                          AND EXISTS (
                              SELECT 1 FROM public.visits v
                              WHERE v.organization_id = patients.organization_id
                                AND v.id = c.target_id AND v.patient_id = patients.id
                          ))
                      OR (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND c.stage = 'PRIMARY'
                          AND EXISTS (
                              SELECT 1 FROM public.appointments ap
                              WHERE ap.organization_id = patients.organization_id
                                AND ap.id = c.target_id AND ap.patient_id = patients.id
                          ))
                      OR (c.purpose = 'QUEUE_READ' AND c.method = 'GET'
                          AND c.path = '/v1/queue' AND c.stage = 'PRIMARY'
                          AND EXISTS (
                              SELECT 1 FROM public.queue_entries qe
                              WHERE qe.organization_id = patients.organization_id
                                AND qe.patient_id = patients.id
                                AND qe.disposition = 'ACTIVE'
                          ))
                  )
            ));

        DROP POLICY f01_api_update ON public.patients;
        CREATE POLICY f01_api_update ON public.patients FOR UPDATE TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = patients.organization_id::text AND c.stage = 'PRIMARY'
                  AND (
                      (c.purpose = 'PATIENT_REGISTRY' AND c.method IN ('PATCH', 'POST')
                          AND c.target_id IS NOT NULL AND patients.id = c.target_id)
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments' AND patients.id = c.patient_id)
                      OR (c.purpose = 'VISIT_FOLLOWUP' AND c.method IN ('PATCH', 'POST')
                          AND EXISTS (
                              SELECT 1 FROM public.visits v
                              WHERE v.organization_id = patients.organization_id
                                AND v.id = c.target_id AND v.patient_id = patients.id
                          ))
                      OR (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                          AND EXISTS (
                              SELECT 1 FROM public.appointments ap
                              WHERE ap.organization_id = patients.organization_id
                                AND ap.id = c.target_id AND ap.patient_id = patients.id
                          ))
                  )
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = patients.organization_id::text AND c.stage = 'PRIMARY'
                  AND c.purpose = 'PATIENT_REGISTRY' AND c.target_id = patients.id
                  AND (c.method = 'PATCH' OR (c.method = 'POST' AND c.role = 'DOCTOR_OWNER'))
            ));

        DROP POLICY f01_api_read ON public.appointments;
        CREATE POLICY f01_api_read ON public.appointments FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                LEFT JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointments.organization_id::text
                  AND (
                      (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'GET'
                          AND c.path = '/v1/appointments' AND c.stage = 'PRIMARY')
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'GET'
                          AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND (
                              appointments.id = c.target_id
                              OR EXISTS (
                                  SELECT 1 FROM public.followup_claims cl
                                  WHERE cl.organization_id = appointments.organization_id
                                    AND cl.appointment_id = c.target_id
                                    AND cl.source_appointment_id = appointments.id
                              )
                          ))
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments'
                          AND (
                              a.appointment_id = appointments.id
                              OR (c.stage = 'PRIMARY' AND (
                                  appointments.patient_id = c.patient_id
                                  OR EXISTS (
                                      SELECT 1 FROM public.visits v
                                      WHERE v.organization_id = appointments.organization_id
                                        AND v.id = c.source_visit_id
                                        AND v.appointment_id = appointments.id
                                        AND v.patient_id = c.patient_id
                                  )
                              ))
                              OR EXISTS (
                                  SELECT 1 FROM public.idempotency_receipts r
                                  WHERE r.organization_id = appointments.organization_id
                                    AND r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
                                    AND r.staff_id::text = c.staff_id AND r.actor_id::text = c.staff_id
                                    AND r.operation = 'appointments.book'
                                    AND r.idempotency_key = c.idempotency_key
                                    AND (
                                        r.result_projection->>'id' = appointments.id::text
                                        OR EXISTS (
                                            SELECT 1 FROM public.followup_claims cl
                                            WHERE cl.organization_id = appointments.organization_id
                                              AND cl.appointment_id::text = r.result_projection->>'id'
                                              AND cl.source_appointment_id = appointments.id
                                        )
                                    )
                              )
                          ))
                      OR (c.purpose = 'VISIT_FOLLOWUP' AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                          AND (
                              EXISTS (
                                  SELECT 1 FROM public.visits v
                                  WHERE v.organization_id = appointments.organization_id
                                    AND v.id = c.target_id AND v.appointment_id = appointments.id
                              )
                              OR (c.method = 'POST' AND appointments.id = c.target_appointment_id
                                  AND EXISTS (
                                      SELECT 1 FROM public.followup_claims cl
                                      WHERE cl.organization_id = appointments.organization_id
                                        AND cl.source_visit_id = c.target_id
                                        AND cl.appointment_id = appointments.id
                                  ))
                          ))
                      OR (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                          AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                          AND c.target_id IS NOT NULL AND appointments.id = c.target_id)
                      OR (c.purpose = 'QUEUE_READ' AND c.method = 'GET'
                          AND c.path = '/v1/queue' AND c.stage = 'PRIMARY'
                          AND appointments.status IN ('Waiting', 'In Examination'))
                  )
            ));

        DROP POLICY f01_api_update ON public.appointments;
        CREATE POLICY f01_api_update ON public.appointments FOR UPDATE TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = appointments.organization_id::text AND c.stage = 'PRIMARY'
                  AND (
                      (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments' AND appointments.patient_id = c.patient_id
                          AND EXISTS (
                              SELECT 1 FROM public.visits v
                              WHERE v.organization_id = appointments.organization_id
                                AND v.id = c.source_visit_id
                                AND v.appointment_id = appointments.id
                                AND v.patient_id = c.patient_id
                          ))
                      OR (c.purpose = 'VISIT_FOLLOWUP' AND c.method IN ('PATCH', 'POST')
                          AND (
                              EXISTS (
                                  SELECT 1 FROM public.visits v
                                  WHERE v.organization_id = appointments.organization_id
                                    AND v.id = c.target_id AND v.appointment_id = appointments.id
                              )
                              OR (c.method = 'POST' AND appointments.id = c.target_appointment_id
                                  AND EXISTS (
                                      SELECT 1 FROM public.followup_claims cl
                                      WHERE cl.organization_id = appointments.organization_id
                                        AND cl.source_visit_id = c.target_id
                                        AND cl.appointment_id = appointments.id
                                  ))
                          ))
                      OR (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                          AND c.target_id IS NOT NULL AND appointments.id = c.target_id)
                  )
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = appointments.organization_id::text AND c.stage = 'PRIMARY'
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                  AND c.target_id IS NOT NULL AND appointments.id = c.target_id
                  AND appointments.status = 'Waiting'
            ));

        DROP POLICY f01_api_insert ON public.appointment_history;
        CREATE POLICY f01_api_insert ON public.appointment_history FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                LEFT JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_history.organization_id::text
                  AND c.stage = 'PRIMARY' AND c.method = 'POST'
                  AND (
                      (c.purpose = 'APPOINTMENT_BOOKING' AND c.path = '/v1/appointments'
                          AND a.appointment_id = appointment_history.appointment_id)
                      OR (c.purpose = 'QUEUE_CHECK_IN'
                          AND c.target_id = appointment_history.appointment_id)
                  )
            ));

        DROP POLICY f01_api_read ON public.clinic_policy_versions;
        CREATE POLICY f01_api_read ON public.clinic_policy_versions FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = clinic_policy_versions.organization_id::text
                  AND (c.purpose IN ('APPOINTMENT_BOOKING','VISIT_FOLLOWUP',
                                     'SETTINGS_PRICING','SETTINGS_SCHEDULE','SLOTS',
                                     'QUEUE_CHECK_IN','QUEUE_READ'))
            ));

        DROP POLICY f01_api_read ON public.clinic_policy_heads;
        CREATE POLICY f01_api_read ON public.clinic_policy_heads FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = clinic_policy_heads.organization_id::text
                  AND (c.purpose IN ('APPOINTMENT_BOOKING','VISIT_FOLLOWUP',
                                     'SETTINGS_PRICING','SETTINGS_SCHEDULE','SLOTS',
                                     'QUEUE_CHECK_IN','QUEUE_READ'))
            ));

        DROP POLICY f01_api_read ON public.idempotency_receipts;
        DROP POLICY f01_api_insert ON public.idempotency_receipts;
        CREATE POLICY f01_api_read ON public.idempotency_receipts FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = idempotency_receipts.organization_id::text
                  AND idempotency_receipts.scope_kind = 'TENANT'
                  AND idempotency_receipts.actor_kind = 'STAFF'
                  AND idempotency_receipts.staff_id::text = c.staff_id
                  AND idempotency_receipts.actor_id::text = c.staff_id
                  AND idempotency_receipts.idempotency_key = c.idempotency_key
                  AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                  AND idempotency_receipts.operation = CASE
                    WHEN c.path = '/v1/patients' AND c.method = 'POST' THEN 'patients.create'
                    WHEN c.path = '/v1/appointments' AND c.method = 'POST' THEN 'appointments.book'
                    WHEN c.path = '/v1/staff/receptionists' AND c.method = 'POST' THEN 'staff.create'
                    WHEN c.path = '/v1/settings/pricing' AND c.method = 'PATCH' THEN 'settings.pricing.update'
                    WHEN c.path = '/v1/settings/schedule' AND c.method = 'PATCH' THEN 'settings.schedule.stage'
                    WHEN c.path = '/v1/settings/schedule/preview' AND c.method = 'POST' THEN 'settings.schedule.preview'
                    WHEN c.path = '/v1/settings/schedule/activate' AND c.method = 'POST' THEN 'settings.schedule.activate'
                    WHEN c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'PATCH' THEN 'visits.followup_decision'
                    WHEN c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'POST' THEN 'visits.extend_followup'
                    WHEN c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' THEN 'appointments.check_in'
                  END
            ));
        CREATE POLICY f01_api_insert ON public.idempotency_receipts FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = idempotency_receipts.organization_id::text
                  AND idempotency_receipts.scope_kind = 'TENANT'
                  AND idempotency_receipts.actor_kind = 'STAFF'
                  AND idempotency_receipts.staff_id::text = c.staff_id
                  AND idempotency_receipts.actor_id::text = c.staff_id
                  AND idempotency_receipts.idempotency_key = c.idempotency_key
                  AND c.stage = 'PRIMARY'
                  AND idempotency_receipts.operation = CASE
                    WHEN c.path = '/v1/patients' AND c.method = 'POST' THEN 'patients.create'
                    WHEN c.path = '/v1/appointments' AND c.method = 'POST' THEN 'appointments.book'
                    WHEN c.path = '/v1/staff/receptionists' AND c.method = 'POST' THEN 'staff.create'
                    WHEN c.path = '/v1/settings/pricing' AND c.method = 'PATCH' THEN 'settings.pricing.update'
                    WHEN c.path = '/v1/settings/schedule' AND c.method = 'PATCH' THEN 'settings.schedule.stage'
                    WHEN c.path = '/v1/settings/schedule/preview' AND c.method = 'POST' THEN 'settings.schedule.preview'
                    WHEN c.path = '/v1/settings/schedule/activate' AND c.method = 'POST' THEN 'settings.schedule.activate'
                    WHEN c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'PATCH' THEN 'visits.followup_decision'
                    WHEN c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'POST' THEN 'visits.extend_followup'
                    WHEN c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' THEN 'appointments.check_in'
                  END
            ));

        DROP POLICY f01_api_insert ON public.sensitive_audit;
        CREATE POLICY f01_api_insert ON public.sensitive_audit FOR INSERT TO carepulse_api
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM public.f01_context() c
                    WHERE c.organization_id = sensitive_audit.organization_id::text
                      AND c.stage IN ('PRIMARY', 'INVITATION_APPLY')
                      AND (
                          (actor_staff_id::text = c.staff_id AND actor_role = c.role
                              AND c.role IN ('DOCTOR_OWNER', 'RECEPTIONIST'))
                          OR (c.purpose = 'RECEPTIONIST_ACCEPT' AND c.stage = 'PRIMARY'
                              AND actor_staff_id = c.subject_staff_id AND actor_role = 'RECEPTIONIST')
                          OR (c.purpose = 'OWNER_RECOVERY' AND c.stage = 'PRIMARY'
                              AND actor_staff_id::text = c.staff_id AND actor_role = 'SYSTEM')
                      )
                )
                AND operator_id IS NULL
                AND EXISTS (
                    SELECT 1 FROM public.f01_context() c
                    WHERE (c.purpose = 'PATIENT_REGISTRY' AND (
                            (c.method = 'POST' AND c.path = '/v1/patients' AND sensitive_audit.action = 'patients.create')
                            OR (c.method = 'PATCH' AND sensitive_audit.action = 'patients.update')
                            OR (c.method = 'POST' AND c.path LIKE '%/archive' AND sensitive_audit.action = 'patients.archive')
                            OR (c.method = 'POST' AND c.path LIKE '%/unarchive' AND sensitive_audit.action = 'patients.unarchive')))
                        OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST' AND sensitive_audit.action = 'appointments.book')
                        OR (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND sensitive_audit.action = 'appointments.check_in')
                        OR (c.purpose = 'SETTINGS_PRICING' AND c.method = 'PATCH' AND sensitive_audit.action = 'settings.pricing.update')
                        OR (c.purpose = 'SETTINGS_SCHEDULE' AND c.path = '/v1/settings/schedule/activate' AND sensitive_audit.action = 'settings.schedule.activate')
                        OR (c.purpose = 'STAFF_MANAGEMENT' AND c.method <> 'GET' AND sensitive_audit.action IN ('RECEPTIONIST_CREATED','RECEPTIONIST_ENABLE','RECEPTIONIST_DISABLE','RECEPTIONIST_INVITE_ISSUED','RECEPTIONIST_INVITE_REVOKED'))
                        OR (c.purpose = 'RECEPTIONIST_ACCEPT' AND sensitive_audit.action = 'RECEPTIONIST_INVITE_ACCEPTED')
                        OR (c.purpose = 'OWNER_RECOVERY' AND c.path = '/v1/staff/recovery/reserve' AND sensitive_audit.action = 'PASSWORD_RESET_RESERVED')
                )
            );

        DROP POLICY f01_api_read ON public.outbox_events;
        DROP POLICY f01_api_insert ON public.outbox_events;
        CREATE POLICY f01_api_read ON public.outbox_events FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = outbox_events.organization_id::text
                  AND c.stage = 'PRIMARY' AND c.method = 'POST'
                  AND (
                      (c.purpose = 'APPOINTMENT_BOOKING'
                          AND outbox_events.event_type = 'APPOINTMENT_BOOKED'
                          AND outbox_events.aggregate_type = 'APPOINTMENT'
                          AND EXISTS (
                              SELECT 1 FROM public.appointments a
                              WHERE a.id::text = outbox_events.aggregate_id
                                AND a.organization_id = outbox_events.organization_id
                                AND a.patient_id = c.patient_id
                          ))
                      OR (c.purpose = 'QUEUE_CHECK_IN'
                          AND outbox_events.event_type = 'PATIENT_CHECKED_IN'
                          AND outbox_events.aggregate_type = 'APPOINTMENT'
                          AND outbox_events.aggregate_id = c.target_id::text)
                  )
            ));
        CREATE POLICY f01_api_insert ON public.outbox_events FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = outbox_events.organization_id::text
                  AND c.stage = 'PRIMARY' AND c.method = 'POST'
                  AND (
                      (c.purpose = 'APPOINTMENT_BOOKING'
                          AND outbox_events.event_type = 'APPOINTMENT_BOOKED'
                          AND outbox_events.aggregate_type = 'APPOINTMENT'
                          AND EXISTS (
                              SELECT 1 FROM public.appointments a
                              WHERE a.id::text = outbox_events.aggregate_id
                                AND a.organization_id = outbox_events.organization_id
                                AND a.patient_id = c.patient_id
                          ))
                      OR (c.purpose = 'QUEUE_CHECK_IN'
                          AND outbox_events.event_type = 'PATIENT_CHECKED_IN'
                          AND outbox_events.aggregate_type = 'APPOINTMENT'
                          AND outbox_events.aggregate_id = c.target_id::text)
                  )
            ));
    """)


def downgrade() -> None:
    raise RuntimeError("B-12 queue check-in grants migration is forward-only")
