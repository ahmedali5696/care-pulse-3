"""B-15: signed admission transaction and target-bound receipt replay."""

from alembic import op

revision = "0057_w3_queue_admit"
down_revision = "0056_b14_target_binding"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Missing unique B-15 marker in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b15_body jsonb,
            ADD COLUMN b15_digest text;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method = 'POST' AND p_path = '/v1/queue/admit' THEN 'QUEUE_ADMIT'""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b15;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b15(bytea,text,bytea,bytea)
            FROM PUBLIC, carepulse_gate;

        CREATE FUNCTION public.f01_register_proof(payload bytea, key_id text, mac bytea, body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            request_id bigint;
            req f01_private.requests%ROWTYPE;
            raw json;
            command jsonb;
            v_target uuid;
            v_patient uuid;
            field_name text;
            required_fields constant text[] := ARRAY['appointment_id','patient_id','date','lane',
                'expected_appointment_revision','expected_queue_day_revision','expected_queue_entry_revision',
                'expected_control_revision','expected_capacity_revision','expected_admission_revision',
                'expected_head_appointment_id'];
            optional_fields constant text[] := ARRAY['non_head_reason','confirmation_id','physical_presence_confirmed'];
            reason_whitespace constant text :=
                U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
                || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000';
        BEGIN
            request_id := public.f01_register_proof_before_b15(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF req.purpose <> 'QUEUE_ADMIT' THEN RETURN request_id; END IF;
            BEGIN
                IF req.query <> '' OR req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                    RAISE EXCEPTION 'Invalid admission envelope' USING ERRCODE = '22023';
                END IF;
                raw := convert_from(body,'UTF8')::json;
                PERFORM f01_private.canonical_intake_json(raw);
                command := raw::jsonb;
                IF jsonb_typeof(command) IS DISTINCT FROM 'object'
                    OR NOT command ?& required_fields
                    OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k
                        WHERE k <> ALL(required_fields || optional_fields))
                    OR EXISTS (SELECT 1 FROM json_each(raw) e
                        WHERE e.key LIKE 'expected_%_revision'
                            AND (json_typeof(e.value) IS DISTINCT FROM 'number'
                                OR e.value::text !~ '^(0|[1-9][0-9]{0,18})$'
                                OR NOT pg_input_is_valid(e.value::text,'bigint')
                                OR (e.key <> 'expected_appointment_revision' AND e.value::text = '0')))
                    OR jsonb_typeof(command->'date') IS DISTINCT FROM 'string'
                    OR command->>'date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                    OR jsonb_typeof(command->'lane') IS DISTINCT FROM 'string'
                    OR command->>'lane' NOT IN ('MAIN','EMERGENCY') THEN
                    RAISE EXCEPTION 'Invalid signed admission fields' USING ERRCODE = '22023';
                END IF;
                FOREACH field_name IN ARRAY ARRAY['appointment_id','patient_id','expected_head_appointment_id'] LOOP
                    IF jsonb_typeof(command->field_name) IS DISTINCT FROM 'string' THEN
                        RAISE EXCEPTION 'Invalid admission resource' USING ERRCODE = '22023';
                    END IF;
                    command := jsonb_set(command,ARRAY[field_name],to_jsonb((command->>field_name)::uuid));
                END LOOP;
                IF command ? 'physical_presence_confirmed'
                    AND command->'physical_presence_confirmed' IS DISTINCT FROM to_jsonb(true) THEN
                    RAISE EXCEPTION 'Invalid admission presence assertion' USING ERRCODE = '22023';
                END IF;
                command := jsonb_strip_nulls(command);
                IF command ? 'confirmation_id' THEN
                    IF jsonb_typeof(command->'confirmation_id') <> 'string' THEN
                        RAISE EXCEPTION 'Invalid admission approval' USING ERRCODE = '22023';
                    END IF;
                    command := jsonb_set(command,'{confirmation_id}',to_jsonb((command->>'confirmation_id')::uuid));
                END IF;
                IF command ? 'non_head_reason' THEN
                    IF jsonb_typeof(command->'non_head_reason') <> 'string'
                        OR btrim(command->>'non_head_reason',reason_whitespace) = ''
                        OR command->>'non_head_reason' ~ '[\x01-\x1f]' THEN
                        RAISE EXCEPTION 'Invalid admission exception reason' USING ERRCODE = '22023';
                    END IF;
                    command := jsonb_set(command,'{non_head_reason}',
                        to_jsonb(btrim(command->>'non_head_reason',reason_whitespace)));
                END IF;
                command := jsonb_set(command,'{date}',to_jsonb((command->>'date')::date));
                v_target := (command->>'appointment_id')::uuid;
                v_patient := (command->>'patient_id')::uuid;
                UPDATE f01_private.requests r SET b15_body=command,
                    target_id=v_target, patient_id=v_patient,
                    b15_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(
                        jsonb_build_object('operation','queue.admit','target',
                            jsonb_build_object('appointment_id',v_target),'body',command)::json),'UTF8')),'hex')
                WHERE r.id=request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b15_authority()
        RETURNS TABLE (organization_id uuid, staff_id uuid, role text, stage text,
            target_id uuid, patient_id uuid, command jsonb, digest text, idempotency_key text)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path = pg_catalog, pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,c.stage,r.target_id,r.patient_id,
                r.b15_body,r.b15_digest,r.idempotency_key
            FROM public.f01_context() c
            JOIN f01_private.requests r ON r.id=c.request_id
            WHERE c.purpose='QUEUE_ADMIT' AND c.method='POST' AND c.path='/v1/queue/admit'
                AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY')
                AND c.role IN ('DOCTOR_OWNER','RECEPTIONIST')
                AND r.body_scope_valid AND r.b15_digest IS NOT NULL
                AND r.target_id IS NOT NULL AND r.patient_id IS NOT NULL;
        $$;
        REVOKE ALL ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea),
            public.f01_b15_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b15_authority() TO carepulse_migration;
    """,
        execution_options={"no_parameters": True},
    )
    marker = "OR req.purpose = 'VISIT_FOLLOWUP'"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + """
                           OR (req.purpose='QUEUE_ADMIT' AND req.b15_digest IS NOT NULL)""",
    )
    _rewrite(
        "f01_private.guard_audit_appointment_history()",
        "IF session_user = 'carepulse_api' AND NOT EXISTS (",
        """IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_b15_authority() a
                WHERE a.stage='PRIMARY' AND a.organization_id=NEW.organization_id
                    AND a.staff_id=NEW.actor_staff_id AND a.role=NEW.actor_role
                    AND a.target_id=NEW.appointment_id AND NEW.action='ADMITTED'
                    AND NEW.appointment_revision=(a.command->>'expected_appointment_revision')::bigint+1
                    AND NEW.old_status='Waiting' AND NEW.new_status='In Examination'
                    AND NEW.old_service_date=(a.command->>'date')::date
                    AND NEW.new_service_date=NEW.old_service_date AND NEW.old_number=NEW.new_number
                    AND NEW.price_snapshot_appointment_id=NEW.appointment_id
                    AND NEW.replacement_appointment_id IS NULL
                    AND NEW.reason IS NOT DISTINCT FROM a.command->>'non_head_reason'
            ) AND NOT EXISTS (""",
    )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner")


def _queue_guard() -> None:
    _rewrite(
        "public.f01_guard_signed_queue_day()",
        "IF session_user = 'carepulse_api' AND NOT EXISTS (",
        """IF session_user = 'carepulse_api' AND NOT EXISTS (
                SELECT 1 FROM public.f01_b15_authority() a
                JOIN public.queue_entries e ON e.organization_id=a.organization_id
                    AND e.appointment_id=a.target_id AND e.patient_id=a.patient_id
                    AND e.queue_day_id=NEW.id AND e.doctor_staff_id=NEW.doctor_staff_id
                    AND e.disposition='CLOSED' AND e.closure_transaction_id=pg_current_xact_id()
                WHERE TG_OP='UPDATE' AND a.stage='PRIMARY'
                    AND a.organization_id=NEW.organization_id
                    AND NEW.service_date=(a.command->>'date')::date AND NEW.lane=a.command->>'lane'
                    AND OLD.revision=(a.command->>'expected_queue_day_revision')::bigint
                    AND NEW.revision=OLD.revision+1 AND NEW.arrival_counter=OLD.arrival_counter
            ) AND NOT EXISTS (""",
    )


def _receipts() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b15_read_receipt() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC' AS $$
        DECLARE a record; r public.idempotency_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b15_authority();
            IF NOT FOUND THEN RAISE EXCEPTION 'Admission receipt scope denied' USING ERRCODE='42501'; END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.clinic_policy_heads m JOIN public.clinic_policy_versions v
                    ON (v.organization_id,v.kind,v.version)=(m.organization_id,m.kind,m.current_version)
                WHERE m.organization_id=a.organization_id AND m.kind='ADMISSION'
                    AND (v.payload->>'actors'='BOTH'
                        OR (v.payload->>'actors'='DOCTOR' AND a.role='DOCTOR_OWNER')
                        OR (v.payload->>'actors'='RECEPTIONIST' AND a.role='RECEPTIONIST'))
            ) THEN RAISE EXCEPTION 'Admission mode denied' USING ERRCODE='42501'; END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.appointments x WHERE x.organization_id=a.organization_id
                    AND x.id=a.target_id AND x.patient_id=a.patient_id
                    AND x.service_date=(a.command->>'date')::date AND x.lane=a.command->>'lane'
            ) THEN RAISE EXCEPTION 'Admission case not found' USING ERRCODE='P0002'; END IF;
            SELECT * INTO r FROM public.idempotency_receipts ir
            WHERE ir.organization_id=a.organization_id AND ir.scope_id=a.organization_id
                AND ir.scope_kind='TENANT' AND ir.actor_kind='STAFF'
                AND ir.staff_id=a.staff_id AND ir.actor_id=a.staff_id
                AND ir.operation='queue.admit' AND ir.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Admission idempotency conflict' USING ERRCODE='CP001';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.visits v
                JOIN public.appointment_history h ON h.organization_id=v.organization_id
                    AND h.appointment_id=v.appointment_id AND h.appointment_revision=v.admission_history_revision
                JOIN public.queue_entries e ON e.organization_id=v.organization_id
                    AND e.appointment_id=v.appointment_id AND e.patient_id=v.patient_id
                    AND e.doctor_staff_id=v.doctor_staff_id AND e.service_date=v.service_date
                    AND e.closure_history_revision=h.appointment_revision AND e.disposition='CLOSED'
                WHERE v.organization_id=a.organization_id AND v.appointment_id=a.target_id
                    AND v.patient_id=a.patient_id AND v.service_date=(a.command->>'date')::date
                    AND e.lane=a.command->>'lane' AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                    AND h.action='ADMITTED' AND h.old_status='Waiting' AND h.new_status='In Examination'
                    AND h.old_service_date=v.service_date AND h.new_service_date=v.service_date
                    AND h.reason IS NOT DISTINCT FROM a.command->>'non_head_reason'
                    AND h.appointment_revision=r.result_revision
                    AND h.appointment_revision=(a.command->>'expected_appointment_revision')::bigint+1
                    AND e.closure_queue_revision=(a.command->>'expected_queue_day_revision')::bigint
                    AND e.revision=(a.command->>'expected_queue_entry_revision')::bigint+1
                    AND r.result_projection=jsonb_build_object(
                        'visit_id',v.id,'appointment_id',v.appointment_id,'patient_id',v.patient_id,
                        'service_date',v.service_date,'lane',e.lane,'status','In Examination',
                        'appointment_revision',h.appointment_revision,'queue_day_revision',e.closure_queue_revision+1,
                        'queue_entry_revision',e.revision,'control_revision',a.command->'expected_control_revision',
                        'capacity_revision',a.command->'expected_capacity_revision',
                        'admission_revision',a.command->'expected_admission_revision',
                        'routine_cursor',CASE WHEN e.ordinary_head_admitted
                            THEN greatest(e.closure_routine_cursor,h.new_number) ELSE e.closure_routine_cursor END,
                        'confirmation_id',a.command->'confirmation_id','admitted_at',v.admitted_at)
                    AND (a.command->>'confirmation_id' IS NULL OR EXISTS (
                        SELECT 1 FROM public.doctor_admission_confirmations f
                        WHERE f.organization_id=v.organization_id AND f.appointment_id=v.appointment_id
                            AND f.id=(a.command->>'confirmation_id')::uuid AND f.state='CONSUMED'
                            AND f.consuming_admission_revision=h.appointment_revision))
                    AND EXISTS (
                        SELECT 1 FROM public.sensitive_audit s
                        WHERE s.organization_id=v.organization_id AND s.action='queue.admit'
                            AND s.actor_staff_id=a.staff_id AND s.actor_role=a.role
                            AND s.correlation_id=v.appointment_id::text
                            AND s.reason IS NOT DISTINCT FROM h.reason
                            AND s.after_state=r.result_projection || jsonb_build_object(
                                'head_appointment_id',a.command->'expected_head_appointment_id',
                                'non_head_reason',h.reason,
                                'physical_presence_confirmed',coalesce(a.command->'physical_presence_confirmed',to_jsonb(false)),
                                'actor_staff_id',a.staff_id,'actor_role',a.role))
            ) THEN RAISE EXCEPTION 'Admission receipt evidence denied' USING ERRCODE='42501'; END IF;
            IF NOT EXISTS (SELECT 1 FROM public.f01_b15_authority() live WHERE live.digest=a.digest) THEN
                RAISE EXCEPTION 'Admission authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',r.result_projection,'revision',r.result_revision,'replayed',true);
        END $$;

        CREATE FUNCTION public.f01_b15_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.f01_b15_authority() a WHERE a.stage='RECEIPT_RECOVERY')
                THEN RAISE EXCEPTION 'Admission recovery scope denied' USING ERRCODE='42501'; END IF;
            RETURN public.f01_b15_read_receipt();
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b15_read_receipt(),public.f01_b15_replay() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b15_replay() TO carepulse_api;
    """)


def _admit() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b15_admit() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp SET TimeZone = 'UTC' AS $$
        DECLARE
            a record;
            h public.clinic_policy_heads%ROWTYPE;
            m public.clinic_policy_heads%ROWTYPE;
            c public.doctor_controls%ROWTYPE;
            q public.queue_days%ROWTYPE;
            x public.appointments%ROWTYPE;
            e public.queue_entries%ROWTYPE;
            v public.visits%ROWTYPE;
            f public.doctor_admission_confirmations%ROWTYPE;
            capacity_payload jsonb;
            replay jsonb;
            selected_head uuid;
            reason text;
            active_count bigint;
            confirmation_id uuid;
            projection jsonb;
            evidence jsonb;
            cairo_today date;
        BEGIN
            SELECT * INTO a FROM public.f01_b15_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' THEN
                RAISE EXCEPTION 'Admission scope denied' USING ERRCODE='42501';
            END IF;
            -- Existing-ID admission remains lawful after archive. Lock the
            -- signed Patient lifecycle row before the policy/control locks,
            -- without reapplying the new-ID active-patient guard.
            PERFORM 1 FROM public.patients p
            WHERE p.organization_id=a.organization_id AND p.id=a.patient_id FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Admission case not found' USING ERRCODE='P0002'; END IF;
            -- Same order as B-14 capacity/mode/confirmation writers.
            SELECT * INTO STRICT h FROM public.clinic_policy_heads ph
            WHERE ph.organization_id=a.organization_id AND ph.kind='CAPACITY' FOR UPDATE;
            SELECT * INTO STRICT m FROM public.clinic_policy_heads ph
            WHERE ph.organization_id=a.organization_id AND ph.kind='ADMISSION' FOR UPDATE;
            SELECT * INTO STRICT c FROM public.doctor_controls dc
            WHERE dc.organization_id=a.organization_id FOR UPDATE;
            replay := public.f01_b15_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            SELECT vp.payload INTO STRICT capacity_payload FROM public.clinic_policy_versions vp
            WHERE vp.organization_id=a.organization_id AND vp.kind='CAPACITY' AND vp.version=h.current_version;
            IF c.capacity_policy_version<>h.current_version
                OR c.revision<>(a.command->>'expected_control_revision')::bigint
                OR h.revision<>(a.command->>'expected_capacity_revision')::bigint
                OR m.revision<>(a.command->>'expected_admission_revision')::bigint THEN
                RAISE EXCEPTION 'Stale admission control revision' USING ERRCODE='40001';
            END IF;
            IF (capacity_payload->>'paused')::boolean THEN
                RAISE EXCEPTION 'Doctor paused' USING ERRCODE='23514';
            END IF;
            SELECT count(*) INTO active_count FROM public.appointments ax
            WHERE ax.organization_id=a.organization_id AND ax.doctor_staff_id=c.doctor_staff_id
                AND ax.status='In Examination';
            IF active_count>=(capacity_payload->>'capacity')::numeric THEN
                RAISE EXCEPTION 'Examination capacity exhausted' USING ERRCODE='23514';
            END IF;
            SELECT * INTO q FROM public.queue_days qd
            WHERE qd.organization_id=a.organization_id AND qd.doctor_staff_id=c.doctor_staff_id
                AND qd.service_date=(a.command->>'date')::date AND qd.lane=a.command->>'lane' FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Admission queue not found' USING ERRCODE='P0002'; END IF;
            SELECT * INTO x FROM public.appointments ax
            WHERE ax.organization_id=a.organization_id AND ax.id=a.target_id AND ax.patient_id=a.patient_id
                AND ax.doctor_staff_id=q.doctor_staff_id AND ax.service_date=q.service_date AND ax.lane=q.lane FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Admission case not found' USING ERRCODE='P0002'; END IF;
            SELECT * INTO e FROM public.queue_entries qe
            WHERE qe.organization_id=x.organization_id AND qe.appointment_id=x.id AND qe.patient_id=x.patient_id
                AND qe.doctor_staff_id=x.doctor_staff_id AND qe.service_date=q.service_date AND qe.lane=q.lane
                AND qe.queue_day_id=q.id AND qe.disposition='ACTIVE' FOR UPDATE;
            IF NOT FOUND OR x.status<>'Waiting' THEN
                RAISE EXCEPTION 'Admission requires active Waiting case' USING ERRCODE='23514';
            END IF;
            IF x.revision<>(a.command->>'expected_appointment_revision')::bigint
                OR q.revision<>(a.command->>'expected_queue_day_revision')::bigint
                OR e.revision<>(a.command->>'expected_queue_entry_revision')::bigint THEN
                RAISE EXCEPTION 'Stale admission case revision' USING ERRCODE='40001';
            END IF;
            SELECT qe.appointment_id INTO selected_head FROM public.queue_entries qe
            JOIN public.appointments ax ON ax.organization_id=qe.organization_id AND ax.id=qe.appointment_id
            WHERE qe.organization_id=a.organization_id AND qe.queue_day_id=q.id
                AND qe.disposition='ACTIVE' AND ax.status='Waiting' AND ax.service_date=q.service_date
            ORDER BY CASE WHEN qe.lane='MAIN' AND ax.classification='URGENT' THEN 0 ELSE 1 END,
                CASE WHEN qe.lane='MAIN' AND ax.classification='ROUTINE' THEN qe.routine_rank
                    ELSE qe.arrival_sequence END, qe.id LIMIT 1;
            IF selected_head IS DISTINCT FROM (a.command->>'expected_head_appointment_id')::uuid THEN
                RAISE EXCEPTION 'Stale selected lane head' USING ERRCODE='40001';
            END IF;
            reason := a.command->>'non_head_reason';
            IF (selected_head=x.id) IS DISTINCT FROM (reason IS NULL) THEN
                RAISE EXCEPTION 'Admission exception reason does not match selected head' USING ERRCODE='23514';
            END IF;
            cairo_today := (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date;
            IF q.service_date>cairo_today OR (q.service_date<cairo_today
                AND a.command->'physical_presence_confirmed' IS DISTINCT FROM to_jsonb(true)) THEN
                RAISE EXCEPTION 'Admission requires fresh original-day presence' USING ERRCODE='23514';
            END IF;
            confirmation_id := (a.command->>'confirmation_id')::uuid;
            IF active_count=0 AND confirmation_id IS NOT NULL THEN
                RAISE EXCEPTION 'Unexpected additional-case approval' USING ERRCODE='23514';
            ELSIF active_count>0 THEN
                SELECT * INTO f FROM public.doctor_admission_confirmations cf
                WHERE cf.organization_id=a.organization_id AND cf.id=confirmation_id
                    AND cf.appointment_id=x.id AND cf.patient_id=x.patient_id
                    AND cf.doctor_staff_id=x.doctor_staff_id AND cf.service_date=q.service_date AND cf.lane=q.lane
                    FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Additional-case approval denied' USING ERRCODE='23514'; END IF;
                PERFORM 1 FROM public.staff_accounts s JOIN public."user" u ON u.id=s.auth_user_id
                WHERE s.organization_id=f.organization_id AND s.id=f.issuing_doctor_staff_id FOR SHARE OF s,u;
                IF NOT FOUND OR NOT EXISTS (
                    SELECT 1 FROM public.f01_b14_valid_case(a.organization_id,x.id,q.service_date,q.lane) cf
                    WHERE cf.id=f.id
                ) THEN RAISE EXCEPTION 'Additional-case approval denied' USING ERRCODE='23514'; END IF;
            END IF;
            UPDATE public.appointments ax SET status='In Examination',revision=x.revision+1
            WHERE ax.organization_id=x.organization_id AND ax.id=x.id RETURNING * INTO x;
            INSERT INTO public.appointment_history
                (id,organization_id,appointment_id,appointment_revision,action,old_status,new_status,
                 old_service_date,new_service_date,old_number,new_number,actor_staff_id,actor_role,
                 reason,price_snapshot_appointment_id)
            VALUES (gen_random_uuid(),x.organization_id,x.id,x.revision,'ADMITTED','Waiting','In Examination',
                x.service_date,x.service_date,x.current_number,x.current_number,a.staff_id,a.role,reason,x.id);
            UPDATE public.queue_entries qe SET disposition='CLOSED',closure_history_revision=x.revision,
                revision=e.revision+1
            WHERE qe.organization_id=e.organization_id AND qe.id=e.id RETURNING * INTO e;
            UPDATE public.queue_days qd SET revision=q.revision+1,
                routine_cursor=CASE WHEN e.ordinary_head_admitted
                    THEN greatest(q.routine_cursor,x.current_number) ELSE q.routine_cursor END
            WHERE qd.organization_id=q.organization_id AND qd.id=q.id RETURNING * INTO q;
            INSERT INTO public.visits
                (id,organization_id,appointment_id,patient_id,doctor_staff_id,service_date,
                 admission_history_revision,admitted_at)
            VALUES (gen_random_uuid(),x.organization_id,x.id,x.patient_id,x.doctor_staff_id,x.service_date,
                x.revision,clock_timestamp()) RETURNING * INTO v;
            IF confirmation_id IS NOT NULL THEN
                UPDATE public.doctor_admission_confirmations cf SET state='CONSUMED',
                    consumed_at=clock_timestamp(),consuming_admission_revision=x.revision
                WHERE cf.organization_id=f.organization_id AND cf.id=f.id AND cf.state='ISSUED';
                IF NOT FOUND THEN RAISE EXCEPTION 'Approval consumption denied' USING ERRCODE='23514'; END IF;
            END IF;
            projection := jsonb_build_object(
                'visit_id',v.id,'appointment_id',x.id,'patient_id',x.patient_id,
                'service_date',x.service_date,'lane',x.lane,'status',x.status,'appointment_revision',x.revision,
                'queue_day_revision',q.revision,'queue_entry_revision',e.revision,'control_revision',c.revision,
                'capacity_revision',h.revision,'admission_revision',m.revision,'routine_cursor',q.routine_cursor,
                'confirmation_id',confirmation_id,'admitted_at',v.admitted_at);
            evidence := projection || jsonb_build_object('head_appointment_id',selected_head,
                'non_head_reason',reason,'physical_presence_confirmed',
                coalesce(a.command->'physical_presence_confirmed',to_jsonb(false)),
                'actor_staff_id',a.staff_id,'actor_role',a.role);
            INSERT INTO public.sensitive_audit
                (id,organization_id,actor_staff_id,actor_role,action,reason,before_state,after_state,correlation_id)
            VALUES (gen_random_uuid(),a.organization_id,a.staff_id,a.role,'queue.admit',reason,
                jsonb_build_object('appointment_id',x.id,'status','Waiting','revision',x.revision-1),
                evidence,x.id::text);
            INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
            VALUES (gen_random_uuid(),a.organization_id,'APPOINTMENT_ADMITTED','APPOINTMENT',x.id::text,evidence);
            INSERT INTO public.idempotency_receipts
                (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,operation,idempotency_key,
                 request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,
                a.staff_id,'queue.admit',a.idempotency_key,a.digest,projection,x.revision);
            IF NOT EXISTS (SELECT 1 FROM public.f01_b15_authority() live WHERE live.stage='PRIMARY'
                AND live.digest=a.digest AND live.target_id=x.id) THEN
                RAISE EXCEPTION 'Admission authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',projection,'revision',x.revision,'replayed',false);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b15_admit() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b15_admit() TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _queue_guard()
    _receipts()
    _admit()


def downgrade() -> None:
    raise RuntimeError("B-15 admission migration is forward-only")
