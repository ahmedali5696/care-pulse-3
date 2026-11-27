"""B-18: signed target/date-specific same-ID rescheduling."""

from alembic import op

revision = "0063_b18_reschedule"
down_revision = "0062_b17_typed_body"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str, *, count: int = 1) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != count:
        raise RuntimeError(f"Unexpected B-18 marker count in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b18_body jsonb, ADD COLUMN b18_digest text;
        ALTER TABLE f01_private.activations ADD COLUMN b18_number bigint,
            ADD COLUMN b18_finished boolean NOT NULL DEFAULT false;
    """)

    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method='POST' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '/reschedule$')
                    THEN 'APPOINTMENT_RESCHEDULE'""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b18;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b18(bytea,text,bytea,bytea)
            FROM PUBLIC,carepulse_gate;

        CREATE FUNCTION f01_private.b18_command(raw json) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
        DECLARE command jsonb:=raw::jsonb; field text; day date;
            revisions text[]:=ARRAY['expected_appointment_revision','expected_schedule_revision',
                'expected_capacity_revision','expected_price_revision','expected_queue_day_revision',
                'expected_claim_revision','expected_source_decision_revision','expected_visit_revision',
                'expected_followup_revision','expected_authorization_revision'];
        BEGIN
            IF jsonb_typeof(command) IS DISTINCT FROM 'object'
                OR f01_private.b16_revision(raw->'expected_appointment_revision') IS NOT TRUE
                OR f01_private.b16_revision(raw->'expected_schedule_revision') IS NOT TRUE
                OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                OR f01_private.b16_reason(command->>'reason') IS NULL
                OR jsonb_typeof(command->'service_date') IS DISTINCT FROM 'string'
                OR command->>'service_date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k WHERE k<>ALL(
                    revisions || ARRAY['reason','service_date','physical_presence_confirmed',
                    'extension_event_id','starts_at','ends_at']))
                OR (command ? 'physical_presence_confirmed'
                    AND command->'physical_presence_confirmed' IS DISTINCT FROM 'true'::jsonb)
                OR ((command ? 'starts_at' OR command ? 'ends_at') AND
                    (jsonb_typeof(command->'starts_at') IS DISTINCT FROM 'string'
                        OR jsonb_typeof(command->'ends_at') IS DISTINCT FROM 'string')) THEN
                RAISE EXCEPTION 'Invalid reschedule command' USING ERRCODE='22023';
            END IF;
            day:=(command->>'service_date')::date;
            IF NOT isfinite(day) OR day::text<>command->>'service_date' THEN
                RAISE EXCEPTION 'Invalid reschedule date' USING ERRCODE='22023';
            END IF;
            FOREACH field IN ARRAY revisions LOOP
                IF command->>field IS NOT NULL
                    AND f01_private.b16_revision(raw->field) IS NOT TRUE THEN
                    RAISE EXCEPTION 'Invalid reschedule revision' USING ERRCODE='22023';
                END IF;
            END LOOP;
            IF command->>'extension_event_id' IS NOT NULL THEN
                IF jsonb_typeof(command->'extension_event_id') IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION 'Invalid extension reference' USING ERRCODE='22023';
                END IF;
                command:=jsonb_set(command,'{extension_event_id}',
                    to_jsonb((command->>'extension_event_id')::uuid));
            END IF;
            RETURN jsonb_set(command,'{reason}',to_jsonb(f01_private.b16_reason(command->>'reason')));
        END $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea,key_id text,mac bytea,body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE request_id bigint; req f01_private.requests%ROWTYPE; raw json; command jsonb;
        BEGIN
            request_id:=public.f01_register_proof_before_b18(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF req.purpose<>'APPOINTMENT_RESCHEDULE' THEN RETURN request_id; END IF;
            BEGIN
                IF req.query<>'' OR req.target_id IS NULL OR req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                    RAISE EXCEPTION 'Invalid reschedule envelope' USING ERRCODE='22023';
                END IF;
                raw:=convert_from(body,'UTF8')::json;
                PERFORM f01_private.canonical_intake_json(raw);
                command:=f01_private.b18_command(raw);
                UPDATE f01_private.requests r SET b18_body=command,
                    b18_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(jsonb_build_object(
                        'operation','appointments.reschedule','appointment_id',req.target_id,
                        'body',command)::json),'UTF8')),'hex') WHERE r.id=request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b18_authority()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,stage text,
            idempotency_key text,target_id uuid,command jsonb,digest text,number bigint,finished boolean)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,c.stage,r.idempotency_key,r.target_id,
                r.b18_body,r.b18_digest,a.b18_number,a.b18_finished
            FROM public.f01_context() c JOIN f01_private.requests r ON r.id=c.request_id
            JOIN f01_private.activations a ON a.request_id=c.request_id AND a.stage=c.stage
            JOIN public.staff_accounts s ON s.organization_id=r.organization_id AND s.id=r.staff_id
            WHERE r.purpose='APPOINTMENT_RESCHEDULE' AND r.body_scope_valid
                AND r.b18_digest IS NOT NULL AND r.target_id IS NOT NULL
                AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY') AND s.enabled AND s.verified
                AND s.reset_block_operation_id IS NULL AND s.role=r.role
                AND s.security_revision=r.security_revision AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST');
        $$;

        CREATE FUNCTION public.f01_b18_mark(p_number bigint,p_finished boolean) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            UPDATE f01_private.activations a
            SET b18_number=p_number,b18_finished=p_finished
            FROM public.f01_context() c,public.f01_b18_authority() b
            WHERE a.request_id=c.request_id AND a.stage=c.stage AND b.stage='PRIMARY'
                AND NOT a.b18_finished AND p_number>0
                AND ((NOT p_finished AND a.b18_number IS NULL)
                    OR (p_finished AND a.b18_number=p_number));
            IF NOT FOUND THEN RAISE EXCEPTION 'Reschedule stage denied' USING ERRCODE='42501'; END IF;
        END $$;

        CREATE FUNCTION f01_private.assert_b18_complete() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM f01_private.activations a
                WHERE a.request_id=NEW.request_id AND a.stage=NEW.stage AND a.b18_finished) THEN
                RAISE EXCEPTION 'Incomplete reschedule transaction' USING ERRCODE='42501';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER f01_b18_complete AFTER UPDATE ON f01_private.activations
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            WHEN (OLD.b18_number IS NULL AND NEW.b18_number IS NOT NULL)
            EXECUTE FUNCTION f01_private.assert_b18_complete();
        REVOKE ALL ON FUNCTION f01_private.b18_command(json),
            public.f01_register_proof(bytea,text,bytea,bytea),public.f01_b18_authority(),
            public.f01_b18_mark(bigint,boolean),f01_private.assert_b18_complete() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b18_authority(),public.f01_b18_mark(bigint,boolean) TO carepulse_migration;
        """,
        execution_options={"no_parameters": True},
    )
    marker = "OR req.purpose = 'VISIT_FOLLOWUP'"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + "\n                           OR (req.purpose='APPOINTMENT_RESCHEDULE' AND req.b18_digest IS NOT NULL)",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )


def _scope() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b18_scope()
        RETURNS TABLE (organization_id uuid,target_id uuid,patient_id uuid,doctor_staff_id uuid,
            service_date date,destination_date date,stage text,prepared boolean,finished boolean)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT x.organization_id,x.id,x.patient_id,x.doctor_staff_id,x.service_date,
                (a.command->>'service_date')::date,a.stage,a.number IS NOT NULL,a.finished
            FROM public.f01_b18_authority() a JOIN public.appointments x
                ON x.organization_id=a.organization_id AND x.id=a.target_id
            JOIN public.patients p ON p.organization_id=x.organization_id AND p.id=x.patient_id
            WHERE a.role='RECEPTIONIST' OR a.staff_id=x.doctor_staff_id;
        $$;
        REVOKE ALL ON FUNCTION public.f01_b18_scope() FROM PUBLIC;
        CREATE FUNCTION public.f01_b18_primary_scope()
        RETURNS TABLE (organization_id uuid,target_id uuid,patient_id uuid,doctor_staff_id uuid,
            service_date date,destination_date date,stage text,prepared boolean,finished boolean)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT * FROM public.f01_b18_scope() s WHERE s.stage='PRIMARY' AND NOT s.finished;
        $$;
        REVOKE ALL ON FUNCTION public.f01_b18_primary_scope() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b18_primary_scope() TO carepulse_api;
    """)
    for table in ("clinic_policy_heads", "clinic_policy_versions"):
        op.execute(f"""
            CREATE POLICY f01_api_b18_policy_read ON public.{table} FOR SELECT TO carepulse_api USING (
                {table}.kind IN ('SCHEDULE','CAPACITY','FOLLOWUP','PRICE') AND EXISTS (
                    SELECT 1 FROM public.f01_b18_primary_scope() s WHERE s.organization_id={table}.organization_id))
        """)
    for table in ("clinic_schedule_intervals", "clinic_schedule_holidays"):
        op.execute(f"""
            CREATE POLICY f01_api_b18_schedule_read ON public.{table} FOR SELECT TO carepulse_api USING (
                EXISTS (SELECT 1 FROM public.f01_b18_primary_scope() s
                    WHERE s.organization_id={table}.organization_id))
        """)
    _rewrite(
        "public.f01_counted_appointment_intervals(uuid,date)",
        "OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'",
        """OR EXISTS (SELECT 1 FROM public.f01_b18_primary_scope() s
                              WHERE s.organization_id=p_tenant AND s.destination_date=p_service_date)
                          OR (c.purpose = 'APPOINTMENT_BOOKING' AND c.method = 'POST'""",
    )
    _rewrite(
        "public.f01_counted_appointment_intervals(uuid,date)",
        "AND a.service_date = p_service_date",
        """AND a.service_date = p_service_date
                  AND NOT EXISTS (SELECT 1 FROM public.f01_b18_primary_scope() s
                      WHERE s.organization_id=p_tenant AND s.destination_date=p_service_date
                          AND (a.id=s.target_id OR a.doctor_staff_id<>s.doctor_staff_id))""",
    )
    # The protected B-10 slot validator also rechecks capacity immediately at
    # write time. The only exclusion is the actual prepared B-18 target.
    marker = "a.organization_id=org AND a.service_date=day"
    _rewrite(
        "public.b10_schedule_without_offset_guard(uuid,jsonb)",
        marker,
        marker
        + """
                      AND NOT EXISTS (SELECT 1 FROM public.f01_b18_primary_scope() s
                          WHERE s.organization_id=org AND s.destination_date=day AND s.prepared
                              AND (a.id=s.target_id OR a.doctor_staff_id<>s.doctor_staff_id))""",
        count=3,
    )
    # D-08's source context remains private; this branch cannot authorize any
    # separate source decision or extension writer.
    marker = "AND ctx.target_id = visit AND ctx.method IN ('PATCH','POST')))"
    _rewrite(
        "public.d08_context(uuid,uuid,jsonb)",
        marker,
        """AND ctx.target_id = visit AND ctx.method IN ('PATCH','POST')) OR
                    (ctx.purpose='APPOINTMENT_RESCHEDULE' AND ctx.stage='PRIMARY' AND EXISTS (
                        SELECT 1 FROM public.f01_b18_primary_scope() s
                        JOIN public.followup_claims cl ON cl.organization_id=s.organization_id
                            AND cl.appointment_id=s.target_id
                        WHERE s.organization_id=org AND s.prepared AND cl.source_visit_id=visit)))""",
    )


def _guards() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b18_history_scope(h public.appointment_history)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_b18_authority() a
                JOIN public.appointments x ON x.organization_id=a.organization_id AND x.id=a.target_id
                JOIN public.appointment_history old ON old.organization_id=x.organization_id
                    AND old.appointment_id=x.id AND old.appointment_revision=x.revision-1
                WHERE a.stage='PRIMARY' AND a.number IS NOT NULL AND NOT a.finished
                    AND a.organization_id=h.organization_id AND a.target_id=h.appointment_id
                    AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                    AND h.appointment_revision=x.revision AND h.appointment_revision=
                        (a.command->>'expected_appointment_revision')::bigint+1
                    AND h.action='RESCHEDULE' AND h.reason=a.command->>'reason'
                    AND h.old_status=old.new_status AND h.old_service_date=old.new_service_date
                    AND h.old_number=old.new_number AND h.new_status=x.status
                    AND h.new_service_date=x.service_date AND h.new_service_date=(a.command->>'service_date')::date
                    AND h.new_number=x.current_number AND h.new_number=a.number
                    AND h.price_snapshot_appointment_id=x.id AND h.replacement_appointment_id IS NULL);
        $$;
        CREATE FUNCTION public.f01_b18_closed_entry_witness()
        RETURNS SETOF public.queue_entries
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT e.* FROM public.f01_b18_authority() a
            JOIN public.appointments x ON x.organization_id=a.organization_id AND x.id=a.target_id
            JOIN public.appointment_history h ON h.organization_id=x.organization_id
                AND h.appointment_id=x.id AND h.appointment_revision=x.revision
            JOIN public.queue_entries e ON e.organization_id=x.organization_id AND e.appointment_id=x.id
            WHERE a.stage='PRIMARY' AND a.number IS NOT NULL AND x.status='Booked'
                AND x.classification='ROUTINE' AND x.lane='MAIN'
                AND x.revision=(a.command->>'expected_appointment_revision')::bigint+1
                AND h.action='RESCHEDULE' AND h.old_status='Waiting' AND h.new_status='Booked'
                AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role AND h.reason=a.command->>'reason'
                AND h.new_service_date=x.service_date AND h.new_service_date=(a.command->>'service_date')::date
                AND h.old_service_date<>h.new_service_date AND e.service_date=h.old_service_date
                AND e.patient_id=x.patient_id AND e.doctor_staff_id=x.doctor_staff_id AND e.lane=x.lane
                AND e.disposition='CLOSED' AND e.closure_history_revision=x.revision
                AND e.closure_queue_revision=(a.command->>'expected_queue_day_revision')::bigint
                AND e.closure_transaction_id=pg_current_xact_id();
        $$;
        REVOKE ALL ON FUNCTION public.f01_b18_history_scope(public.appointment_history),
            public.f01_b18_closed_entry_witness() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b18_history_scope(public.appointment_history)
            TO carepulse_identity_owner;
    """)
    op.execute(
        "GRANT CREATE ON SCHEMA public TO carepulse_identity_owner; SET LOCAL ROLE carepulse_identity_owner"
    )
    marker = "AND NOT public.f01_b17_history_scope(NEW)"
    _rewrite(
        "f01_private.guard_audit_appointment_history()",
        marker,
        marker + " AND NOT public.f01_b18_history_scope(NEW)",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )
    condition = """TG_OP='UPDATE' AND NEW.arrival_counter=OLD.arrival_counter
        AND NEW.routine_cursor=OLD.routine_cursor AND NEW.revision=OLD.revision+1
        AND EXISTS (SELECT 1 FROM public.f01_b18_closed_entry_witness() e
            WHERE e.organization_id=OLD.organization_id AND e.queue_day_id=OLD.id
                AND e.closure_queue_revision=OLD.revision AND e.closure_routine_cursor=OLD.routine_cursor)"""
    marker = "IF session_user = 'carepulse_api' AND NOT EXISTS ("
    _rewrite(
        "public.f01_guard_signed_queue_day()",
        marker,
        f"IF session_user='carepulse_api' AND {condition} THEN RETURN NEW; END IF; {marker}",
    )
    marker = "ELSIF NEW.arrival_counter = OLD.arrival_counter"
    _rewrite(
        "public.f01_guard_queue_arrival_increment()",
        marker,
        f"ELSIF {condition} THEN RETURN NEW; {marker}",
    )
    marker = "IF OLD.disposition='ACTIVE' AND NEW.disposition='CLOSED'"
    # Both earlier B-15 and B-17 closure branches are retained; use the
    # complete B-17 predicate as the unique insertion point.
    marker = (
        marker + "\n        AND NEW.routine_rank IS NOT DISTINCT FROM OLD.routine_rank"
    )
    _rewrite(
        "public.f01_assert_queue_rank_shift()",
        marker,
        """IF OLD.disposition='ACTIVE' AND NEW.disposition='CLOSED'
        AND NEW.routine_rank IS NOT DISTINCT FROM OLD.routine_rank AND NEW.revision=OLD.revision+1
        AND EXISTS (SELECT 1 FROM public.f01_b18_closed_entry_witness() e
            JOIN public.queue_days q ON q.organization_id=e.organization_id AND q.id=e.queue_day_id
            WHERE e.organization_id=NEW.organization_id AND e.id=NEW.id AND e.revision=NEW.revision
                AND e.closure_history_revision=NEW.closure_history_revision
                AND q.revision=e.closure_queue_revision+1
                AND q.revision_transaction_id=pg_current_xact_id()) THEN RETURN NULL; END IF;
        """
        + marker,
    )


def _prepare_and_replay() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b18_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; s record; r public.idempotency_receipts%ROWTYPE;
            h public.appointment_history%ROWTYPE; p jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b18_authority();
            IF NOT FOUND THEN RAISE EXCEPTION 'Reschedule authority denied' USING ERRCODE='42501'; END IF;
            SELECT * INTO s FROM public.f01_b18_scope();
            IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            SELECT * INTO r FROM public.idempotency_receipts t
                WHERE t.scope_kind='TENANT' AND t.scope_id=a.organization_id AND t.actor_kind='STAFF'
                    AND t.actor_id=a.staff_id AND t.organization_id=a.organization_id AND t.staff_id=a.staff_id
                    AND t.operation='appointments.reschedule' AND t.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Reschedule idempotency conflict' USING ERRCODE='CP001';
            END IF;
            p:=r.result_projection;
            SELECT * INTO h FROM public.appointment_history ah
                WHERE ah.organization_id=a.organization_id AND ah.appointment_id=a.target_id
                    AND ah.appointment_revision=r.result_revision AND ah.action='RESCHEDULE'
                    AND ah.old_status IN ('Booked','Waiting') AND ah.new_status IN ('Booked','Waiting')
                    AND ah.actor_staff_id=a.staff_id AND ah.actor_role=a.role AND ah.reason=a.command->>'reason'
                    AND ah.new_service_date=(a.command->>'service_date')::date;
            IF NOT FOUND OR p->>'appointment_id' IS DISTINCT FROM a.target_id::text
                OR p->>'service_date' IS DISTINCT FROM h.new_service_date::text
                OR p->>'status' IS DISTINCT FROM h.new_status
                OR (p->>'number')::bigint IS DISTINCT FROM h.new_number
                OR (p->>'appointment_revision')::bigint IS DISTINCT FROM r.result_revision
                OR r.result_revision IS DISTINCT FROM (a.command->>'expected_appointment_revision')::bigint+1 THEN
                RAISE EXCEPTION 'Reschedule receipt evidence denied' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',p,'revision',r.result_revision);
        END $$;

        CREATE FUNCTION public.f01_b18_prepare() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; s record; x public.appointments%ROWTYPE; q public.queue_days%ROWTYPE;
            kind_name text; destination date; reserved_number bigint; lock_id uuid; source_appointment uuid;
        BEGIN
            SELECT * INTO a FROM public.f01_b18_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.number IS NOT NULL OR a.finished THEN
                RAISE EXCEPTION 'Reschedule preparation denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO s FROM public.f01_b18_scope();
            IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            destination:=s.destination_date;
            PERFORM 1 FROM public.patients p WHERE p.organization_id=s.organization_id
                AND p.id=s.patient_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Patient unavailable' USING ERRCODE='23514'; END IF;
            FOREACH kind_name IN ARRAY ARRAY['SCHEDULE','CAPACITY','FOLLOWUP','PRICE'] LOOP
                PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id=s.organization_id
                    AND h.kind=kind_name FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Reschedule policy unavailable' USING ERRCODE='23514'; END IF;
            END LOOP;
            IF public.f01_b18_replay() IS NOT NULL THEN
                RAISE EXCEPTION 'Reschedule already committed' USING ERRCODE='CP001';
            END IF;
            SELECT * INTO STRICT x FROM public.appointments ax WHERE ax.organization_id=s.organization_id AND ax.id=s.target_id;
            IF x.revision<>(a.command->>'expected_appointment_revision')::bigint THEN
                RAISE EXCEPTION 'Stale reschedule Appointment' USING ERRCODE='40001';
            END IF;
            IF x.status NOT IN ('Booked','Waiting') OR x.classification NOT IN ('ROUTINE','URGENT')
                OR x.lane<>'MAIN' OR (x.classification='ROUTINE' AND x.status='Waiting' AND destination=x.service_date)
                OR (x.classification='URGENT' AND (x.status<>'Waiting' OR destination<>x.service_date
                    OR destination<>(clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date
                    OR a.command->'physical_presence_confirmed' IS DISTINCT FROM 'true'::jsonb)) THEN
                RAISE EXCEPTION 'Reschedule transition denied' USING ERRCODE='23514';
            END IF;
            IF EXISTS (SELECT 1 FROM public.appointments other
                WHERE other.organization_id=x.organization_id AND other.patient_id=x.patient_id
                    AND other.service_date=destination AND other.id<>x.id
                    AND other.status IN ('Booked','Waiting','In Examination','Completed')) THEN
                RAISE EXCEPTION 'Patient already has a counted destination appointment' USING ERRCODE='23514';
            END IF;
            IF destination<>x.service_date THEN
                INSERT INTO public.appointment_day_counters (organization_id,service_date,last_number)
                VALUES (x.organization_id,destination,1)
                ON CONFLICT (organization_id,service_date) DO UPDATE
                    SET last_number=appointment_day_counters.last_number+1
                RETURNING last_number INTO reserved_number;
            ELSE
                PERFORM 1 FROM public.appointment_day_counters d WHERE d.organization_id=x.organization_id
                    AND d.service_date=destination FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Appointment counter unavailable' USING ERRCODE='23514'; END IF;
                reserved_number:=x.current_number;
            END IF;
            IF x.status='Waiting' THEN
                PERFORM 1 FROM public.doctor_controls d WHERE d.organization_id=x.organization_id
                    AND d.doctor_staff_id=x.doctor_staff_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Doctor control unavailable' USING ERRCODE='23514'; END IF;
                SELECT * INTO q FROM public.queue_days d WHERE d.organization_id=x.organization_id
                    AND d.doctor_staff_id=x.doctor_staff_id AND d.service_date=x.service_date AND d.lane=x.lane FOR UPDATE;
                IF NOT FOUND OR q.revision IS DISTINCT FROM (a.command->>'expected_queue_day_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale Waiting QueueDay' USING ERRCODE='40001';
                END IF;
            ELSIF a.command->>'expected_queue_day_revision' IS NOT NULL THEN
                RAISE EXCEPTION 'Unexpected Booked queue revision' USING ERRCODE='23514';
            END IF;
            SELECT c.source_appointment_id INTO source_appointment FROM public.followup_claims c
                WHERE c.organization_id=x.organization_id AND c.appointment_id=x.id;
            FOR lock_id IN SELECT DISTINCT ids.id FROM unnest(ARRAY[x.id,source_appointment]) AS ids(id)
                WHERE ids.id IS NOT NULL ORDER BY ids.id LOOP
                PERFORM 1 FROM public.appointments ax WHERE ax.organization_id=x.organization_id AND ax.id=lock_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Reschedule Appointment link missing' USING ERRCODE='23514'; END IF;
            END LOOP;
            SELECT * INTO STRICT x FROM public.appointments ax WHERE ax.organization_id=s.organization_id AND ax.id=s.target_id;
            IF x.revision<>(a.command->>'expected_appointment_revision')::bigint
                OR x.patient_id<>s.patient_id OR x.doctor_staff_id<>s.doctor_staff_id
                OR x.service_date<>s.service_date OR (x.status='Waiting' AND q.id IS NULL) THEN
                RAISE EXCEPTION 'Stale locked Appointment' USING ERRCODE='40001';
            END IF;
            IF x.status='Waiting' THEN
                PERFORM 1 FROM public.queue_entries e WHERE e.organization_id=x.organization_id
                    AND e.appointment_id=x.id AND e.queue_day_id=q.id AND e.service_date=x.service_date
                    AND e.patient_id=x.patient_id AND e.doctor_staff_id=x.doctor_staff_id
                    AND e.lane=x.lane AND e.disposition='ACTIVE' FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Active Waiting entry unavailable' USING ERRCODE='23514'; END IF;
            END IF;
            PERFORM public.f01_b18_mark(reserved_number,false);
            RETURN jsonb_build_object('appointment_id',x.id,'classification',x.classification);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b18_prepare(),public.f01_b18_replay() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b18_prepare(),public.f01_b18_replay() TO carepulse_api;
    """)


def _followup() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b18_check_claim(x public.appointments, command jsonb)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; cl public.followup_claims%ROWTYPE; extension public.followup_authorization_events%ROWTYPE;
            source_context jsonb; policy public.clinic_policy_heads%ROWTYPE; auth_revision bigint; destination date;
        BEGIN
            SELECT * INTO a FROM public.f01_b18_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.number IS NULL OR a.finished
                OR a.organization_id<>x.organization_id OR a.target_id<>x.id OR a.command IS DISTINCT FROM command THEN
                RAISE EXCEPTION 'Reschedule claim scope denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO cl FROM public.followup_claims c WHERE c.organization_id=x.organization_id AND c.appointment_id=x.id;
            IF x.service_type<>'FOLLOW_UP' THEN
                IF cl.id IS NOT NULL OR EXISTS (SELECT 1 FROM jsonb_each(command) f
                    WHERE f.key IN ('expected_claim_revision','expected_source_decision_revision','expected_visit_revision',
                        'expected_followup_revision','expected_authorization_revision','extension_event_id')
                        AND f.value<>'null'::jsonb) THEN
                    RAISE EXCEPTION 'Unexpected claim evidence' USING ERRCODE='23514';
                END IF;
                RETURN NULL;
            END IF;
            IF cl.id IS NULL OR cl.patient_id<>x.patient_id OR cl.doctor_staff_id<>x.doctor_staff_id
                OR cl.state<>'ACTIVE' OR cl.revision IS DISTINCT FROM (command->>'expected_claim_revision')::bigint
                OR NOT EXISTS (SELECT 1 FROM public.appointment_price_snapshots ps
                    WHERE ps.organization_id=x.organization_id AND ps.appointment_id=x.id
                        AND ps.entitlement_provenance=cl.policy_snapshot) THEN
                RAISE EXCEPTION 'Stale reschedule claim' USING ERRCODE='40001';
            END IF;
            SELECT * INTO STRICT policy FROM public.clinic_policy_heads h
                WHERE h.organization_id=x.organization_id AND h.kind='FOLLOWUP';
            IF policy.revision IS DISTINCT FROM (command->>'expected_followup_revision')::bigint THEN
                RAISE EXCEPTION 'Stale Follow-up policy' USING ERRCODE='40001';
            END IF;
            source_context:=public.d08_context(x.organization_id,cl.source_visit_id,
                jsonb_build_object('source_revision',(command->>'expected_source_decision_revision')::bigint,
                    'clinical_revision',(command->>'expected_visit_revision')::bigint,'policy_version',policy.current_version));
            PERFORM 1 FROM public.followup_claims c WHERE c.organization_id=x.organization_id AND c.id=cl.id FOR UPDATE;
            SELECT * INTO STRICT cl FROM public.followup_claims c WHERE c.organization_id=x.organization_id AND c.id=cl.id;
            IF cl.state<>'ACTIVE' OR cl.revision IS DISTINCT FROM (command->>'expected_claim_revision')::bigint THEN
                RAISE EXCEPTION 'Stale locked Follow-up claim' USING ERRCODE='40001';
            END IF;
            SELECT coalesce(max(e.authorization_revision),0) INTO auth_revision
                FROM public.followup_authorization_events e WHERE e.organization_id=x.organization_id AND e.claim_id=cl.id;
            IF auth_revision IS DISTINCT FROM (command->>'expected_authorization_revision')::bigint THEN
                RAISE EXCEPTION 'Stale Follow-up authorization' USING ERRCODE='40001';
            END IF;
            destination:=(command->>'service_date')::date;
            PERFORM public.d08_check_authorized_date(source_context,destination);
            IF destination>cl.approved_deadline OR command->>'extension_event_id' IS NOT NULL THEN
                SELECT * INTO extension FROM public.followup_authorization_events e
                    WHERE e.organization_id=x.organization_id AND e.claim_id=cl.id AND e.appointment_id=x.id
                        AND e.source_visit_id=cl.source_visit_id AND e.kind='EXTENSION'
                        AND e.id=(command->>'extension_event_id')::uuid AND e.authorization_revision=auth_revision
                        AND e.claim_revision=cl.revision AND e.appointment_revision<=x.revision
                        AND e.new_deadline>=destination AND e.new_deadline>=e.prior_deadline
                        AND e.reason IS NOT NULL AND btrim(e.reason)<>'';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Move requires prior confirmed extension' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN cl.revision;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b18_check_claim(public.appointments,jsonb) FROM PUBLIC;
    """)


def _apply() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b18_schedule(x public.appointments, command jsonb)
        RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE schedule_version bigint; schedule_policy jsonb; destination date;
            local_now timestamp:=clock_timestamp() AT TIME ZONE 'Africa/Cairo';
            local_weekday integer:=extract(isodow FROM local_now); override_work boolean;
        BEGIN
            destination:=(command->>'service_date')::date;
            SELECT h.current_version,v.payload INTO schedule_version,schedule_policy
                FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                    ON (v.organization_id,v.kind,v.version)=(h.organization_id,h.kind,h.current_version)
                WHERE h.organization_id=x.organization_id AND h.kind='SCHEDULE'
                    AND h.revision=(command->>'expected_schedule_revision')::bigint;
            IF NOT FOUND THEN RAISE EXCEPTION 'Stale reschedule schedule' USING ERRCODE='40001'; END IF;
            IF schedule_policy->>'booking_mode'='EXACT_TIME' AND (schedule_policy->>'allow_overlap')::boolean
                AND NOT EXISTS (SELECT 1 FROM public.clinic_policy_heads h WHERE h.organization_id=x.organization_id
                    AND h.kind='CAPACITY' AND h.revision=(command->>'expected_capacity_revision')::bigint) THEN
                RAISE EXCEPTION 'Stale reschedule capacity' USING ERRCODE='40001';
            END IF;
            PERFORM public.b10_assert_followup_schedule(x.organization_id,jsonb_build_object(
                'service_date',destination,'booking_mode',schedule_policy->>'booking_mode',
                'starts_at',command->'starts_at','ends_at',command->'ends_at'));
            IF x.classification='URGENT' THEN
                IF destination<>x.service_date OR destination<>local_now::date
                    OR command->'physical_presence_confirmed' IS DISTINCT FROM 'true'::jsonb
                    OR NOT EXISTS (SELECT 1 FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                        ON (v.organization_id,v.kind,v.version)=(h.organization_id,h.kind,h.current_version)
                        WHERE h.organization_id=x.organization_id AND h.kind='PRICE'
                            AND h.revision=(command->>'expected_price_revision')::bigint
                            AND v.payload->'urgent_enabled'='true'::jsonb) THEN
                    RAISE EXCEPTION 'Urgent reschedule evidence or policy denied' USING ERRCODE='23514';
                END IF;
                SELECT EXISTS (SELECT 1 FROM public.clinic_schedule_intervals i
                    WHERE i.organization_id=x.organization_id AND i.policy_version=schedule_version
                        AND i.interval_kind='OVERRIDE_WORK' AND i.service_date=destination) INTO override_work;
                IF EXISTS (SELECT 1 FROM public.clinic_schedule_holidays h
                        WHERE h.organization_id=x.organization_id AND h.policy_version=schedule_version
                            AND h.service_date=destination)
                    OR (NOT override_work AND (coalesce(schedule_policy->'open_override_dates' @> to_jsonb(ARRAY[destination::text]),false)
                        OR (schedule_policy ? 'working_weekdays'
                            AND NOT schedule_policy->'working_weekdays' @> to_jsonb(ARRAY[local_weekday]))))
                    OR NOT EXISTS (SELECT 1 FROM public.clinic_schedule_intervals i
                        WHERE i.organization_id=x.organization_id AND i.policy_version=schedule_version
                            AND ((override_work AND i.interval_kind='OVERRIDE_WORK' AND i.service_date=destination)
                                OR (NOT override_work AND i.interval_kind='WORK' AND i.weekday=local_weekday))
                            AND i.starts_at<=local_now::time AND local_now::time<i.ends_at)
                    OR EXISTS (SELECT 1 FROM public.clinic_schedule_intervals i
                        WHERE i.organization_id=x.organization_id AND i.policy_version=schedule_version
                            AND i.interval_kind='BREAK' AND i.weekday=local_weekday
                            AND i.starts_at<=local_now::time AND local_now::time<i.ends_at) THEN
                    RAISE EXCEPTION 'Urgent reschedule outside work period' USING ERRCODE='23514';
                END IF;
            ELSIF command->>'physical_presence_confirmed' IS NOT NULL THEN
                RAISE EXCEPTION 'Unexpected Urgent presence evidence' USING ERRCODE='23514';
            END IF;
            RETURN schedule_policy->>'booking_mode';
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b18_schedule(public.appointments,jsonb) FROM PUBLIC;

        CREATE FUNCTION public.f01_b18_apply() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; s record; x public.appointments%ROWTYPE; changed public.appointments%ROWTYPE;
            q public.queue_days%ROWTYPE; assignment uuid; history_id uuid:=gen_random_uuid();
            destination date; mode text; moment timestamptz; claim_revision bigint; day_revision bigint;
            before_state jsonb; after_state jsonb; projection jsonb; payload jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b18_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.number IS NULL OR a.finished THEN
                RAISE EXCEPTION 'Reschedule write scope denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO s FROM public.f01_b18_scope();
            IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            SELECT * INTO STRICT x FROM public.appointments ax WHERE ax.organization_id=s.organization_id AND ax.id=s.target_id;
            IF x.revision<>(a.command->>'expected_appointment_revision')::bigint
                OR x.status NOT IN ('Booked','Waiting') THEN
                RAISE EXCEPTION 'Stale reschedule Appointment' USING ERRCODE='40001';
            END IF;
            destination:=s.destination_date;
            claim_revision:=public.f01_b18_check_claim(x,a.command);
            mode:=public.f01_b18_schedule(x,a.command);
            IF x.status='Waiting' THEN
                SELECT * INTO q FROM public.queue_days d WHERE d.organization_id=x.organization_id
                    AND d.doctor_staff_id=x.doctor_staff_id AND d.service_date=x.service_date AND d.lane=x.lane;
                IF NOT FOUND OR q.revision IS DISTINCT FROM (a.command->>'expected_queue_day_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale Waiting QueueDay' USING ERRCODE='40001';
                END IF;
                day_revision:=q.revision;
            END IF;
            IF destination<>x.service_date THEN
                assignment:=gen_random_uuid();
                INSERT INTO public.appointment_number_assignments (id,organization_id,appointment_id,service_date,number,prior_assignment_id)
                VALUES (assignment,x.organization_id,x.id,destination,a.number,x.current_assignment_id);
            ELSE
                assignment:=x.current_assignment_id;
            END IF;
            UPDATE public.appointments ax SET
                service_date=destination,booking_mode=mode,
                starts_at=(a.command->>'starts_at')::timestamptz,ends_at=(a.command->>'ends_at')::timestamptz,
                status=CASE WHEN destination<>x.service_date THEN 'Booked' ELSE x.status END,
                current_number=a.number,current_assignment_id=assignment,revision=x.revision+1
                WHERE ax.organization_id=x.organization_id AND ax.id=x.id AND ax.revision=x.revision
                RETURNING * INTO changed;
            IF NOT FOUND THEN RAISE EXCEPTION 'Stale reschedule target' USING ERRCODE='40001'; END IF;
            INSERT INTO public.appointment_history (id,organization_id,appointment_id,appointment_revision,action,
                old_status,new_status,old_service_date,new_service_date,old_number,new_number,actor_staff_id,
                actor_role,reason,price_snapshot_appointment_id)
            VALUES (history_id,x.organization_id,x.id,changed.revision,'RESCHEDULE',x.status,changed.status,
                x.service_date,changed.service_date,x.current_number,changed.current_number,a.staff_id,a.role,
                a.command->>'reason',x.id) RETURNING occurred_at INTO moment;
            IF x.status='Waiting' AND changed.status='Booked' THEN
                UPDATE public.queue_entries e SET disposition='CLOSED',closure_history_revision=changed.revision,
                    revision=e.revision+1 WHERE e.organization_id=x.organization_id AND e.appointment_id=x.id
                    AND e.queue_day_id=q.id AND e.disposition='ACTIVE';
                IF NOT FOUND THEN RAISE EXCEPTION 'Waiting entry unavailable' USING ERRCODE='23514'; END IF;
                UPDATE public.queue_days d SET revision=d.revision+1 WHERE d.organization_id=x.organization_id AND d.id=q.id
                    RETURNING d.revision INTO day_revision;
            END IF;
            before_state:=jsonb_build_object('appointment_id',x.id,'revision',x.revision,'status',x.status,
                'service_date',x.service_date,'number',x.current_number,'booking_mode',x.booking_mode,
                'starts_at',x.starts_at,'ends_at',x.ends_at);
            after_state:=jsonb_build_object('appointment_id',changed.id,'revision',changed.revision,'status',changed.status,
                'service_date',changed.service_date,'number',changed.current_number,'booking_mode',changed.booking_mode,
                'starts_at',changed.starts_at,'ends_at',changed.ends_at);
            IF x.classification='URGENT' THEN
                after_state:=after_state || jsonb_build_object('physical_presence_confirmed',true);
            END IF;
            INSERT INTO public.sensitive_audit (id,organization_id,actor_staff_id,actor_role,action,reason,
                correlation_id,before_state,after_state,occurred_at)
            VALUES (gen_random_uuid(),x.organization_id,a.staff_id,a.role,'APPOINTMENT_RESCHEDULED',a.command->>'reason',
                history_id::text,before_state,after_state,moment);
            payload:=jsonb_build_object('appointment_id',x.id,'status',changed.status,'service_date',changed.service_date,
                'appointment_revision',changed.revision);
            INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
            VALUES (gen_random_uuid(),x.organization_id,'APPOINTMENT_RESCHEDULED','APPOINTMENT',x.id::text,
                payload || jsonb_build_object('actor_staff_id',a.staff_id,'actor_role',a.role));
            IF a.role='DOCTOR_OWNER' THEN
                INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
                VALUES (gen_random_uuid(),x.organization_id,'DOCTOR_RESCHEDULED_APPOINTMENT','APPOINTMENT',x.id::text,payload);
            END IF;
            projection:=jsonb_build_object('appointment_id',x.id,'appointment_revision',changed.revision,
                'status',changed.status,'service_date',changed.service_date,'number',changed.current_number,
                'booking_mode',changed.booking_mode,'starts_at',changed.starts_at,'ends_at',changed.ends_at,
                'queue_day_revision',day_revision,'claim_revision',claim_revision);
            INSERT INTO public.idempotency_receipts (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,a.staff_id,
                'appointments.reschedule',a.idempotency_key,a.digest,projection,changed.revision);
            PERFORM public.f01_b18_mark(a.number,true);
            RETURN jsonb_build_object('projection',projection,'revision',changed.revision);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b18_apply() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b18_apply() TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _scope()
    _guards()
    _prepare_and_replay()
    _followup()
    _apply()


def downgrade() -> None:
    raise RuntimeError("B-18 migration is forward-only")
