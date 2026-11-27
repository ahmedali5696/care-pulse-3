"""B-13: publish future queue policy and place signed Routine cohorts."""

from alembic import op

revision = "0059_b13_queue_policy_placement"
down_revision = "0058_b15_admission_guards"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Missing unique B-13 guard marker in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b13_body jsonb,
            ADD COLUMN b13_digest text;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method='GET' AND p_path='/v1/settings/queue' THEN 'SETTINGS_QUEUE_READ'
                WHEN p_method='PATCH' AND p_path='/v1/settings/queue' THEN 'SETTINGS_QUEUE_UPDATE'
                WHEN p_method='POST' AND p_path='/v1/queue/placement' THEN 'QUEUE_PLACEMENT'""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b13;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b13(bytea,text,bytea,bytea)
            FROM PUBLIC,carepulse_gate;

        CREATE FUNCTION f01_private.b13_operation(purpose text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
            SELECT CASE purpose WHEN 'SETTINGS_QUEUE_UPDATE' THEN 'settings.queue.update'
                WHEN 'QUEUE_PLACEMENT' THEN 'queue.placement' END;
        $$;

        CREATE FUNCTION f01_private.b13_revision(v json) RETURNS boolean
        LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
            SELECT coalesce(json_typeof(v)='number' AND v::text ~ '^[1-9][0-9]{0,18}$'
                AND pg_input_is_valid(v::text,'bigint'),false);
        $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea,key_id text,mac bytea,body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            request_id bigint;
            req f01_private.requests%ROWTYPE;
            raw json;
            command jsonb;
            target jsonb := '{}'::jsonb;
            normalized_entries jsonb := '[]'::jsonb;
            ordered_ids jsonb := '[]'::jsonb;
            selected json;
            entry_id uuid;
            reason_whitespace constant text :=
                U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
                || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
        BEGIN
            request_id := public.f01_register_proof_before_b13(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF req.purpose NOT IN ('SETTINGS_QUEUE_READ','SETTINGS_QUEUE_UPDATE','QUEUE_PLACEMENT') THEN
                RETURN request_id;
            END IF;
            BEGIN
                IF req.query<>'' THEN
                    RAISE EXCEPTION 'Invalid queue query' USING ERRCODE='22023';
                END IF;
                IF req.purpose='SETTINGS_QUEUE_READ' THEN
                    IF octet_length(body)<>0 THEN
                        RAISE EXCEPTION 'Invalid queue read body' USING ERRCODE='22023';
                    END IF;
                    UPDATE f01_private.requests r SET b13_body='{}'::jsonb WHERE r.id=request_id;
                    RETURN request_id;
                END IF;
                IF req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                    RAISE EXCEPTION 'Invalid queue command envelope' USING ERRCODE='22023';
                END IF;
                raw := convert_from(body,'UTF8')::json;
                PERFORM f01_private.canonical_intake_json(raw);
                command := raw::jsonb;
                IF jsonb_typeof(command) IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION 'Invalid queue command' USING ERRCODE='22023';
                END IF;
                IF req.purpose='SETTINGS_QUEUE_UPDATE' THEN
                    IF NOT command ?& ARRAY['routine_order','late_depth','expected_policy_revision']
                        OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k
                            WHERE k NOT IN ('routine_order','late_depth','expected_policy_revision'))
                        OR jsonb_typeof(command->'routine_order') IS DISTINCT FROM 'string'
                        OR command->>'routine_order' NOT IN ('BOOKED_NUMBER','ARRIVAL_FIFO')
                        OR json_typeof(raw->'late_depth') IS DISTINCT FROM 'number'
                        OR (raw->'late_depth')::text !~ '^(0|[1-9][0-9]{0,9})$'
                        OR NOT pg_input_is_valid(command->>'late_depth','integer')
                        OR NOT f01_private.b13_revision(raw->'expected_policy_revision') THEN
                        RAISE EXCEPTION 'Invalid queue policy fields' USING ERRCODE='22023';
                    END IF;
                ELSE
                    IF NOT command ?& ARRAY['date','entries','expected_queue_day_revision','reason']
                        OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k
                            WHERE k NOT IN ('date','entries','expected_queue_day_revision','reason'))
                        OR jsonb_typeof(command->'date') IS DISTINCT FROM 'string'
                        OR command->>'date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                        OR NOT f01_private.b13_revision(raw->'expected_queue_day_revision')
                        OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                        OR btrim(command->>'reason',reason_whitespace)=''
                        OR command->>'reason' ~ '[\x01-\x1f]'
                        OR jsonb_typeof(command->'entries') IS DISTINCT FROM 'array' THEN
                        RAISE EXCEPTION 'Invalid placement fields' USING ERRCODE='22023';
                    END IF;
                    FOR selected IN SELECT value FROM json_array_elements(raw->'entries') LOOP
                        IF json_typeof(selected) IS DISTINCT FROM 'object'
                            OR NOT selected::jsonb ?& ARRAY['queue_entry_id','expected_queue_entry_revision']
                            OR EXISTS (SELECT 1 FROM json_object_keys(selected) k
                                WHERE k NOT IN ('queue_entry_id','expected_queue_entry_revision'))
                            OR json_typeof(selected->'queue_entry_id') IS DISTINCT FROM 'string'
                            OR NOT f01_private.b13_revision(selected->'expected_queue_entry_revision') THEN
                            RAISE EXCEPTION 'Invalid placement entry' USING ERRCODE='22023';
                        END IF;
                        entry_id := (selected->>'queue_entry_id')::uuid;
                        IF ordered_ids @> jsonb_build_array(entry_id) THEN
                            RAISE EXCEPTION 'Duplicate placement entry' USING ERRCODE='22023';
                        END IF;
                        ordered_ids := ordered_ids || jsonb_build_array(entry_id);
                        normalized_entries := normalized_entries || jsonb_build_array(
                            jsonb_build_object('queue_entry_id',entry_id,
                                'expected_queue_entry_revision',selected->'expected_queue_entry_revision'));
                    END LOOP;
                    IF ordered_ids='[]'::jsonb THEN
                        RAISE EXCEPTION 'Empty placement cohort' USING ERRCODE='22023';
                    END IF;
                    command := jsonb_set(command,'{date}',to_jsonb((command->>'date')::date));
                    command := jsonb_set(command,'{reason}',to_jsonb(btrim(command->>'reason',reason_whitespace)));
                    command := jsonb_set(command,'{entries}',normalized_entries);
                    target := jsonb_build_object('date',command->'date','ordered_queue_entry_ids',ordered_ids);
                END IF;
                UPDATE f01_private.requests r SET b13_body=command,
                    b13_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(
                        jsonb_build_object('operation',f01_private.b13_operation(req.purpose),
                            'target',target,'body',command)::json),'UTF8')),'hex')
                WHERE r.id=request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b13_authority()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,purpose text,stage text,
            operation text,idempotency_key text,command jsonb,digest text)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,r.purpose,c.stage,
                f01_private.b13_operation(r.purpose),r.idempotency_key,r.b13_body,r.b13_digest
            FROM public.f01_context() c
            JOIN f01_private.requests r ON r.id=c.request_id
            JOIN public.staff_accounts s ON s.organization_id=r.organization_id AND s.id=r.staff_id
            WHERE r.body_scope_valid AND r.b13_body IS NOT NULL
                AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
                AND s.role=r.role AND s.security_revision=r.security_revision
                AND ((r.purpose IN ('SETTINGS_QUEUE_READ','SETTINGS_QUEUE_UPDATE')
                    AND (r.role='DOCTOR_OWNER' OR (r.role='RECEPTIONIST' AND s.operational_manager)))
                    OR (r.purpose='QUEUE_PLACEMENT' AND r.role='RECEPTIONIST'))
                AND (c.stage='PRIMARY' OR (c.stage='RECEIPT_RECOVERY' AND r.b13_digest IS NOT NULL));
        $$;
        REVOKE ALL ON FUNCTION f01_private.b13_operation(text),f01_private.b13_revision(json),
            public.f01_register_proof(bytea,text,bytea,bytea),public.f01_b13_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b13_authority() TO carepulse_migration;
        """,
        execution_options={"no_parameters": True},
    )
    marker = "OR req.purpose = 'VISIT_FOLLOWUP'"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + """
                           OR (f01_private.b13_operation(req.purpose) IS NOT NULL
                               AND req.b13_digest IS NOT NULL)""",
    )
    op.execute("""
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)


def _queue_guards() -> None:
    # A permutation must preserve active-rank uniqueness at statement end,
    # without extra interim rank changes or revision increments.
    op.execute("""
        DROP INDEX public.queue_entries_unique_active_routine_rank;
        ALTER TABLE public.queue_entries ADD CONSTRAINT queue_entries_unique_active_routine_rank
            EXCLUDE USING btree (organization_id WITH =,queue_day_id WITH =,routine_rank WITH =)
            WHERE (disposition='ACTIVE' AND routine_rank IS NOT NULL)
            DEFERRABLE INITIALLY IMMEDIATE;

        CREATE FUNCTION public.f01_b13_day_scope(p_org uuid,p_date date,p_lane text,p_revision bigint)
        RETURNS boolean LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_b13_authority() a
                WHERE a.purpose='QUEUE_PLACEMENT' AND a.stage='PRIMARY'
                    AND a.organization_id=p_org AND p_lane='MAIN'
                    AND (a.command->>'date')::date=p_date
                    AND (a.command->>'expected_queue_day_revision')::bigint=p_revision);
        $$;
        REVOKE ALL ON FUNCTION public.f01_b13_day_scope(uuid,date,text,bigint) FROM PUBLIC;
    """)
    condition = """TG_OP='UPDATE' AND NEW.arrival_counter=OLD.arrival_counter
                    AND NEW.routine_cursor=OLD.routine_cursor AND NEW.revision=OLD.revision+1
                    AND public.f01_b13_day_scope(OLD.organization_id,OLD.service_date,OLD.lane,OLD.revision)"""
    marker = "IF session_user = 'carepulse_api' AND NOT EXISTS ("
    _rewrite(
        "public.f01_guard_signed_queue_day()",
        marker,
        f"""IF session_user='carepulse_api' AND {condition} THEN
                RETURN NEW;
            END IF;
            {marker}""",
    )
    marker = "ELSIF NEW.arrival_counter = OLD.arrival_counter"
    _rewrite(
        "public.f01_guard_queue_arrival_increment()",
        marker,
        f"""ELSIF {condition} THEN
                    RETURN NEW;
                {marker}""",
    )
    marker = "IF OLD.disposition='ACTIVE' AND NEW.disposition='CLOSED'"
    _rewrite(
        "public.f01_assert_queue_rank_shift()",
        marker,
        """IF OLD.disposition='ACTIVE' AND NEW.disposition='ACTIVE'
                AND OLD.routine_rank IS NOT NULL AND NEW.routine_rank IS NOT NULL
                AND NEW.revision=OLD.revision+1 AND EXISTS (
                    SELECT 1 FROM public.f01_b13_authority() a
                    CROSS JOIN LATERAL jsonb_array_elements(a.command->'entries')
                        WITH ORDINALITY AS s(entry,rank)
                    JOIN public.queue_days q ON q.organization_id=a.organization_id
                        AND q.id=NEW.queue_day_id AND q.service_date=NEW.service_date
                        AND q.lane='MAIN'
                    JOIN public.appointments x ON x.organization_id=q.organization_id
                        AND x.id=NEW.appointment_id AND x.patient_id=NEW.patient_id
                        AND x.doctor_staff_id=q.doctor_staff_id AND x.service_date=q.service_date
                        AND x.lane=q.lane AND x.status='Waiting' AND x.classification='ROUTINE'
                    WHERE a.purpose='QUEUE_PLACEMENT' AND a.stage='PRIMARY'
                        AND a.organization_id=NEW.organization_id
                        AND q.service_date=(a.command->>'date')::date
                        AND q.revision=(a.command->>'expected_queue_day_revision')::bigint+1
                        AND q.revision_transaction_id=pg_current_xact_id()
                        AND NEW.id=(s.entry->>'queue_entry_id')::uuid
                        AND OLD.revision=(s.entry->>'expected_queue_entry_revision')::bigint
                        AND NEW.routine_rank=s.rank
                ) THEN
                RETURN NULL;
            END IF;
            """
        + marker,
    )


def _policy() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b13_policy_projection(h public.clinic_policy_heads,
            v public.clinic_policy_versions) RETURNS jsonb
        LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
            SELECT jsonb_build_object('routine_order',v.payload->'routine_order',
                'late_depth',(v.payload->>'late_depth')::numeric::integer,
                'policy_version',v.version,'policy_revision',h.revision,
                'effective_on',v.effective_on,'pending_effective',v.pending_effective);
        $$;

        CREATE FUNCTION public.f01_b13_settings() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; projection jsonb; revision bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b13_authority();
            IF NOT FOUND OR a.purpose<>'SETTINGS_QUEUE_READ' OR a.stage<>'PRIMARY' THEN
                RAISE EXCEPTION 'Queue settings scope denied' USING ERRCODE='42501';
            END IF;
            SELECT public.f01_b13_policy_projection(h,v),h.revision INTO STRICT projection,revision
            FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                ON (v.organization_id,v.kind,v.version)=(h.organization_id,h.kind,h.current_version)
            WHERE h.organization_id=a.organization_id AND h.kind='QUEUE';
            IF NOT EXISTS (SELECT 1 FROM public.f01_b13_authority() live
                WHERE live.purpose='SETTINGS_QUEUE_READ' AND live.stage='PRIMARY') THEN
                RAISE EXCEPTION 'Queue settings authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',projection,'revision',revision);
        END $$;

        CREATE FUNCTION public.f01_b13_next_operating_day(p_org uuid,p_today date) RETURNS date
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            calendar_version bigint;
            calendar_payload jsonb;
            weekdays integer[];
            next_override date;
            candidate date := p_today;
            step_days integer;
        BEGIN
            SELECT v.version,v.payload INTO STRICT calendar_version,calendar_payload
            FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                ON (v.organization_id,v.kind,v.version)=(h.organization_id,h.kind,h.current_version)
            WHERE h.organization_id=p_org AND h.kind='SCHEDULE';
            IF calendar_payload ? 'working_weekdays' THEN
                SELECT array_agg(day::integer) INTO weekdays
                FROM jsonb_array_elements_text(calendar_payload->'working_weekdays') day;
            ELSE
                SELECT array_agg(DISTINCT s.weekday) INTO weekdays FROM public.clinic_schedule_intervals s
                WHERE s.organization_id=p_org AND s.policy_version=calendar_version AND s.interval_kind='WORK';
            END IF;
            SELECT min(day) INTO next_override FROM (
                SELECT s.service_date AS day FROM public.clinic_schedule_intervals s
                WHERE s.organization_id=p_org AND s.policy_version=calendar_version
                    AND s.interval_kind='OVERRIDE_WORK'
                UNION
                SELECT day::date FROM jsonb_array_elements_text(
                    coalesce(calendar_payload->'open_override_dates','[]'::jsonb)) day
            ) overrides WHERE day>p_today AND NOT EXISTS (
                SELECT 1 FROM public.clinic_schedule_holidays h
                WHERE h.organization_id=p_org AND h.policy_version=calendar_version AND h.service_date=overrides.day);
            IF coalesce(cardinality(weekdays),0)=0 THEN RETURN next_override; END IF;
            LOOP
                SELECT min((w-extract(isodow FROM candidate)::integer+6)%7+1) INTO step_days
                FROM unnest(weekdays) w;
                IF step_days IS NULL OR candidate>date '9999-12-31'-step_days THEN
                    RETURN next_override;
                END IF;
                candidate := candidate+step_days;
                IF next_override IS NOT NULL AND next_override<=candidate THEN RETURN next_override; END IF;
                IF NOT EXISTS (SELECT 1 FROM public.clinic_schedule_holidays h
                    WHERE h.organization_id=p_org AND h.policy_version=calendar_version
                        AND h.service_date=candidate) THEN RETURN candidate; END IF;
            END LOOP;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b13_policy_projection(public.clinic_policy_heads,public.clinic_policy_versions),
            public.f01_b13_settings(),public.f01_b13_next_operating_day(uuid,date) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b13_settings() TO carepulse_api;
    """)


def _receipts() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b13_read_receipt() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; r public.idempotency_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b13_authority();
            IF NOT FOUND OR a.operation IS NULL OR a.digest IS NULL THEN
                RAISE EXCEPTION 'Queue receipt scope denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO r FROM public.idempotency_receipts ir
            WHERE ir.scope_kind='TENANT' AND ir.scope_id=a.organization_id
                AND ir.organization_id=a.organization_id AND ir.actor_kind='STAFF'
                AND ir.actor_id=a.staff_id AND ir.staff_id=a.staff_id
                AND ir.operation=a.operation AND ir.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Queue idempotency conflict' USING ERRCODE='CP001';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.sensitive_audit s
                WHERE s.organization_id=a.organization_id AND s.actor_staff_id=a.staff_id
                    AND s.actor_role=a.role AND s.action=a.operation
                    AND s.reason IS NOT DISTINCT FROM a.command->>'reason'
                    AND s.after_state=r.result_projection
                    AND s.correlation_id=CASE WHEN a.purpose='QUEUE_PLACEMENT'
                        THEN a.command->>'date' ELSE a.organization_id::text END) THEN
                RAISE EXCEPTION 'Queue receipt evidence denied' USING ERRCODE='42501';
            END IF;
            IF a.purpose='SETTINGS_QUEUE_UPDATE' AND NOT EXISTS (
                SELECT 1 FROM public.clinic_policy_versions v
                WHERE v.organization_id=a.organization_id AND v.kind='QUEUE'
                    AND v.version=(r.result_projection->>'policy_version')::bigint
                    AND v.created_by_staff_id=a.staff_id
                    AND r.result_revision=(a.command->>'expected_policy_revision')::bigint+1
                    AND r.result_projection=jsonb_build_object('routine_order',v.payload->'routine_order',
                        'late_depth',(v.payload->>'late_depth')::numeric::integer,
                        'policy_version',v.version,'policy_revision',r.result_revision,
                        'effective_on',v.effective_on,'pending_effective',v.pending_effective)
                    AND v.payload=jsonb_build_object('routine_order',a.command->'routine_order',
                        'late_depth',a.command->'late_depth')) THEN
                RAISE EXCEPTION 'Queue policy receipt mismatch' USING ERRCODE='42501';
            ELSIF a.purpose='QUEUE_PLACEMENT' AND (
                r.result_revision<>(a.command->>'expected_queue_day_revision')::bigint+1
                OR r.result_projection->'date' IS DISTINCT FROM a.command->'date'
                OR r.result_projection->'queue_day_revision' IS DISTINCT FROM to_jsonb(r.result_revision)
                OR (SELECT jsonb_agg(e.entry->'queue_entry_id' ORDER BY e.rank)
                    FROM jsonb_array_elements(r.result_projection->'entries') WITH ORDINALITY e(entry,rank))
                    IS DISTINCT FROM
                    (SELECT jsonb_agg(e.entry->'queue_entry_id' ORDER BY e.rank)
                    FROM jsonb_array_elements(a.command->'entries') WITH ORDINALITY e(entry,rank))
            ) THEN RAISE EXCEPTION 'Queue placement receipt mismatch' USING ERRCODE='42501'; END IF;
            IF NOT EXISTS (SELECT 1 FROM public.f01_b13_authority() live WHERE live.digest=a.digest) THEN
                RAISE EXCEPTION 'Queue receipt authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',r.result_projection,'revision',r.result_revision,'replayed',true);
        END $$;

        CREATE FUNCTION public.f01_b13_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.f01_b13_authority() a
                WHERE a.stage='RECEIPT_RECOVERY' AND a.operation IS NOT NULL) THEN
                RAISE EXCEPTION 'Queue recovery scope denied' USING ERRCODE='42501';
            END IF;
            RETURN public.f01_b13_read_receipt();
        END $$;

        CREATE FUNCTION public.f01_b13_record(p_before jsonb,p_projection jsonb,p_revision bigint)
        RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record;
        BEGIN
            SELECT * INTO a FROM public.f01_b13_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.digest IS NULL THEN
                RAISE EXCEPTION 'Queue evidence scope denied' USING ERRCODE='42501';
            END IF;
            INSERT INTO public.sensitive_audit
                (id,organization_id,actor_staff_id,actor_role,action,reason,before_state,after_state,correlation_id)
            VALUES (gen_random_uuid(),a.organization_id,a.staff_id,a.role,a.operation,a.command->>'reason',
                p_before,p_projection,CASE WHEN a.purpose='QUEUE_PLACEMENT'
                    THEN a.command->>'date' ELSE a.organization_id::text END);
            INSERT INTO public.idempotency_receipts
                (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,operation,
                    idempotency_key,request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,
                a.staff_id,a.operation,a.idempotency_key,a.digest,p_projection,p_revision);
            RETURN jsonb_build_object('projection',p_projection,'revision',p_revision,'replayed',false);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b13_read_receipt(),public.f01_b13_replay(),
            public.f01_b13_record(jsonb,jsonb,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b13_replay() TO carepulse_api;
    """)


def _writers() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b13_publish() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            a record;
            h public.clinic_policy_heads%ROWTYPE;
            v public.clinic_policy_versions%ROWTYPE;
            replay jsonb;
            before_state jsonb;
            next_day date;
        BEGIN
            SELECT * INTO a FROM public.f01_b13_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.purpose<>'SETTINGS_QUEUE_UPDATE' THEN
                RAISE EXCEPTION 'Queue policy scope denied' USING ERRCODE='42501';
            END IF;
            replay := public.f01_b13_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            PERFORM 1 FROM public.clinic_policy_heads ph
            WHERE ph.organization_id=a.organization_id AND ph.kind='SCHEDULE' FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Schedule head unavailable' USING ERRCODE='23514'; END IF;
            SELECT * INTO STRICT h FROM public.clinic_policy_heads ph
            WHERE ph.organization_id=a.organization_id AND ph.kind='QUEUE' FOR UPDATE;
            replay := public.f01_b13_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            IF h.revision<>(a.command->>'expected_policy_revision')::bigint THEN
                RAISE EXCEPTION 'Stale queue policy revision' USING ERRCODE='40001';
            END IF;
            next_day := public.f01_b13_next_operating_day(a.organization_id,
                (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date);
            SELECT * INTO STRICT v FROM public.clinic_policy_versions pv
            WHERE pv.organization_id=a.organization_id AND pv.kind='QUEUE' AND pv.version=h.current_version;
            before_state := public.f01_b13_policy_projection(h,v);
            INSERT INTO public.clinic_policy_versions
                (organization_id,kind,version,payload,created_by_staff_id,effective_on,pending_effective)
            VALUES (a.organization_id,'QUEUE',h.current_version+1,
                jsonb_build_object('routine_order',a.command->'routine_order','late_depth',a.command->'late_depth'),
                a.staff_id,next_day,next_day IS NULL) RETURNING * INTO v;
            UPDATE public.clinic_policy_heads ph SET current_version=v.version,
                revision=h.revision+1,queue_effective_on=next_day,queue_pending=next_day IS NULL
            WHERE ph.organization_id=a.organization_id AND ph.kind='QUEUE' RETURNING * INTO h;
            RETURN public.f01_b13_record(before_state,public.f01_b13_policy_projection(h,v),h.revision);
        END $$;

        CREATE FUNCTION public.f01_b13_place() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            a record;
            q public.queue_days%ROWTYPE;
            replay jsonb;
            before_entries jsonb;
            after_entries jsonb;
            before_state jsonb;
            projection jsonb;
            cohort_size bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b13_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.purpose<>'QUEUE_PLACEMENT' THEN
                RAISE EXCEPTION 'Queue placement scope denied' USING ERRCODE='42501';
            END IF;
            replay := public.f01_b13_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            SELECT * INTO q FROM public.queue_days d
            WHERE d.organization_id=a.organization_id AND d.service_date=(a.command->>'date')::date
                AND d.lane='MAIN' FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Queue day unavailable' USING ERRCODE='P0002'; END IF;
            replay := public.f01_b13_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            IF q.revision<>(a.command->>'expected_queue_day_revision')::bigint THEN
                RAISE EXCEPTION 'Stale queue day revision' USING ERRCODE='40001';
            END IF;
            -- The locked day stabilizes the complete cohort before appointment locks.
            PERFORM 1 FROM public.appointments x
            JOIN public.queue_entries e ON e.organization_id=x.organization_id AND e.appointment_id=x.id
            WHERE e.organization_id=a.organization_id AND e.queue_day_id=q.id
                AND e.disposition='ACTIVE' AND e.routine_rank IS NOT NULL
            ORDER BY x.id FOR UPDATE OF x;
            SELECT count(*),jsonb_agg(jsonb_build_object('queue_entry_id',e.id,
                'routine_rank',e.routine_rank,'queue_entry_revision',e.revision) ORDER BY e.routine_rank)
                INTO cohort_size,before_entries
            FROM public.queue_entries e
            JOIN public.appointments x ON x.organization_id=e.organization_id AND x.id=e.appointment_id
                AND x.patient_id=e.patient_id AND x.doctor_staff_id=e.doctor_staff_id
                AND x.service_date=e.service_date AND x.lane=e.lane
            WHERE e.organization_id=a.organization_id AND e.queue_day_id=q.id
                AND e.disposition='ACTIVE' AND e.routine_rank IS NOT NULL
                AND x.status='Waiting' AND x.classification='ROUTINE';
            IF cohort_size<>jsonb_array_length(a.command->'entries') OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(a.command->'entries') selected(entry)
                WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(before_entries) live(entry)
                    WHERE live.entry->'queue_entry_id'=selected.entry->'queue_entry_id'
                        AND live.entry->'queue_entry_revision'=selected.entry->'expected_queue_entry_revision')
            ) THEN
                RAISE EXCEPTION 'Invalid or stale placement cohort' USING ERRCODE='40001';
            END IF;
            before_state := jsonb_build_object('date',q.service_date,
                'queue_day_revision',q.revision,'entries',before_entries);
            UPDATE public.queue_entries e SET routine_rank=s.rank,revision=e.revision+1
            FROM jsonb_array_elements(a.command->'entries') WITH ORDINALITY s(entry,rank)
            WHERE e.organization_id=a.organization_id AND e.queue_day_id=q.id
                AND e.id=(s.entry->>'queue_entry_id')::uuid
                AND e.disposition='ACTIVE' AND e.routine_rank IS NOT NULL
                AND e.routine_rank<>s.rank;
            UPDATE public.queue_days d SET revision=q.revision+1
            WHERE d.organization_id=a.organization_id AND d.id=q.id RETURNING * INTO q;
            SELECT jsonb_agg(jsonb_build_object('queue_entry_id',e.id,
                'routine_rank',e.routine_rank,'queue_entry_revision',e.revision) ORDER BY e.routine_rank)
                INTO after_entries FROM public.queue_entries e
            WHERE e.organization_id=a.organization_id AND e.queue_day_id=q.id
                AND e.disposition='ACTIVE' AND e.routine_rank IS NOT NULL;
            projection := jsonb_build_object('date',q.service_date,'queue_day_revision',q.revision,
                'entries',after_entries);
            RETURN public.f01_b13_record(before_state,projection,q.revision);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b13_publish(),public.f01_b13_place() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b13_publish(),public.f01_b13_place() TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _queue_guards()
    _policy()
    _receipts()
    _writers()


def downgrade() -> None:
    raise RuntimeError("B-13 queue policy and placement migration is forward-only")
