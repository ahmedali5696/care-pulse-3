"""B-11: separate signed, atomic new-ID intake authority and evidence."""

from alembic import op
from sqlalchemy import text

revision = "0051_b11_intake"
down_revision = "0050_b12_checkin_completeness"
branch_labels = None
depends_on = None

_PURPOSES = "('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT')"


def _rewrite(signature: str, before: str, after: str) -> None:
    """Extend the exact installed function without replacing unrelated behavior or ACLs."""
    bind = op.get_bind()
    definition = bind.execute(
        text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
        {"signature": signature},
    ).scalar_one()
    if not definition or definition.count(before) != 1:
        raise RuntimeError(
            "Missing unique historical intake extension marker: " + signature
        )
    bind.exec_driver_sql(
        definition.replace(before, after), execution_options={"no_parameters": True}
    )


def _policy(
    table: str, operation: str, predicate: str, check: str | None = None
) -> None:
    using = f"USING ({predicate})" if operation != "INSERT" else ""
    with_check = (
        f"WITH CHECK ({check if check is not None else predicate})"
        if operation != "SELECT"
        else ""
    )
    op.execute(
        f"CREATE POLICY f01_api_intake_{operation.lower()} ON public.{table} "
        f"FOR {operation} TO carepulse_api {using} {with_check}"
    )


def _scope(table: str, predicate: str = "true", *, primary: bool = True) -> str:
    stage = "AND i.stage = 'PRIMARY'" if primary else ""
    return (
        "EXISTS (SELECT 1 FROM public.f01_intake_authority() i "
        f"WHERE i.organization_id = {table}.organization_id {stage} AND ({predicate}))"
    )


def upgrade() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN intake_body jsonb,
            ADD COLUMN intake_digest text, ADD COLUMN intake_date date;
        ALTER TABLE f01_private.activations ADD COLUMN intake_number bigint,
            ADD COLUMN intake_day_id uuid,
            ADD COLUMN intake_entry_id uuid,
            ADD COLUMN intake_audit_recorded boolean NOT NULL DEFAULT false,
            ADD COLUMN intake_outbox_recorded boolean NOT NULL DEFAULT false,
            ADD COLUMN intake_receipt_recorded boolean NOT NULL DEFAULT false;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method = 'POST' AND p_path = '/v1/intake/routine' THEN 'INTAKE_ROUTINE'
                WHEN p_method = 'POST' AND p_path = '/v1/intake/emergency' THEN 'INTAKE_EMERGENCY'
                WHEN p_method = 'POST' AND p_path = '/v1/intake/urgent' THEN 'INTAKE_URGENT'""",
    )
    # Preserve the exact 0049 register implementation and nonce/freshness checks.
    op.execute(r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_intake;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_intake(bytea,text,bytea,bytea)
            FROM PUBLIC, carepulse_gate;
    """)
    _rewrite(
        "public.f01_register_proof_before_intake(bytea,text,bytea,bytea)",
        "f01_register_proof.key_id",
        "f01_register_proof_before_intake.key_id",
    )
    op.execute(r"""

        CREATE FUNCTION f01_private.canonical_intake_json(value json)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
        DECLARE result text;
        BEGIN
            IF json_typeof(value) = 'object' THEN
                IF EXISTS (SELECT 1 FROM json_each(value) e GROUP BY e.key HAVING count(*) > 1) THEN
                    RAISE EXCEPTION 'Duplicate intake field' USING ERRCODE = '22023';
                END IF;
                SELECT string_agg(to_json(e.key)::text || chr(58) ||
                    f01_private.canonical_intake_json(e.value), ',' ORDER BY e.key COLLATE "C")
                    INTO result FROM json_each(value) e;
                RETURN '{' || coalesce(result, '') || '}';
            ELSIF json_typeof(value) = 'array' THEN
                SELECT string_agg(f01_private.canonical_intake_json(e.value), ',' ORDER BY e.n)
                    INTO result FROM json_array_elements(value) WITH ORDINALITY e(value,n);
                RETURN '[' || coalesce(result, '') || ']';
            END IF;
            RETURN value::jsonb::text;
        END $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea, key_id text, mac bytea, body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            request_id bigint;
            req f01_private.requests%ROWTYPE;
            command jsonb;
            raw json;
            service_day date;
            signed_patient uuid;
            digest text;
            reason_whitespace constant text :=
                U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
                || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
            permitted text[] := ARRAY['patient_id','physical_presence_confirmed','discount',
                'expected_base_egp','expected_net_egp','expected_price_revision'];
        BEGIN
            request_id := public.f01_register_proof_before_intake(payload, key_id, mac, body);
            SELECT * INTO req FROM f01_private.requests r WHERE r.id = request_id;
            IF req.purpose NOT IN ('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT') THEN
                RETURN request_id;
            END IF;
            BEGIN
                raw := convert_from(body, 'UTF8')::json;
                -- Recursively reject duplicate keys before converting to jsonb.
                PERFORM f01_private.canonical_intake_json(raw);
                command := raw::jsonb;
                IF req.purpose = 'INTAKE_ROUTINE' THEN
                    permitted := permitted || ARRAY['service_date','requested_service_type',
                        'source_visit_id','starts_at','ends_at','expected_followup_revision',
                        'expected_schedule_revision','expected_capacity_revision','expected_source_revision',
                        'expected_source_decision_revision','expected_visit_revision',
                        'new_booking_date_exception','fallback_acknowledgment'];
                ELSIF req.purpose = 'INTAKE_URGENT' THEN
                    permitted := permitted || ARRAY['starts_at','ends_at','expected_schedule_revision',
                        'expected_capacity_revision'];
                END IF;
                IF jsonb_typeof(command) IS DISTINCT FROM 'object' OR req.query <> ''
                    OR req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$'
                    OR command->'physical_presence_confirmed' IS DISTINCT FROM to_jsonb(true)
                    OR jsonb_typeof(command->'patient_id') IS DISTINCT FROM 'string'
                    OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k WHERE k <> ALL(permitted))
                    OR jsonb_typeof(command->'discount') IS DISTINCT FROM 'object'
                    OR EXISTS (SELECT 1 FROM jsonb_object_keys(command->'discount') k
                        WHERE k NOT IN ('discount_type','discount_value','discount_reason'))
                    OR jsonb_typeof(command->'discount'->'discount_type') IS DISTINCT FROM 'string'
                    OR command->'discount'->>'discount_type' NOT IN ('NONE','FIXED','PERCENT')
                    OR jsonb_typeof(command->'discount'->'discount_value') IS DISTINCT FROM 'string'
                    OR coalesce(jsonb_typeof(command->'discount'->'discount_reason'),'null') NOT IN ('string','null')
                    OR jsonb_typeof(command->'expected_base_egp') IS DISTINCT FROM 'string'
                    OR jsonb_typeof(command->'expected_net_egp') IS DISTINCT FROM 'string'
                    OR command->>'expected_base_egp' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$'
                    OR command->>'expected_net_egp' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$'
                    OR (command->'discount'->>'discount_type' = 'PERCENT' AND
                        command->'discount'->>'discount_value' !~ '^((0|[1-9][0-9]?)(\.[0-9]+)?|100(\.0+)?)$')
                    OR (command->'discount'->>'discount_type' <> 'PERCENT' AND
                        command->'discount'->>'discount_value' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$')
                    OR (command->'discount'->>'discount_type' = 'NONE' AND
                        (command->'discount'->>'discount_reason' IS NOT NULL OR
                         (command->'discount'->>'discount_value')::numeric <> 0))
                    OR (command->'discount'->>'discount_type' <> 'NONE' AND
                        coalesce(btrim(command->'discount'->>'discount_reason',reason_whitespace),'') = '')
                    OR jsonb_typeof(command->'expected_price_revision') IS DISTINCT FROM 'number'
                    OR EXISTS (SELECT 1 FROM json_each(raw) e WHERE e.key LIKE 'expected_%_revision'
                        AND json_typeof(e.value) <> 'null' AND (json_typeof(e.value) <> 'number'
                            OR e.value::text !~ '^(0|[1-9][0-9]{0,18})$'))
                    OR (req.purpose <> 'INTAKE_EMERGENCY' AND
                        jsonb_typeof(command->'expected_schedule_revision') IS DISTINCT FROM 'number')
                    OR (command->>'starts_at' IS NULL) <> (command->>'ends_at' IS NULL)
                    OR coalesce(jsonb_typeof(command->'starts_at'),'null') NOT IN ('string','null')
                    OR coalesce(jsonb_typeof(command->'ends_at'),'null') NOT IN ('string','null')
                    OR (req.purpose = 'INTAKE_URGENT' AND (command ? 'starts_at' OR command ? 'ends_at')
                        AND (command->>'starts_at' IS NULL OR command->>'ends_at' IS NULL)) THEN
                    RAISE EXCEPTION 'Invalid intake body' USING ERRCODE = '22023';
                END IF;
                signed_patient := (command->>'patient_id')::uuid;
                command := jsonb_set(command, '{patient_id}', to_jsonb(signed_patient));
                IF req.purpose = 'INTAKE_ROUTINE' THEN
                    IF jsonb_typeof(command->'service_date') IS DISTINCT FROM 'string'
                        OR command->>'service_date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                        OR coalesce(command->>'requested_service_type','') NOT IN ('NEW_EXAMINATION','FOLLOW_UP') THEN
                        RAISE EXCEPTION 'Invalid Routine intake body' USING ERRCODE = '22023';
                    END IF;
                    service_day := (command->>'service_date')::date;
                    IF command->>'source_visit_id' IS NOT NULL THEN
                        IF jsonb_typeof(command->'source_visit_id') <> 'string' THEN
                            RAISE EXCEPTION 'Invalid source Visit' USING ERRCODE = '22023';
                        END IF;
                        command := jsonb_set(command, '{source_visit_id}',
                            to_jsonb((command->>'source_visit_id')::uuid));
                    END IF;
                    IF command->>'fallback_acknowledgment' IS NOT NULL AND (
                        jsonb_typeof(command->'fallback_acknowledgment') IS DISTINCT FROM 'object'
                        OR command->'fallback_acknowledgment' IS DISTINCT FROM jsonb_build_object(
                            'service_type','NEW_EXAMINATION','discount_reaffirmed',true)) THEN
                        RAISE EXCEPTION 'Invalid fallback acknowledgement' USING ERRCODE = '22023';
                    END IF;
                    IF command->>'new_booking_date_exception' IS NOT NULL AND (
                        jsonb_typeof(command->'new_booking_date_exception') IS DISTINCT FROM 'object'
                        OR (command->'new_booking_date_exception' - 'selected_service_date' - 'reason') <> '{}'::jsonb
                        OR command->'new_booking_date_exception'->>'selected_service_date' IS DISTINCT FROM service_day::text
                        OR coalesce(jsonb_typeof(command->'new_booking_date_exception'->'reason'),'null') NOT IN ('string','null')) THEN
                        RAISE EXCEPTION 'Invalid intake date exception' USING ERRCODE = '22023';
                    END IF;
                    IF command->'new_booking_date_exception'->>'reason' IS NOT NULL THEN
                        IF btrim(command->'new_booking_date_exception'->>'reason',reason_whitespace) = ''
                            OR command->'new_booking_date_exception'->>'reason' ~ '[\x01-\x1f]' THEN
                            RAISE EXCEPTION 'Invalid intake date exception reason' USING ERRCODE = '22023';
                        END IF;
                        command := jsonb_set(command, '{new_booking_date_exception,reason}',
                            to_jsonb(btrim(command->'new_booking_date_exception'->>'reason',reason_whitespace)));
                    END IF;
                    IF (command->>'requested_service_type' = 'NEW_EXAMINATION' AND
                        (command->>'source_visit_id' IS NOT NULL OR command->>'fallback_acknowledgment' IS NOT NULL
                         OR command->>'new_booking_date_exception' IS NOT NULL OR command->>'expected_source_revision' IS NOT NULL
                         OR command->>'expected_source_decision_revision' IS NOT NULL OR command->>'expected_visit_revision' IS NOT NULL))
                        OR (command->>'requested_service_type' = 'FOLLOW_UP' AND command->>'expected_followup_revision' IS NULL) THEN
                        RAISE EXCEPTION 'Invalid signed Routine service intent' USING ERRCODE = '22023';
                    END IF;
                ELSE
                    service_day := (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date;
                END IF;
                digest := encode(sha256(convert_to(f01_private.canonical_intake_json(command::json), 'UTF8')), 'hex');
                UPDATE f01_private.requests r SET intake_body = command, intake_digest = digest,
                    intake_date = service_day, patient_id = signed_patient,
                    source_visit_id = (command->>'source_visit_id')::uuid WHERE r.id = request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid = false WHERE r.id = request_id;
            END;
            RETURN request_id;
        END $$;
        REVOKE ALL ON FUNCTION f01_private.canonical_intake_json(json),
            public.f01_register_proof(bytea,text,bytea,bytea) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
    """)
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        "OR req.purpose = 'VISIT_FOLLOWUP'",
        f"""OR req.purpose = 'VISIT_FOLLOWUP'
                           OR (req.purpose IN {_PURPOSES} AND req.method = 'POST'
                               AND req.intake_digest IS NOT NULL)""",
    )
    op.execute("""
        CREATE FUNCTION public.f01_intake_authority()
        RETURNS TABLE (organization_id uuid, staff_id uuid, role text, stage text,
            purpose text, operation text, idempotency_key text, patient_id uuid,
            service_date date, classification text, lane text, command jsonb, digest text,
            appointment_id uuid, day_id uuid, entry_id uuid, allocated_number bigint)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path = pg_catalog, pg_temp AS $$
            SELECT r.organization_id, r.staff_id, r.role, c.stage, r.purpose,
                CASE r.purpose WHEN 'INTAKE_ROUTINE' THEN 'appointments.intake_routine'
                    WHEN 'INTAKE_EMERGENCY' THEN 'appointments.intake_emergency'
                    ELSE 'appointments.intake_urgent' END,
                r.idempotency_key, r.patient_id, r.intake_date,
                CASE r.purpose WHEN 'INTAKE_ROUTINE' THEN 'ROUTINE'
                    WHEN 'INTAKE_EMERGENCY' THEN 'EMERGENCY' ELSE 'URGENT' END,
                CASE WHEN r.purpose = 'INTAKE_EMERGENCY' THEN 'EMERGENCY' ELSE 'MAIN' END,
                r.intake_body, r.intake_digest, a.audit_appointment_id, a.intake_day_id, a.intake_entry_id,
                a.intake_number
            FROM public.f01_context() c
            JOIN f01_private.requests r ON r.id = c.request_id
            JOIN f01_private.activations a ON a.request_id = c.request_id AND a.stage = c.stage
            WHERE c.purpose IN ('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT')
              AND c.method = 'POST' AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY')
              AND c.role IN ('DOCTOR_OWNER','RECEPTIONIST')
              AND (c.purpose <> 'INTAKE_URGENT' OR c.role = 'RECEPTIONIST')
              AND r.intake_digest IS NOT NULL;
        $$;
        REVOKE ALL ON FUNCTION public.f01_intake_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_intake_authority() TO carepulse_api, carepulse_migration;
    """)
    # The same existing activation captures exactly one new Appointment ID.
    _rewrite(
        "f01_private.capture_audit_appointment()",
        "IF NOT FOUND OR NOT COALESCE(ctx.purpose = 'APPOINTMENT_BOOKING'",
        """IF EXISTS (SELECT 1 FROM public.f01_intake_authority() i
                WHERE i.stage = 'PRIMARY' AND i.organization_id = NEW.organization_id
                  AND i.patient_id = NEW.patient_id AND i.service_date = NEW.service_date
                  AND i.classification = NEW.classification AND i.lane = NEW.lane
                  AND NEW.status = 'Waiting' AND NEW.revision = 0
                  AND NEW.current_number = i.allocated_number AND i.day_id IS NOT NULL
                  AND (NEW.service_type = 'NEW_EXAMINATION' AND (i.purpose <> 'INTAKE_ROUTINE'
                      OR i.command->>'requested_service_type' = 'NEW_EXAMINATION'
                      OR i.command->'fallback_acknowledgment'->'discount_reaffirmed' = to_jsonb(true))
                      OR (NEW.service_type = 'FOLLOW_UP' AND i.purpose = 'INTAKE_ROUTINE'
                          AND i.command->>'requested_service_type' = 'FOLLOW_UP'
                          AND i.command->>'source_visit_id' IS NOT NULL
                          AND i.command->>'fallback_acknowledgment' IS NULL))
                  AND (i.purpose = 'INTAKE_ROUTINE' OR NEW.service_type = 'NEW_EXAMINATION')
                  AND (i.purpose = 'INTAKE_ROUTINE' OR
                      NEW.service_date = (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date)
                  AND (i.command->>'starts_at')::timestamptz IS NOT DISTINCT FROM NEW.starts_at
                  AND (i.command->>'ends_at')::timestamptz IS NOT DISTINCT FROM NEW.ends_at) THEN
                NULL;
            ELSIF NOT FOUND OR NOT COALESCE(ctx.purpose = 'APPOINTMENT_BOOKING'""",
    )
    _rewrite(
        "f01_private.capture_audit_appointment()",
        "AND ctx.patient_id = NEW.patient_id AND NEW.revision = 0, false)",
        "AND ctx.patient_id = NEW.patient_id AND NEW.revision = 0 AND NEW.status = 'Booked' AND NEW.classification = 'ROUTINE' AND NEW.lane = 'MAIN', false)",
    )
    _rewrite(
        "f01_private.guard_audit_appointment_history()",
        "OR (c.purpose = 'QUEUE_CHECK_IN'",
        """OR (c.purpose IN ('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT')
                          AND EXISTS (SELECT 1 FROM public.f01_intake_authority() i
                              WHERE i.stage = 'PRIMARY' AND i.appointment_id = NEW.appointment_id)
                          AND NEW.action = 'CREATED' AND NEW.appointment_revision = 0
                          AND NEW.old_status IS NULL AND NEW.new_status = 'Waiting')
                      OR (c.purpose = 'QUEUE_CHECK_IN'""",
    )
    op.execute("""
        CREATE FUNCTION public.f01_record_intake_progress()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ctx record;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NULL; END IF;
            SELECT * INTO ctx FROM public.f01_context();
            IF NOT FOUND OR ctx.purpose NOT IN ('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT') THEN
                RETURN NULL;
            END IF;
            IF TG_TABLE_NAME = 'appointment_day_counters' THEN
                UPDATE f01_private.activations a SET intake_number = NEW.last_number
                WHERE a.request_id = ctx.request_id AND a.stage = ctx.stage AND a.intake_number IS NULL;
            ELSIF TG_TABLE_NAME = 'queue_days' THEN
                UPDATE f01_private.activations a SET intake_day_id = NEW.id
                WHERE a.request_id = ctx.request_id AND a.stage = ctx.stage AND a.intake_day_id IS NULL
                    AND a.intake_number IS NOT NULL;
            ELSIF TG_TABLE_NAME = 'queue_entries' THEN
                UPDATE f01_private.activations a SET intake_entry_id = NEW.id
                WHERE a.request_id = ctx.request_id AND a.stage = ctx.stage AND a.intake_entry_id IS NULL
                    AND a.audit_appointment_id = NEW.appointment_id AND a.intake_day_id = NEW.queue_day_id;
            ELSE
                UPDATE f01_private.activations a SET
                    intake_audit_recorded = a.intake_audit_recorded OR TG_TABLE_NAME = 'sensitive_audit',
                    intake_outbox_recorded = a.intake_outbox_recorded OR TG_TABLE_NAME = 'outbox_events',
                    intake_receipt_recorded = a.intake_receipt_recorded OR TG_TABLE_NAME = 'idempotency_receipts'
                WHERE a.request_id = ctx.request_id AND a.stage = ctx.stage AND a.intake_entry_id IS NOT NULL
                  AND NOT CASE TG_TABLE_NAME WHEN 'sensitive_audit' THEN a.intake_audit_recorded
                      WHEN 'outbox_events' THEN a.intake_outbox_recorded
                      WHEN 'idempotency_receipts' THEN a.intake_receipt_recorded ELSE true END;
            END IF;
            IF NOT FOUND OR ctx.stage <> 'PRIMARY' THEN
                RAISE EXCEPTION 'Intake effect scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN NULL;
        END $$;

        CREATE FUNCTION f01_private.assert_intake_complete()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM f01_private.activations a
                WHERE a.request_id = NEW.request_id AND a.stage = NEW.stage
                    AND a.intake_number = NEW.intake_number AND a.audit_appointment_id IS NOT NULL
                    AND a.intake_day_id IS NOT NULL AND a.intake_entry_id IS NOT NULL
                    AND a.intake_audit_recorded AND a.intake_outbox_recorded AND a.intake_receipt_recorded) THEN
                RAISE EXCEPTION 'Intake commit evidence incomplete' USING ERRCODE = '42501';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER f01_intake_evidence_committed
            AFTER UPDATE ON f01_private.activations DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW WHEN (OLD.intake_number IS NULL AND NEW.intake_number IS NOT NULL)
            EXECUTE FUNCTION f01_private.assert_intake_complete();
        REVOKE ALL ON FUNCTION public.f01_record_intake_progress(),
            f01_private.assert_intake_complete() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_record_intake_progress() TO carepulse_migration;
    """)
    op.execute("""
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)

    # Additional permissive policies cover only this signed intake; historical
    # policies and grants remain unchanged, including B-12's restrictive checks.
    op.execute("""
        CREATE FUNCTION public.f01_intake_retained()
        RETURNS TABLE (appointment_id uuid, entry_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path = pg_catalog, pg_temp AS $$
            SELECT (r.result_projection->>'id')::uuid, (r.result_projection->>'queue_entry_id')::uuid
            FROM public.f01_intake_authority() i
            JOIN public.idempotency_receipts r ON r.organization_id = i.organization_id
                AND r.scope_id = i.organization_id AND r.scope_kind = 'TENANT'
                AND r.actor_kind = 'STAFF' AND r.staff_id = i.staff_id AND r.actor_id = i.staff_id
                AND r.idempotency_key = i.idempotency_key AND r.operation = i.operation
                AND r.result_projection->>'patient_id' = i.patient_id::text;
        $$;
        REVOKE ALL ON FUNCTION public.f01_intake_retained() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_intake_retained() TO carepulse_api;
    """)
    _policy(
        "staff_accounts",
        "SELECT",
        _scope("staff_accounts", "staff_accounts.role = 'DOCTOR_OWNER'"),
    )
    patient = _scope("patients", "patients.id = i.patient_id", primary=False)
    _policy("patients", "SELECT", patient)
    _policy(
        "patients", "UPDATE", _scope("patients", "patients.id = i.patient_id"), "false"
    )
    for table in ("clinic_policy_heads", "clinic_policy_versions"):
        _policy(
            table,
            "SELECT",
            _scope(
                table,
                f"{table}.kind IN ('PRICE','SCHEDULE','CAPACITY','FOLLOWUP','QUEUE')",
            ),
        )
    _policy(
        "clinic_policy_heads",
        "UPDATE",
        _scope(
            "clinic_policy_heads",
            "clinic_policy_heads.kind IN ('PRICE','SCHEDULE','CAPACITY','FOLLOWUP','QUEUE')",
        ),
        "false",
    )
    for table in ("clinic_schedule_intervals", "clinic_schedule_holidays"):
        _policy(table, "SELECT", _scope(table))
    _policy(
        "appointment_day_counters",
        "SELECT",
        _scope(
            "appointment_day_counters",
            "appointment_day_counters.service_date = i.service_date",
        ),
    )
    for operation in ("INSERT", "UPDATE"):
        _policy(
            "appointment_day_counters",
            operation,
            _scope(
                "appointment_day_counters",
                "appointment_day_counters.service_date = i.service_date",
            ),
        )
    _policy(
        "appointments",
        "INSERT",
        _scope(
            "appointments",
            """appointments.id = i.appointment_id
        AND appointments.patient_id = i.patient_id AND appointments.service_date = i.service_date
        AND appointments.status = 'Waiting' AND appointments.classification = i.classification
        AND appointments.lane = i.lane AND appointments.revision = 0""",
        ),
    )
    retained = "EXISTS (SELECT 1 FROM public.f01_intake_retained() r WHERE r.appointment_id = appointments.id)"
    _policy(
        "appointments",
        "SELECT",
        _scope(
            "appointments",
            f"""appointments.id = i.appointment_id
        OR (appointments.patient_id = i.patient_id AND ({retained} OR i.stage = 'PRIMARY'))
        OR (i.stage = 'PRIMARY' AND appointments.status = 'Waiting'
            AND appointments.service_date = i.service_date AND appointments.lane = i.lane)""",
            primary=False,
        ),
    )
    for table in (
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "appointment_history",
    ):
        own = _scope(table, f"{table}.appointment_id = i.appointment_id")
        _policy(
            table,
            "SELECT",
            own
            + " OR "
            + _scope(
                table,
                f"EXISTS (SELECT 1 FROM public.f01_intake_retained() r WHERE r.appointment_id = {table}.appointment_id)",
                primary=False,
            ),
        )
        _policy(table, "INSERT", own)

    # The existing B-10 writer retains its checks and lock order; only a
    # signed Routine request may produce a Waiting preferential appointment.
    booking_scope = "ctx.purpose = 'APPOINTMENT_BOOKING' AND ctx.method = 'POST'\n                    AND ctx.path = '/v1/appointments' AND ctx.stage = 'PRIMARY'"
    routine_scope = "(ctx.purpose = 'INTAKE_ROUTINE' AND ctx.method = 'POST' AND ctx.path = '/v1/intake/routine' AND ctx.stage = 'PRIMARY')"
    for signature in (
        "d08_context(uuid,uuid,jsonb)",
        "b10_lock_followup_prefix(uuid,uuid)",
        "create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)",
    ):
        _rewrite(
            "public." + signature,
            booking_scope,
            f"(({booking_scope}) OR {routine_scope})",
        )
    _rewrite(
        "public.b10_operational_source_proof(uuid,uuid)",
        "AND ctx.purpose = 'APPOINTMENT_BOOKING'",
        "AND ctx.purpose IN ('APPOINTMENT_BOOKING','INTAKE_ROUTINE')",
    )
    _rewrite(
        "public.create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)",
        "RETURN public.b10_unchecked_claim_creation(org, visit, expected, booking);",
        """IF EXISTS (SELECT 1 FROM public.f01_intake_authority() i WHERE i.stage = 'PRIMARY')
                AND NOT EXISTS (SELECT 1 FROM public.f01_intake_authority() i
                    WHERE i.stage = 'PRIMARY' AND i.purpose = 'INTAKE_ROUTINE'
                      AND i.command->>'requested_service_type' = 'FOLLOW_UP'
                      AND i.command->>'source_visit_id' = visit::text AND i.patient_id = patient
                      AND i.service_date = day AND i.allocated_number = (booking->>'allocated_number')::bigint
                      AND i.command->>'fallback_acknowledgment' IS NULL
                      AND (i.command->>'expected_source_decision_revision')::bigint = (expected->>'source_revision')::bigint
                      AND (i.command->>'expected_visit_revision')::bigint = (expected->>'clinical_revision')::bigint
                      AND coalesce(i.command->'new_booking_date_exception', 'null'::jsonb)
                          = coalesce(booking->'new_booking_date_exception', 'null'::jsonb)) THEN
                RAISE EXCEPTION 'Signed intake source scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN public.b10_unchecked_claim_creation(org, visit, expected, booking);""",
    )
    waiting = "CASE WHEN EXISTS (SELECT 1 FROM public.f01_intake_authority() i WHERE i.stage = 'PRIMARY' AND i.purpose = 'INTAKE_ROUTINE') THEN 'Waiting' ELSE 'Booked' END"
    for marker in (
        "source.doctor_staff_id, day, 'Booked',",
        "0, 'CREATED', 'Booked', day, number,",
    ):
        _rewrite(
            "public.b10_unchecked_claim_creation(uuid,uuid,jsonb,jsonb)",
            marker,
            marker.replace("'Booked'", waiting),
        )
    source = _scope(
        "visits",
        "i.purpose = 'INTAKE_ROUTINE' AND visits.id = (i.command->>'source_visit_id')::uuid AND visits.patient_id = i.patient_id",
    )
    _policy("visits", "SELECT", source)
    _policy("visits", "UPDATE", source, "false")
    _policy(
        "appointments",
        "UPDATE",
        _scope(
            "appointments",
            """i.purpose = 'INTAKE_ROUTINE'
        AND appointments.patient_id = i.patient_id AND EXISTS (SELECT 1 FROM public.visits v
            WHERE v.organization_id = i.organization_id AND v.patient_id = i.patient_id
                AND v.id = (i.command->>'source_visit_id')::uuid AND v.appointment_id = appointments.id)""",
        ),
        "false",
    )
    _policy(
        "followup_claims",
        "SELECT",
        _scope(
            "followup_claims",
            """i.purpose = 'INTAKE_ROUTINE'
        AND followup_claims.source_visit_id = (i.command->>'source_visit_id')::uuid
        AND followup_claims.patient_id = i.patient_id""",
        ),
    )

    _rewrite(
        "public.f01_counted_appointment_intervals(uuid,date)",
        "OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'",
        """OR EXISTS (SELECT 1 FROM public.f01_intake_authority() i
                              WHERE i.stage = 'PRIMARY' AND i.organization_id = p_tenant
                                AND i.service_date = p_service_date)
                          OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'""",
    )
    _rewrite(
        "public.f01_queue_partition_matches(uuid,uuid,date,text)",
        "OR (c.purpose = 'QUEUE_READ' AND c.method = 'GET'",
        """OR EXISTS (SELECT 1 FROM public.f01_intake_authority() i
                          JOIN public.staff_accounts s ON s.organization_id = i.organization_id
                            AND s.role = 'DOCTOR_OWNER'
                          WHERE i.stage = 'PRIMARY' AND i.organization_id = p_tenant
                            AND s.id = p_doctor AND i.service_date = p_service_date AND i.lane = p_lane)
                      OR (c.purpose = 'QUEUE_READ' AND c.method = 'GET'""",
    )
    for table in ("queue_days", "queue_entries"):
        partition = _scope(
            table, f"{table}.service_date = i.service_date AND {table}.lane = i.lane"
        )
        retained_entry = _scope(
            "queue_entries",
            "EXISTS (SELECT 1 FROM public.f01_intake_retained() r WHERE r.entry_id = queue_entries.id AND r.appointment_id = queue_entries.appointment_id)",
            primary=False,
        )
        _policy(
            table,
            "SELECT",
            partition if table == "queue_days" else partition + " OR " + retained_entry,
        )
        _policy(
            table,
            "INSERT",
            partition
            if table == "queue_days"
            else _scope(
                table,
                "queue_entries.appointment_id = i.appointment_id AND queue_entries.queue_day_id = i.day_id",
            ),
        )
        _policy(
            table,
            "UPDATE",
            partition
            if table == "queue_days"
            else _scope(
                table,
                "i.purpose = 'INTAKE_ROUTINE' AND queue_entries.routine_rank IS NOT NULL AND queue_entries.queue_day_id = i.day_id",
            ),
            None if table == "queue_days" else "false",
        )
    op.execute(f"""
        ALTER POLICY f01_api_queue_partition ON public.queue_entries
        USING ((disposition = 'ACTIVE' AND public.f01_queue_partition_matches(
                organization_id, doctor_staff_id, service_date, lane)) OR ({retained_entry}));
    """)
    _rewrite(
        "public.f01_guard_signed_queue_day()",
        "IF session_user = 'carepulse_api' AND NOT EXISTS (",
        """IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_intake_authority() i
                WHERE i.stage = 'PRIMARY' AND i.day_id IS NULL
                  AND i.organization_id = NEW.organization_id AND i.service_date = NEW.service_date
                  AND i.lane = NEW.lane
            ) AND NOT EXISTS (""",
    )
    _rewrite(
        "public.f01_guard_checkin_entry()",
        "IF session_user = 'carepulse_api' AND NOT EXISTS (",
        """IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_intake_authority() i
                WHERE i.stage = 'PRIMARY' AND i.entry_id IS NULL
                  AND i.organization_id = NEW.organization_id AND i.appointment_id = NEW.appointment_id
                  AND i.day_id = NEW.queue_day_id AND i.patient_id = NEW.patient_id
                  AND i.service_date = NEW.service_date AND i.lane = NEW.lane
            ) AND NOT EXISTS (""",
    )
    receipt = _scope(
        "idempotency_receipts",
        """idempotency_receipts.scope_kind = 'TENANT'
        AND idempotency_receipts.actor_kind = 'STAFF' AND idempotency_receipts.staff_id = i.staff_id
        AND idempotency_receipts.actor_id = i.staff_id AND idempotency_receipts.operation = i.operation
        AND idempotency_receipts.idempotency_key = i.idempotency_key
        AND idempotency_receipts.scope_id = i.organization_id""",
        primary=False,
    )
    _policy("idempotency_receipts", "SELECT", receipt)
    _policy(
        "idempotency_receipts",
        "INSERT",
        receipt
        + " AND "
        + _scope(
            "idempotency_receipts",
            "idempotency_receipts.request_digest = i.digest AND i.appointment_id IS NOT NULL",
        ),
    )
    _policy(
        "sensitive_audit",
        "INSERT",
        _scope(
            "sensitive_audit",
            """sensitive_audit.action = i.operation
        AND sensitive_audit.actor_staff_id = i.staff_id AND sensitive_audit.actor_role = i.role
        AND sensitive_audit.correlation_id = i.appointment_id::text AND sensitive_audit.operator_id IS NULL""",
        ),
    )
    _policy(
        "outbox_events",
        "INSERT",
        _scope(
            "outbox_events",
            """outbox_events.event_type = 'APPOINTMENT_INTAKE'
        AND outbox_events.aggregate_type = 'APPOINTMENT' AND outbox_events.aggregate_id = i.appointment_id::text""",
        ),
    )

    op.execute(r"""
        CREATE FUNCTION public.f01_guard_intake_counter()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE i record;
            kind_name text;
        BEGIN
            SELECT * INTO i FROM public.f01_intake_authority();
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF i.stage <> 'PRIMARY' OR i.allocated_number IS NOT NULL
                OR i.organization_id IS DISTINCT FROM NEW.organization_id
                OR i.service_date IS DISTINCT FROM NEW.service_date THEN
                RAISE EXCEPTION 'Intake number scope denied' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.patients p WHERE p.organization_id = i.organization_id
                AND p.id = i.patient_id AND NOT p.archived FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Intake patient unavailable' USING ERRCODE = '23514'; END IF;
            FOREACH kind_name IN ARRAY ARRAY['SCHEDULE','CAPACITY','FOLLOWUP','PRICE','QUEUE'] LOOP
                IF (kind_name = 'CAPACITY' AND i.purpose = 'INTAKE_EMERGENCY')
                    OR (kind_name = 'FOLLOWUP' AND i.purpose <> 'INTAKE_ROUTINE') THEN CONTINUE; END IF;
                PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id = i.organization_id
                    AND h.kind = kind_name FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Intake policy unavailable' USING ERRCODE = '23514'; END IF;
            END LOOP;
            RETURN NEW;
        END $$;

        CREATE FUNCTION public.f01_guard_intake_appointment()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE i record;
            schedule_version bigint;
            schedule_policy jsonb;
            local_now timestamp := clock_timestamp() AT TIME ZONE 'Africa/Cairo';
            local_weekday integer := extract(isodow FROM local_now);
            override_work boolean;
        BEGIN
            SELECT * INTO i FROM public.f01_intake_authority();
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF i.stage <> 'PRIMARY' OR i.allocated_number IS NULL OR i.day_id IS NULL
                OR i.appointment_id IS NOT NULL OR i.organization_id <> NEW.organization_id
                OR i.patient_id <> NEW.patient_id OR i.service_date <> NEW.service_date
                OR i.classification <> NEW.classification OR i.lane <> NEW.lane THEN
                RAISE EXCEPTION 'Intake appointment scope denied' USING ERRCODE = '42501';
            END IF;
            IF i.purpose = 'INTAKE_EMERGENCY' THEN
                IF NEW.booking_mode <> 'OPEN' THEN
                    RAISE EXCEPTION 'Emergency intake interval denied' USING ERRCODE = '42501';
                END IF;
                RETURN NEW;
            END IF;
            SELECT h.current_version, v.payload INTO schedule_version, schedule_policy
            FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                ON (v.organization_id,v.kind,v.version) = (h.organization_id,h.kind,h.current_version)
            WHERE h.organization_id = i.organization_id AND h.kind = 'SCHEDULE'
                AND h.revision = (i.command->>'expected_schedule_revision')::bigint;
            IF NOT FOUND OR NEW.booking_mode IS DISTINCT FROM schedule_policy->>'booking_mode'
                OR (i.purpose = 'INTAKE_ROUTINE' AND schedule_policy->'routine_walk_in_enabled' IS DISTINCT FROM to_jsonb(true)) THEN
                RAISE EXCEPTION 'Intake schedule scope denied' USING ERRCODE = '23514';
            END IF;
            IF NEW.booking_mode = 'EXACT_TIME' AND (schedule_policy->>'allow_overlap')::boolean
                AND NOT EXISTS (SELECT 1 FROM public.clinic_policy_heads h WHERE h.organization_id = i.organization_id
                    AND h.kind = 'CAPACITY' AND h.revision = (i.command->>'expected_capacity_revision')::bigint) THEN
                RAISE EXCEPTION 'Intake capacity revision denied' USING ERRCODE = '23514';
            END IF;
            PERFORM public.b10_assert_followup_schedule(i.organization_id, jsonb_build_object(
                'service_date', NEW.service_date, 'booking_mode', NEW.booking_mode,
                'starts_at', i.command->'starts_at', 'ends_at', i.command->'ends_at'));
            IF i.purpose = 'INTAKE_URGENT' THEN
                SELECT EXISTS (SELECT 1 FROM public.clinic_schedule_intervals s
                    WHERE s.organization_id = i.organization_id AND s.policy_version = schedule_version
                        AND s.interval_kind = 'OVERRIDE_WORK' AND s.service_date = i.service_date) INTO override_work;
                IF i.service_date <> local_now::date
                    OR EXISTS (SELECT 1 FROM public.clinic_schedule_holidays h
                        WHERE h.organization_id = i.organization_id AND h.policy_version = schedule_version
                            AND h.service_date = i.service_date)
                    OR (NOT override_work AND (coalesce(schedule_policy->'open_override_dates' @> to_jsonb(ARRAY[i.service_date::text]),false)
                        OR (schedule_policy ? 'working_weekdays' AND NOT schedule_policy->'working_weekdays' @> to_jsonb(ARRAY[local_weekday]))))
                    OR NOT EXISTS (SELECT 1 FROM public.clinic_schedule_intervals s
                        WHERE s.organization_id = i.organization_id AND s.policy_version = schedule_version
                          AND ((override_work AND s.interval_kind = 'OVERRIDE_WORK' AND s.service_date = i.service_date)
                               OR (NOT override_work AND s.interval_kind = 'WORK' AND s.weekday = local_weekday))
                          AND s.starts_at <= local_now::time AND local_now::time < s.ends_at)
                    OR EXISTS (SELECT 1 FROM public.clinic_schedule_intervals s
                        WHERE s.organization_id = i.organization_id AND s.policy_version = schedule_version
                          AND s.interval_kind = 'BREAK' AND s.weekday = local_weekday
                          AND s.starts_at <= local_now::time AND local_now::time < s.ends_at) THEN
                    RAISE EXCEPTION 'Urgent intake operating period denied' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION public.f01_guard_intake_price()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE i record;
            price jsonb;
            base_key text;
        BEGIN
            SELECT * INTO i FROM public.f01_intake_authority();
            IF NOT FOUND THEN RETURN NEW; END IF;
            SELECT v.payload INTO price FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                ON (v.organization_id,v.kind,v.version) = (h.organization_id,h.kind,h.current_version)
            WHERE h.organization_id = i.organization_id AND h.kind = 'PRICE'
                AND h.revision = (i.command->>'expected_price_revision')::bigint
                AND h.current_version = NEW.pricing_version;
            base_key := CASE WHEN i.purpose = 'INTAKE_URGENT' THEN 'urgent_base_egp'
                WHEN NEW.service_type = 'FOLLOW_UP' THEN 'preferential_followup_egp' ELSE 'standard_new_exam_egp' END;
            IF NOT FOUND OR i.stage <> 'PRIMARY' OR i.appointment_id IS DISTINCT FROM NEW.appointment_id
                OR i.organization_id <> NEW.organization_id OR i.classification <> NEW.classification
                OR NEW.creation_standard_base_egp IS DISTINCT FROM (price->>'standard_new_exam_egp')::numeric
                OR NEW.base_egp IS DISTINCT FROM (price->>base_key)::numeric
                OR NEW.base_egp IS DISTINCT FROM (i.command->>'expected_base_egp')::numeric
                OR NEW.net_egp IS DISTINCT FROM (i.command->>'expected_net_egp')::numeric
                OR NEW.discount_type IS DISTINCT FROM i.command->'discount'->>'discount_type'
                OR NEW.discount_reason IS DISTINCT FROM i.command->'discount'->>'discount_reason'
                OR (CASE WHEN NEW.discount_type = 'PERCENT' THEN
                        NEW.discount_percent_text IS DISTINCT FROM i.command->'discount'->>'discount_value'
                     ELSE NEW.discount_value IS DISTINCT FROM (i.command->'discount'->>'discount_value')::numeric END)
                OR (NEW.service_type = 'FOLLOW_UP' AND NOT EXISTS (
                    SELECT 1 FROM public.clinic_policy_heads h WHERE h.organization_id = i.organization_id
                        AND h.kind = 'FOLLOWUP' AND h.revision = (i.command->>'expected_followup_revision')::bigint))
                OR (i.purpose = 'INTAKE_URGENT' AND price->'urgent_enabled' IS DISTINCT FROM to_jsonb(true)) THEN
                RAISE EXCEPTION 'Intake signed price scope denied' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION public.f01_guard_intake_rank()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE i record;
            expected_rank bigint;
        BEGIN
            SELECT * INTO i FROM public.f01_intake_authority();
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF i.purpose = 'INTAKE_ROUTINE' THEN
                SELECT coalesce(max(e.routine_rank),0) + 1 INTO expected_rank
                FROM public.queue_entries e WHERE e.organization_id = i.organization_id
                    AND e.queue_day_id = i.day_id AND e.disposition = 'ACTIVE';
                IF NEW.routine_rank IS DISTINCT FROM expected_rank THEN
                    RAISE EXCEPTION 'New Routine arrival rank denied' USING ERRCODE = '42501';
                END IF;
            ELSIF NEW.routine_rank IS NOT NULL THEN
                RAISE EXCEPTION 'Non-Routine arrival rank denied' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION public.f01_guard_intake_evidence()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE i record;
            appt public.appointments%ROWTYPE;
            entry public.queue_entries%ROWTYPE;
            day public.queue_days%ROWTYPE;
            price public.appointment_price_snapshots%ROWTYPE;
            evidence jsonb;
            expected_projection jsonb;
            live_position bigint;
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NEW; END IF;
            SELECT * INTO i FROM public.f01_intake_authority();
            IF NOT FOUND OR i.stage <> 'PRIMARY' OR i.entry_id IS NULL
                OR i.organization_id IS DISTINCT FROM NEW.organization_id THEN
                RAISE EXCEPTION 'Intake evidence scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO appt FROM public.appointments a WHERE a.organization_id = i.organization_id
                AND a.id = i.appointment_id AND a.patient_id = i.patient_id AND a.status = 'Waiting'
                AND a.revision = 0 AND a.current_number = i.allocated_number;
            IF NOT FOUND THEN RAISE EXCEPTION 'Intake appointment evidence denied' USING ERRCODE = '42501'; END IF;
            SELECT * INTO entry FROM public.queue_entries e WHERE e.organization_id = i.organization_id
                AND e.id = i.entry_id AND e.appointment_id = appt.id AND e.queue_day_id = i.day_id
                AND e.disposition = 'ACTIVE' AND e.attested_by_staff_id = i.staff_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'Intake arrival evidence denied' USING ERRCODE = '42501'; END IF;
            SELECT * INTO day FROM public.queue_days q WHERE q.organization_id = i.organization_id
                AND q.id = i.day_id AND q.revision_transaction_id = pg_current_xact_id()
                AND q.arrival_counter = entry.arrival_sequence;
            IF NOT FOUND THEN RAISE EXCEPTION 'Intake queue evidence denied' USING ERRCODE = '42501'; END IF;
            evidence := jsonb_build_object('appointment_id', appt.id, 'queue_entry_id', entry.id,
                'doctor_staff_id', appt.doctor_staff_id, 'service_date', appt.service_date,
                'lane', appt.lane, 'classification', appt.classification, 'arrival_sequence', entry.arrival_sequence);
            IF TG_TABLE_NAME = 'sensitive_audit' THEN
                IF NEW.action IS DISTINCT FROM i.operation OR NEW.actor_staff_id IS DISTINCT FROM i.staff_id
                    OR NEW.actor_role IS DISTINCT FROM i.role OR NEW.correlation_id IS DISTINCT FROM appt.id::text
                    OR NEW.operator_id IS NOT NULL OR NEW.reason IS NOT NULL OR NEW.recovery_operation_id IS NOT NULL
                    OR NEW.before_state IS NOT NULL OR NEW.after_state IS DISTINCT FROM
                        evidence || jsonb_build_object('status','Waiting','revision',0) THEN
                    RAISE EXCEPTION 'Intake audit evidence denied' USING ERRCODE = '42501';
                END IF;
            ELSIF TG_TABLE_NAME = 'outbox_events' THEN
                IF NEW.event_type IS DISTINCT FROM 'APPOINTMENT_INTAKE' OR NEW.aggregate_type IS DISTINCT FROM 'APPOINTMENT'
                    OR NEW.aggregate_id IS DISTINCT FROM appt.id::text OR NEW.payload IS DISTINCT FROM evidence THEN
                    RAISE EXCEPTION 'Intake outbox evidence denied' USING ERRCODE = '42501';
                END IF;
            ELSE
                SELECT * INTO price FROM public.appointment_price_snapshots p WHERE p.organization_id = i.organization_id
                    AND p.appointment_id = appt.id;
                IF NOT FOUND THEN RAISE EXCEPTION 'Intake price evidence denied' USING ERRCODE = '42501'; END IF;
                SELECT count(*) INTO live_position FROM public.queue_entries e JOIN public.appointments a
                    ON (a.organization_id,a.id) = (e.organization_id,e.appointment_id)
                WHERE e.organization_id = i.organization_id AND e.queue_day_id = day.id
                    AND e.disposition = 'ACTIVE' AND a.status = 'Waiting' AND (
                        (appt.lane = 'EMERGENCY' AND e.arrival_sequence <= entry.arrival_sequence)
                        OR (appt.lane = 'MAIN' AND a.classification = 'URGENT' AND
                            (appt.classification = 'ROUTINE' OR e.arrival_sequence <= entry.arrival_sequence))
                        OR (appt.classification = 'ROUTINE' AND e.routine_rank <= entry.routine_rank));
                expected_projection := jsonb_build_object(
                    'id', appt.id, 'patient_id', appt.patient_id, 'doctor_staff_id', appt.doctor_staff_id,
                    'service_date', appt.service_date, 'status', appt.status, 'service_type', appt.service_type,
                    'classification', appt.classification, 'lane', appt.lane, 'booking_mode', appt.booking_mode,
                    'number', appt.current_number, 'revision', appt.revision,
                    'base_egp', round(price.base_egp,2)::text, 'discount_type', price.discount_type,
                    'discount_value', CASE WHEN price.discount_type = 'PERCENT' THEN price.discount_percent_text
                        ELSE round(price.discount_value,2)::text END,
                    'discount_reason', price.discount_reason, 'is_free', price.is_free, 'net_egp', round(price.net_egp,2)::text,
                    'pricing_version', price.pricing_version, 'creation_standard_base_egp', round(price.creation_standard_base_egp,2)::text,
                    'history', jsonb_build_array(jsonb_build_object('action','CREATED','old_status',NULL,
                        'new_status','Waiting','old_service_date',NULL,'new_service_date',appt.service_date,
                        'old_number',NULL,'new_number',appt.current_number,'appointment_revision',0,'reason',NULL)),
                    'queue_entry_id', entry.id, 'queue_day_id', day.id, 'arrival_sequence', entry.arrival_sequence,
                    'routine_rank', entry.routine_rank, 'position', live_position, 'attested_by_staff_id', entry.attested_by_staff_id,
                    'queue_day_revision', day.revision, 'queue_entry_revision', entry.revision,
                    'queue_policy_version', day.queue_policy_version, 'routine_cursor', day.routine_cursor);
                IF NEW.operation IS DISTINCT FROM i.operation OR NEW.scope_kind IS DISTINCT FROM 'TENANT'
                    OR NEW.actor_kind IS DISTINCT FROM 'STAFF' OR NEW.scope_id IS DISTINCT FROM i.organization_id
                    OR NEW.staff_id IS DISTINCT FROM i.staff_id OR NEW.actor_id IS DISTINCT FROM i.staff_id
                    OR NEW.idempotency_key IS DISTINCT FROM i.idempotency_key OR NEW.request_digest IS DISTINCT FROM i.digest
                    OR NEW.result_revision IS DISTINCT FROM 0
                    OR NOT (NEW.result_projection ?& ARRAY['attested_at','starts_at','ends_at'])
                    OR jsonb_typeof(NEW.result_projection->'attested_at') IS DISTINCT FROM 'string'
                    OR (NEW.result_projection->>'attested_at')::timestamptz IS DISTINCT FROM entry.attested_at
                    OR coalesce(jsonb_typeof(NEW.result_projection->'starts_at'),'missing') NOT IN ('string','null')
                    OR coalesce(jsonb_typeof(NEW.result_projection->'ends_at'),'missing') NOT IN ('string','null')
                    OR (NEW.result_projection->>'starts_at')::timestamptz IS DISTINCT FROM appt.starts_at
                    OR (NEW.result_projection->>'ends_at')::timestamptz IS DISTINCT FROM appt.ends_at
                    OR NEW.result_projection - ARRAY['attested_at','starts_at','ends_at'] IS DISTINCT FROM expected_projection THEN
                    RAISE EXCEPTION 'Intake receipt evidence denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        EXCEPTION WHEN data_exception THEN
            RAISE EXCEPTION 'Intake evidence invalid' USING ERRCODE = '42501';
        END $$;

        CREATE TRIGGER f01_intake_counter_guard BEFORE INSERT OR UPDATE ON public.appointment_day_counters
            FOR EACH ROW EXECUTE FUNCTION public.f01_guard_intake_counter();
        CREATE TRIGGER f01_intake_appointment_guard BEFORE INSERT ON public.appointments
            FOR EACH ROW EXECUTE FUNCTION public.f01_guard_intake_appointment();
        CREATE TRIGGER f01_intake_price_guard BEFORE INSERT ON public.appointment_price_snapshots
            FOR EACH ROW EXECUTE FUNCTION public.f01_guard_intake_price();
        CREATE TRIGGER f01_intake_rank_guard BEFORE INSERT ON public.queue_entries
            FOR EACH ROW EXECUTE FUNCTION public.f01_guard_intake_rank();
        CREATE TRIGGER f01_record_intake_number AFTER INSERT OR UPDATE ON public.appointment_day_counters
            FOR EACH ROW EXECUTE FUNCTION public.f01_record_intake_progress();
        CREATE TRIGGER f01_record_intake_day AFTER INSERT OR UPDATE ON public.queue_days
            FOR EACH ROW EXECUTE FUNCTION public.f01_record_intake_progress();
        CREATE TRIGGER f01_record_intake_entry AFTER INSERT ON public.queue_entries
            FOR EACH ROW EXECUTE FUNCTION public.f01_record_intake_progress();
        REVOKE ALL ON FUNCTION public.f01_guard_intake_counter(), public.f01_guard_intake_appointment(),
            public.f01_guard_intake_price(), public.f01_guard_intake_rank(),
            public.f01_guard_intake_evidence() FROM PUBLIC;
    """)
    for table, condition in (
        (
            "sensitive_audit",
            "NEW.action IN ('appointments.intake_routine','appointments.intake_emergency','appointments.intake_urgent')",
        ),
        ("outbox_events", "NEW.event_type = 'APPOINTMENT_INTAKE'"),
        (
            "idempotency_receipts",
            "NEW.operation IN ('appointments.intake_routine','appointments.intake_emergency','appointments.intake_urgent')",
        ),
    ):
        op.execute(f"""
            CREATE TRIGGER f01_guard_intake_evidence BEFORE INSERT ON public.{table}
                FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION public.f01_guard_intake_evidence();
            CREATE TRIGGER f01_record_intake_evidence AFTER INSERT ON public.{table}
                FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION public.f01_record_intake_progress();
        """)


def downgrade() -> None:
    raise RuntimeError("B-11 intake authority migration is forward-only")
