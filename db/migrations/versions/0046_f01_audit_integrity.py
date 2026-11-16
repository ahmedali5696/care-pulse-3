"""Enforce F-01 audit integrity, operation/resource read isolation, and native identity isolation."""

from alembic import op

revision = "0046_f01_audit_integrity"
down_revision = "0045_f01_revocation_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        GRANT TRIGGER ON public.patients, public.appointments, public.appointment_history,
            public.staff_accounts, public.invitations, public.sensitive_audit
            TO carepulse_identity_owner;
        GRANT SELECT (issued_at) ON public.invitations TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
    """)
    op.execute(r"""
        ALTER TABLE f01_private.activations
            ADD COLUMN audit_patient_id uuid,
            ADD COLUMN audit_appointment_id uuid,
            ADD COLUMN staff_audit_action text,
            ADD COLUMN staff_audit_before jsonb,
            ADD COLUMN staff_audit_after jsonb,
            ADD COLUMN staff_audit_correlation uuid,
            ADD COLUMN staff_audit_consumed boolean NOT NULL DEFAULT false;

        ALTER FUNCTION public.f01_context() ROWS 1;

        CREATE FUNCTION public.f01_audit_targets()
        RETURNS TABLE (patient_id uuid, appointment_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path = pg_catalog, pg_temp AS $$
            SELECT a.audit_patient_id, a.audit_appointment_id
            FROM public.f01_context() c
            JOIN f01_private.activations a
                ON a.request_id = c.request_id AND a.stage = c.stage
            WHERE session_user = 'carepulse_api';
        $$;

        CREATE FUNCTION f01_private.capture_audit_patient() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR NOT COALESCE(ctx.purpose = 'PATIENT_REGISTRY'
                AND ctx.method = 'POST' AND ctx.path = '/v1/patients'
                AND ctx.stage = 'PRIMARY' AND ctx.organization_id = NEW.organization_id::text
                AND NOT NEW.archived AND NEW.lifecycle_revision = 0, false) THEN
                RAISE EXCEPTION 'Patient creation evidence denied' USING ERRCODE = '42501';
            END IF;
            UPDATE f01_private.activations SET audit_patient_id = NEW.id
                WHERE request_id = ctx.request_id AND stage = ctx.stage AND audit_patient_id IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Patient creation evidence already exists' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.capture_audit_appointment() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR NOT COALESCE(ctx.purpose = 'APPOINTMENT_BOOKING'
                AND ctx.method = 'POST' AND ctx.path = '/v1/appointments'
                AND ctx.stage = 'PRIMARY' AND ctx.organization_id = NEW.organization_id::text
                AND ctx.patient_id = NEW.patient_id AND NEW.revision = 0, false) THEN
                RAISE EXCEPTION 'Appointment creation evidence denied' USING ERRCODE = '42501';
            END IF;
            UPDATE f01_private.activations SET audit_appointment_id = NEW.id
                WHERE request_id = ctx.request_id AND stage = ctx.stage AND audit_appointment_id IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Appointment creation evidence already exists' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.guard_audit_appointment_history() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN f01_private.activations a ON a.request_id = c.request_id AND a.stage = c.stage
                WHERE c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                    AND c.path = '/v1/appointments' AND c.stage = 'PRIMARY'
                    AND c.organization_id = NEW.organization_id::text
                    AND c.staff_id = NEW.actor_staff_id::text AND c.role = NEW.actor_role
                    AND a.audit_appointment_id = NEW.appointment_id
                    AND NEW.action = 'CREATED' AND NEW.appointment_revision = 0
                    AND NEW.price_snapshot_appointment_id = NEW.appointment_id
                    AND NEW.replacement_appointment_id IS NULL AND NEW.reason IS NULL
            ) THEN
                RAISE EXCEPTION 'Appointment history scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.guard_staff_account_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF FOUND
               AND ctx.purpose = 'OWNER_ACTIVATION' AND ctx.role = 'BOOTSTRAP'
               AND ctx.stage = 'PRIMARY' AND ctx.method = 'POST'
               AND ctx.path = '/v1/provisioning/owner-activate'
               AND ctx.organization_id = NEW.organization_id::text
               AND NEW.role = 'DOCTOR_OWNER' AND NEW.enabled AND NEW.verified
               AND NEW.auth_user_id = ctx.native_user_id
               AND EXISTS (
                   SELECT 1 FROM public."user" u
                   WHERE u.id = NEW.auth_user_id AND u."emailVerified"
                     AND u.email = NEW.verified_email
               ) THEN
                RETURN NEW;
            END IF;
            IF NOT FOUND OR NOT COALESCE(
                ctx.purpose = 'STAFF_MANAGEMENT' AND ctx.role = 'DOCTOR_OWNER'
                AND ctx.stage = 'PRIMARY' AND ctx.method = 'POST'
                AND ctx.path = '/v1/staff/receptionists'
                AND ctx.organization_id = NEW.organization_id::text
                AND NEW.role = 'RECEPTIONIST' AND NEW.enabled AND NOT NEW.verified
                AND NEW.auth_user_id = ctx.native_user_id, false) THEN
                RAISE EXCEPTION 'Native identity not eligible for staff provisioning'
                    USING ERRCODE = '42501';
            END IF;
            IF NOT public.native_account_unbound_for_receptionist(NEW.organization_id, NEW.auth_user_id)
               OR NOT EXISTS (
                   SELECT 1 FROM public."user" u
                   WHERE u.id = NEW.auth_user_id AND NOT u."emailVerified"
                     AND u.email = NEW.verified_email
               ) THEN
                RAISE EXCEPTION 'Native identity not eligible for staff provisioning'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.capture_staff_audit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record; audit_action text; previous jsonb; latest jsonb; correlation uuid;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR NOT COALESCE(ctx.purpose = 'STAFF_MANAGEMENT'
                AND ctx.role = 'DOCTOR_OWNER' AND ctx.organization_id = NEW.organization_id::text
                AND NEW.role = 'RECEPTIONIST' AND ctx.stage = 'PRIMARY', false) THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF ctx.method = 'POST' AND ctx.path = '/v1/staff/receptionists'
                    AND NEW.auth_user_id = ctx.native_user_id AND NEW.enabled AND NOT NEW.verified THEN
                    audit_action := 'RECEPTIONIST_CREATED';
                    latest := jsonb_build_object('id', NEW.id, 'enabled', NEW.enabled, 'verified', NEW.verified);
                    correlation := NEW.id;
                END IF;
            ELSIF NEW.id = ctx.target_id AND NEW.revision::numeric = OLD.revision::numeric + 1
                  AND NEW.security_revision::numeric = OLD.security_revision::numeric + 1 THEN
                IF ctx.method = 'POST' AND OLD.enabled IS DISTINCT FROM NEW.enabled
                    AND ((ctx.path LIKE '%/enable' AND NOT OLD.enabled AND NEW.enabled)
                        OR (ctx.path LIKE '%/disable' AND NOT NEW.enabled
                            AND OLD.revision = ctx.expected_staff_revision
                            AND OLD.security_revision = ctx.expected_security_revision)) THEN
                    audit_action := CASE WHEN NEW.enabled THEN 'RECEPTIONIST_ENABLE' ELSE 'RECEPTIONIST_DISABLE' END;
                    previous := jsonb_build_object('id', OLD.id, 'enabled', OLD.enabled);
                    latest := jsonb_build_object('id', NEW.id, 'enabled', NEW.enabled);
                ELSIF ctx.method = 'PATCH' AND ctx.path LIKE '%/permissions'
                    AND OLD.operational_manager IS DISTINCT FROM NEW.operational_manager
                    AND NEW.operational_manager = ctx.manager_enabled
                    AND OLD.revision = ctx.expected_staff_revision
                    AND OLD.security_revision = ctx.expected_security_revision THEN
                    audit_action := 'RECEPTIONIST_PERMISSIONS';
                    previous := jsonb_build_object('id', OLD.id, 'operational_manager', OLD.operational_manager,
                        'revision', OLD.revision, 'security_revision', OLD.security_revision);
                    latest := jsonb_build_object('id', NEW.id, 'operational_manager', NEW.operational_manager,
                        'revision', NEW.revision, 'security_revision', NEW.security_revision);
                END IF;
            END IF;
            IF audit_action IS NOT NULL THEN
                UPDATE f01_private.activations SET staff_audit_action = audit_action,
                    staff_audit_before = previous, staff_audit_after = latest,
                    staff_audit_correlation = correlation
                    WHERE request_id = ctx.request_id AND stage = ctx.stage AND staff_audit_action IS NULL;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Staff mutation evidence already exists' USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.capture_invitation_audit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record; before_invitation text; after_invitation text; audit_action text;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR NOT COALESCE(ctx.purpose = 'STAFF_MANAGEMENT'
                AND ctx.role = 'DOCTOR_OWNER' AND ctx.method = 'POST'
                AND ctx.organization_id = NEW.organization_id::text AND NEW.staff_id = ctx.target_id
                AND NEW.purpose = 'RECEPTIONIST'
                AND ((ctx.stage = 'INVITATION_APPLY' AND ctx.path LIKE '%/invite')
                    OR (ctx.stage = 'PRIMARY' AND ctx.path LIKE '%/revoke-invite')), false) THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF ctx.stage <> 'INVITATION_APPLY' OR NEW.state <> 'ISSUED'
                    OR NEW.issued_by_staff_id::text IS DISTINCT FROM ctx.staff_id THEN RETURN NEW; END IF;
                SELECT i.state INTO before_invitation FROM public.invitations i
                    WHERE i.organization_id = NEW.organization_id AND i.staff_id = NEW.staff_id AND i.id <> NEW.id
                    ORDER BY i.issued_at DESC, i.id DESC LIMIT 1;
                audit_action := 'RECEPTIONIST_INVITE_ISSUED';
            ELSE
                IF OLD.state <> 'ISSUED' OR NEW.state <> 'REVOKED' THEN RETURN NEW; END IF;
                SELECT CASE WHEN i.id = NEW.id THEN OLD.state ELSE i.state END INTO before_invitation
                    FROM public.invitations i WHERE i.organization_id = NEW.organization_id AND i.staff_id = NEW.staff_id
                    ORDER BY i.issued_at DESC, i.id DESC LIMIT 1;
                IF ctx.stage = 'PRIMARY' THEN audit_action := 'RECEPTIONIST_INVITE_REVOKED'; END IF;
            END IF;
            SELECT i.state INTO after_invitation FROM public.invitations i
                WHERE i.organization_id = NEW.organization_id AND i.staff_id = NEW.staff_id
                ORDER BY i.issued_at DESC, i.id DESC LIMIT 1;
            UPDATE f01_private.activations SET
                staff_audit_before = COALESCE(staff_audit_before,
                    jsonb_build_object('id', NEW.staff_id, 'invitation_state', before_invitation)),
                staff_audit_action = audit_action,
                staff_audit_after = jsonb_build_object('id', NEW.staff_id, 'invitation_state', after_invitation),
                staff_audit_correlation = CASE WHEN TG_OP = 'INSERT' THEN NEW.id END
                WHERE request_id = ctx.request_id AND stage = ctx.stage AND NOT staff_audit_consumed
                    AND (staff_audit_action IS NULL OR staff_audit_action = 'RECEPTIONIST_INVITE_REVOKED');
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Invitation mutation evidence already exists' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION f01_private.guard_staff_audit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NEW.action NOT IN ('RECEPTIONIST_CREATED', 'RECEPTIONIST_ENABLE', 'RECEPTIONIST_DISABLE',
                'RECEPTIONIST_PERMISSIONS', 'RECEPTIONIST_INVITE_ISSUED', 'RECEPTIONIST_INVITE_REVOKED')
                AND ctx.purpose IS DISTINCT FROM 'STAFF_MANAGEMENT' THEN RETURN NEW; END IF;
            IF NOT COALESCE(ctx.purpose = 'STAFF_MANAGEMENT' AND ctx.role = 'DOCTOR_OWNER'
                    AND ctx.staff_id = NEW.actor_staff_id::text AND ctx.role = NEW.actor_role
                    AND ctx.organization_id = NEW.organization_id::text AND NEW.operator_id IS NULL
                    AND NEW.recovery_operation_id IS NULL AND NULLIF(btrim(NEW.reason), '') IS NOT NULL, false) THEN
                RAISE EXCEPTION 'Staff audit scope denied' USING ERRCODE = '42501';
            END IF;
            UPDATE f01_private.activations SET staff_audit_consumed = true
                WHERE request_id = ctx.request_id AND stage = ctx.stage AND NOT staff_audit_consumed
                    AND staff_audit_action = NEW.action
                    AND staff_audit_before IS NOT DISTINCT FROM NEW.before_state
                    AND staff_audit_after IS NOT DISTINCT FROM NEW.after_state
                    AND (staff_audit_correlation IS NULL OR staff_audit_correlation::text = NEW.correlation_id);
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Staff audit mutation evidence denied' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        REVOKE ALL ON FUNCTION public.f01_audit_targets(),
            f01_private.capture_audit_patient(), f01_private.capture_audit_appointment(),
            f01_private.guard_audit_appointment_history(), f01_private.guard_staff_account_insert(),
            f01_private.capture_staff_audit(), f01_private.capture_invitation_audit(),
            f01_private.guard_staff_audit() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_audit_targets()
            TO carepulse_api, carepulse_migration;

        CREATE TRIGGER z_f01_capture_audit_patient BEFORE INSERT ON public.patients
            FOR EACH ROW EXECUTE FUNCTION f01_private.capture_audit_patient();
        CREATE TRIGGER z_f01_capture_audit_appointment BEFORE INSERT ON public.appointments
            FOR EACH ROW EXECUTE FUNCTION f01_private.capture_audit_appointment();
        CREATE TRIGGER a_f01_guard_audit_appointment_history BEFORE INSERT ON public.appointment_history
            FOR EACH ROW EXECUTE FUNCTION f01_private.guard_audit_appointment_history();
        CREATE TRIGGER a_f01_guard_staff_account_insert BEFORE INSERT ON public.staff_accounts
            FOR EACH ROW EXECUTE FUNCTION f01_private.guard_staff_account_insert();
        CREATE TRIGGER f01_capture_staff_audit AFTER INSERT OR UPDATE ON public.staff_accounts
            FOR EACH ROW EXECUTE FUNCTION f01_private.capture_staff_audit();
        CREATE TRIGGER f01_capture_invitation_audit AFTER INSERT OR UPDATE ON public.invitations
            FOR EACH ROW EXECUTE FUNCTION f01_private.capture_invitation_audit();
        CREATE TRIGGER a_f01_guard_staff_audit BEFORE INSERT ON public.sensitive_audit
            FOR EACH ROW EXECUTE FUNCTION f01_private.guard_staff_audit();
    """)
    op.execute("""
        RESET ROLE;
        GRANT EXECUTE ON FUNCTION public.native_account_unbound_for_receptionist(uuid, text)
            TO carepulse_identity_owner;
        REVOKE TRIGGER ON public.patients, public.appointments, public.appointment_history,
            public.staff_accounts, public.invitations, public.sensitive_audit
            FROM carepulse_identity_owner;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)

    # R04: Tighten unbound native account verification and isolate public."user" reads.
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.native_account_unbound_for_receptionist(
            tenant_id uuid, native_user_id text)
        RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT session_user = 'carepulse_api'
               AND EXISTS (
                   SELECT 1 FROM public.f01_context() ctx
                   WHERE ctx.role = 'DOCTOR_OWNER'
                     AND ctx.purpose = 'STAFF_MANAGEMENT'
                     AND ctx.stage = 'PRIMARY'
                     AND ctx.method = 'POST'
                     AND ctx.path = '/v1/staff/receptionists'
                     AND ctx.native_user_id = $2
                     AND ctx.organization_id = $1::text
               )
               AND tenant_id = public.f01_org()::uuid
               AND EXISTS (
                   SELECT 1 FROM public."user" u
                   WHERE u.id = native_user_id
                     AND NOT u."emailVerified"
                     AND btrim(u.name) <> ''
                     AND NOT EXISTS (
                         SELECT 1 FROM public."account" a WHERE a."userId" = u.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM public."session" sess WHERE sess."userId" = u.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM public.staff_accounts s WHERE s.auth_user_id = u.id)
               )
        $$;

        DROP POLICY f01_api_read ON public."user";
        CREATE POLICY f01_api_read ON public."user" FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE EXISTS (
                    SELECT 1 FROM public.staff_accounts s
                    WHERE s.organization_id::text = c.organization_id
                      AND s.id::text = c.staff_id
                      AND s.auth_user_id = "user".id
                )
                OR (c.purpose = 'RECEPTIONIST_ACCEPT' AND c.stage = 'PRIMARY'
                    AND "user".id = c.native_user_id
                    AND EXISTS (
                        SELECT 1 FROM public.staff_accounts s
                        WHERE s.organization_id::text = c.organization_id
                          AND s.id = c.subject_staff_id
                          AND s.role = 'RECEPTIONIST'
                          AND s.auth_user_id = "user".id
                    ))
                OR (c.purpose = 'STAFF_MANAGEMENT' AND c.role = 'DOCTOR_OWNER'
                    AND c.stage = 'PRIMARY' AND c.method = 'POST'
                    AND c.path = '/v1/staff/receptionists'
                    AND "user".id = c.native_user_id
                    AND public.native_account_unbound_for_receptionist(
                        c.organization_id::uuid, "user".id))
            ));
    """)

    # R03: Narrow projections for slots and schedule review + tighten operational source proof.
    op.execute(r"""
        CREATE FUNCTION public.f01_counted_appointment_intervals(
            p_tenant uuid, p_service_date date)
        RETURNS TABLE (starts_at timestamptz, ends_at timestamptz)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT (
                (session_user = 'carepulse_migration'
                 AND NULLIF(current_setting('app.organization_id', true), '')::uuid = p_tenant)
                OR (session_user = 'carepulse_api' AND EXISTS (
                    SELECT 1 FROM public.f01_context() c
                    WHERE c.organization_id = p_tenant::text AND c.stage = 'PRIMARY'
                      AND (
                          (c.purpose = 'SLOTS' AND c.method = 'GET' AND c.path = '/v1/slots'
                           AND c.query = ('service_date=' || to_char(p_service_date, 'YYYY-MM-DD')))
                          OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                              AND c.path = '/v1/appointments')
                      )
                ))
            ) THEN
                RAISE EXCEPTION 'Appointment interval projection scope denied'
                    USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
                SELECT a.starts_at, a.ends_at
                FROM public.appointments a
                WHERE a.organization_id = p_tenant
                  AND a.service_date = p_service_date
                  AND a.booking_mode = 'EXACT_TIME'
                  AND a.status IN ('Booked', 'Waiting', 'In Examination', 'Completed');
        END $$;

        CREATE FUNCTION public.f01_schedule_review_appointments(
            p_tenant uuid, p_today date)
        RETURNS TABLE (
            id uuid, service_date date, status text, booking_mode text,
            starts_at timestamptz, ends_at timestamptz, revision bigint)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT (
                (session_user = 'carepulse_migration'
                 AND NULLIF(current_setting('app.organization_id', true), '')::uuid = p_tenant)
                OR (session_user = 'carepulse_api' AND EXISTS (
                    SELECT 1 FROM public.f01_context() c
                    WHERE c.organization_id = p_tenant::text
                      AND c.purpose = 'SETTINGS_SCHEDULE'
                      AND c.stage = 'PRIMARY'
                      AND (
                          (c.method = 'PATCH' AND c.path = '/v1/settings/schedule')
                          OR (c.method = 'POST'
                              AND c.path IN ('/v1/settings/schedule/preview',
                                             '/v1/settings/schedule/activate'))
                      )
                      AND (
                          c.role = 'DOCTOR_OWNER'
                          OR (c.role = 'RECEPTIONIST' AND EXISTS (
                              SELECT 1 FROM public.staff_accounts s
                              WHERE s.organization_id = p_tenant
                                AND s.id::text = c.staff_id
                                AND s.operational_manager
                          ))
                      )
                ))
            ) THEN
                RAISE EXCEPTION 'Schedule review appointment projection scope denied'
                    USING ERRCODE = '42501';
            END IF;
            RETURN QUERY
                SELECT a.id, a.service_date, a.status, a.booking_mode,
                       a.starts_at, a.ends_at, a.revision
                FROM public.appointments a
                WHERE a.organization_id = p_tenant
                  AND a.service_date >= p_today
                  AND a.status IN ('Booked', 'Waiting', 'In Examination', 'Completed')
                ORDER BY a.service_date, a.id;
        END $$;

        REVOKE ALL ON FUNCTION public.f01_counted_appointment_intervals(uuid, date),
            public.f01_schedule_review_appointments(uuid, date) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_counted_appointment_intervals(uuid, date),
            public.f01_schedule_review_appointments(uuid, date)
            TO carepulse_api, carepulse_migration;

        CREATE OR REPLACE FUNCTION public.b10_operational_source_proof(
            org uuid, source_visit uuid)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actor uuid;
            proof jsonb;
            decision jsonb;
            visible_decision jsonb;
            completion date;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.f01_context() ctx
                WHERE ctx.organization_id = org::text
                  AND ctx.purpose = 'APPOINTMENT_BOOKING'
                  AND ((ctx.method = 'POST' AND ctx.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                          AND ctx.source_visit_id = source_visit
                          AND EXISTS (
                              SELECT 1 FROM public.visits v
                              WHERE v.organization_id = org AND v.id = source_visit
                                AND v.patient_id = ctx.patient_id))
                    OR (ctx.method = 'GET' AND ctx.stage = 'PRIMARY' AND EXISTS (
                        SELECT 1 FROM public.visits v
                        WHERE v.organization_id = org AND v.id = source_visit
                          AND (v.appointment_id = ctx.target_id OR EXISTS (
                              SELECT 1 FROM public.followup_claims cl
                              WHERE cl.organization_id = org AND cl.source_visit_id = v.id
                                AND cl.appointment_id = ctx.target_id)))))) THEN
                RAISE EXCEPTION 'Request writer scope denied' USING ERRCODE = '42501';
            END IF;
            IF session_user <> 'carepulse_api'
               OR public.f01_org() IS DISTINCT FROM org::text
               OR NULLIF(public.f01_staff(), '') IS NULL THEN
                RAISE EXCEPTION 'Source proof context denied' USING ERRCODE = '42501';
            END IF;
            actor := public.f01_staff()::uuid;
            PERFORM 1 FROM public.organizations
            WHERE id = org AND state = 'ACTIVE';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source proof clinic denied' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
            WHERE s.organization_id = org AND s.id = actor AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL AND u."emailVerified"
              AND u.email = s.verified_email
              AND s.role IN ('DOCTOR_OWNER', 'RECEPTIONIST');
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source proof actor denied' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.visits v
                JOIN public.appointments a
                  ON (a.organization_id, a.id) = (v.organization_id, v.appointment_id)
            WHERE v.organization_id = org AND v.id = source_visit
              AND (v.patient_id, v.doctor_staff_id) =
                  (a.patient_id, a.doctor_staff_id);
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            proof := public.d08_legacy_source(org, source_visit);
            IF proof->>'state' IS DISTINCT FROM 'VERIFIED' THEN
                RETURN jsonb_build_object('state', 'ANCESTRY_UNKNOWN');
            END IF;
            decision := NULLIF(proof->'decision', 'null'::jsonb);
            IF decision IS NOT NULL THEN
                SELECT (completed_at AT TIME ZONE 'Africa/Cairo')::date
                    INTO completion FROM public.completion_attestations
                WHERE organization_id = org AND visit_id = source_visit;
                IF completion IS NULL THEN
                    RETURN jsonb_build_object('state', 'ANCESTRY_UNKNOWN');
                END IF;
                PERFORM public.d08_validate_decision(decision, completion);
                visible_decision := jsonb_build_object('kind', decision->>'kind');
                IF decision->>'kind' = 'DEADLINE' THEN
                    visible_decision := visible_decision || jsonb_build_object(
                        'deadline_date', decision->>'deadline_date');
                END IF;
            END IF;
            RETURN jsonb_build_object(
                'state', 'VERIFIED', 'decision', visible_decision,
                'actor_staff_id', proof->>'actor_staff_id',
                'actor_role', proof->>'actor_role');
        END $$;
    """)

    # R03: Restrict Patient, Appointment, Visit, and Follow-up RLS policies to operation/resource boundaries.
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
                  )
            ))
            WITH CHECK (false);

        DROP POLICY f01_api_read ON public.appointment_price_snapshots;
        CREATE POLICY f01_api_read ON public.appointment_price_snapshots FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                LEFT JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_price_snapshots.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING'
                  AND (
                      (c.method = 'GET' AND c.path = '/v1/appointments' AND c.stage = 'PRIMARY')
                      OR (c.method = 'GET' AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND appointment_price_snapshots.appointment_id = c.target_id)
                      OR (c.method = 'POST' AND c.path = '/v1/appointments'
                          AND (
                              a.appointment_id = appointment_price_snapshots.appointment_id
                              OR (c.stage = 'PRIMARY' AND EXISTS (
                                  SELECT 1 FROM public.visits v
                                  WHERE v.organization_id = appointment_price_snapshots.organization_id
                                    AND v.id = c.source_visit_id
                                    AND v.appointment_id = appointment_price_snapshots.appointment_id
                                    AND v.patient_id = c.patient_id
                              ))
                              OR EXISTS (
                                  SELECT 1 FROM public.idempotency_receipts r
                                  WHERE r.organization_id = appointment_price_snapshots.organization_id
                                    AND r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
                                    AND r.staff_id::text = c.staff_id AND r.actor_id::text = c.staff_id
                                    AND r.operation = 'appointments.book'
                                    AND r.idempotency_key = c.idempotency_key
                                    AND r.result_projection->>'id' = appointment_price_snapshots.appointment_id::text
                              )
                          ))
                  )
            ));

        DROP POLICY f01_api_insert ON public.appointment_price_snapshots;
        CREATE POLICY f01_api_insert ON public.appointment_price_snapshots FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_price_snapshots.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING' AND c.stage = 'PRIMARY'
                  AND c.method = 'POST' AND c.path = '/v1/appointments'
                  AND a.appointment_id = appointment_price_snapshots.appointment_id
            ));

        DROP POLICY f01_api_read ON public.appointment_history;
        CREATE POLICY f01_api_read ON public.appointment_history FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                LEFT JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_history.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING'
                  AND (
                      (c.method = 'GET' AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND appointment_history.appointment_id = c.target_id)
                      OR (c.method = 'POST' AND c.path = '/v1/appointments'
                          AND (
                              a.appointment_id = appointment_history.appointment_id
                              OR (c.stage = 'PRIMARY' AND EXISTS (
                                  SELECT 1 FROM public.visits v
                                  WHERE v.organization_id = appointment_history.organization_id
                                    AND v.id = c.source_visit_id
                                    AND v.appointment_id = appointment_history.appointment_id
                                    AND v.patient_id = c.patient_id
                              ))
                              OR EXISTS (
                                  SELECT 1 FROM public.idempotency_receipts r
                                  WHERE r.organization_id = appointment_history.organization_id
                                    AND r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
                                    AND r.staff_id::text = c.staff_id AND r.actor_id::text = c.staff_id
                                    AND r.operation = 'appointments.book'
                                    AND r.idempotency_key = c.idempotency_key
                                    AND r.result_projection->>'id' = appointment_history.appointment_id::text
                              )
                          ))
                  )
            ));

        DROP POLICY f01_api_insert ON public.appointment_history;
        CREATE POLICY f01_api_insert ON public.appointment_history FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_history.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING' AND c.stage = 'PRIMARY'
                  AND c.method = 'POST' AND c.path = '/v1/appointments'
                  AND a.appointment_id = appointment_history.appointment_id
            ));

        DROP POLICY f01_api_read ON public.appointment_number_assignments;
        CREATE POLICY f01_api_read ON public.appointment_number_assignments FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_number_assignments.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING' AND c.stage = 'PRIMARY'
                  AND c.method = 'POST' AND c.path = '/v1/appointments'
                  AND a.appointment_id = appointment_number_assignments.appointment_id
            ));

        DROP POLICY f01_api_insert ON public.appointment_number_assignments;
        CREATE POLICY f01_api_insert ON public.appointment_number_assignments FOR INSERT TO carepulse_api
            WITH CHECK (EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN public.f01_audit_targets() a ON true
                WHERE c.organization_id = appointment_number_assignments.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING' AND c.stage = 'PRIMARY'
                  AND c.method = 'POST' AND c.path = '/v1/appointments'
                  AND a.appointment_id = appointment_number_assignments.appointment_id
            ));

        DROP POLICY f01_api_read ON public.appointment_day_counters;
        CREATE POLICY f01_api_read ON public.appointment_day_counters FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = appointment_day_counters.organization_id::text
                  AND c.purpose = 'APPOINTMENT_BOOKING' AND c.stage = 'PRIMARY'
                  AND c.method = 'POST' AND c.path = '/v1/appointments'
            ));

        DROP POLICY f01_api_read ON public.visits;
        CREATE POLICY f01_api_read ON public.visits FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = visits.organization_id::text
                  AND (
                      (c.purpose = 'VISIT_FOLLOWUP'
                          AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                          AND visits.id = c.target_id)
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'GET'
                          AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND (
                              visits.appointment_id = c.target_id
                              OR EXISTS (
                                  SELECT 1 FROM public.followup_claims cl
                                  WHERE cl.organization_id = visits.organization_id
                                    AND cl.appointment_id = c.target_id
                                    AND cl.source_visit_id = visits.id
                              )
                          ))
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments'
                          AND (
                              (c.stage = 'PRIMARY' AND visits.id = c.source_visit_id
                                  AND visits.patient_id = c.patient_id)
                              OR EXISTS (
                                  SELECT 1 FROM public.idempotency_receipts r
                                  JOIN public.followup_claims cl
                                    ON cl.organization_id = r.organization_id
                                   AND cl.appointment_id::text = r.result_projection->>'id'
                                  WHERE r.organization_id = visits.organization_id
                                    AND r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
                                    AND r.staff_id::text = c.staff_id AND r.actor_id::text = c.staff_id
                                    AND r.operation = 'appointments.book'
                                    AND r.idempotency_key = c.idempotency_key
                                    AND cl.source_visit_id = visits.id
                              )
                          ))
                  )
            ));

        DROP POLICY f01_api_update ON public.visits;
        CREATE POLICY f01_api_update ON public.visits FOR UPDATE TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = visits.organization_id::text AND c.stage = 'PRIMARY'
                  AND (
                      (c.purpose = 'VISIT_FOLLOWUP' AND c.method IN ('PATCH', 'POST')
                          AND visits.id = c.target_id)
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments'
                          AND visits.id = c.source_visit_id
                          AND visits.patient_id = c.patient_id)
                  )
            ))
            WITH CHECK (false);

        DROP POLICY f01_api_read ON public.completion_attestations;
        CREATE POLICY f01_api_read ON public.completion_attestations FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.visits v
                WHERE v.organization_id = completion_attestations.organization_id
                  AND v.id = completion_attestations.visit_id
            ));

        DROP POLICY f01_api_read ON public.followup_source_state;
        CREATE POLICY f01_api_read ON public.followup_source_state FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.visits v
                WHERE v.organization_id = followup_source_state.organization_id
                  AND v.id = followup_source_state.visit_id
            ));

        DROP POLICY f01_api_read ON public.followup_claims;
        CREATE POLICY f01_api_read ON public.followup_claims FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = followup_claims.organization_id::text
                  AND (
                      (c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'POST'
                          AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                          AND followup_claims.source_visit_id = c.target_id
                          AND followup_claims.appointment_id = c.target_appointment_id)
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'GET'
                          AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND (
                              followup_claims.appointment_id = c.target_id
                              OR followup_claims.source_appointment_id = c.target_id
                          ))
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'
                          AND c.path = '/v1/appointments'
                          AND (
                              (c.stage = 'PRIMARY' AND followup_claims.source_visit_id = c.source_visit_id
                                  AND followup_claims.patient_id = c.patient_id)
                              OR EXISTS (
                                  SELECT 1 FROM public.idempotency_receipts r
                                  WHERE r.organization_id = followup_claims.organization_id
                                    AND r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
                                    AND r.staff_id::text = c.staff_id AND r.actor_id::text = c.staff_id
                                    AND r.operation = 'appointments.book'
                                    AND r.idempotency_key = c.idempotency_key
                                    AND r.result_projection->>'id' = followup_claims.appointment_id::text
                              )
                          ))
                  )
            ));

        DROP POLICY f01_api_read ON public.followup_source_decision_events;
        CREATE POLICY f01_api_read ON public.followup_source_decision_events FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = followup_source_decision_events.organization_id::text
                  AND c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'PATCH'
                  AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                  AND followup_source_decision_events.source_visit_id = c.target_id
            ));

        DROP POLICY f01_api_read ON public.followup_authorization_events;
        CREATE POLICY f01_api_read ON public.followup_authorization_events FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN public.followup_claims cl
                  ON cl.organization_id = followup_authorization_events.organization_id
                 AND cl.id = followup_authorization_events.claim_id
                WHERE c.organization_id = followup_authorization_events.organization_id::text
                  AND (
                      (c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'POST'
                          AND c.stage IN ('PRIMARY', 'RECEIPT_RECOVERY')
                          AND cl.source_visit_id = c.target_id
                          AND cl.appointment_id = c.target_appointment_id)
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'GET'
                          AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND cl.appointment_id = c.target_id)
                  )
            ));

        DROP POLICY f01_api_read ON public.followup_extensions;
        CREATE POLICY f01_api_read ON public.followup_extensions FOR SELECT TO carepulse_api
            USING (EXISTS (
                SELECT 1 FROM public.f01_context() c
                JOIN public.followup_claims cl
                  ON cl.organization_id = followup_extensions.organization_id
                 AND cl.id = followup_extensions.claim_id
                WHERE c.organization_id = followup_extensions.organization_id::text
                  AND (
                      (c.purpose = 'VISIT_FOLLOWUP' AND c.method = 'POST'
                          AND c.stage = 'PRIMARY'
                          AND cl.source_visit_id = c.target_id
                          AND cl.appointment_id = c.target_appointment_id)
                      OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'GET'
                          AND c.target_id IS NOT NULL AND c.stage = 'PRIMARY'
                          AND cl.appointment_id = c.target_id)
                  )
            ));
    """)


def downgrade() -> None:
    raise RuntimeError("F-01 audit integrity migration is forward-only")
