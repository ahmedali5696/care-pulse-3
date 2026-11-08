"""B-10: ordered follow-up writers consume an existing booking allocation.

The older 0028 entrypoints are fenced; historical claims and receipts are untouched.
Caller-set runtime GUCs remain subject to the separate G3 / Issue #26 review.
"""

from alembic import op

revision = "0029_b10_ordered_followup_writers"
down_revision = "0028_d08_followup_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        LOCK TABLE public.appointments, public.followup_claims,
            public.followup_authorization_events IN SHARE ROW EXCLUSIVE MODE
    """)
    _fence_legacy_writers()
    _ordered_prefix()
    _ordered_creation()
    _ordered_extension()
    _fence_direct_followup_moves()
    _fence_direct_terminal_writes()
    _privileges()


def _fence_legacy_writers() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.create_followup_claim(
            org uuid, visit uuid, expected jsonb, booking jsonb)
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Obsolete follow-up creation writer' USING ERRCODE = '42501';
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.extend_followup_claim(
            org uuid, claim uuid, expected jsonb, extension jsonb)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Obsolete follow-up extension writer' USING ERRCODE = '42501';
        END $$
    """)
    for name in ("create_followup_claim", "extend_followup_claim"):
        op.execute(f"""
            REVOKE ALL ON FUNCTION public.{name}(uuid,uuid,jsonb,jsonb)
            FROM PUBLIC, carepulse_runtime
        """)


def _ordered_prefix() -> None:
    # This is repeated by d08_context after the allocator/ordered Appointment locks.
    # Prelocking here prevents a direct writer call from reversing that lock prefix.
    op.execute("""
        CREATE FUNCTION public.b10_lock_followup_prefix(org uuid, visit uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actor uuid;
            patient uuid;
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR current_setting('transaction_isolation') <> 'read committed'
               OR current_setting('app.organization_id', true) IS DISTINCT FROM org::text
               OR COALESCE(current_setting('app.staff_id', true), '') = '' THEN
                RAISE EXCEPTION 'Follow-up context denied' USING ERRCODE = '42501';
            END IF;
            actor := current_setting('app.staff_id', true)::uuid;
            PERFORM 1 FROM public.organizations WHERE id = org AND state = 'ACTIVE'
                FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Inactive clinic' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.staff_accounts s JOIN public."user" u
                ON u.id = s.auth_user_id
            WHERE s.organization_id = org AND s.id = actor AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL AND u."emailVerified"
              AND u.email = s.verified_email
              AND s.role IN ('DOCTOR_OWNER','RECEPTIONIST')
            FOR SHARE OF s, u;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Inactive actor' USING ERRCODE = '42501';
            END IF;
            SELECT patient_id INTO patient FROM public.visits
            WHERE organization_id = org AND id = visit;
            PERFORM 1 FROM public.patients WHERE organization_id = org AND id = patient
                FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source patient unavailable' USING ERRCODE = '23514';
            END IF;
            PERFORM 1 FROM public.clinic_policy_heads
            WHERE organization_id = org AND kind = 'FOLLOWUP' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'FOLLOWUP policy missing' USING ERRCODE = '23514';
            END IF;
        END $$
    """)


def _ordered_creation() -> None:
    op.execute("""
        CREATE FUNCTION public.create_followup_claim_ordered(
            org uuid, visit uuid, expected jsonb, booking jsonb)
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            context jsonb;
            source public.followup_source_state%ROWTYPE;
            target uuid := gen_random_uuid(); claim uuid := gen_random_uuid();
            assignment uuid := gen_random_uuid(); number bigint; ceiling bigint;
            day date;
            mode text := booking->>'booking_mode';
            starts timestamptz := (booking->>'starts_at')::timestamptz;
            ends timestamptz := (booking->>'ends_at')::timestamptz;
            declaration jsonb := booking->'new_booking_date_exception';
            snapshot jsonb;
            price jsonb := booking->'price_snapshot';
            e public.followup_authorization_events%ROWTYPE;
        BEGIN
            IF jsonb_typeof(expected) IS DISTINCT FROM 'object'
               OR NOT (expected ?& ARRAY['source_revision','clinical_revision','policy_version'])
               OR (expected - 'source_revision' - 'clinical_revision' - 'policy_version') <> '{}'::jsonb
               OR jsonb_typeof(booking) IS DISTINCT FROM 'object'
               OR (booking - 'service_date' - 'booking_mode' - 'starts_at' - 'ends_at'
                    - 'price_snapshot' - 'allocated_number' - 'new_booking_date_exception') <> '{}'::jsonb
               OR COALESCE(booking->>'service_date', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
               OR jsonb_typeof(booking->'allocated_number') IS DISTINCT FROM 'number'
               OR COALESCE(booking->>'allocated_number', '') !~ '^[1-9][0-9]*$'
               OR jsonb_typeof(price) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid ordered claim input' USING ERRCODE = '23514';
            END IF;
            day := (booking->>'service_date')::date;
            number := (booking->>'allocated_number')::bigint;
            IF NOT isfinite(day) OR day::text <> booking->>'service_date' THEN
                RAISE EXCEPTION 'Finite Cairo service date required' USING ERRCODE = '23514';
            END IF;
            PERFORM public.b10_lock_followup_prefix(org, visit);
            -- B-09 allocates exactly once, before the post-allocation timed scan.
            SELECT last_number INTO ceiling FROM public.appointment_day_counters
            WHERE organization_id = org AND service_date = day FOR UPDATE;
            IF ceiling IS DISTINCT FROM number THEN
                RAISE EXCEPTION 'Caller must supply reserved day number' USING ERRCODE = '23514';
            END IF;
            context := public.d08_context(org, visit, expected);
            SELECT * INTO STRICT source FROM public.followup_source_state
            WHERE organization_id = org AND visit_id = visit;
            PERFORM public.d08_check_authorized_date(context, day);
            PERFORM 1 FROM public.patients WHERE organization_id = org
                AND id = source.patient_id AND NOT archived FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Patient unavailable for new booking' USING ERRCODE = '23514';
            END IF;
            snapshot := jsonb_build_object(
                'schema_version', 2, 'source_visit_id', visit,
                'source_decision_revision', source.source_revision,
                'source_clinical_revision', (context->>'clinical_revision')::bigint,
                'doctor_staff_id', source.doctor_staff_id,
                'actor_staff_id', context->>'actor_staff_id', 'actor_role', context->>'actor_role',
                'authorized', true, 'policy_revision', (context->>'policy_version')::bigint,
                'selected_date', day, 'booking_mode', mode,
                'starts_at', starts, 'ends_at', ends);
            IF day > (context->>'deadline')::date THEN
                IF jsonb_typeof(declaration) IS DISTINCT FROM 'object'
                   OR (declaration - 'selected_service_date' - 'reason') <> '{}'::jsonb
                   OR jsonb_typeof(declaration->'selected_service_date') IS DISTINCT FROM 'string'
                   OR declaration->>'selected_service_date' IS DISTINCT FROM day::text
                   OR (declaration ? 'reason' AND
                       jsonb_typeof(declaration->'reason') NOT IN ('string','null'))
                   OR (declaration ? 'reason' AND jsonb_typeof(declaration->'reason') = 'string'
                       AND (btrim(declaration->>'reason') = '' OR
                            declaration->>'reason' <> btrim(declaration->>'reason') OR
                            declaration->>'reason' ~ '[[:cntrl:]]')) THEN
                    RAISE EXCEPTION 'Explicit matching NEW_DATE declaration required'
                        USING ERRCODE = '23514';
                END IF;
                e.id := gen_random_uuid(); e.kind := 'NEW_DATE';
                e.reason := declaration->>'reason';
                IF context->>'actor_role' = 'DOCTOR_OWNER' AND e.reason IS NULL THEN
                    RAISE EXCEPTION 'Doctor NEW_DATE requires reason' USING ERRCODE = '23514';
                END IF;
                snapshot := snapshot || jsonb_build_object(
                    'new_date_event_id', e.id, 'new_booking_date_exception', declaration);
            ELSIF booking ? 'new_booking_date_exception' THEN
                RAISE EXCEPTION 'Unnecessary NEW_DATE declaration' USING ERRCODE = '23514';
            END IF;
            INSERT INTO public.appointments (
                id, organization_id, patient_id, doctor_staff_id, service_date, status,
                service_type, classification, lane, booking_mode, starts_at, ends_at,
                current_assignment_id, current_number)
            VALUES (target, org, source.patient_id, source.doctor_staff_id, day, 'Booked',
                'FOLLOW_UP', 'ROUTINE', 'MAIN', mode, starts, ends, assignment, number);
            INSERT INTO public.appointment_number_assignments (
                id, organization_id, appointment_id, service_date, number)
            VALUES (assignment, org, target, day, number);
            INSERT INTO public.appointment_price_snapshots (
                organization_id, appointment_id, service_type, classification,
                base_egp, discount_type, discount_value, discount_reason, is_free, net_egp,
                pricing_version, creation_standard_base_egp, entitlement_provenance)
            VALUES (org, target, 'FOLLOW_UP', 'ROUTINE', (price->>'base_egp')::numeric,
                price->>'discount_type', (price->>'discount_value')::numeric,
                price->>'discount_reason', (price->>'is_free')::boolean,
                (price->>'net_egp')::numeric, (price->>'pricing_version')::bigint,
                (price->>'creation_standard_base_egp')::numeric, snapshot);
            INSERT INTO public.appointment_history (
                id, organization_id, appointment_id, appointment_revision, action, new_status,
                new_service_date, new_number, actor_staff_id, actor_role, price_snapshot_appointment_id)
            VALUES (gen_random_uuid(), org, target, 0, 'CREATED', 'Booked', day, number,
                (context->>'actor_staff_id')::uuid, context->>'actor_role', target);
            INSERT INTO public.followup_claims (
                id, organization_id, appointment_id, patient_id, doctor_staff_id,
                source_visit_id, source_appointment_id, policy_snapshot, completion_local_date,
                approved_deadline, state)
            VALUES (claim, org, target, source.patient_id, source.doctor_staff_id,
                visit, source.appointment_id, snapshot, (context->>'completion_date')::date,
                (context->>'deadline')::date, 'ACTIVE');
            IF e.id IS NOT NULL THEN
                e.organization_id := org; e.claim_id := claim; e.source_visit_id := visit;
                e.appointment_id := target; e.authorization_revision := 1;
                e.prior_deadline := (context->>'deadline')::date; e.new_deadline := day;
                e.selected_date := day; e.booking_mode := mode; e.starts_at := starts; e.ends_at := ends;
                e.source_revision := source.source_revision;
                e.clinical_revision := (context->>'clinical_revision')::bigint;
                e.policy_version := (context->>'policy_version')::bigint;
                e.policy_kind := 'FOLLOWUP'; e.claim_revision := 1; e.appointment_revision := 0;
                e.actor_staff_id := (context->>'actor_staff_id')::uuid;
                e.actor_role := context->>'actor_role';
                e.occurred_at := date_trunc('second', clock_timestamp());
                e.sensitive_audit_id := gen_random_uuid();
                PERFORM public.d08_append_authorization(e);
            END IF;
            RETURN claim;
        END $$
    """)


def _ordered_extension() -> None:
    op.execute("""
        CREATE FUNCTION public.extend_followup_claim_ordered(
            org uuid, claim uuid, expected jsonb, extension jsonb)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            c public.followup_claims%ROWTYPE;
            a public.appointments%ROWTYPE;
            context jsonb;
            e public.followup_authorization_events%ROWTYPE;
            prior public.followup_authorization_events%ROWTYPE;
            locked_id uuid;
        BEGIN
            IF jsonb_typeof(expected) IS DISTINCT FROM 'object'
               OR NOT (expected ?& ARRAY['source_revision','clinical_revision','policy_version',
                                           'claim_revision','authorization_revision','appointment_revision'])
               OR (expected - 'source_revision' - 'clinical_revision' - 'policy_version'
                    - 'claim_revision' - 'authorization_revision' - 'appointment_revision') <> '{}'::jsonb
               OR jsonb_typeof(extension) IS DISTINCT FROM 'object'
               OR (extension - 'deadline_date' - 'reason') <> '{}'::jsonb
               OR jsonb_typeof(extension->'reason') IS DISTINCT FROM 'string'
               OR COALESCE(extension->>'deadline_date', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
               OR btrim(extension->>'reason') = ''
               OR extension->>'reason' <> btrim(extension->>'reason')
               OR extension->>'reason' ~ '[[:cntrl:]]' THEN
                RAISE EXCEPTION 'Invalid ordered extension input' USING ERRCODE = '23514';
            END IF;
            SELECT * INTO c FROM public.followup_claims
            WHERE organization_id = org AND id = claim;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Claim unavailable' USING ERRCODE = '23514';
            END IF;
            PERFORM public.b10_lock_followup_prefix(org, c.source_visit_id);
            -- Existing source and target must both precede the Visit/source/claim locks.
            FOR locked_id IN SELECT DISTINCT locked.appointment_id FROM unnest(
                    ARRAY[c.source_appointment_id, c.appointment_id]) AS locked(appointment_id)
                ORDER BY locked.appointment_id LOOP
                PERFORM 1 FROM public.appointments WHERE organization_id = org
                    AND id = locked_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Source or target unavailable' USING ERRCODE = '23514';
                END IF;
            END LOOP;
            context := public.d08_context(org, c.source_visit_id, expected);
            PERFORM 1 FROM public.followup_claims WHERE organization_id = org
                AND id = claim FOR UPDATE;
            SELECT * INTO STRICT c FROM public.followup_claims
            WHERE organization_id = org AND id = claim;
            SELECT * INTO STRICT a FROM public.appointments
            WHERE organization_id = org AND id = c.appointment_id;
            IF c.state <> 'ACTIVE' OR a.status NOT IN ('Booked','Waiting','In Examination')
               OR expected->>'claim_revision' IS DISTINCT FROM c.revision::text
               OR expected->>'appointment_revision' IS DISTINCT FROM a.revision::text THEN
                RAISE EXCEPTION 'Claim no longer at expected active revision'
                    USING ERRCODE = '40001';
            END IF;
            -- Read the tail in a fresh statement after any claim-lock wait.
            SELECT * INTO prior FROM public.followup_authorization_events
            WHERE organization_id = org AND claim_id = claim
            ORDER BY authorization_revision DESC LIMIT 1;
            e.authorization_revision := COALESCE(prior.authorization_revision, 0) + 1;
            IF expected->>'authorization_revision' IS DISTINCT FROM
                    (e.authorization_revision - 1)::text THEN
                RAISE EXCEPTION 'Stale authorization revision' USING ERRCODE = '40001';
            END IF;
            e.prior_deadline := COALESCE(prior.new_deadline, public.d08_legacy_deadline(org, claim));
            e.new_deadline := (extension->>'deadline_date')::date;
            e.reason := extension->>'reason';
            IF e.new_deadline IS NULL OR NOT isfinite(e.new_deadline)
               OR e.new_deadline::text <> extension->>'deadline_date'
               OR e.new_deadline < e.prior_deadline THEN
                RAISE EXCEPTION 'Extension requires nonshortening finite date'
                    USING ERRCODE = '23514';
            END IF;
            PERFORM public.d08_check_authorized_date(context, e.new_deadline);
            e.id := gen_random_uuid(); e.organization_id := org; e.claim_id := claim;
            e.source_visit_id := c.source_visit_id; e.appointment_id := c.appointment_id;
            e.kind := 'EXTENSION'; e.source_revision := (context->'source'->>'source_revision')::bigint;
            e.clinical_revision := (context->>'clinical_revision')::bigint;
            e.policy_version := (context->>'policy_version')::bigint;
            e.policy_kind := 'FOLLOWUP'; e.claim_revision := c.revision;
            e.appointment_revision := a.revision;
            e.actor_staff_id := (context->>'actor_staff_id')::uuid;
            e.actor_role := context->>'actor_role';
            e.occurred_at := date_trunc('second', clock_timestamp());
            e.sensitive_audit_id := gen_random_uuid();
            PERFORM public.d08_append_authorization(e);
            RETURN e.authorization_revision;
        END $$
    """)


def _fence_direct_followup_moves() -> None:
    # B-18 owns any future extension-backed move and its required sorted locks.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_followup_move() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF OLD.service_type = 'FOLLOW_UP' AND
               (NEW.service_date, NEW.booking_mode, NEW.starts_at, NEW.ends_at)
               IS DISTINCT FROM
               (OLD.service_date, OLD.booking_mode, OLD.starts_at, OLD.ends_at) THEN
                RAISE EXCEPTION 'Follow-up movement requires an ordered writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)


def _fence_direct_terminal_writes() -> None:
    # A direct claim UPDATE can otherwise reverse extension's target-before-claim order.
    op.execute("""
        CREATE FUNCTION public.b10_guard_direct_claim_update() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF current_user <> 'carepulse_migration' THEN
                RAISE EXCEPTION 'Claim terminal write requires protected writer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER b10_claim_terminal_fence BEFORE UPDATE ON public.followup_claims
        FOR EACH ROW EXECUTE FUNCTION public.b10_guard_direct_claim_update()
    """)


def _privileges() -> None:
    op.execute("""
        REVOKE ALL ON FUNCTION public.b10_lock_followup_prefix(uuid,uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.b10_guard_direct_claim_update() FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)
            FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)
            TO carepulse_runtime;
        GRANT EXECUTE ON FUNCTION public.extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)
            TO carepulse_runtime;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.followup_source_decision_events,
            public.followup_authorization_events FROM carepulse_runtime, PUBLIC;
        REVOKE INSERT ON public.followup_claims, public.followup_extensions
            FROM carepulse_runtime, PUBLIC;
        REVOKE UPDATE ON public.followup_claims FROM carepulse_runtime, PUBLIC;
        REVOKE UPDATE (state, terminal_history_revision, terminal_at, revision)
            ON public.followup_claims FROM carepulse_runtime, PUBLIC
    """)


def downgrade() -> None:
    raise RuntimeError("B-10 ordered Follow-up writers are forward-only")
