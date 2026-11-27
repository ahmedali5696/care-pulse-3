"""B-20: operation-bound staff reconciliation, absence and DayReview."""

from alembic import op

revision = "0071_b20_staff"
down_revision = "0070_b19_legacy_evidence"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    bind = op.get_bind()
    definition = bind.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Unexpected B-20 marker in {signature}")
    bind.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b20_body jsonb,
            ADD COLUMN b20_day date, ADD COLUMN b20_digest text;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
            WHEN p_method='GET' AND p_path='/v1/reconciliation' THEN 'RECONCILIATION_READ'
            WHEN p_method='POST' AND p_path ~ ('^/v1/appointments/' || uuid_pattern || '/manual-no-show$')
                THEN 'APPOINTMENT_MANUAL_NO_SHOW'
            WHEN p_method='POST' AND p_path ~ '^/v1/days/[0-9]{4}-[0-9]{2}-[0-9]{2}/review$'
                THEN 'DAY_REVIEW'
            WHEN p_method='POST' AND p_path ~ '^/v1/days/[0-9]{4}-[0-9]{2}-[0-9]{2}/close$'
                THEN 'DAY_CLOSE'""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b20;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b20(bytea,text,bytea,bytea)
            FROM PUBLIC,carepulse_gate;

        CREATE FUNCTION f01_private.b20_operation(purpose text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
            SELECT CASE purpose WHEN 'APPOINTMENT_MANUAL_NO_SHOW' THEN 'appointments.manual_no_show'
                WHEN 'DAY_REVIEW' THEN 'days.review' WHEN 'DAY_CLOSE' THEN 'days.close' END;
        $$;

        CREATE FUNCTION f01_private.b20_command(purpose text, raw json) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
        DECLARE command jsonb:=raw::jsonb; field text; permitted text[];
        BEGIN
            IF jsonb_typeof(command) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid reconciliation command' USING ERRCODE='22023';
            END IF;
            IF purpose='APPOINTMENT_MANUAL_NO_SHOW' THEN
                permitted:=ARRAY['verified_absence','reason','expected_appointment_revision','expected_claim_revision'];
                IF command->'verified_absence' IS DISTINCT FROM 'true'::jsonb
                    OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                    OR f01_private.b16_reason(command->>'reason') IS NULL
                    OR NOT f01_private.b16_revision(raw->'expected_appointment_revision') THEN
                    RAISE EXCEPTION 'Invalid verified absence' USING ERRCODE='22023';
                END IF;
            ELSE
                permitted:=ARRAY['expected_review_revision','notes','reason'];
                IF NOT f01_private.b16_revision(raw->'expected_review_revision')
                    OR (command->>'notes' IS NOT NULL AND jsonb_typeof(command->'notes')<>'string')
                    OR ((command->>'expected_review_revision')::bigint>0 AND command->>'reason' IS NULL)
                    OR (command->>'reason' IS NOT NULL AND
                        (jsonb_typeof(command->'reason')<>'string' OR f01_private.b16_reason(command->>'reason') IS NULL)) THEN
                    RAISE EXCEPTION 'Invalid day review' USING ERRCODE='22023';
                END IF;
                IF purpose='DAY_CLOSE' THEN
                    permitted:=permitted || ARRAY['expected_main_queue_day_revision','expected_emergency_queue_day_revision'];
                    IF NOT f01_private.b16_revision(raw->'expected_main_queue_day_revision')
                        OR NOT f01_private.b16_revision(raw->'expected_emergency_queue_day_revision') THEN
                        RAISE EXCEPTION 'Invalid close revision' USING ERRCODE='22023';
                    END IF;
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_object_keys(command) k WHERE k<>ALL(permitted)) THEN
                RAISE EXCEPTION 'Unexpected reconciliation field' USING ERRCODE='22023';
            END IF;
            FOREACH field IN ARRAY permitted LOOP
                IF field LIKE 'expected_%_revision' AND command->>field IS NOT NULL
                    AND NOT f01_private.b16_revision(raw->field) THEN
                    RAISE EXCEPTION 'Invalid revision' USING ERRCODE='22023';
                END IF;
            END LOOP;
            IF command->>'reason' IS NOT NULL THEN
                command:=jsonb_set(command,'{reason}',to_jsonb(f01_private.b16_reason(command->>'reason')));
            END IF;
            RETURN jsonb_strip_nulls(command);
        END $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea,key_id text,mac bytea,body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE request_id bigint; req f01_private.requests%ROWTYPE; raw json;
            command jsonb; selected_day date; matches text[];
        BEGIN
            request_id:=public.f01_register_proof_before_b20(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF req.purpose NOT IN ('RECONCILIATION_READ','APPOINTMENT_MANUAL_NO_SHOW','DAY_REVIEW','DAY_CLOSE') THEN
                RETURN request_id;
            END IF;
            BEGIN
                IF req.purpose='RECONCILIATION_READ' THEN
                    matches:=regexp_match(req.query,'^date=([0-9]{4}-[0-9]{2}-[0-9]{2})(&lane=(MAIN|EMERGENCY))?(&limit=([0-9]+))?(&offset=([0-9]+))?$');
                    IF matches IS NULL OR octet_length(body)<>0 OR req.target_id IS NOT NULL
                        OR coalesce(matches[5]::integer,50) NOT BETWEEN 1 AND 100
                        OR coalesce(matches[7]::integer,0)<0 THEN
                        RAISE EXCEPTION 'Invalid reconciliation query' USING ERRCODE='22023';
                    END IF;
                    selected_day:=matches[1]::date;
                    IF to_char(selected_day,'YYYY-MM-DD')<>matches[1] THEN
                        RAISE EXCEPTION 'Invalid reconciliation date' USING ERRCODE='22023';
                    END IF;
                    command:=jsonb_build_object('lane',matches[3],'limit',coalesce(matches[5]::integer,50),
                        'offset',coalesce(matches[7]::integer,0));
                ELSE
                    IF req.query<>'' OR length(req.idempotency_key) NOT BETWEEN 1 AND 128
                        OR btrim(req.idempotency_key)<>req.idempotency_key THEN
                        RAISE EXCEPTION 'Invalid reconciliation envelope' USING ERRCODE='22023';
                    END IF;
                    raw:=convert_from(body,'UTF8')::json;
                    PERFORM f01_private.canonical_intake_json(raw);
                    command:=f01_private.b20_command(req.purpose,raw);
                    IF req.purpose='APPOINTMENT_MANUAL_NO_SHOW' THEN
                        IF req.target_id IS NULL THEN
                            RAISE EXCEPTION 'Missing Appointment' USING ERRCODE='22023';
                        END IF;
                    ELSE
                        selected_day:=split_part(req.path,'/',4)::date;
                        IF to_char(selected_day,'YYYY-MM-DD')<>split_part(req.path,'/',4) THEN
                            RAISE EXCEPTION 'Invalid review date' USING ERRCODE='22023';
                        END IF;
                    END IF;
                END IF;
                UPDATE f01_private.requests r SET b20_body=command,b20_day=selected_day,
                    b20_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(
                        jsonb_build_object('purpose',req.purpose,'target',req.target_id,
                            'service_date',selected_day,'body',command)::json),'UTF8')),'hex')
                    WHERE r.id=request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b20_authority()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,purpose text,stage text,
            operation text,target_id uuid,service_date date,command jsonb,digest text,idempotency_key text)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,r.purpose,c.stage,
                f01_private.b20_operation(r.purpose),r.target_id,r.b20_day,r.b20_body,r.b20_digest,r.idempotency_key
            FROM public.f01_context() c JOIN f01_private.requests r ON r.id=c.request_id
            JOIN public.staff_accounts s ON s.organization_id=r.organization_id AND s.id=r.staff_id
            WHERE r.purpose IN ('RECONCILIATION_READ','APPOINTMENT_MANUAL_NO_SHOW','DAY_REVIEW','DAY_CLOSE')
                AND r.body_scope_valid AND r.b20_digest IS NOT NULL AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY')
                AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
                AND s.role=r.role AND s.security_revision=r.security_revision
                AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST');
        $$;
        REVOKE ALL ON FUNCTION f01_private.b20_operation(text),f01_private.b20_command(text,json),
            public.f01_register_proof(bytea,text,bytea,bytea),public.f01_b20_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b20_authority() TO carepulse_migration;
        """,
        execution_options={"no_parameters": True},
    )
    marker = "OR (req.purpose IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT') AND req.b19_digest IS NOT NULL)"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + """
            OR (f01_private.b20_operation(req.purpose) IS NOT NULL AND req.b20_digest IS NOT NULL)""",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )


def _reconciliation() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b20_reconciliation() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; today date; review public.day_reviews%ROWTYPE; entries jsonb;
            row_count bigint; main_revision bigint; emergency_revision bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b20_authority();
            IF NOT FOUND OR a.purpose<>'RECONCILIATION_READ' OR a.stage<>'PRIMARY' THEN
                RAISE EXCEPTION 'Reconciliation read denied' USING ERRCODE='42501';
            END IF;
            today:=(clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date;
            SELECT * INTO review FROM public.day_reviews r
                WHERE r.organization_id=a.organization_id AND r.service_date=a.service_date;
            SELECT coalesce(max(q.revision) FILTER (WHERE q.lane='MAIN'),0),
                coalesce(max(q.revision) FILTER (WHERE q.lane='EMERGENCY'),0)
                INTO main_revision,emergency_revision
                FROM public.queue_days q WHERE q.organization_id=a.organization_id AND q.service_date=a.service_date;
            SELECT count(*) INTO row_count FROM public.appointments x
                WHERE x.organization_id=a.organization_id AND x.service_date=a.service_date
                    AND x.status IN ('Booked','Waiting','In Examination')
                    AND (a.command->>'lane' IS NULL OR x.lane=a.command->>'lane');
            SELECT coalesce(jsonb_agg(to_jsonb(page) ORDER BY page.booking_number,page.appointment_id),'[]'::jsonb)
                INTO entries FROM (
                    SELECT x.id AS appointment_id,x.patient_id,array_to_string(p.name_parts,' ') AS patient_name,
                        x.service_date,x.status,x.classification,x.lane,x.service_type,x.booking_mode,
                        x.starts_at,x.ends_at,x.current_number AS booking_number,x.revision AS appointment_revision,
                        cl.revision AS claim_revision,v.id AS visit_id,x.service_date<today AS overdue,
                        x.service_date<today AND x.status IN ('Booked','Waiting') AS requires_review
                    FROM public.appointments x
                    JOIN public.patients p ON p.organization_id=x.organization_id AND p.id=x.patient_id
                    LEFT JOIN public.followup_claims cl ON cl.organization_id=x.organization_id AND cl.appointment_id=x.id
                    LEFT JOIN public.visits v ON v.organization_id=x.organization_id AND v.appointment_id=x.id
                    WHERE x.organization_id=a.organization_id AND x.service_date=a.service_date
                        AND x.status IN ('Booked','Waiting','In Examination')
                        AND (a.command->>'lane' IS NULL OR x.lane=a.command->>'lane')
                    ORDER BY x.current_number,x.id
                    LIMIT (a.command->>'limit')::integer OFFSET (a.command->>'offset')::integer
                ) page;
            RETURN jsonb_build_object('service_date',a.service_date,'current_service_date',today,
                'review_revision',coalesce(review.revision,0),'review_disposition',review.disposition,
                'review_id',review.id,'review_notes',review.notes,'review_actor_staff_id',review.actor_staff_id,
                'review_occurred_at',review.occurred_at,'main_queue_day_revision',main_revision,
                'emergency_queue_day_revision',emergency_revision,'total',row_count,
                'limit',(a.command->>'limit')::integer,'offset',(a.command->>'offset')::integer,'entries',entries);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b20_reconciliation() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b20_reconciliation() TO carepulse_api;
    """)


def _absence() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b20_history_scope(h public.appointment_history) RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_b20_authority() a
                JOIN public.appointments x ON x.organization_id=a.organization_id AND x.id=a.target_id
                WHERE a.purpose='APPOINTMENT_MANUAL_NO_SHOW' AND a.stage='PRIMARY'
                    AND h.organization_id=x.organization_id AND h.appointment_id=x.id
                    AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                    AND h.action='STATUS' AND h.old_status='Booked' AND h.new_status='No-show'
                    AND x.status='No-show' AND x.revision=h.appointment_revision
                    AND h.appointment_revision=(a.command->>'expected_appointment_revision')::bigint+1
                    AND h.old_service_date=x.service_date AND h.new_service_date=x.service_date
                    AND h.old_number=x.current_number AND h.new_number=x.current_number
                    AND h.reason=a.command->>'reason' AND h.price_snapshot_appointment_id=x.id
                    AND h.replacement_appointment_id IS NULL);
        $$;
        REVOKE ALL ON FUNCTION public.f01_b20_history_scope(public.appointment_history) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b20_history_scope(public.appointment_history) TO carepulse_identity_owner;
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
    """)
    marker = "AND NOT public.f01_b18_history_scope(NEW)"
    _rewrite(
        "f01_private.guard_audit_appointment_history()",
        marker,
        marker + " AND NOT public.f01_b20_history_scope(NEW)",
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )
    # Internal helpers have no application-role EXECUTE. The two callers supply
    # scope from an activated signed staff proof or from a verified worker login.
    op.execute("""
        CREATE FUNCTION public.b20_lock_unarrived(p_tenant uuid,p_target uuid)
        RETURNS public.appointments LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE initial public.appointments%ROWTYPE; locked public.appointments%ROWTYPE;
            source_appointment uuid; source_visit uuid; lock_id uuid; kind_name text;
        BEGIN
            SELECT * INTO initial FROM public.appointments x WHERE x.organization_id=p_tenant AND x.id=p_target;
            IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            PERFORM 1 FROM public.patients p WHERE p.organization_id=p_tenant AND p.id=initial.patient_id FOR UPDATE;
            FOREACH kind_name IN ARRAY ARRAY['SCHEDULE','FOLLOWUP'] LOOP
                PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id=p_tenant AND h.kind=kind_name FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Policy unavailable' USING ERRCODE='23514'; END IF;
            END LOOP;
            SELECT c.source_appointment_id,c.source_visit_id INTO source_appointment,source_visit
                FROM public.followup_claims c WHERE c.organization_id=p_tenant AND c.appointment_id=p_target;
            FOR lock_id IN SELECT DISTINCT id FROM unnest(ARRAY[p_target,source_appointment]) AS ids(id)
                WHERE id IS NOT NULL ORDER BY id LOOP
                PERFORM 1 FROM public.appointments x WHERE x.organization_id=p_tenant AND x.id=lock_id FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Appointment link missing' USING ERRCODE='23514'; END IF;
            END LOOP;
            SELECT * INTO STRICT locked FROM public.appointments x WHERE x.organization_id=p_tenant AND x.id=p_target;
            IF locked.status<>'Booked' OR EXISTS (SELECT 1 FROM public.queue_entries e
                WHERE e.organization_id=p_tenant AND e.appointment_id=p_target AND e.disposition='ACTIVE')
                OR EXISTS (SELECT 1 FROM public.visits v WHERE v.organization_id=p_tenant AND v.appointment_id=p_target) THEN
                RAISE EXCEPTION 'Appointment has arrived or is terminal' USING ERRCODE='23514';
            END IF;
            IF source_visit IS NOT NULL THEN
                PERFORM 1 FROM public.visits v WHERE v.organization_id=p_tenant AND v.id=source_visit
                    AND v.appointment_id=source_appointment FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Claim source Visit missing' USING ERRCODE='23514'; END IF;
                PERFORM 1 FROM public.followup_source_state f WHERE f.organization_id=p_tenant AND f.visit_id=source_visit FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Claim source missing' USING ERRCODE='23514'; END IF;
            END IF;
            RETURN locked;
        END $$;

        CREATE FUNCTION public.b20_absence_claim(x public.appointments)
        RETURNS public.followup_claims LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE cl public.followup_claims%ROWTYPE;
        BEGIN
            SELECT * INTO cl FROM public.followup_claims c WHERE c.organization_id=x.organization_id
                AND c.appointment_id=x.id FOR UPDATE;
            IF x.service_type='FOLLOW_UP' THEN
                IF cl.id IS NULL OR cl.state<>'ACTIVE' OR cl.patient_id<>x.patient_id OR cl.doctor_staff_id<>x.doctor_staff_id
                    OR NOT EXISTS (SELECT 1 FROM public.appointment_price_snapshots ps
                        WHERE ps.organization_id=x.organization_id AND ps.appointment_id=x.id
                        AND ps.entitlement_provenance=cl.policy_snapshot) THEN
                    RAISE EXCEPTION 'Claim unavailable' USING ERRCODE='23514';
                END IF;
            ELSIF cl.id IS NOT NULL THEN
                RAISE EXCEPTION 'Unexpected claim' USING ERRCODE='23514';
            END IF;
            RETURN cl;
        END $$;

        CREATE FUNCTION public.b20_apply_no_show(x public.appointments,cl public.followup_claims,
            p_staff uuid,p_role text,p_reason text,p_cutoff_version bigint DEFAULT NULL,
            p_cutoff_at timestamptz DEFAULT NULL) RETURNS jsonb
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE history_id uuid:=gen_random_uuid(); moment timestamptz; before_state jsonb;
            claim_revision bigint;
        BEGIN
            IF NOT ((session_user='carepulse_api' AND p_role IN ('DOCTOR_OWNER','RECEPTIONIST')
                AND p_staff IS NOT NULL AND p_cutoff_version IS NULL AND p_cutoff_at IS NULL)
                OR (session_user='carepulse_worker' AND p_role='SYSTEM' AND p_staff IS NULL
                    AND p_cutoff_version IS NOT NULL AND p_cutoff_at IS NOT NULL
                    AND p_cutoff_at<=clock_timestamp())) THEN
                RAISE EXCEPTION 'Absence actor scope denied' USING ERRCODE='42501';
            END IF;
            UPDATE public.appointments a SET status='No-show',revision=x.revision+1
                WHERE a.organization_id=x.organization_id AND a.id=x.id AND a.revision=x.revision AND a.status='Booked';
            IF NOT FOUND THEN RAISE EXCEPTION 'Stale absence' USING ERRCODE='40001'; END IF;
            INSERT INTO public.appointment_history(id,organization_id,appointment_id,appointment_revision,action,
                old_status,new_status,old_service_date,new_service_date,old_number,new_number,actor_staff_id,
                actor_role,reason,price_snapshot_appointment_id)
            VALUES(history_id,x.organization_id,x.id,x.revision+1,'STATUS','Booked','No-show',
                x.service_date,x.service_date,x.current_number,x.current_number,p_staff,p_role,p_reason,x.id)
                RETURNING occurred_at INTO moment;
            IF cl.id IS NOT NULL THEN
                UPDATE public.followup_claims c SET state='RELEASED',revision=c.revision+1,
                    terminal_history_revision=x.revision+1,terminal_at=moment
                    WHERE c.organization_id=x.organization_id AND c.id=cl.id AND c.state='ACTIVE'
                    RETURNING c.revision INTO claim_revision;
                IF NOT FOUND THEN RAISE EXCEPTION 'Stale claim' USING ERRCODE='40001'; END IF;
            END IF;
            before_state:=jsonb_build_object('appointment_id',x.id,'service_date',x.service_date,
                'status','Booked','revision',x.revision,'history_id',history_id,'claim_id',cl.id);
            INSERT INTO public.sensitive_audit(id,organization_id,actor_staff_id,actor_role,action,reason,
                correlation_id,before_state,after_state,occurred_at)
            VALUES(gen_random_uuid(),x.organization_id,p_staff,p_role,'APPOINTMENT_MARKED_NO_SHOW',p_reason,
                history_id::text,before_state,before_state || jsonb_build_object('status','No-show','revision',x.revision+1)
                    || CASE WHEN p_role='SYSTEM' THEN jsonb_build_object('origin','WORKER',
                        'cutoff_policy_version',p_cutoff_version,'cutoff_at',p_cutoff_at)
                        ELSE jsonb_build_object('verified_absence',true) END,
                moment);
            INSERT INTO public.outbox_events(id,organization_id,event_type,aggregate_type,aggregate_id,payload)
            VALUES(gen_random_uuid(),x.organization_id,'APPOINTMENT_MARKED_NO_SHOW','APPOINTMENT',x.id::text,
                jsonb_build_object('appointment_id',x.id,'service_date',x.service_date,'status','No-show',
                    'appointment_revision',x.revision+1,'actor_staff_id',p_staff,'actor_role',p_role));
            RETURN jsonb_build_object('appointment_id',x.id,'status','No-show','service_date',x.service_date,
                'appointment_revision',x.revision+1,'claim_revision',claim_revision);
        END $$;
        REVOKE ALL ON FUNCTION public.b20_lock_unarrived(uuid,uuid),
            public.b20_absence_claim(public.appointments),
            public.b20_apply_no_show(public.appointments,public.followup_claims,uuid,text,text,bigint,timestamptz) FROM PUBLIC;
    """)


def _day_review() -> None:
    legacy = "public.record_day_review(uuid,date,bigint,text,text)"
    signature = "public.f01_b20_record_day_review(uuid,date,bigint,text,text)"
    bind = op.get_bind()
    definition = bind.exec_driver_sql(
        f"SELECT pg_get_functiondef('{legacy}'::regprocedure)"
    ).scalar_one()
    marker = "FUNCTION public.record_day_review("
    if definition.count(marker) != 1:
        raise RuntimeError("Unexpected D-11 function declaration")
    bind.exec_driver_sql(
        definition.replace(marker, "FUNCTION public.f01_b20_record_day_review("),
        execution_options={"no_parameters": True},
    )
    op.execute("REVOKE ALL ON FUNCTION " + signature + " FROM PUBLIC")
    _rewrite(
        signature,
        "session_user <> 'carepulse_runtime'",
        "session_user <> 'carepulse_api'",
    )
    _rewrite(
        signature, "current_setting('app.organization_id', true)", "public.f01_org()"
    )
    _rewrite(
        signature,
        "NULLIF(current_setting('app.staff_id', true), '')",
        "NULLIF(public.f01_staff(), '')",
    )
    _rewrite(
        signature,
        "current_setting('app.staff_id', true)::uuid",
        "public.f01_staff()::uuid",
    )
    _rewrite(
        signature,
        "actual_actor := public.f01_staff()::uuid;",
        """IF NOT EXISTS (SELECT 1 FROM public.f01_b20_authority() a
            WHERE a.organization_id=target_organization AND a.service_date=target_date AND a.stage='PRIMARY'
                AND (a.command->>'expected_review_revision')::bigint=expected_revision
                AND requested_notes IS NOT DISTINCT FROM a.command->>'notes'
                AND ((a.purpose='DAY_REVIEW' AND requested_disposition='REVIEWED')
                    OR (a.purpose='DAY_CLOSE' AND requested_disposition='FINAL_CLOSED'))) THEN
            RAISE EXCEPTION 'Review scope denied' USING ERRCODE='42501';
        END IF;
        actual_actor := public.f01_staff()::uuid;""",
    )
    _rewrite(
        signature,
        "CASE WHEN revision = 1 THEN 'Day review'\n                     ELSE 'Day review amendment' END",
        """coalesce((SELECT a.command->>'reason' FROM public.f01_b20_authority() a),'Day review')""",
    )


def _staff_mutation() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b20_staff_mutation() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; x public.appointments%ROWTYPE; cl public.followup_claims%ROWTYPE;
            r public.idempotency_receipts%ROWTYPE; projection jsonb; rev bigint; review record;
            main_revision bigint; emergency_revision bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b20_authority();
            IF NOT FOUND OR a.operation IS NULL THEN
                RAISE EXCEPTION 'Staff reconciliation denied' USING ERRCODE='42501';
            END IF;
            IF a.purpose='APPOINTMENT_MANUAL_NO_SHOW' THEN
                SELECT * INTO x FROM public.appointments ax WHERE ax.organization_id=a.organization_id
                    AND ax.id=a.target_id AND (a.role='RECEPTIONIST' OR ax.doctor_staff_id=a.staff_id);
                IF NOT FOUND THEN RAISE EXCEPTION 'Appointment unavailable' USING ERRCODE='P0002'; END IF;
            END IF;
            SELECT * INTO r FROM public.idempotency_receipts t WHERE t.scope_kind='TENANT'
                AND t.scope_id=a.organization_id AND t.organization_id=a.organization_id AND t.actor_kind='STAFF'
                AND t.actor_id=a.staff_id AND t.staff_id=a.staff_id AND t.operation=a.operation AND t.idempotency_key=a.idempotency_key;
            IF FOUND THEN
                IF r.request_digest<>a.digest THEN RAISE EXCEPTION 'Idempotency conflict' USING ERRCODE='CP001'; END IF;
                projection:=r.result_projection;
                IF a.purpose='APPOINTMENT_MANUAL_NO_SHOW' THEN
                    IF r.result_revision IS DISTINCT FROM (a.command->>'expected_appointment_revision')::bigint+1
                        OR projection IS DISTINCT FROM jsonb_build_object(
                            'appointment_id',a.target_id,'status','No-show','service_date',x.service_date,
                            'appointment_revision',r.result_revision,'claim_revision',
                            CASE WHEN a.command->>'expected_claim_revision' IS NULL THEN NULL
                                ELSE (a.command->>'expected_claim_revision')::bigint+1 END)
                        OR NOT EXISTS (SELECT 1 FROM public.appointment_history h
                            WHERE h.organization_id=a.organization_id AND h.appointment_id=a.target_id
                                AND h.appointment_revision=r.result_revision AND h.action='STATUS'
                                AND h.old_status='Booked' AND h.new_status='No-show'
                                AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role AND h.reason=a.command->>'reason'
                                AND ((x.service_type<>'FOLLOW_UP' AND projection->>'claim_revision' IS NULL
                                    AND NOT EXISTS (SELECT 1 FROM public.followup_claims c
                                        WHERE c.organization_id=x.organization_id AND c.appointment_id=x.id))
                                    OR (x.service_type='FOLLOW_UP' AND EXISTS (SELECT 1 FROM public.followup_claims c
                                        WHERE c.organization_id=x.organization_id AND c.appointment_id=x.id
                                        AND c.state='RELEASED' AND c.revision=(projection->>'claim_revision')::bigint
                                        AND c.terminal_history_revision=h.appointment_revision AND c.terminal_at=h.occurred_at)))) THEN
                        RAISE EXCEPTION 'Absence receipt evidence denied' USING ERRCODE='42501';
                    END IF;
                ELSE
                    IF r.result_revision IS DISTINCT FROM (a.command->>'expected_review_revision')::bigint+1
                        OR NOT EXISTS (SELECT 1 FROM public.day_reviews d JOIN public.day_review_history h
                            ON h.organization_id=d.organization_id AND h.day_review_id=d.id
                            JOIN public.sensitive_audit audit ON audit.organization_id=h.organization_id
                                AND audit.id=h.audit_event_id
                            WHERE d.organization_id=a.organization_id AND d.service_date=a.service_date
                                AND h.revision=r.result_revision AND h.actor_staff_id=a.staff_id
                                AND audit.actor_role=a.role AND h.notes IS NOT DISTINCT FROM a.command->>'notes'
                                AND audit.reason=coalesce(a.command->>'reason','Day review')
                                AND projection=jsonb_build_object('day_review_id',d.id,'service_date',a.service_date,
                                    'revision',r.result_revision,'disposition',CASE WHEN a.purpose='DAY_CLOSE'
                                        THEN 'FINAL_CLOSED' ELSE 'REVIEWED' END)
                                AND h.disposition=projection->>'disposition') THEN
                        RAISE EXCEPTION 'Review receipt evidence denied' USING ERRCODE='42501';
                    END IF;
                END IF;
                RETURN projection;
            END IF;
            IF a.stage<>'PRIMARY' THEN RAISE EXCEPTION 'Missing receipt' USING ERRCODE='CP001'; END IF;
            IF a.purpose='APPOINTMENT_MANUAL_NO_SHOW' THEN
                x:=public.b20_lock_unarrived(a.organization_id,a.target_id);
                IF x.revision<>(a.command->>'expected_appointment_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale Appointment' USING ERRCODE='40001';
                END IF;
                IF x.service_date >= (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date THEN
                    RAISE EXCEPTION 'Only prior-day absence qualifies' USING ERRCODE='23514';
                END IF;
                cl:=public.b20_absence_claim(x);
                IF cl.revision IS DISTINCT FROM (a.command->>'expected_claim_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale claim revision' USING ERRCODE='40001';
                END IF;
                projection:=public.b20_apply_no_show(x,cl,a.staff_id,a.role,a.command->>'reason');
                rev:=x.revision+1;
            ELSE
                IF a.purpose='DAY_CLOSE' THEN
                    PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id=a.organization_id
                        AND h.kind='SCHEDULE' FOR UPDATE;
                    IF NOT FOUND THEN RAISE EXCEPTION 'Schedule unavailable' USING ERRCODE='23514'; END IF;
                    SELECT coalesce(max(q.revision) FILTER (WHERE q.lane='MAIN'),0),
                        coalesce(max(q.revision) FILTER (WHERE q.lane='EMERGENCY'),0)
                        INTO main_revision,emergency_revision FROM public.queue_days q
                        WHERE q.organization_id=a.organization_id AND q.service_date=a.service_date;
                    IF main_revision<>(a.command->>'expected_main_queue_day_revision')::bigint
                        OR emergency_revision<>(a.command->>'expected_emergency_queue_day_revision')::bigint THEN
                        RAISE EXCEPTION 'Stale day revisions' USING ERRCODE='40001';
                    END IF;
                END IF;
                SELECT * INTO STRICT review FROM public.f01_b20_record_day_review(a.organization_id,a.service_date,
                    (a.command->>'expected_review_revision')::bigint,
                    CASE WHEN a.purpose='DAY_CLOSE' THEN 'FINAL_CLOSED' ELSE 'REVIEWED' END,a.command->>'notes');
                rev:=review.revision;
                projection:=jsonb_build_object('day_review_id',review.day_review_id,'service_date',a.service_date,
                    'revision',rev,'disposition',CASE WHEN a.purpose='DAY_CLOSE' THEN 'FINAL_CLOSED' ELSE 'REVIEWED' END);
            END IF;
            INSERT INTO public.idempotency_receipts(id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES(gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,a.staff_id,
                a.operation,a.idempotency_key,a.digest,projection,rev);
            RETURN projection;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b20_staff_mutation() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b20_staff_mutation() TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _reconciliation()
    _absence()
    _day_review()
    _staff_mutation()


def downgrade() -> None:
    raise RuntimeError("B-20 staff migration is forward-only")
