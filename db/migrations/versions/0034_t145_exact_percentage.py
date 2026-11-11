"""T145: exact forward-only percentage snapshot storage."""

from alembic import op

revision = "0034_t145_exact_percentage"
down_revision = "0033_d08_terminal_writer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        LOCK TABLE public.appointment_price_snapshots IN SHARE ROW EXCLUSIVE MODE;
        ALTER TABLE public.appointment_price_snapshots
            ADD COLUMN discount_percent_text text,
            ALTER COLUMN discount_value DROP NOT NULL;
    """)
    _exact_percentage_functions()
    _snapshot_constraints()
    _future_insert_guard()
    _followup_writer()
    _privileges()


def _exact_percentage_functions() -> None:
    op.execute(r"""
        CREATE FUNCTION public.t145_percent_valid(value text)
        RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
        SET search_path = pg_catalog AS $$
        DECLARE
            integer_part text;
            fractional_part text;
        BEGIN
            IF value !~ '^(0|[1-9][0-9]*)(\.[0-9]+)?$' THEN
                RETURN false;
            END IF;
            integer_part := split_part(value, '.', 1);
            fractional_part := CASE
                WHEN strpos(value, '.') = 0 THEN ''
                ELSE split_part(value, '.', 2)
            END;
            IF length(integer_part) > 3
               OR (length(integer_part) = 3 AND integer_part <> '100')
               OR (integer_part = '100' AND fractional_part ~ '[1-9]') THEN
                RETURN false;
            END IF;
            RETURN true;
        END $$;

        CREATE FUNCTION public.t145_decimal_compare_rational(
            value text, numerator numeric, denominator numeric)
        RETURNS integer
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
        SET search_path = pg_catalog AS $$
        DECLARE
            integer_part integer;
            fractional_part text;
            rational_integer numeric;
            remainder numeric;
            scale numeric;
            rational_chunk numeric;
            supplied_chunk numeric;
            digit_offset integer := 1;
            chunk_length integer;
            chunk_size constant integer := 32;
        BEGIN
            IF denominator <= 0 OR denominator >= 'Infinity'::numeric
               OR trunc(denominator) <> denominator
               OR trunc(numerator) <> numerator
               OR public.t145_percent_valid(value) IS NOT TRUE THEN
                RETURN NULL;
            END IF;
            IF numerator < 0 THEN
                RETURN 1;
            END IF;
            IF numerator > 100 * denominator THEN
                RETURN -1;
            END IF;

            integer_part := split_part(value, '.', 1)::integer;
            fractional_part := CASE
                WHEN strpos(value, '.') = 0 THEN ''
                ELSE split_part(value, '.', 2)
            END;
            rational_integer := div(numerator, denominator);
            remainder := mod(numerator, denominator);
            IF integer_part::numeric < rational_integer THEN
                RETURN -1;
            ELSIF integer_part::numeric > rational_integer THEN
                RETURN 1;
            END IF;

            WHILE digit_offset <= length(fractional_part) LOOP
                chunk_length := least(chunk_size, length(fractional_part) - digit_offset + 1);
                scale := power(10::numeric, chunk_length);
                remainder := remainder * scale;
                rational_chunk := div(remainder, denominator);
                remainder := mod(remainder, denominator);
                supplied_chunk := substr(fractional_part, digit_offset, chunk_length)::numeric;
                IF supplied_chunk < rational_chunk THEN
                    RETURN -1;
                ELSIF supplied_chunk > rational_chunk THEN
                    RETURN 1;
                END IF;
                digit_offset := digit_offset + chunk_length;
            END LOOP;
            IF remainder = 0 THEN
                RETURN 0;
            END IF;
            RETURN -1;
        END $$;

        CREATE FUNCTION public.t145_percent_net_matches(
            base numeric, net numeric, value text)
        RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
        SET search_path = pg_catalog AS $$
        DECLARE
            base_cents numeric;
            net_cents numeric;
            discount_cents numeric;
            lower_numerator numeric;
            upper_numerator numeric;
        BEGIN
            IF base < 0 OR base >= 'Infinity'::numeric OR trunc(base, 2) <> base
               OR net < 0 OR net >= 'Infinity'::numeric OR trunc(net, 2) <> net
               OR net > base OR public.t145_percent_valid(value) IS NOT TRUE THEN
                RETURN false;
            END IF;
            base_cents := base * 100;
            net_cents := net * 100;
            IF base_cents = 0 THEN
                RETURN net_cents = 0;
            END IF;
            discount_cents := base_cents - net_cents;
            lower_numerator := 50 * (2 * discount_cents - 1);
            upper_numerator := 50 * (2 * discount_cents + 1);
            RETURN public.t145_decimal_compare_rational(
                       value, lower_numerator, base_cents) > 0
               AND public.t145_decimal_compare_rational(
                       value, upper_numerator, base_cents) <= 0;
        END $$;
    """)


def _snapshot_constraints() -> None:
    op.execute("""
        ALTER TABLE public.appointment_price_snapshots
            DROP CONSTRAINT appointment_price_discount_valid,
            DROP CONSTRAINT appointment_price_final_valid;

        ALTER TABLE public.appointment_price_snapshots
            ADD CONSTRAINT appointment_price_discount_valid CHECK ((
                ((discount_type = 'NONE'
                      AND discount_percent_text IS NULL
                      AND discount_value IS NOT NULL
                      AND discount_value >= 0 AND discount_value < 'Infinity'::numeric
                      AND discount_value = 0 AND discount_reason IS NULL)
                  OR (discount_type = 'FIXED'
                      AND discount_percent_text IS NULL
                      AND discount_value IS NOT NULL
                      AND discount_value >= 0 AND discount_value < 'Infinity'::numeric
                      AND discount_value <= base_egp
                      AND trunc(discount_value, 2) = discount_value
                      AND discount_reason IS NOT NULL
                      AND btrim(discount_reason) <> '')
                  OR (discount_type = 'PERCENT'
                      AND discount_reason IS NOT NULL
                      AND btrim(discount_reason) <> ''
                      AND ((discount_percent_text IS NULL
                            AND discount_value IS NOT NULL
                            AND discount_value >= 0
                            AND discount_value < 'Infinity'::numeric
                            AND discount_value <= 100)
                           OR (discount_percent_text IS NOT NULL
                               AND discount_value IS NULL
                               AND public.t145_percent_valid(discount_percent_text)))))
            ) IS TRUE),
            ADD CONSTRAINT appointment_price_final_valid CHECK ((
                (CASE
                    WHEN discount_type = 'PERCENT' AND discount_percent_text IS NOT NULL
                        THEN public.t145_percent_net_matches(
                            base_egp, net_egp, discount_percent_text)
                    ELSE net_egp = CASE
                        WHEN discount_type = 'FIXED'
                            THEN round(base_egp - discount_value, 2)
                        WHEN discount_type = 'PERCENT'
                            THEN round(base_egp - base_egp * discount_value / 100, 2)
                        ELSE base_egp
                    END
                END)
                AND is_free = (net_egp = 0)
                AND (service_type <> 'NEW_EXAMINATION'
                     OR classification = 'URGENT'
                     OR base_egp = creation_standard_base_egp)
                AND (classification <> 'URGENT' OR
                     (NOT is_free AND base_egp > creation_standard_base_egp
                      AND net_egp > creation_standard_base_egp))
            ) IS TRUE);
    """)


def _future_insert_guard() -> None:
    op.execute("""
        CREATE FUNCTION public.t145_guard_price_snapshot_insert()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.discount_type = 'PERCENT' THEN
                IF NEW.discount_percent_text IS NULL OR NEW.discount_value IS NOT NULL THEN
                    RAISE EXCEPTION 'New percentage snapshots require exact text authority'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.discount_percent_text IS NOT NULL OR NEW.discount_value IS NULL THEN
                RAISE EXCEPTION 'Percentage text is invalid for this discount type'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER appointment_price_percent_insert_guard
            BEFORE INSERT ON public.appointment_price_snapshots
            FOR EACH ROW EXECUTE FUNCTION public.t145_guard_price_snapshot_insert();
    """)


def _followup_writer() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.b10_unchecked_claim_creation(
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
               OR jsonb_typeof(price) IS DISTINCT FROM 'object'
               OR (price->>'discount_type' = 'PERCENT'
                   AND jsonb_typeof(price->'discount_value') IS DISTINCT FROM 'string') THEN
                RAISE EXCEPTION 'Invalid ordered claim input' USING ERRCODE = '23514';
            END IF;
            day := (booking->>'service_date')::date;
            number := (booking->>'allocated_number')::bigint;
            IF NOT isfinite(day) OR day::text <> booking->>'service_date' THEN
                RAISE EXCEPTION 'Finite Cairo service date required' USING ERRCODE = '23514';
            END IF;
            PERFORM public.b10_lock_followup_prefix(org, visit);
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
                base_egp, discount_type, discount_value, discount_percent_text,
                discount_reason, is_free, net_egp, pricing_version,
                creation_standard_base_egp, entitlement_provenance)
            VALUES (org, target, 'FOLLOW_UP', 'ROUTINE', (price->>'base_egp')::numeric,
                price->>'discount_type',
                CASE WHEN price->>'discount_type' = 'PERCENT'
                     THEN NULL::numeric ELSE (price->>'discount_value')::numeric END,
                CASE WHEN price->>'discount_type' = 'PERCENT'
                     THEN price->>'discount_value' ELSE NULL END,
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
        END $$;
    """)


def _privileges() -> None:
    op.execute("""
        REVOKE ALL ON FUNCTION public.t145_percent_valid(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.t145_decimal_compare_rational(text,numeric,numeric)
            FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.t145_percent_net_matches(numeric,numeric,text)
            FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.t145_guard_price_snapshot_insert() FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.b10_unchecked_claim_creation(uuid,uuid,jsonb,jsonb)
            FROM PUBLIC, carepulse_runtime;
        GRANT EXECUTE ON FUNCTION public.t145_percent_valid(text) TO carepulse_runtime;
        GRANT EXECUTE ON FUNCTION public.t145_decimal_compare_rational(text,numeric,numeric)
            TO carepulse_runtime;
        GRANT EXECUTE ON FUNCTION public.t145_percent_net_matches(numeric,numeric,text)
            TO carepulse_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("T145 exact percentage migration is forward-only")
