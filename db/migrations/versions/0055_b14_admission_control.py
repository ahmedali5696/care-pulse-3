"""B-14: signed admission controls and exact-case confirmation authority."""

from alembic import op

revision = "0055_b14_admission_control"
down_revision = "0054_b12_receipt_scope"
branch_labels = None
depends_on = None


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b14_body jsonb,
            ADD COLUMN b14_digest text, ADD COLUMN b14_target_id uuid;
    """)
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('f01_private.route_purpose(text,text)'::regprocedure)"
    ).scalar_one()
    marker = "RETURN CASE"
    if definition.count(marker) != 1:
        raise RuntimeError("Missing unique B-14 route marker")
    connection.exec_driver_sql(
        definition.replace(
            marker,
            """RETURN CASE
                WHEN p_method = 'GET' AND p_path = '/v1/settings/admission'
                    THEN 'ADMISSION_SETTINGS_READ'
                WHEN p_method = 'PATCH' AND p_path = '/v1/settings/admission'
                    THEN 'ADMISSION_MODE_UPDATE'
                WHEN p_method = 'PATCH' AND p_path = '/v1/queue/capacity'
                    THEN 'QUEUE_CAPACITY_UPDATE'
                WHEN p_method = 'POST' AND p_path = '/v1/queue/pause'
                    THEN 'QUEUE_PAUSE'
                WHEN p_method = 'POST' AND p_path = '/v1/queue/admission-confirmations'
                    THEN 'QUEUE_CONFIRMATION_ISSUE'
                WHEN p_method = 'GET' AND p_path = '/v1/queue/admission-confirmations/usable'
                    THEN 'QUEUE_CONFIRMATION_USABLE'
                WHEN p_method = 'POST' AND p_path ~
                    ('^/v1/queue/admission-confirmations/' || uuid_pattern || '/revoke$')
                    THEN 'QUEUE_CONFIRMATION_REVOKE'""",
        ),
        execution_options={"no_parameters": True},
    )
    op.execute(r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b14;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b14(bytea,text,bytea,bytea)
            FROM PUBLIC, carepulse_gate;

        CREATE FUNCTION f01_private.b14_operation(purpose text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
            SELECT CASE purpose
                WHEN 'ADMISSION_MODE_UPDATE' THEN 'settings.admission.update'
                WHEN 'QUEUE_CAPACITY_UPDATE' THEN 'queue.capacity'
                WHEN 'QUEUE_PAUSE' THEN 'queue.pause'
                WHEN 'QUEUE_CONFIRMATION_ISSUE' THEN 'queue.confirmation.issue'
                WHEN 'QUEUE_CONFIRMATION_REVOKE' THEN 'queue.confirmation.revoke'
            END;
        $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea, key_id text, mac bytea, body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            request_id bigint;
            req f01_private.requests%ROWTYPE;
            command jsonb;
            raw json;
            fields text[];
            target_id uuid;
            operation text;
            digest text;
            target jsonb := '{}'::jsonb;
            service_day date;
            uuid_pattern constant text := '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}';
            reason_whitespace constant text :=
                U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
                || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
        BEGIN
            request_id := public.f01_register_proof_before_b14(payload, key_id, mac, body);
            SELECT * INTO req FROM f01_private.requests r WHERE r.id = request_id;
            operation := f01_private.b14_operation(req.purpose);
            IF operation IS NULL AND req.purpose NOT IN
                ('ADMISSION_SETTINGS_READ','QUEUE_CONFIRMATION_USABLE') THEN
                RETURN request_id;
            END IF;
            BEGIN
                IF req.method = 'GET' THEN
                    IF octet_length(body) <> 0 THEN
                        RAISE EXCEPTION 'Invalid B-14 read body' USING ERRCODE = '22023';
                    END IF;
                    IF req.purpose = 'ADMISSION_SETTINGS_READ' THEN
                        IF req.query <> '' THEN
                            RAISE EXCEPTION 'Invalid B-14 settings query' USING ERRCODE = '22023';
                        END IF;
                        command := '{}'::jsonb;
                    ELSE
                        IF req.query !~ ('^appointment_id=' || uuid_pattern ||
                            '&date=[0-9]{4}-[0-9]{2}-[0-9]{2}&lane=(MAIN|EMERGENCY)$') THEN
                            RAISE EXCEPTION 'Invalid B-14 case query' USING ERRCODE = '22023';
                        END IF;
                        target_id := split_part(split_part(req.query,'&',1),'=',2)::uuid;
                        service_day := split_part(split_part(req.query,'&',2),'=',2)::date;
                        command := jsonb_build_object('appointment_id',target_id,
                            'date',service_day,'lane',split_part(split_part(req.query,'&',3),'=',2));
                    END IF;
                ELSE
                    IF req.query <> '' OR req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                        RAISE EXCEPTION 'Invalid B-14 mutation envelope' USING ERRCODE = '22023';
                    END IF;
                    raw := convert_from(body, 'UTF8')::json;
                    PERFORM f01_private.canonical_intake_json(raw);
                    command := raw::jsonb;
                    fields := CASE req.purpose
                        WHEN 'ADMISSION_MODE_UPDATE' THEN ARRAY['mode','expected_admission_revision']
                        WHEN 'QUEUE_CAPACITY_UPDATE' THEN ARRAY['capacity','expected_capacity_revision',
                            'expected_control_revision']
                        WHEN 'QUEUE_PAUSE' THEN ARRAY['paused','expected_control_revision']
                        WHEN 'QUEUE_CONFIRMATION_ISSUE' THEN ARRAY['appointment_id','patient_id','date','lane',
                            'expected_appointment_revision','expected_queue_day_revision','expected_control_revision',
                            'expected_capacity_revision','expected_admission_revision']
                        WHEN 'QUEUE_CONFIRMATION_REVOKE' THEN ARRAY['expected_control_revision','revoke_reason']
                    END;
                    IF jsonb_typeof(command) IS DISTINCT FROM 'object'
                        OR NOT command ?& fields
                        OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k WHERE k <> ALL(fields))
                        OR EXISTS (SELECT 1 FROM json_each(raw) e
                            WHERE e.key LIKE 'expected_%_revision'
                              AND (json_typeof(e.value) IS DISTINCT FROM 'number'
                                OR e.value::text !~ '^(0|[1-9][0-9]{0,18})$'
                                OR NOT pg_input_is_valid(e.value::text,'bigint')
                                OR (e.key <> 'expected_appointment_revision' AND e.value::text = '0'))) THEN
                        RAISE EXCEPTION 'Invalid B-14 fields' USING ERRCODE = '22023';
                    END IF;
                    IF req.purpose = 'ADMISSION_MODE_UPDATE' THEN
                        IF jsonb_typeof(command->'mode') IS DISTINCT FROM 'string'
                            OR command->>'mode' NOT IN ('BOTH','DOCTOR','RECEPTIONIST') THEN
                            RAISE EXCEPTION 'Invalid admission mode' USING ERRCODE = '22023';
                        END IF;
                    ELSIF req.purpose = 'QUEUE_CAPACITY_UPDATE' THEN
                        IF jsonb_typeof(command->'capacity') IS DISTINCT FROM 'number'
                            OR (raw->'capacity')::text !~ '^[1-9][0-9]{0,9}$'
                            OR NOT pg_input_is_valid(command->>'capacity','integer') THEN
                            RAISE EXCEPTION 'Invalid capacity' USING ERRCODE = '22023';
                        END IF;
                    ELSIF req.purpose = 'QUEUE_PAUSE' THEN
                        IF jsonb_typeof(command->'paused') IS DISTINCT FROM 'boolean' THEN
                            RAISE EXCEPTION 'Invalid Pause state' USING ERRCODE = '22023';
                        END IF;
                    ELSIF req.purpose = 'QUEUE_CONFIRMATION_ISSUE' THEN
                        IF jsonb_typeof(command->'appointment_id') IS DISTINCT FROM 'string'
                            OR jsonb_typeof(command->'patient_id') IS DISTINCT FROM 'string'
                            OR jsonb_typeof(command->'date') IS DISTINCT FROM 'string'
                            OR command->>'date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                            OR jsonb_typeof(command->'lane') IS DISTINCT FROM 'string'
                            OR command->>'lane' NOT IN ('MAIN','EMERGENCY') THEN
                            RAISE EXCEPTION 'Invalid signed case' USING ERRCODE = '22023';
                        END IF;
                        target_id := (command->>'appointment_id')::uuid;
                        service_day := (command->>'date')::date;
                        command := jsonb_set(command,'{appointment_id}',to_jsonb(target_id));
                        command := jsonb_set(command,'{patient_id}',to_jsonb((command->>'patient_id')::uuid));
                        command := jsonb_set(command,'{date}',to_jsonb(service_day));
                    ELSE
                        target_id := split_part(req.path,'/',5)::uuid;
                        IF jsonb_typeof(command->'revoke_reason') IS DISTINCT FROM 'string'
                            OR btrim(command->>'revoke_reason',reason_whitespace) = ''
                            OR command->>'revoke_reason' ~ '[\x01-\x1f]' THEN
                            RAISE EXCEPTION 'Invalid revocation reason' USING ERRCODE = '22023';
                        END IF;
                        command := jsonb_set(command,'{revoke_reason}',
                            to_jsonb(btrim(command->>'revoke_reason',reason_whitespace)));
                        target := jsonb_build_object('confirmation_id',target_id);
                    END IF;
                    digest := encode(sha256(convert_to(f01_private.canonical_intake_json(
                        jsonb_build_object('operation',operation,'target',target,'body',command)::json),
                        'UTF8')),'hex');
                END IF;
                UPDATE f01_private.requests r SET b14_body = command, b14_digest = digest,
                    b14_target_id = target_id WHERE r.id = request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid = false WHERE r.id = request_id;
            END;
            RETURN request_id;
        END $$;
        REVOKE ALL ON FUNCTION f01_private.b14_operation(text),
            public.f01_register_proof(bytea,text,bytea,bytea) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
    """)
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.f01_bind(bigint,text,integer,xid8)'::regprocedure)"
    ).scalar_one()
    marker = "OR req.purpose = 'VISIT_FOLLOWUP'"
    if definition.count(marker) != 1:
        raise RuntimeError("Missing unique B-14 recovery marker")
    connection.exec_driver_sql(
        definition.replace(
            marker,
            marker
            + """
                           OR (f01_private.b14_operation(req.purpose) IS NOT NULL
                               AND req.b14_digest IS NOT NULL)""",
        ),
        execution_options={"no_parameters": True},
    )
    op.execute("""
        CREATE FUNCTION public.f01_b14_authority()
        RETURNS TABLE (organization_id uuid, staff_id uuid, role text, security_revision bigint,
            purpose text, stage text, operation text, idempotency_key text, command jsonb,
            digest text, target_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path = pg_catalog, pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,r.security_revision,r.purpose,c.stage,
                f01_private.b14_operation(r.purpose),r.idempotency_key,r.b14_body,
                r.b14_digest,r.b14_target_id
            FROM public.f01_context() c
            JOIN f01_private.requests r ON r.id = c.request_id
            JOIN public.staff_accounts s ON s.organization_id = r.organization_id AND s.id = r.staff_id
            WHERE r.b14_body IS NOT NULL AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL AND s.role = r.role
              AND s.security_revision = r.security_revision
              AND (
                  (r.purpose IN ('ADMISSION_SETTINGS_READ','ADMISSION_MODE_UPDATE')
                    AND (r.role = 'DOCTOR_OWNER' OR (r.role = 'RECEPTIONIST' AND s.operational_manager)))
                  OR (r.purpose IN ('QUEUE_CAPACITY_UPDATE','QUEUE_PAUSE',
                      'QUEUE_CONFIRMATION_ISSUE','QUEUE_CONFIRMATION_REVOKE') AND r.role = 'DOCTOR_OWNER')
                  OR (r.purpose = 'QUEUE_CONFIRMATION_USABLE' AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST'))
              )
              AND (c.stage = 'PRIMARY' OR (c.stage = 'RECEIPT_RECOVERY' AND r.b14_digest IS NOT NULL));
        $$;
        REVOKE ALL ON FUNCTION public.f01_b14_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b14_authority() TO carepulse_migration;
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)


def _confirmation_authority() -> None:
    # B-14 obtains both instants from one server-clock reading inside its
    # guarded writer. Preserve the inherited clock assignment for other paths.
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('public.guard_admission_confirmation()'::regprocedure)"
    ).scalar_one()
    marker = "NEW.issued_at := clock_timestamp();"
    if definition.count(marker) != 1:
        raise RuntimeError("Missing unique confirmation clock marker")
    connection.exec_driver_sql(
        definition.replace(
            marker,
            """IF NOT EXISTS (SELECT 1 FROM public.f01_context() c
                        WHERE c.purpose='QUEUE_CONFIRMATION_ISSUE' AND c.stage='PRIMARY') THEN
                        NEW.issued_at := clock_timestamp();
                    END IF;""",
        ),
        execution_options={"no_parameters": True},
    )
    op.execute("""
        ALTER TABLE public.doctor_admission_confirmations
            ADD COLUMN issuer_security_revision bigint CHECK (issuer_security_revision >= 0);

        CREATE FUNCTION public.f01_b14_issuer_snapshot() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                SELECT s.security_revision INTO NEW.issuer_security_revision
                FROM public.staff_accounts s
                WHERE s.organization_id = NEW.organization_id
                    AND s.id = NEW.issuing_doctor_staff_id;
            ELSIF NEW.issuer_security_revision IS DISTINCT FROM OLD.issuer_security_revision THEN
                RAISE EXCEPTION 'Issuer security evidence is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER f01_b14_issuer_snapshot
            BEFORE INSERT OR UPDATE ON public.doctor_admission_confirmations
            FOR EACH ROW EXECUTE FUNCTION public.f01_b14_issuer_snapshot();

        CREATE FUNCTION public.f01_b14_valid_case(p_org uuid, p_appt uuid, p_date date, p_lane text)
        RETURNS SETOF public.doctor_admission_confirmations
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT f.*
            FROM public.doctor_admission_confirmations f
            JOIN public.staff_accounts s ON s.organization_id=f.organization_id
                AND s.id=f.issuing_doctor_staff_id
            JOIN public."user" u ON u.id=s.auth_user_id
            JOIN public.appointments a ON a.organization_id=f.organization_id
                AND a.id=f.appointment_id AND a.patient_id=f.patient_id
                AND a.doctor_staff_id=f.doctor_staff_id AND a.service_date=f.service_date
                AND a.lane=f.lane AND a.revision=f.appointment_revision
            JOIN public.queue_days q ON q.organization_id=f.organization_id
                AND q.doctor_staff_id=f.doctor_staff_id AND q.service_date=f.service_date
                AND q.lane=f.lane AND q.revision=f.queue_day_revision
            JOIN public.doctor_controls c ON c.organization_id=f.organization_id
                AND c.doctor_staff_id=f.doctor_staff_id AND c.revision=f.control_revision
            JOIN public.clinic_policy_heads h ON h.organization_id=f.organization_id
                AND h.kind='CAPACITY' AND h.revision=f.capacity_revision
                AND h.current_version=c.capacity_policy_version
            JOIN public.clinic_policy_heads m ON m.organization_id=f.organization_id
                AND m.kind='ADMISSION' AND m.revision=f.admission_revision
            WHERE f.organization_id=p_org AND f.appointment_id=p_appt
                AND f.service_date=p_date AND f.lane=p_lane
                AND f.state='ISSUED' AND clock_timestamp()<f.expires_at
                AND f.issuing_doctor_staff_id=f.doctor_staff_id
                AND s.role='DOCTOR_OWNER' AND s.enabled AND s.verified
                AND s.reset_block_operation_id IS NULL
                AND s.security_revision=f.issuer_security_revision
                AND u."emailVerified" AND u.email=s.verified_email
                AND a.status='Waiting'
                AND EXISTS (
                    SELECT 1 FROM public.queue_entries e
                    WHERE e.organization_id=f.organization_id AND e.queue_day_id=q.id
                        AND e.appointment_id=f.appointment_id AND e.patient_id=f.patient_id
                        AND e.doctor_staff_id=f.doctor_staff_id AND e.service_date=f.service_date
                        AND e.lane=f.lane AND e.disposition='ACTIVE'
                );
        $$;

        CREATE FUNCTION public.f01_b14_case_projection(f public.doctor_admission_confirmations)
        RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC' AS $$
            SELECT jsonb_build_object('id',f.id,'expires_at',f.expires_at,
                'appointment_revision',f.appointment_revision,'queue_day_revision',f.queue_day_revision,
                'control_revision',f.control_revision,'capacity_revision',f.capacity_revision,
                'admission_revision',f.admission_revision);
        $$;

        CREATE FUNCTION public.f01_b14_settings() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE a record; result jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b14_authority();
            IF NOT FOUND OR a.purpose <> 'ADMISSION_SETTINGS_READ' OR a.stage <> 'PRIMARY' THEN
                RAISE EXCEPTION 'B-14 settings scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT jsonb_build_object('projection',jsonb_build_object('mode',v.payload->'actors',
                'admission_revision',h.revision),'revision',h.revision) INTO result
            FROM public.clinic_policy_heads h
            JOIN public.clinic_policy_versions v ON v.organization_id=h.organization_id
                AND v.kind=h.kind AND v.version=h.current_version
            WHERE h.organization_id=a.organization_id AND h.kind='ADMISSION';
            RETURN result;
        END $$;

        CREATE FUNCTION public.f01_b14_usable() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE a record; result jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b14_authority();
            IF NOT FOUND OR a.purpose <> 'QUEUE_CONFIRMATION_USABLE' OR a.stage <> 'PRIMARY' THEN
                RAISE EXCEPTION 'B-14 usable scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT jsonb_build_object('projection',public.f01_b14_case_projection(f),
                'revision',f.appointment_revision) INTO result
            FROM public.f01_b14_valid_case(a.organization_id,a.target_id,
                (a.command->>'date')::date,a.command->>'lane') f
            JOIN public.clinic_policy_heads h ON h.organization_id=f.organization_id
                AND h.kind='CAPACITY' AND h.revision=f.capacity_revision
            JOIN public.clinic_policy_versions v ON v.organization_id=h.organization_id
                AND v.kind=h.kind AND v.version=h.current_version
            JOIN public.clinic_policy_heads m ON m.organization_id=f.organization_id
                AND m.kind='ADMISSION' AND m.revision=f.admission_revision
            JOIN public.clinic_policy_versions mv ON mv.organization_id=m.organization_id
                AND mv.kind=m.kind AND mv.version=m.current_version
            WHERE NOT (v.payload->>'paused')::boolean
                AND (mv.payload->>'actors'='BOTH'
                    OR (mv.payload->>'actors'='DOCTOR' AND a.role='DOCTOR_OWNER')
                    OR (mv.payload->>'actors'='RECEPTIONIST' AND a.role='RECEPTIONIST'))
                AND (SELECT count(*) FROM public.appointments x
                    WHERE x.organization_id=f.organization_id AND x.doctor_staff_id=f.doctor_staff_id
                        AND x.status='In Examination')
                    BETWEEN 1 AND (v.payload->>'capacity')::numeric-1
            ORDER BY f.issued_at DESC LIMIT 1;
            RETURN result;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b14_issuer_snapshot(),
            public.f01_b14_valid_case(uuid,uuid,date,text),
            public.f01_b14_case_projection(public.doctor_admission_confirmations),
            public.f01_b14_settings(),public.f01_b14_usable() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b14_settings(),public.f01_b14_usable() TO carepulse_api;
    """)


def _receipts() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b14_read_receipt() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC' AS $$
        DECLARE a record; r public.idempotency_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b14_authority();
            IF NOT FOUND OR a.operation IS NULL OR a.digest IS NULL THEN
                RAISE EXCEPTION 'B-14 receipt scope denied' USING ERRCODE = '42501';
            END IF;
            IF a.purpose='QUEUE_CONFIRMATION_ISSUE' AND NOT EXISTS (
                SELECT 1 FROM public.appointments x WHERE x.organization_id=a.organization_id
                    AND x.id=a.target_id AND x.doctor_staff_id=a.staff_id
                    AND x.patient_id=(a.command->>'patient_id')::uuid
            ) THEN
                RAISE EXCEPTION 'B-14 case not found' USING ERRCODE = 'P0002';
            ELSIF a.purpose='QUEUE_CONFIRMATION_REVOKE' AND NOT EXISTS (
                SELECT 1 FROM public.doctor_admission_confirmations f
                WHERE f.organization_id=a.organization_id AND f.id=a.target_id
                    AND f.issuing_doctor_staff_id=a.staff_id
            ) THEN
                RAISE EXCEPTION 'B-14 case not found' USING ERRCODE = 'P0002';
            END IF;
            SELECT * INTO r FROM public.idempotency_receipts x
            WHERE x.scope_kind='TENANT' AND x.scope_id=a.organization_id
                AND x.organization_id=a.organization_id AND x.actor_kind='STAFF'
                AND x.actor_id=a.staff_id AND x.staff_id=a.staff_id
                AND x.operation=a.operation AND x.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'B-14 idempotency conflict' USING ERRCODE = 'CP001';
            END IF;
            IF a.purpose='QUEUE_CONFIRMATION_ISSUE' AND NOT EXISTS (
                SELECT 1 FROM public.doctor_admission_confirmations f
                WHERE f.organization_id=a.organization_id AND f.id=(r.result_projection->>'id')::uuid
                    AND f.appointment_id=a.target_id AND f.issuing_doctor_staff_id=a.staff_id
                    AND f.patient_id=(a.command->>'patient_id')::uuid
                    AND f.service_date=(a.command->>'date')::date AND f.lane=a.command->>'lane'
                    AND r.result_projection=public.f01_b14_case_projection(f)
                        || jsonb_build_object('state','ISSUED','issued_at',f.issued_at)
            ) THEN
                RAISE EXCEPTION 'B-14 receipt target mismatch' USING ERRCODE = '42501';
            ELSIF a.purpose='QUEUE_CONFIRMATION_REVOKE' AND NOT EXISTS (
                SELECT 1 FROM public.doctor_admission_confirmations f
                WHERE f.organization_id=a.organization_id AND f.id=a.target_id
                    AND f.issuing_doctor_staff_id=a.staff_id AND f.state='REVOKED'
                    AND r.result_projection=jsonb_build_object('id',f.id,'state',f.state,
                        'revoked_at',f.revoked_at,'control_revision',r.result_revision)
            ) THEN
                RAISE EXCEPTION 'B-14 receipt target mismatch' USING ERRCODE = '42501';
            END IF;
            RETURN jsonb_build_object('projection',r.result_projection,
                'revision',r.result_revision,'replayed',true);
        END $$;

        CREATE FUNCTION public.f01_b14_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.f01_b14_authority() a
                WHERE a.stage='RECEIPT_RECOVERY' AND a.operation IS NOT NULL) THEN
                RAISE EXCEPTION 'B-14 recovery scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN public.f01_b14_read_receipt();
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b14_read_receipt(),public.f01_b14_replay() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b14_replay() TO carepulse_api;
    """)


def _mutations() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b14_mutate(p_ttl_minutes integer) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC' AS $$
        DECLARE
            a record;
            h public.clinic_policy_heads%ROWTYPE;
            m public.clinic_policy_heads%ROWTYPE;
            c public.doctor_controls%ROWTYPE;
            q public.queue_days%ROWTYPE;
            x public.appointments%ROWTYPE;
            f public.doctor_admission_confirmations%ROWTYPE;
            capacity_payload jsonb;
            admission_payload jsonb;
            next_payload jsonb;
            projection jsonb;
            before_state jsonb;
            replay jsonb;
            revision bigint;
            active_count bigint;
            desired_capacity integer;
            overlap_allowed boolean;
            issue_clock timestamptz;
            event_name text;
            aggregate_kind text := 'DOCTOR_CONTROL';
            aggregate_id uuid;
            correlation_id uuid;
        BEGIN
            SELECT * INTO a FROM public.f01_b14_authority();
            IF NOT FOUND OR a.stage <> 'PRIMARY' OR a.operation IS NULL OR a.digest IS NULL THEN
                RAISE EXCEPTION 'B-14 mutation scope denied' USING ERRCODE = '42501';
            END IF;
            replay := public.f01_b14_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            IF a.purpose='QUEUE_CONFIRMATION_ISSUE' THEN
                IF p_ttl_minutes IS NULL OR p_ttl_minutes<=0 THEN
                    RAISE EXCEPTION 'B-14 invalid TTL configuration' USING ERRCODE = '22023';
                END IF;
                PERFORM 1 FROM public.patients p
                WHERE p.organization_id=a.organization_id AND p.id=(a.command->>'patient_id')::uuid
                FOR SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'B-14 patient not found' USING ERRCODE = 'P0002';
                END IF;
            END IF;

            IF a.purpose='QUEUE_CAPACITY_UPDATE' THEN
                SELECT (v.payload->>'allow_overlap')::boolean INTO STRICT overlap_allowed
                FROM public.clinic_policy_heads sh
                JOIN public.clinic_policy_versions v ON v.organization_id=sh.organization_id
                    AND v.kind=sh.kind AND v.version=sh.current_version
                WHERE sh.organization_id=a.organization_id AND sh.kind='SCHEDULE' FOR UPDATE OF sh;
            END IF;
            SELECT * INTO STRICT h FROM public.clinic_policy_heads ph
            WHERE ph.organization_id=a.organization_id AND ph.kind='CAPACITY' FOR UPDATE;
            SELECT * INTO STRICT m FROM public.clinic_policy_heads ph
            WHERE ph.organization_id=a.organization_id AND ph.kind='ADMISSION' FOR UPDATE;
            SELECT * INTO STRICT c FROM public.doctor_controls dc
            WHERE dc.organization_id=a.organization_id FOR UPDATE;
            IF c.capacity_policy_version<>h.current_version THEN
                RAISE EXCEPTION 'B-14 capacity/control mismatch' USING ERRCODE = '23514';
            END IF;
            SELECT v.payload INTO STRICT capacity_payload FROM public.clinic_policy_versions v
            WHERE v.organization_id=a.organization_id AND v.kind='CAPACITY' AND v.version=h.current_version;
            SELECT v.payload INTO STRICT admission_payload FROM public.clinic_policy_versions v
            WHERE v.organization_id=a.organization_id AND v.kind='ADMISSION' AND v.version=m.current_version;
            replay := public.f01_b14_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            aggregate_id := c.doctor_staff_id;
            correlation_id := a.organization_id;

            IF a.purpose='ADMISSION_MODE_UPDATE' THEN
                IF m.revision<>(a.command->>'expected_admission_revision')::bigint THEN
                    RAISE EXCEPTION 'B-14 stale admission revision' USING ERRCODE = '40001';
                END IF;
                before_state := jsonb_build_object('mode',admission_payload->'actors','admission_revision',m.revision);
                next_payload := jsonb_set(admission_payload,'{actors}',a.command->'mode');
                INSERT INTO public.clinic_policy_versions
                    (organization_id,kind,version,payload,created_by_staff_id)
                    VALUES (a.organization_id,'ADMISSION',m.current_version+1,next_payload,a.staff_id);
                UPDATE public.clinic_policy_heads ph SET current_version=m.current_version+1,revision=m.revision+1
                    WHERE ph.organization_id=a.organization_id AND ph.kind='ADMISSION';
                revision := m.revision+1;
                projection := jsonb_build_object('mode',next_payload->'actors','admission_revision',revision);
                event_name := 'ADMISSION_MODE_CHANGED';
            ELSIF a.purpose IN ('QUEUE_CAPACITY_UPDATE','QUEUE_PAUSE') THEN
                IF c.revision<>(a.command->>'expected_control_revision')::bigint
                    OR (a.purpose='QUEUE_CAPACITY_UPDATE'
                        AND h.revision<>(a.command->>'expected_capacity_revision')::bigint) THEN
                    RAISE EXCEPTION 'B-14 stale control revision' USING ERRCODE = '40001';
                END IF;
                before_state := jsonb_build_object('capacity',(capacity_payload->>'capacity')::numeric::integer,
                    'paused',capacity_payload->'paused','capacity_revision',h.revision,'control_revision',c.revision);
                IF a.purpose='QUEUE_CAPACITY_UPDATE' THEN
                    desired_capacity := (a.command->>'capacity')::integer;
                    SELECT count(*) INTO active_count FROM public.appointments ax
                    WHERE ax.organization_id=a.organization_id AND ax.doctor_staff_id=c.doctor_staff_id
                        AND ax.status='In Examination';
                    IF desired_capacity<active_count OR (overlap_allowed AND desired_capacity<2) THEN
                        RAISE EXCEPTION 'B-14 capacity conflicts with existing policy or examinations'
                            USING ERRCODE = '23514';
                    END IF;
                    IF overlap_allowed AND EXISTS (
                        SELECT 1 FROM public.appointments candidate
                        WHERE candidate.organization_id=a.organization_id
                            AND candidate.doctor_staff_id=c.doctor_staff_id
                            AND candidate.service_date >= (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date
                            AND candidate.status IN ('Booked','Waiting','In Examination','Completed')
                            AND candidate.starts_at IS NOT NULL
                            AND (SELECT count(*) FROM public.appointments other
                                WHERE other.organization_id=candidate.organization_id
                                    AND other.doctor_staff_id=candidate.doctor_staff_id
                                    AND other.service_date=candidate.service_date
                                    AND other.status IN ('Booked','Waiting','In Examination','Completed')
                                    AND other.starts_at<=candidate.starts_at
                                    AND other.ends_at>candidate.starts_at)>desired_capacity
                    ) THEN
                        RAISE EXCEPTION 'B-14 capacity conflicts with scheduled appointments' USING ERRCODE = '23514';
                    END IF;
                    next_payload := jsonb_set(capacity_payload,'{capacity}',a.command->'capacity');
                    event_name := 'EXAMINATION_CAPACITY_CHANGED';
                ELSE
                    next_payload := jsonb_set(capacity_payload,'{paused}',a.command->'paused');
                    event_name := 'DOCTOR_PAUSE_CHANGED';
                END IF;
                INSERT INTO public.clinic_policy_versions
                    (organization_id,kind,version,payload,created_by_staff_id)
                    VALUES (a.organization_id,'CAPACITY',h.current_version+1,next_payload,a.staff_id);
                UPDATE public.clinic_policy_heads ph SET current_version=h.current_version+1,revision=h.revision+1
                    WHERE ph.organization_id=a.organization_id AND ph.kind='CAPACITY';
                UPDATE public.doctor_controls dc SET capacity_policy_version=h.current_version+1,revision=c.revision+1
                    WHERE dc.organization_id=a.organization_id AND dc.doctor_staff_id=c.doctor_staff_id;
                revision := c.revision+1;
                projection := jsonb_build_object('capacity',(next_payload->>'capacity')::numeric::integer,'paused',next_payload->'paused',
                    'capacity_revision',h.revision+1,'control_revision',revision);
            ELSIF a.purpose='QUEUE_CONFIRMATION_ISSUE' THEN
                SELECT * INTO q FROM public.queue_days qd
                WHERE qd.organization_id=a.organization_id AND qd.doctor_staff_id=a.staff_id
                    AND qd.service_date=(a.command->>'date')::date AND qd.lane=a.command->>'lane' FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'B-14 queue not found' USING ERRCODE = 'P0002'; END IF;
                SELECT * INTO x FROM public.appointments ax
                WHERE ax.organization_id=a.organization_id AND ax.id=a.target_id
                    AND ax.doctor_staff_id=a.staff_id AND ax.patient_id=(a.command->>'patient_id')::uuid
                    AND ax.service_date=q.service_date AND ax.lane=q.lane FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'B-14 case not found' USING ERRCODE = 'P0002'; END IF;
                IF x.revision<>(a.command->>'expected_appointment_revision')::bigint
                    OR q.revision<>(a.command->>'expected_queue_day_revision')::bigint
                    OR c.revision<>(a.command->>'expected_control_revision')::bigint
                    OR h.revision<>(a.command->>'expected_capacity_revision')::bigint
                    OR m.revision<>(a.command->>'expected_admission_revision')::bigint THEN
                    RAISE EXCEPTION 'B-14 stale case revision' USING ERRCODE = '40001';
                END IF;
                SELECT count(*) INTO active_count FROM public.appointments ax
                WHERE ax.organization_id=a.organization_id AND ax.doctor_staff_id=a.staff_id
                    AND ax.status='In Examination';
                IF x.status<>'Waiting' OR active_count<1 OR active_count>=(capacity_payload->>'capacity')::numeric
                    OR NOT EXISTS (SELECT 1 FROM public.queue_entries e
                        WHERE e.organization_id=a.organization_id AND e.queue_day_id=q.id
                            AND e.appointment_id=x.id AND e.patient_id=x.patient_id
                            AND e.doctor_staff_id=a.staff_id AND e.service_date=q.service_date
                            AND e.lane=q.lane AND e.disposition='ACTIVE')
                    OR EXISTS (SELECT 1 FROM public.f01_b14_valid_case(
                        a.organization_id,x.id,q.service_date,q.lane)) THEN
                    RAISE EXCEPTION 'B-14 case is not eligible for another approval' USING ERRCODE = '23514';
                END IF;
                issue_clock := clock_timestamp();
                INSERT INTO public.doctor_admission_confirmations
                    (id,organization_id,issuing_doctor_staff_id,doctor_staff_id,patient_id,appointment_id,
                    service_date,lane,issued_at,expires_at,appointment_revision,queue_day_revision,
                    control_revision,capacity_revision,admission_revision,state)
                VALUES (gen_random_uuid(),a.organization_id,a.staff_id,a.staff_id,x.patient_id,x.id,q.service_date,
                    q.lane,issue_clock,issue_clock+make_interval(mins=>p_ttl_minutes),
                    x.revision,q.revision,c.revision,h.revision,m.revision,'ISSUED')
                RETURNING * INTO f;
                projection := public.f01_b14_case_projection(f)
                    || jsonb_build_object('state',f.state,'issued_at',f.issued_at);
                revision := f.appointment_revision;
                event_name := 'DOCTOR_CONFIRMATION_ISSUED';
                aggregate_kind := 'DOCTOR_ADMISSION_CONFIRMATION';
                aggregate_id := f.id;
                correlation_id := f.id;
            ELSE
                IF c.revision<>(a.command->>'expected_control_revision')::bigint THEN
                    RAISE EXCEPTION 'B-14 stale control revision' USING ERRCODE = '40001';
                END IF;
                SELECT * INTO f FROM public.doctor_admission_confirmations cf
                WHERE cf.organization_id=a.organization_id AND cf.id=a.target_id
                    AND cf.issuing_doctor_staff_id=a.staff_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'B-14 case not found' USING ERRCODE = 'P0002'; END IF;
                IF f.state<>'ISSUED' THEN
                    RAISE EXCEPTION 'B-14 approval not unused' USING ERRCODE = '23514';
                END IF;
                before_state := jsonb_build_object('id',f.id,'state',f.state,'control_revision',c.revision);
                UPDATE public.doctor_admission_confirmations cf
                    SET state='REVOKED',revoked_by_staff_id=a.staff_id,revoked_at=clock_timestamp(),
                        revoke_reason=a.command->>'revoke_reason'
                    WHERE cf.id=f.id AND cf.organization_id=a.organization_id RETURNING * INTO f;
                revision := c.revision;
                projection := jsonb_build_object('id',f.id,'state',f.state,'revoked_at',f.revoked_at,
                    'control_revision',revision);
                event_name := 'DOCTOR_CONFIRMATION_REVOKED';
                aggregate_kind := 'DOCTOR_ADMISSION_CONFIRMATION';
                aggregate_id := f.id;
                correlation_id := f.id;
            END IF;

            INSERT INTO public.sensitive_audit
                (id,organization_id,actor_staff_id,actor_role,action,reason,before_state,after_state,correlation_id)
            VALUES (gen_random_uuid(),a.organization_id,a.staff_id,a.role,a.operation,
                a.command->>'revoke_reason',before_state,projection,correlation_id::text);
            INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
            VALUES (gen_random_uuid(),a.organization_id,event_name,aggregate_kind,aggregate_id::text,projection);
            INSERT INTO public.idempotency_receipts (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,
                staff_id,operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,
                a.staff_id,a.operation,a.idempotency_key,a.digest,projection,revision);
            RETURN jsonb_build_object('projection',projection,'revision',revision,'replayed',false);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b14_mutate(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b14_mutate(integer) TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _confirmation_authority()
    _receipts()
    _mutations()


def downgrade() -> None:
    raise RuntimeError("B-14 admission control migration is forward-only")
