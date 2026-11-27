"""B-17: target-bound cancellation and atomic new-ID Urgent replacement."""

from alembic import op

revision = "0061_b17_cancel_replace"
down_revision = "0060_b16_visits"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Missing unique B-17 marker in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b17_body jsonb, ADD COLUMN b17_digest text;
        ALTER TABLE f01_private.activations ADD COLUMN b17_new_id uuid,
            ADD COLUMN b17_started boolean NOT NULL DEFAULT false,
            ADD COLUMN b17_old_projection jsonb, ADD COLUMN b17_finished boolean NOT NULL DEFAULT false;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method='POST' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '/cancel$')
                    THEN 'APPOINTMENT_CANCEL'
                WHEN p_method='POST' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '/replace-urgent$')
                    THEN 'APPOINTMENT_REPLACE_URGENT'""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b17;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b17(bytea,text,bytea,bytea)
            FROM PUBLIC,carepulse_gate;

        CREATE FUNCTION f01_private.b17_operation(purpose text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
            SELECT CASE purpose WHEN 'APPOINTMENT_CANCEL' THEN 'appointments.cancel'
                WHEN 'APPOINTMENT_REPLACE_URGENT' THEN 'appointments.replace_urgent' END;
        $$;

        CREATE FUNCTION f01_private.b17_command(purpose text,raw json) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
        DECLARE command jsonb:=raw::jsonb; d jsonb; field text;
            permitted text[]:=ARRAY['expected_appointment_revision','expected_queue_day_revision',
                'expected_claim_revision','expected_source_revision','expected_authorization_revision','reason'];
        BEGIN
            IF jsonb_typeof(command) IS DISTINCT FROM 'object'
                OR NOT f01_private.b16_revision(raw->'expected_appointment_revision')
                OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                OR f01_private.b16_reason(command->>'reason') IS NULL THEN
                RAISE EXCEPTION 'Invalid cancellation command' USING ERRCODE='22023';
            END IF;
            IF purpose='APPOINTMENT_REPLACE_URGENT' THEN
                permitted:=permitted || ARRAY['confirmation','physical_presence_confirmed','discount',
                    'expected_base_egp','expected_net_egp','expected_price_revision','expected_schedule_revision',
                    'expected_capacity_revision','starts_at','ends_at'];
                d:=command->'discount';
                IF command->'confirmation' IS DISTINCT FROM to_jsonb('REPLACE'::text)
                    OR command->'physical_presence_confirmed' IS DISTINCT FROM 'true'::jsonb
                    OR NOT f01_private.b16_revision(raw->'expected_price_revision')
                    OR NOT f01_private.b16_revision(raw->'expected_schedule_revision')
                    OR jsonb_typeof(d) IS DISTINCT FROM 'object'
                    OR EXISTS (SELECT 1 FROM jsonb_object_keys(d) k WHERE k NOT IN
                        ('discount_type','discount_value','discount_reason'))
                    OR coalesce(d->>'discount_type','') NOT IN ('NONE','FIXED','PERCENT')
                    OR jsonb_typeof(d->'discount_value') IS DISTINCT FROM 'string'
                    OR coalesce(jsonb_typeof(d->'discount_reason'),'null') NOT IN ('null','string')
                    OR (d->>'discount_type'='PERCENT' AND d->>'discount_value'
                        !~ '^((0|[1-9][0-9]?)(\.[0-9]+)?|100(\.0+)?)$')
                    OR (d->>'discount_type'<>'PERCENT' AND d->>'discount_value' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$')
                    OR (d->>'discount_type'='NONE' AND (d->>'discount_reason' IS NOT NULL
                        OR (d->>'discount_value')::numeric<>0))
                    OR (d->>'discount_type'<>'NONE' AND f01_private.b16_reason(d->>'discount_reason') IS NULL)
                    OR jsonb_typeof(command->'expected_base_egp') IS DISTINCT FROM 'string'
                    OR jsonb_typeof(command->'expected_net_egp') IS DISTINCT FROM 'string'
                    OR command->>'expected_base_egp' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$'
                    OR command->>'expected_net_egp' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$'
                    OR ((command ? 'starts_at' OR command ? 'ends_at') AND
                        (jsonb_typeof(command->'starts_at') IS DISTINCT FROM 'string'
                            OR jsonb_typeof(command->'ends_at') IS DISTINCT FROM 'string')) THEN
                    RAISE EXCEPTION 'Invalid Urgent replacement command' USING ERRCODE='22023';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_object_keys(command) k WHERE k<>ALL(permitted)) THEN
                RAISE EXCEPTION 'Unexpected cancellation command field' USING ERRCODE='22023';
            END IF;
            FOREACH field IN ARRAY permitted LOOP
                IF field LIKE 'expected_%_revision' AND command->>field IS NOT NULL
                    AND NOT f01_private.b16_revision(raw->field) THEN
                    RAISE EXCEPTION 'Invalid cancellation revision' USING ERRCODE='22023';
                END IF;
            END LOOP;
            RETURN jsonb_strip_nulls(jsonb_set(command,'{reason}',to_jsonb(f01_private.b16_reason(command->>'reason'))));
        END $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea,key_id text,mac bytea,body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE request_id bigint; req f01_private.requests%ROWTYPE; raw json; command jsonb;
        BEGIN
            request_id:=public.f01_register_proof_before_b17(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF f01_private.b17_operation(req.purpose) IS NULL THEN RETURN request_id; END IF;
            BEGIN
                IF req.query<>'' OR req.target_id IS NULL OR req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                    RAISE EXCEPTION 'Invalid cancellation envelope' USING ERRCODE='22023';
                END IF;
                raw:=convert_from(body,'UTF8')::json;
                PERFORM f01_private.canonical_intake_json(raw);
                command:=f01_private.b17_command(req.purpose,raw);
                UPDATE f01_private.requests r SET b17_body=command,
                    b17_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(jsonb_build_object(
                        'operation',f01_private.b17_operation(req.purpose),'appointment_id',req.target_id,
                        'body',command)::json),'UTF8')),'hex') WHERE r.id=request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b17_authority()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,stage text,purpose text,
            operation text,idempotency_key text,target_id uuid,command jsonb,digest text,
            new_id uuid,started boolean,old_projection jsonb,finished boolean,
            allocated_number bigint,day_id uuid,entry_id uuid,created_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,c.stage,r.purpose,f01_private.b17_operation(r.purpose),
                r.idempotency_key,r.target_id,r.b17_body,r.b17_digest,a.b17_new_id,a.b17_started,
                a.b17_old_projection,a.b17_finished,a.intake_number,a.intake_day_id,a.intake_entry_id,a.audit_appointment_id
            FROM public.f01_context() c JOIN f01_private.requests r ON r.id=c.request_id
            JOIN f01_private.activations a ON a.request_id=c.request_id AND a.stage=c.stage
            JOIN public.staff_accounts s ON s.organization_id=r.organization_id AND s.id=r.staff_id
            WHERE r.body_scope_valid AND r.b17_digest IS NOT NULL AND r.target_id IS NOT NULL
                AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY') AND s.enabled AND s.verified
                AND s.reset_block_operation_id IS NULL AND s.role=r.role AND s.security_revision=r.security_revision
                AND ((r.purpose='APPOINTMENT_CANCEL' AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST'))
                    OR (r.purpose='APPOINTMENT_REPLACE_URGENT' AND r.role='RECEPTIONIST'));
        $$;

        CREATE FUNCTION public.f01_b17_start(p_new uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            UPDATE f01_private.activations a SET b17_started=true,b17_new_id=p_new
            FROM public.f01_context() c,public.f01_b17_authority() b
            WHERE a.request_id=c.request_id AND a.stage=c.stage AND b.stage='PRIMARY'
                AND NOT a.b17_started AND (p_new IS NOT NULL)=(b.purpose='APPOINTMENT_REPLACE_URGENT');
            IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation start denied' USING ERRCODE='42501'; END IF;
        END $$;

        CREATE FUNCTION public.f01_b17_record(p_projection jsonb) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            UPDATE f01_private.activations a SET b17_old_projection=p_projection
            FROM public.f01_context() c,public.f01_b17_authority() b
            WHERE a.request_id=c.request_id AND a.stage=c.stage AND b.stage='PRIMARY'
                AND a.b17_started AND a.b17_old_projection IS NULL;
            IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation record denied' USING ERRCODE='42501'; END IF;
        END $$;

        CREATE FUNCTION public.f01_b17_mark_finished() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            UPDATE f01_private.activations a SET b17_finished=true
            FROM public.f01_context() c,public.f01_b17_authority() b
            WHERE a.request_id=c.request_id AND a.stage=c.stage AND b.stage='PRIMARY'
                AND a.b17_started AND a.b17_old_projection IS NOT NULL AND NOT a.b17_finished;
            IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation finish denied' USING ERRCODE='42501'; END IF;
        END $$;

        CREATE FUNCTION f01_private.assert_b17_complete() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM f01_private.activations a WHERE a.request_id=NEW.request_id
                AND a.stage=NEW.stage AND a.b17_finished) THEN
                RAISE EXCEPTION 'Incomplete cancellation transaction' USING ERRCODE='42501';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER f01_b17_complete AFTER UPDATE ON f01_private.activations
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NOT OLD.b17_started AND NEW.b17_started)
            EXECUTE FUNCTION f01_private.assert_b17_complete();
        REVOKE ALL ON FUNCTION f01_private.b17_operation(text),f01_private.b17_command(text,json),
            public.f01_register_proof(bytea,text,bytea,bytea),public.f01_b17_authority(),
            public.f01_b17_start(uuid),public.f01_b17_record(jsonb),public.f01_b17_mark_finished(),
            f01_private.assert_b17_complete() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b17_authority(),public.f01_b17_start(uuid),
            public.f01_b17_record(jsonb),public.f01_b17_mark_finished() TO carepulse_migration;
        """,
        execution_options={"no_parameters": True},
    )
    marker = "OR req.purpose = 'VISIT_FOLLOWUP'"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + "\n                           OR (f01_private.b17_operation(req.purpose) IS NOT NULL AND req.b17_digest IS NOT NULL)",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )


def _target_scope() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b17_scope()
        RETURNS TABLE (organization_id uuid,target_id uuid,patient_id uuid,doctor_staff_id uuid,
            service_date date,lane text,stage text,purpose text)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT x.organization_id,x.id,x.patient_id,x.doctor_staff_id,x.service_date,x.lane,a.stage,a.purpose
            FROM public.f01_b17_authority() a JOIN public.appointments x
                ON x.organization_id=a.organization_id AND x.id=a.target_id
            JOIN public.patients p ON p.organization_id=x.organization_id AND p.id=x.patient_id
            WHERE a.role='RECEPTIONIST' OR a.staff_id=x.doctor_staff_id;
        $$;
        REVOKE ALL ON FUNCTION public.f01_b17_scope() FROM PUBLIC;
        CREATE FUNCTION public.f01_b17_primary_scope()
        RETURNS TABLE (organization_id uuid,target_id uuid,patient_id uuid,doctor_staff_id uuid,
            service_date date,lane text,stage text,purpose text)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT * FROM public.f01_b17_scope() s WHERE s.stage='PRIMARY';
        $$;
        REVOKE ALL ON FUNCTION public.f01_b17_primary_scope() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b17_primary_scope() TO carepulse_api;

        CREATE POLICY f01_api_b17_target ON public.appointments FOR SELECT TO carepulse_api USING (
            EXISTS (SELECT 1 FROM public.f01_b17_primary_scope() s WHERE s.organization_id=appointments.organization_id
                AND s.target_id=appointments.id));
        CREATE POLICY f01_api_b17_patient ON public.patients FOR SELECT TO carepulse_api USING (
            EXISTS (SELECT 1 FROM public.f01_b17_primary_scope() s WHERE s.organization_id=patients.organization_id
                AND s.patient_id=patients.id));
        CREATE POLICY f01_api_b17_patient_lock ON public.patients FOR UPDATE TO carepulse_api USING (
            EXISTS (SELECT 1 FROM public.f01_b17_primary_scope() s WHERE s.organization_id=patients.organization_id
                AND s.patient_id=patients.id AND s.stage='PRIMARY')) WITH CHECK (false);
    """)
    for table in ("clinic_policy_heads", "clinic_policy_versions"):
        predicate = f"""EXISTS (SELECT 1 FROM public.f01_b17_primary_scope() s WHERE s.organization_id={table}.organization_id
            AND s.stage='PRIMARY' AND ({table}.kind IN ('SCHEDULE','FOLLOWUP') OR
                (s.purpose='APPOINTMENT_REPLACE_URGENT' AND {table}.kind IN ('PRICE','CAPACITY','QUEUE'))))"""
        op.execute(
            f"CREATE POLICY f01_api_b17_read ON public.{table} FOR SELECT TO carepulse_api USING ({predicate})"
        )
        if table == "clinic_policy_heads":
            op.execute(
                f"CREATE POLICY f01_api_b17_lock ON public.{table} FOR UPDATE TO carepulse_api USING ({predicate}) WITH CHECK (false)"
            )


def _intake_bridge() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b17_intake_scope()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,stage text,
            purpose text,operation text,idempotency_key text,patient_id uuid,service_date date,
            classification text,lane text,command jsonb,digest text,appointment_id uuid,
            day_id uuid,entry_id uuid,allocated_number bigint)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT a.organization_id,a.staff_id,a.role,a.stage,'INTAKE_URGENT'::text,
                a.operation,a.idempotency_key,x.patient_id,x.service_date,'URGENT'::text,'MAIN'::text,
                a.command || jsonb_build_object('b17_linked_id',a.new_id,'b17_ready',a.old_projection IS NOT NULL,
                    'b17_doctor_id',x.doctor_staff_id),
                a.digest,a.created_id,a.day_id,a.entry_id,a.allocated_number
            FROM public.f01_b17_authority() a JOIN public.appointments x
                ON x.organization_id=a.organization_id AND x.id=a.target_id
            WHERE a.stage='PRIMARY' AND a.purpose='APPOINTMENT_REPLACE_URGENT' AND NOT a.finished
                AND x.classification='ROUTINE' AND x.lane='MAIN'
                AND x.service_date=(clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date
                AND (x.status IN ('Booked','Waiting') OR (x.status='Cancelled' AND a.started));
        $$;
        REVOKE ALL ON FUNCTION public.f01_b17_intake_scope() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b17_intake_scope() TO carepulse_identity_owner;
    """)
    op.execute(
        "GRANT CREATE ON SCHEMA public TO carepulse_identity_owner; SET LOCAL ROLE carepulse_identity_owner"
    )
    marker = "AND r.intake_digest IS NOT NULL;"
    _rewrite(
        "public.f01_intake_authority()",
        marker,
        marker[:-1] + " UNION ALL SELECT * FROM public.f01_b17_intake_scope();",
    )
    marker = "ctx.purpose NOT IN ('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT')"
    _rewrite(
        "public.f01_record_intake_progress()",
        marker,
        "ctx.purpose NOT IN ('INTAKE_ROUTINE','INTAKE_EMERGENCY','INTAKE_URGENT','APPOINTMENT_REPLACE_URGENT')",
    )
    marker = "ELSIF TG_TABLE_NAME = 'queue_days' THEN"
    _rewrite(
        "public.f01_record_intake_progress()",
        marker,
        marker
        + """
                IF ctx.purpose='APPOINTMENT_REPLACE_URGENT' AND TG_OP='UPDATE' THEN
                    IF NEW.arrival_counter=OLD.arrival_counter THEN RETURN NULL; END IF;
                END IF;""",
    )
    marker = "AND a.intake_audit_recorded AND a.intake_outbox_recorded AND a.intake_receipt_recorded"
    _rewrite(
        "f01_private.assert_intake_complete()",
        marker,
        "AND ((a.intake_audit_recorded AND a.intake_outbox_recorded AND a.intake_receipt_recorded) OR a.b17_finished)",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )
    marker = "OR i.classification <> NEW.classification OR i.lane <> NEW.lane THEN"
    _rewrite(
        "public.f01_guard_intake_appointment()",
        marker,
        marker[:-5]
        + """
                OR (i.operation='appointments.replace_urgent' AND
                    ((i.command->>'b17_linked_id')::uuid IS DISTINCT FROM NEW.id
                        OR (i.command->>'b17_doctor_id')::uuid IS DISTINCT FROM NEW.doctor_staff_id
                        OR i.command->'b17_ready' IS DISTINCT FROM 'true'::jsonb)) THEN""",
    )
    marker = "WHERE i.stage = 'PRIMARY' AND i.day_id IS NULL"
    _rewrite(
        "public.f01_guard_signed_queue_day()",
        marker,
        marker
        + """
                  AND (i.operation<>'appointments.replace_urgent' OR (i.command->'b17_ready'='true'::jsonb
                        AND (i.command->>'b17_doctor_id')::uuid=NEW.doctor_staff_id))""",
    )
    # Do not inherit B-11's caller-writable evidence policies for this operation.
    for table in ("sensitive_audit", "outbox_events", "idempotency_receipts"):
        op.execute(f"""CREATE POLICY f01_api_b17_protected_evidence ON public.{table}
            AS RESTRICTIVE FOR INSERT TO carepulse_api WITH CHECK (
                NOT EXISTS (SELECT 1 FROM public.f01_b17_primary_scope()))""")
    op.execute("""
        CREATE POLICY f01_api_b17_receipt_private ON public.idempotency_receipts AS RESTRICTIVE
            FOR SELECT TO carepulse_api USING (NOT EXISTS (SELECT 1 FROM public.f01_b17_primary_scope()));
        CREATE POLICY f01_api_b17_appointment_read ON public.appointments AS RESTRICTIVE
            FOR SELECT TO carepulse_api USING (
                NOT EXISTS (SELECT 1 FROM public.f01_b17_primary_scope()) OR EXISTS (
                    SELECT 1 FROM public.f01_b17_primary_scope() s WHERE s.organization_id=appointments.organization_id
                        AND (appointments.id=s.target_id OR EXISTS (
                            SELECT 1 FROM public.f01_intake_authority() i
                            WHERE i.operation='appointments.replace_urgent' AND i.appointment_id=appointments.id))));
        CREATE POLICY f01_api_b17_queue_entry_read ON public.queue_entries AS RESTRICTIVE
            FOR SELECT TO carepulse_api USING (
                NOT EXISTS (SELECT 1 FROM public.f01_b17_primary_scope()) OR EXISTS (
                    SELECT 1 FROM public.f01_b17_primary_scope() s WHERE s.organization_id=queue_entries.organization_id
                        AND (queue_entries.appointment_id=s.target_id OR EXISTS (
                            SELECT 1 FROM public.f01_intake_authority() i
                            WHERE i.operation='appointments.replace_urgent' AND i.appointment_id=queue_entries.appointment_id))));
    """)


def _guards() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b17_history_scope(h public.appointment_history)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_b17_authority() a
                JOIN public.appointments x ON x.organization_id=a.organization_id AND x.id=h.appointment_id
                WHERE a.stage='PRIMARY' AND a.started AND NOT a.finished
                    AND a.organization_id=h.organization_id AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                    AND h.new_service_date=x.service_date AND h.new_number=x.current_number
                    AND h.price_snapshot_appointment_id=x.id AND h.appointment_revision=x.revision
                    AND ((x.id=a.target_id AND h.action='STATUS' AND x.status='Cancelled'
                        AND h.old_status IN ('Booked','Waiting') AND h.new_status='Cancelled'
                        AND x.revision=(a.command->>'expected_appointment_revision')::bigint+1
                        AND h.reason=a.command->>'reason' AND h.old_service_date=x.service_date
                        AND h.old_number=x.current_number AND h.replacement_appointment_id IS NOT DISTINCT FROM a.new_id)
                    OR (x.id=a.new_id AND x.id=a.created_id AND a.old_projection IS NOT NULL
                        AND h.action='CREATED' AND x.status='Waiting' AND h.new_status='Waiting'
                        AND h.appointment_revision=0 AND h.old_status IS NULL AND h.reason IS NULL
                        AND h.replacement_appointment_id IS NULL)));
        $$;
        CREATE FUNCTION public.f01_b17_closed_entry_witness()
        RETURNS SETOF public.queue_entries LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT e.* FROM public.f01_b17_authority() a
            JOIN public.appointments x ON x.organization_id=a.organization_id AND x.id=a.target_id
            JOIN public.queue_entries e ON e.organization_id=x.organization_id AND e.appointment_id=x.id
            JOIN public.appointment_history h ON h.organization_id=x.organization_id AND h.appointment_id=x.id
                AND h.appointment_revision=x.revision
            WHERE a.stage='PRIMARY' AND a.started AND x.status='Cancelled'
                AND x.revision=(a.command->>'expected_appointment_revision')::bigint+1
                AND h.old_status='Waiting' AND h.new_status='Cancelled' AND h.action='STATUS'
                AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role AND h.reason=a.command->>'reason'
                AND h.replacement_appointment_id IS NOT DISTINCT FROM a.new_id
                AND e.patient_id=x.patient_id AND e.doctor_staff_id=x.doctor_staff_id
                AND e.service_date=x.service_date AND e.lane=x.lane
                AND e.disposition='CLOSED' AND e.closure_history_revision=x.revision
                AND e.closure_queue_revision=(a.command->>'expected_queue_day_revision')::bigint
                AND e.closure_transaction_id=pg_current_xact_id();
        $$;
        REVOKE ALL ON FUNCTION public.f01_b17_history_scope(public.appointment_history),
            public.f01_b17_closed_entry_witness() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b17_history_scope(public.appointment_history) TO carepulse_identity_owner;
    """)
    op.execute(
        "GRANT CREATE ON SCHEMA public TO carepulse_identity_owner; SET LOCAL ROLE carepulse_identity_owner"
    )
    marker = (
        "IF session_user = 'carepulse_api' AND NOT public.f01_b16_history_scope(NEW)"
    )
    _rewrite(
        "f01_private.guard_audit_appointment_history()",
        marker,
        marker + " AND NOT public.f01_b17_history_scope(NEW)",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )
    condition = """TG_OP='UPDATE' AND NEW.arrival_counter=OLD.arrival_counter
        AND NEW.routine_cursor=OLD.routine_cursor AND NEW.revision=OLD.revision+1
        AND EXISTS (SELECT 1 FROM public.f01_b17_closed_entry_witness() e
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
    _rewrite(
        "public.f01_assert_queue_rank_shift()",
        marker,
        """IF OLD.disposition='ACTIVE' AND NEW.disposition='CLOSED'
        AND NEW.routine_rank IS NOT DISTINCT FROM OLD.routine_rank AND NEW.revision=OLD.revision+1
        AND EXISTS (SELECT 1 FROM public.f01_b17_closed_entry_witness() e
            JOIN public.queue_days q ON q.organization_id=e.organization_id AND q.id=e.queue_day_id
            WHERE e.organization_id=NEW.organization_id AND e.id=NEW.id AND e.revision=NEW.revision
                AND e.closure_history_revision=NEW.closure_history_revision
                AND q.revision IN (e.closure_queue_revision+1,e.closure_queue_revision+2)
                AND q.revision_transaction_id=pg_current_xact_id()) THEN RETURN NULL; END IF;
        """
        + marker,
    )


def _writers() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b17_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; s record; r public.idempotency_receipts%ROWTYPE; p jsonb; h public.appointment_history%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b17_authority();
            IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation authority denied' USING ERRCODE='42501'; END IF;
            SELECT * INTO s FROM public.f01_b17_scope();
            IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            SELECT * INTO r FROM public.idempotency_receipts t
                WHERE t.scope_kind='TENANT' AND t.scope_id=a.organization_id AND t.actor_kind='STAFF'
                    AND t.actor_id=a.staff_id AND t.organization_id=a.organization_id AND t.staff_id=a.staff_id
                    AND t.operation=a.operation AND t.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Cancellation idempotency conflict' USING ERRCODE='CP001';
            END IF;
            p:=r.result_projection;
            SELECT * INTO h FROM public.appointment_history ah
                WHERE ah.organization_id=a.organization_id AND ah.appointment_id=a.target_id
                    AND ah.appointment_revision=r.result_revision AND ah.action='STATUS'
                    AND ah.old_status IN ('Booked','Waiting') AND ah.new_status='Cancelled'
                    AND ah.actor_staff_id=a.staff_id AND ah.actor_role=a.role AND ah.reason=a.command->>'reason';
            IF NOT FOUND OR p->>'appointment_id' IS DISTINCT FROM a.target_id::text
                OR p->>'status' IS DISTINCT FROM 'Cancelled'
                OR (p->>'appointment_revision')::bigint IS DISTINCT FROM r.result_revision
                OR r.result_revision IS DISTINCT FROM (a.command->>'expected_appointment_revision')::bigint+1
                OR (p->>'replacement_appointment_id')::uuid IS DISTINCT FROM h.replacement_appointment_id THEN
                RAISE EXCEPTION 'Cancellation receipt evidence denied' USING ERRCODE='42501';
            END IF;
            IF a.purpose='APPOINTMENT_REPLACE_URGENT' THEN
                IF NOT EXISTS (SELECT 1 FROM public.appointments x
                    JOIN public.appointment_history nh ON nh.organization_id=x.organization_id
                        AND nh.appointment_id=x.id AND nh.appointment_revision=0 AND nh.action='CREATED'
                        AND nh.actor_staff_id=a.staff_id AND nh.actor_role=a.role AND nh.new_status='Waiting'
                    JOIN public.queue_entries e ON e.organization_id=x.organization_id AND e.appointment_id=x.id
                    JOIN public.appointment_price_snapshots ps ON ps.organization_id=x.organization_id AND ps.appointment_id=x.id
                    WHERE x.organization_id=a.organization_id AND x.id=h.replacement_appointment_id
                        AND x.patient_id=s.patient_id AND x.doctor_staff_id=s.doctor_staff_id AND x.service_date=s.service_date
                        AND x.classification='URGENT' AND x.lane='MAIN' AND x.service_type='NEW_EXAMINATION'
                        AND e.id=(p->>'new_queue_entry_id')::uuid AND e.queue_day_id=(p->>'new_queue_day_id')::uuid
                        AND e.attested_by_staff_id=a.staff_id AND nh.new_number=(p->>'new_number')::bigint
                        AND ps.base_egp=(p->>'new_base_egp')::numeric AND ps.net_egp=(p->>'new_net_egp')::numeric) THEN
                    RAISE EXCEPTION 'Replacement receipt evidence denied' USING ERRCODE='42501';
                END IF;
            ELSIF h.replacement_appointment_id IS NOT NULL THEN
                RAISE EXCEPTION 'Cancellation replacement evidence denied' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',p,'revision',r.result_revision,'replayed',true);
        END $$;

        CREATE FUNCTION public.f01_b17_cancel(p_new uuid) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; s record; x public.appointments%ROWTYPE; cl public.followup_claims%ROWTYPE;
            q public.queue_days%ROWTYPE; lock_id uuid; kind_name text; history_id uuid:=gen_random_uuid();
            moment timestamptz; projection jsonb; claim_revision bigint; day_revision bigint;
            source_visit uuid; source_appointment uuid; before_state jsonb; after_state jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b17_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.started
                OR (p_new IS NOT NULL)<>(a.purpose='APPOINTMENT_REPLACE_URGENT') THEN
                RAISE EXCEPTION 'Cancellation scope denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO s FROM public.f01_b17_scope();
            IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            PERFORM 1 FROM public.patients p WHERE p.organization_id=s.organization_id AND p.id=s.patient_id
                AND (p_new IS NULL OR NOT p.archived) FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Patient unavailable' USING ERRCODE='23514'; END IF;
            FOREACH kind_name IN ARRAY ARRAY['SCHEDULE','CAPACITY','FOLLOWUP','PRICE','QUEUE'] LOOP
                IF p_new IS NULL AND kind_name NOT IN ('SCHEDULE','FOLLOWUP') THEN CONTINUE; END IF;
                PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id=s.organization_id
                    AND h.kind=kind_name FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation policy unavailable' USING ERRCODE='23514'; END IF;
            END LOOP;
            IF public.f01_b17_replay() IS NOT NULL THEN
                RAISE EXCEPTION 'Cancellation already committed' USING ERRCODE='CP001';
            END IF;
            SELECT * INTO STRICT x FROM public.appointments ax WHERE ax.organization_id=s.organization_id AND ax.id=s.target_id;
            IF x.status NOT IN ('Booked','Waiting') OR x.revision<>(a.command->>'expected_appointment_revision')::bigint THEN
                RAISE EXCEPTION 'Stale cancellation Appointment' USING ERRCODE='40001';
            END IF;
            IF p_new IS NOT NULL AND (p_new=x.id OR a.allocated_number IS NULL OR a.day_id IS NOT NULL
                OR x.classification<>'ROUTINE' OR x.lane<>'MAIN'
                OR x.service_date<>(clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date) THEN
                RAISE EXCEPTION 'Replacement eligibility denied' USING ERRCODE='23514';
            END IF;
            IF x.status='Waiting' OR p_new IS NOT NULL OR a.command ? 'expected_queue_day_revision' THEN
                PERFORM 1 FROM public.doctor_controls d WHERE d.organization_id=x.organization_id
                    AND d.doctor_staff_id=x.doctor_staff_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Doctor control unavailable' USING ERRCODE='23514'; END IF;
                SELECT * INTO q FROM public.queue_days d WHERE d.organization_id=x.organization_id
                    AND d.doctor_staff_id=x.doctor_staff_id AND d.service_date=x.service_date AND d.lane=x.lane FOR UPDATE;
                IF x.status='Waiting' AND (q.id IS NULL OR q.revision IS DISTINCT FROM
                    (a.command->>'expected_queue_day_revision')::bigint) THEN
                    RAISE EXCEPTION 'Stale Waiting QueueDay' USING ERRCODE='40001';
                ELSIF x.status='Booked' AND a.command ? 'expected_queue_day_revision'
                    AND coalesce(q.revision,0)<>(a.command->>'expected_queue_day_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale replacement QueueDay' USING ERRCODE='40001';
                END IF;
            END IF;
            SELECT c.source_visit_id,c.source_appointment_id INTO source_visit,source_appointment
                FROM public.followup_claims c WHERE c.organization_id=x.organization_id AND c.appointment_id=x.id;
            FOR lock_id IN SELECT DISTINCT ids.id FROM unnest(ARRAY[x.id,source_appointment]) AS ids(id)
                WHERE ids.id IS NOT NULL ORDER BY ids.id LOOP
                PERFORM 1 FROM public.appointments ax WHERE ax.organization_id=x.organization_id AND ax.id=lock_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation Appointment link missing' USING ERRCODE='23514'; END IF;
            END LOOP;
            SELECT * INTO STRICT x FROM public.appointments ax WHERE ax.organization_id=s.organization_id AND ax.id=s.target_id;
            IF x.status NOT IN ('Booked','Waiting') OR x.revision<>(a.command->>'expected_appointment_revision')::bigint
                OR x.patient_id<>s.patient_id OR x.doctor_staff_id<>s.doctor_staff_id
                OR x.service_date<>s.service_date OR x.lane<>s.lane
                OR (x.status='Waiting' AND q.id IS NULL) THEN
                RAISE EXCEPTION 'Stale locked cancellation Appointment' USING ERRCODE='40001';
            END IF;
            IF x.service_type='FOLLOW_UP' THEN
                PERFORM 1 FROM public.visits v WHERE v.organization_id=x.organization_id AND v.id=source_visit
                    AND v.appointment_id=source_appointment FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Claim source Visit missing' USING ERRCODE='23514'; END IF;
                PERFORM 1 FROM public.followup_source_state f WHERE f.organization_id=x.organization_id
                    AND f.visit_id=source_visit FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Claim source missing' USING ERRCODE='23514'; END IF;
                SELECT * INTO cl FROM public.followup_claims c WHERE c.organization_id=x.organization_id
                    AND c.appointment_id=x.id FOR UPDATE;
                IF NOT FOUND OR cl.state<>'ACTIVE' OR cl.patient_id<>x.patient_id OR cl.doctor_staff_id<>x.doctor_staff_id
                    OR cl.source_visit_id<>source_visit OR cl.source_appointment_id<>source_appointment
                    OR cl.revision IS DISTINCT FROM (a.command->>'expected_claim_revision')::bigint
                    OR NOT EXISTS (SELECT 1 FROM public.appointment_price_snapshots ps
                        WHERE ps.organization_id=x.organization_id AND ps.appointment_id=x.id
                            AND ps.entitlement_provenance=cl.policy_snapshot)
                    OR NOT EXISTS (SELECT 1 FROM public.followup_source_state f WHERE f.organization_id=x.organization_id
                        AND f.visit_id=source_visit AND f.source_revision=(a.command->>'expected_source_revision')::bigint)
                    OR (SELECT coalesce(max(e.authorization_revision),0) FROM public.followup_authorization_events e
                        WHERE e.organization_id=x.organization_id AND e.claim_id=cl.id)
                            IS DISTINCT FROM (a.command->>'expected_authorization_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale cancellation claim' USING ERRCODE='40001';
                END IF;
            ELSIF source_visit IS NOT NULL OR a.command ?| ARRAY['expected_claim_revision',
                'expected_source_revision','expected_authorization_revision'] THEN
                RAISE EXCEPTION 'Unexpected claim revision' USING ERRCODE='23514';
            END IF;
            PERFORM public.f01_b17_start(p_new);
            UPDATE public.appointments ax SET status='Cancelled',revision=x.revision+1
                WHERE ax.organization_id=x.organization_id AND ax.id=x.id;
            INSERT INTO public.appointment_history (id,organization_id,appointment_id,appointment_revision,action,
                old_status,new_status,old_service_date,new_service_date,old_number,new_number,actor_staff_id,
                actor_role,reason,price_snapshot_appointment_id,replacement_appointment_id)
            VALUES (history_id,x.organization_id,x.id,x.revision+1,'STATUS',x.status,'Cancelled',
                x.service_date,x.service_date,x.current_number,x.current_number,a.staff_id,a.role,
                a.command->>'reason',x.id,p_new) RETURNING occurred_at INTO moment;
            IF x.status='Waiting' THEN
                UPDATE public.queue_entries e SET disposition='CLOSED',closure_history_revision=x.revision+1,revision=e.revision+1
                    WHERE e.organization_id=x.organization_id AND e.appointment_id=x.id AND e.queue_day_id=q.id
                        AND e.disposition='ACTIVE';
                IF NOT FOUND THEN RAISE EXCEPTION 'Waiting entry unavailable' USING ERRCODE='23514'; END IF;
                UPDATE public.queue_days d SET revision=d.revision+1 WHERE d.organization_id=x.organization_id AND d.id=q.id
                    RETURNING d.revision INTO day_revision;
            END IF;
            IF cl.id IS NOT NULL THEN
                UPDATE public.followup_claims c SET state='RELEASED',revision=c.revision+1,
                    terminal_history_revision=x.revision+1,terminal_at=moment WHERE c.organization_id=x.organization_id
                        AND c.id=cl.id AND c.state='ACTIVE' RETURNING c.revision INTO claim_revision;
                IF NOT FOUND THEN RAISE EXCEPTION 'Cancellation claim unavailable' USING ERRCODE='40001'; END IF;
            END IF;
            before_state:=jsonb_build_object('appointment_id',x.id,'revision',x.revision,'status',x.status,
                'history_id',history_id,'claim_id',cl.id,'source_visit_id',source_visit,'source_appointment_id',source_appointment);
            after_state:=before_state || jsonb_build_object('revision',x.revision+1,'status','Cancelled','replacement_appointment_id',p_new);
            INSERT INTO public.sensitive_audit (id,organization_id,actor_staff_id,actor_role,action,reason,
                correlation_id,before_state,after_state,occurred_at)
            VALUES (gen_random_uuid(),x.organization_id,a.staff_id,a.role,'APPOINTMENT_CANCELLED',a.command->>'reason',
                history_id::text,before_state,after_state,moment);
            INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
            VALUES (gen_random_uuid(),x.organization_id,'APPOINTMENT_CANCELLED','APPOINTMENT',x.id::text,
                jsonb_build_object('appointment_id',x.id,'status','Cancelled','appointment_revision',x.revision+1,
                    'actor_staff_id',a.staff_id,'actor_role',a.role,'replacement_appointment_id',p_new));
            IF a.role='DOCTOR_OWNER' THEN
                INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
                VALUES (gen_random_uuid(),x.organization_id,'DOCTOR_CANCELLED_APPOINTMENT','APPOINTMENT',x.id::text,
                    jsonb_build_object('appointment_id',x.id,'status','Cancelled','appointment_revision',x.revision+1));
            END IF;
            projection:=jsonb_build_object('appointment_id',x.id,'status','Cancelled','appointment_revision',x.revision+1,
                'claim_revision',claim_revision,'queue_day_revision',day_revision,'replacement_appointment_id',p_new);
            PERFORM public.f01_b17_record(projection);
            RETURN projection;
        END $$;

        CREATE FUNCTION public.f01_b17_finish(p_projection jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; s record; x public.appointments%ROWTYPE; e public.queue_entries%ROWTYPE;
            q public.queue_days%ROWTYPE; ps public.appointment_price_snapshots%ROWTYPE; p jsonb; rev bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b17_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR NOT a.started OR a.finished OR a.old_projection IS NULL THEN
                RAISE EXCEPTION 'Cancellation finalization denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO STRICT s FROM public.f01_b17_scope();
            p:=a.old_projection;
            rev:=(p->>'appointment_revision')::bigint;
            IF a.purpose='APPOINTMENT_REPLACE_URGENT' THEN
                SELECT * INTO x FROM public.appointments ax WHERE ax.organization_id=a.organization_id AND ax.id=a.new_id;
                SELECT * INTO e FROM public.queue_entries qe WHERE qe.organization_id=a.organization_id AND qe.id=a.entry_id;
                SELECT * INTO q FROM public.queue_days qd WHERE qd.organization_id=a.organization_id AND qd.id=a.day_id;
                SELECT * INTO ps FROM public.appointment_price_snapshots ap WHERE ap.organization_id=a.organization_id
                    AND ap.appointment_id=a.new_id;
                IF x.id IS NULL OR x.id IS DISTINCT FROM a.created_id OR x.patient_id<>s.patient_id
                    OR x.doctor_staff_id<>s.doctor_staff_id OR x.service_date<>s.service_date
                    OR x.status<>'Waiting' OR x.revision<>0 OR x.classification<>'URGENT'
                    OR x.lane<>'MAIN' OR x.service_type<>'NEW_EXAMINATION' OR x.current_number IS DISTINCT FROM a.allocated_number
                    OR e.id IS NULL OR e.appointment_id<>x.id OR e.patient_id<>x.patient_id
                    OR e.doctor_staff_id<>x.doctor_staff_id OR e.service_date<>x.service_date OR e.lane<>x.lane
                    OR e.disposition<>'ACTIVE' OR e.attested_by_staff_id<>a.staff_id OR e.routine_rank IS NOT NULL
                    OR q.id IS NULL OR e.queue_day_id<>q.id OR e.arrival_sequence<>q.arrival_counter
                    OR ps.appointment_id IS NULL OR ps.classification<>'URGENT' OR ps.is_free
                    OR ps.entitlement_provenance IS NOT NULL
                    OR NOT EXISTS (SELECT 1 FROM public.appointment_history h WHERE h.organization_id=a.organization_id
                        AND h.appointment_id=a.target_id AND h.appointment_revision=rev AND h.replacement_appointment_id=x.id)
                    OR NOT EXISTS (SELECT 1 FROM public.appointment_history h WHERE h.organization_id=a.organization_id
                        AND h.appointment_id=x.id AND h.appointment_revision=0 AND h.action='CREATED'
                        AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role AND h.new_status='Waiting') THEN
                    RAISE EXCEPTION 'Replacement finalization evidence denied' USING ERRCODE='42501';
                END IF;
                p:=p || jsonb_build_object('new_appointment_revision',0,'new_status','Waiting','new_number',x.current_number,
                    'new_queue_entry_id',e.id,'new_queue_day_id',q.id,'new_queue_day_revision',q.revision,
                    'new_base_egp',round(ps.base_egp,2)::text,'new_net_egp',round(ps.net_egp,2)::text);
                INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
                VALUES (gen_random_uuid(),x.organization_id,'APPOINTMENT_REPLACED_URGENT','APPOINTMENT',x.id::text,
                    jsonb_build_object('appointment_id',x.id,'old_appointment_id',a.target_id,'queue_entry_id',e.id,
                        'status','Waiting','appointment_revision',0,'actor_staff_id',a.staff_id,'actor_role',a.role));
            END IF;
            IF p_projection IS DISTINCT FROM p THEN
                RAISE EXCEPTION 'Cancellation projection mismatch' USING ERRCODE='42501';
            END IF;
            INSERT INTO public.idempotency_receipts (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,a.staff_id,
                a.operation,a.idempotency_key,a.digest,p,rev);
            PERFORM public.f01_b17_mark_finished();
            RETURN jsonb_build_object('projection',p,'revision',rev,'replayed',false);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b17_replay(),public.f01_b17_cancel(uuid),public.f01_b17_finish(jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b17_replay(),public.f01_b17_cancel(uuid),public.f01_b17_finish(jsonb) TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _target_scope()
    _intake_bridge()
    _guards()
    _writers()


def downgrade() -> None:
    raise RuntimeError("B-17 migration is forward-only")
