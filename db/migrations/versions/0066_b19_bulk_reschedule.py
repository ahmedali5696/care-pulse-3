"""B-19: signed immutable cohort previews and atomic same-ID movement."""

from alembic import op

revision = "0066_b19_bulk_reschedule"
down_revision = "0065_b18_followup_fence"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Unexpected B-19 marker count in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b19_body jsonb, ADD COLUMN b19_digest text;
        ALTER TABLE f01_private.activations ADD COLUMN b19_number bigint,
            ADD COLUMN b19_active jsonb, ADD COLUMN b19_finished boolean NOT NULL DEFAULT false;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method='POST' AND p_path='/v1/appointments/bulk-reschedule/preview'
                    THEN 'APPOINTMENT_BULK_PREVIEW'
                WHEN p_method='POST' AND p_path='/v1/appointments/bulk-reschedule/commit'
                    THEN 'APPOINTMENT_BULK_COMMIT'""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b19;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b19(bytea,text,bytea,bytea)
            FROM PUBLIC,carepulse_gate;

        CREATE FUNCTION f01_private.b19_command(purpose text, raw json) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
        DECLARE command jsonb:=raw::jsonb; field text; day date; ids jsonb;
        BEGIN
            IF jsonb_typeof(command) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid bulk command' USING ERRCODE='22023';
            END IF;
            IF purpose='APPOINTMENT_BULK_COMMIT' THEN
                IF NOT command ?& ARRAY['preview_id','confirmation']
                    OR command - 'preview_id' - 'confirmation'<>'{}'::jsonb
                    OR jsonb_typeof(command->'preview_id') IS DISTINCT FROM 'string'
                    OR command->'confirmation' IS DISTINCT FROM '"RESCHEDULE"'::jsonb THEN
                    RAISE EXCEPTION 'Invalid bulk confirmation' USING ERRCODE='22023';
                END IF;
                RETURN jsonb_set(command,'{preview_id}',to_jsonb((command->>'preview_id')::uuid));
            END IF;
            IF NOT command ?& ARRAY['original_service_date','destination_date','appointment_ids','reason']
                OR command - 'original_service_date' - 'destination_date' - 'appointment_ids' - 'reason'<>'{}'::jsonb
                OR jsonb_typeof(command->'appointment_ids') IS DISTINCT FROM 'array'
                OR jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                OR f01_private.b16_reason(command->>'reason') IS NULL THEN
                RAISE EXCEPTION 'Invalid bulk selection' USING ERRCODE='22023';
            END IF;
            IF jsonb_array_length(command->'appointment_ids')=0 OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(command->'appointment_ids') e
                WHERE jsonb_typeof(e) IS DISTINCT FROM 'string') THEN
                RAISE EXCEPTION 'Invalid bulk selection IDs' USING ERRCODE='22023';
            END IF;
            SELECT jsonb_agg(to_jsonb(e.id::uuid) ORDER BY e.ordinality) INTO ids
                FROM jsonb_array_elements_text(command->'appointment_ids') WITH ORDINALITY e(id,ordinality);
            IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(ids) e GROUP BY e HAVING count(*)>1) THEN
                RAISE EXCEPTION 'Duplicate bulk selection IDs' USING ERRCODE='22023';
            END IF;
            FOREACH field IN ARRAY ARRAY['original_service_date','destination_date'] LOOP
                IF jsonb_typeof(command->field) IS DISTINCT FROM 'string'
                    OR command->>field !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
                    RAISE EXCEPTION 'Invalid bulk date' USING ERRCODE='22023';
                END IF;
                day:=(command->>field)::date;
                IF NOT isfinite(day) OR day::text<>command->>field THEN
                    RAISE EXCEPTION 'Invalid bulk date' USING ERRCODE='22023';
                END IF;
            END LOOP;
            RETURN jsonb_set(jsonb_set(command,'{appointment_ids}',ids),'{reason}',
                to_jsonb(f01_private.b16_reason(command->>'reason')));
        END $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea,key_id text,mac bytea,body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE request_id bigint; req f01_private.requests%ROWTYPE; raw json; command jsonb;
        BEGIN
            request_id:=public.f01_register_proof_before_b19(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF req.purpose NOT IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT') THEN
                RETURN request_id;
            END IF;
            BEGIN
                IF req.query<>'' OR req.target_id IS NOT NULL OR req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                    RAISE EXCEPTION 'Invalid bulk envelope' USING ERRCODE='22023';
                END IF;
                raw:=convert_from(body,'UTF8')::json;
                PERFORM f01_private.canonical_intake_json(raw);
                command:=f01_private.b19_command(req.purpose,raw);
                UPDATE f01_private.requests r SET b19_body=command,
                    b19_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(jsonb_build_object(
                        'operation',CASE WHEN req.purpose='APPOINTMENT_BULK_PREVIEW'
                            THEN 'appointments.bulk_reschedule.preview' ELSE 'appointments.bulk_reschedule.commit' END,
                        'body',command)::json),'UTF8')),'hex')
                    WHERE r.id=request_id;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b19_authority()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,purpose text,stage text,
            security_revision bigint,idempotency_key text,command jsonb,digest text,
            number bigint,active jsonb,finished boolean)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,r.purpose,c.stage,r.security_revision,
                r.idempotency_key,r.b19_body,r.b19_digest,a.b19_number,a.b19_active,a.b19_finished
            FROM public.f01_context() c JOIN f01_private.requests r ON r.id=c.request_id
            JOIN f01_private.activations a ON a.request_id=c.request_id AND a.stage=c.stage
            JOIN public.staff_accounts s ON s.organization_id=r.organization_id AND s.id=r.staff_id
            WHERE r.purpose IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT')
                AND r.body_scope_valid AND r.b19_digest IS NOT NULL
                AND c.stage IN ('PRIMARY','RECEIPT_RECOVERY') AND s.enabled AND s.verified
                AND s.reset_block_operation_id IS NULL AND s.role=r.role
                AND s.security_revision=r.security_revision AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST');
        $$;

        CREATE FUNCTION public.f01_b19_stage(p_number bigint,p_active jsonb,p_finished boolean)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            UPDATE f01_private.activations a
            SET b19_number=p_number,b19_active=p_active,b19_finished=p_finished
            FROM public.f01_context() c,public.f01_b19_authority() b
            WHERE a.request_id=c.request_id AND a.stage=c.stage AND b.stage='PRIMARY'
                AND b.purpose='APPOINTMENT_BULK_COMMIT' AND NOT a.b19_finished AND p_number>0
                AND (a.b19_number IS NULL OR p_number>=a.b19_number);
            IF NOT FOUND THEN RAISE EXCEPTION 'Bulk stage denied' USING ERRCODE='42501'; END IF;
        END $$;

        CREATE FUNCTION f01_private.assert_b19_complete() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM f01_private.activations a
                WHERE a.request_id=NEW.request_id AND a.stage=NEW.stage
                    AND a.b19_finished AND a.b19_active IS NULL) THEN
                RAISE EXCEPTION 'Incomplete bulk transaction' USING ERRCODE='42501';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER f01_b19_complete AFTER UPDATE ON f01_private.activations
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            WHEN (OLD.b19_number IS NULL AND NEW.b19_number IS NOT NULL)
            EXECUTE FUNCTION f01_private.assert_b19_complete();

        REVOKE ALL ON FUNCTION f01_private.b19_command(text,json),
            public.f01_register_proof(bytea,text,bytea,bytea),public.f01_b19_authority(),
            public.f01_b19_stage(bigint,jsonb,boolean),f01_private.assert_b19_complete() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b19_authority(),public.f01_b19_stage(bigint,jsonb,boolean)
            TO carepulse_migration;
        """,
        execution_options={"no_parameters": True},
    )
    marker = "OR (req.purpose='APPOINTMENT_RESCHEDULE' AND req.b18_digest IS NOT NULL)"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + "\n                           OR (req.purpose IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT') AND req.b19_digest IS NOT NULL)",
    )
    # Only the protected batch writer can stage a single B-18-compatible row.
    marker = "AND s.security_revision=r.security_revision AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST');"
    _rewrite(
        "public.f01_b18_authority()",
        marker,
        marker[:-1]
        + """
            UNION ALL
            SELECT b.organization_id,b.staff_id,b.role,b.stage,b.idempotency_key,
                (b.active->>'appointment_id')::uuid,b.active->'command',b.digest,b.number,false
            FROM public.f01_b19_authority() b
            WHERE b.purpose='APPOINTMENT_BULK_COMMIT' AND b.stage='PRIMARY'
                AND b.active IS NOT NULL AND b.number IS NOT NULL AND NOT b.finished;
        """,
    )
    op.execute(
        "RESET ROLE; REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner"
    )


def _receipts_and_selection() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b19_visible(p_org uuid,p_staff uuid,p_role text,p_ids jsonb)
        RETURNS boolean LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
            SELECT jsonb_typeof(p_ids)='array' AND jsonb_array_length(p_ids)>0
                AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(p_ids) wanted(id)
                    WHERE NOT EXISTS (SELECT 1 FROM public.appointments x
                        WHERE x.organization_id=p_org AND x.id=wanted.id::uuid
                            AND (p_role='RECEPTIONIST' OR x.doctor_staff_id=p_staff)));
        $$;

        CREATE FUNCTION public.f01_b19_preview_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; r public.idempotency_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_PREVIEW' THEN
                RAISE EXCEPTION 'Bulk preview scope denied' USING ERRCODE='42501';
            END IF;
            IF a.command->>'original_service_date'=a.command->>'destination_date' THEN
                RAISE EXCEPTION 'Bulk requires a different destination day' USING ERRCODE='23514';
            END IF;
            IF public.f01_b19_visible(a.organization_id,a.staff_id,a.role,a.command->'appointment_ids')
                IS NOT TRUE THEN
                RAISE EXCEPTION 'Bulk selection unavailable' USING ERRCODE='P0002';
            END IF;
            SELECT * INTO r FROM public.idempotency_receipts t
                WHERE t.scope_kind='TENANT' AND t.scope_id=a.organization_id AND t.actor_kind='STAFF'
                    AND t.actor_id=a.staff_id AND t.organization_id=a.organization_id AND t.staff_id=a.staff_id
                    AND t.operation='appointments.bulk_reschedule.preview' AND t.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Bulk preview idempotency conflict' USING ERRCODE='CP001';
            END IF;
            IF r.result_projection->>'preview_id' IS DISTINCT FROM r.id::text
                OR r.result_projection->'security_revision' IS DISTINCT FROM to_jsonb(a.security_revision)
                OR r.result_revision IS DISTINCT FROM 1::bigint THEN
                RAISE EXCEPTION 'Bulk preview receipt denied' USING ERRCODE='42501';
            END IF;
            RETURN r.result_projection;
        END $$;

        CREATE FUNCTION public.f01_b19_read_preview() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; r public.idempotency_receipts%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_COMMIT' THEN
                RAISE EXCEPTION 'Bulk receipt scope denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO r FROM public.idempotency_receipts t
                WHERE t.id=(a.command->>'preview_id')::uuid
                    AND t.scope_kind='TENANT' AND t.scope_id=a.organization_id AND t.actor_kind='STAFF'
                    AND t.actor_id=a.staff_id AND t.organization_id=a.organization_id AND t.staff_id=a.staff_id
                    AND t.operation='appointments.bulk_reschedule.preview';
            IF NOT FOUND OR r.result_projection->>'preview_id' IS DISTINCT FROM r.id::text
                OR r.result_projection->'security_revision' IS DISTINCT FROM to_jsonb(a.security_revision)
                OR r.result_revision IS DISTINCT FROM 1::bigint
                OR public.f01_b19_visible(a.organization_id,a.staff_id,a.role,
                    r.result_projection->'appointment_ids') IS NOT TRUE THEN
                RAISE EXCEPTION 'Bulk preview unavailable' USING ERRCODE='P0002';
            END IF;
            RETURN r.result_projection;
        END $$;

        CREATE FUNCTION public.f01_b19_selection() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; selection jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.finished THEN
                RAISE EXCEPTION 'Bulk selection scope denied' USING ERRCODE='42501';
            END IF;
            selection:=CASE WHEN a.purpose='APPOINTMENT_BULK_PREVIEW' THEN a.command
                ELSE public.f01_b19_read_preview() END;
            IF selection->>'original_service_date'=selection->>'destination_date' THEN
                RAISE EXCEPTION 'Bulk requires a different destination day' USING ERRCODE='23514';
            END IF;
            IF public.f01_b19_visible(a.organization_id,a.staff_id,a.role,selection->'appointment_ids')
                IS NOT TRUE THEN
                RAISE EXCEPTION 'Bulk selection unavailable' USING ERRCODE='P0002';
            END IF;
            IF EXISTS (SELECT 1 FROM public.appointments x
                WHERE x.organization_id=a.organization_id AND selection->'appointment_ids' ? x.id::text
                    AND x.service_date<>(selection->>'original_service_date')::date) THEN
                RAISE EXCEPTION 'Bulk original day changed' USING ERRCODE='40001';
            END IF;
            RETURN selection;
        END $$;

        CREATE FUNCTION public.f01_b19_commit_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; r public.idempotency_receipts%ROWTYPE; preview jsonb; moved jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_COMMIT' THEN
                RAISE EXCEPTION 'Bulk commit scope denied' USING ERRCODE='42501';
            END IF;
            preview:=public.f01_b19_read_preview();
            SELECT * INTO r FROM public.idempotency_receipts t
                WHERE t.scope_kind='TENANT' AND t.scope_id=a.organization_id AND t.actor_kind='STAFF'
                    AND t.actor_id=a.staff_id AND t.organization_id=a.organization_id AND t.staff_id=a.staff_id
                    AND t.operation='appointments.bulk_reschedule.commit' AND t.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Bulk commit idempotency conflict' USING ERRCODE='CP001';
            END IF;
            moved:=r.result_projection->'moved';
            IF r.result_projection->>'preview_id' IS DISTINCT FROM preview->>'preview_id'
                OR r.result_revision IS DISTINCT FROM 1::bigint
                OR jsonb_typeof(moved) IS DISTINCT FROM 'array'
                OR jsonb_array_length(moved) IS DISTINCT FROM (preview->>'eligible_count')::integer
                OR EXISTS (SELECT 1 FROM jsonb_array_elements(preview->'included') item
                    WHERE NOT EXISTS (
                        SELECT 1 FROM jsonb_array_elements(moved) m
                        JOIN public.appointment_history h
                            ON h.organization_id=a.organization_id AND h.appointment_id=(m->>'appointment_id')::uuid
                            AND h.appointment_revision=(m->>'appointment_revision')::bigint
                        WHERE m->>'appointment_id'=item->>'appointment_id'
                            AND h.appointment_revision=(item->>'appointment_revision')::bigint+1
                            AND h.action='RESCHEDULE' AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                            AND h.old_service_date=(preview->>'original_service_date')::date
                            AND h.new_service_date=(preview->>'destination_date')::date
                            AND h.reason=preview->>'reason' AND h.new_number=(m->>'number')::bigint
                            AND h.new_status='Booked' AND h.replacement_appointment_id IS NULL)) THEN
                RAISE EXCEPTION 'Bulk commit receipt evidence denied' USING ERRCODE='42501';
            END IF;
            RETURN r.result_projection;
        END $$;

        REVOKE ALL ON FUNCTION public.f01_b19_visible(uuid,uuid,text,jsonb),
            public.f01_b19_preview_replay(),public.f01_b19_read_preview(),
            public.f01_b19_selection(),public.f01_b19_commit_replay() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b19_preview_replay(),public.f01_b19_read_preview(),
            public.f01_b19_commit_replay() TO carepulse_api;
    """)


def _operational_reads() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b19_rows() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; selection jsonb; destination date; result jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.finished THEN
                RAISE EXCEPTION 'Bulk rows scope denied' USING ERRCODE='42501';
            END IF;
            selection:=public.f01_b19_selection();
            destination:=(selection->>'destination_date')::date;
            SELECT jsonb_agg(jsonb_build_object(
                'id',x.id,'patient_id',x.patient_id,'doctor_staff_id',x.doctor_staff_id,
                'service_date',x.service_date,'status',x.status,'classification',x.classification,
                'lane',x.lane,'booking_mode',x.booking_mode,'starts_at',x.starts_at,'ends_at',x.ends_at,
                'revision',x.revision,'patient_revision',p.lifecycle_revision,'patient_archived',p.archived,
                'queue_day_revision',CASE WHEN x.status='Waiting' THEN q.revision END,
                'queue_active',CASE WHEN x.status='Waiting' THEN EXISTS (
                    SELECT 1 FROM public.queue_entries e WHERE e.organization_id=x.organization_id
                        AND e.appointment_id=x.id AND e.queue_day_id=q.id AND e.disposition='ACTIVE') END,
                'service_type',x.service_type,'claim_revision',cl.revision,'claim_state',cl.state,
                'approved_deadline',cl.approved_deadline,
                'source_decision_revision',s.source_revision,'visit_revision',v.clinical_revision,
                'followup_revision',CASE WHEN cl.id IS NOT NULL THEN fh.revision END,
                'authorization_revision',CASE WHEN cl.id IS NOT NULL THEN coalesce(auth.revision,0) END,
                'extension_event_id',extension.id,
                'source_eligible',CASE WHEN cl.id IS NOT NULL THEN coalesce(
                    s.provenance_state='VERIFIED' AND source.status='Completed'
                    AND source.service_type='NEW_EXAMINATION'
                    AND destination>=cl.completion_local_date
                    AND coalesce(s.decision->>'kind','DEFAULT')<>'OPTED_OUT'
                    AND NOT coalesce((a.role='RECEPTIONIST' AND s.decision_actor_role='DOCTOR_OWNER'
                        AND s.decision->>'kind'='DEADLINE'
                        AND destination>(s.decision->>'deadline_date')::date),false)
                    AND EXISTS (SELECT 1 FROM public.appointment_price_snapshots ps
                        WHERE ps.organization_id=x.organization_id AND ps.appointment_id=x.id
                            AND ps.entitlement_provenance=cl.policy_snapshot),false) END,
                'destination_patient_conflict',EXISTS (SELECT 1 FROM public.appointments other
                    WHERE other.organization_id=x.organization_id AND other.patient_id=x.patient_id
                        AND other.service_date=destination AND other.id<>x.id
                        AND other.status IN ('Booked','Waiting','In Examination','Completed'))
                ) ORDER BY wanted.ordinality) INTO result
            FROM jsonb_array_elements_text(selection->'appointment_ids') WITH ORDINALITY wanted(id,ordinality)
            JOIN public.appointments x ON x.organization_id=a.organization_id AND x.id=wanted.id::uuid
            JOIN public.patients p ON p.organization_id=x.organization_id AND p.id=x.patient_id
            LEFT JOIN public.queue_days q ON q.organization_id=x.organization_id
                AND q.doctor_staff_id=x.doctor_staff_id AND q.service_date=x.service_date AND q.lane=x.lane
            LEFT JOIN public.followup_claims cl ON cl.organization_id=x.organization_id AND cl.appointment_id=x.id
            LEFT JOIN public.visits v ON v.organization_id=cl.organization_id AND v.id=cl.source_visit_id
            LEFT JOIN public.appointments source ON source.organization_id=cl.organization_id AND source.id=cl.source_appointment_id
            LEFT JOIN public.followup_source_state s ON s.organization_id=cl.organization_id AND s.visit_id=cl.source_visit_id
            LEFT JOIN public.clinic_policy_heads fh ON fh.organization_id=x.organization_id AND fh.kind='FOLLOWUP'
            LEFT JOIN LATERAL (SELECT max(ev.authorization_revision) AS revision
                FROM public.followup_authorization_events ev WHERE ev.organization_id=cl.organization_id
                    AND ev.claim_id=cl.id) auth ON true
            LEFT JOIN LATERAL (SELECT ev.id FROM public.followup_authorization_events ev
                WHERE ev.organization_id=cl.organization_id AND ev.claim_id=cl.id
                    AND ev.appointment_id=x.id AND ev.source_visit_id=cl.source_visit_id AND ev.kind='EXTENSION'
                    AND ev.authorization_revision=auth.revision AND ev.claim_revision=cl.revision
                    AND ev.appointment_revision<=x.revision AND ev.new_deadline>=destination
                    AND ev.new_deadline>=ev.prior_deadline AND ev.reason IS NOT NULL AND btrim(ev.reason)<>''
                LIMIT 1) extension ON true;
            RETURN result;
        END $$;

        CREATE FUNCTION public.f01_b19_policy_scope(p_tenant uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.finished OR a.organization_id IS DISTINCT FROM p_tenant THEN
                RETURN false;
            END IF;
            RETURN public.f01_b19_selection() IS NOT NULL;
        END $$;

        REVOKE ALL ON FUNCTION public.f01_b19_rows(),public.f01_b19_policy_scope(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b19_rows(),public.f01_b19_policy_scope(uuid) TO carepulse_api;
    """)
    # Each operation owns a separate destination-only interval projection.
    for kind in ("preview", "commit"):
        op.execute(f"""
            CREATE FUNCTION public.f01_b19_{kind}_intervals(p_tenant uuid,p_service_date date)
            RETURNS TABLE (starts_at timestamptz,ends_at timestamptz)
            LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            DECLARE a record; selection jsonb;
            BEGIN
                SELECT * INTO a FROM public.f01_b19_authority();
                IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_{kind.upper()}' OR a.stage<>'PRIMARY'
                    OR a.finished OR a.organization_id IS DISTINCT FROM p_tenant THEN
                    RAISE EXCEPTION 'Bulk interval scope denied' USING ERRCODE='42501';
                END IF;
                selection:=public.f01_b19_selection();
                IF p_service_date IS DISTINCT FROM (selection->>'destination_date')::date THEN
                    RAISE EXCEPTION 'Bulk destination interval scope denied' USING ERRCODE='42501';
                END IF;
                RETURN QUERY SELECT x.starts_at,x.ends_at FROM public.appointments x
                    WHERE x.organization_id=p_tenant AND x.service_date=p_service_date
                        AND x.booking_mode='EXACT_TIME' AND x.status IN ('Booked','Waiting','In Examination','Completed');
            END $$;
            REVOKE ALL ON FUNCTION public.f01_b19_{kind}_intervals(uuid,date) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION public.f01_b19_{kind}_intervals(uuid,date) TO carepulse_api;
        """)
    for table in ("clinic_policy_heads", "clinic_policy_versions"):
        op.execute(f"""
            CREATE POLICY f01_api_b19_policy_read ON public.{table} FOR SELECT TO carepulse_api USING (
                kind IN ('SCHEDULE','CAPACITY','FOLLOWUP') AND public.f01_b19_policy_scope(organization_id))
        """)
    for table in ("clinic_schedule_intervals", "clinic_schedule_holidays"):
        op.execute(f"""
            CREATE POLICY f01_api_b19_schedule_read ON public.{table} FOR SELECT TO carepulse_api USING (
                public.f01_b19_policy_scope(organization_id))
        """)
    op.execute("""
        CREATE POLICY f01_api_b19_receipt_private ON public.idempotency_receipts AS RESTRICTIVE
            FOR SELECT TO carepulse_api USING (
                operation NOT IN ('appointments.bulk_reschedule.preview','appointments.bulk_reschedule.commit')
                AND NOT EXISTS (SELECT 1 FROM public.f01_context() c
                    WHERE c.purpose IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT')));
        CREATE POLICY f01_api_b19_receipt_write ON public.idempotency_receipts AS RESTRICTIVE
            FOR INSERT TO carepulse_api WITH CHECK (
                operation NOT IN ('appointments.bulk_reschedule.preview','appointments.bulk_reschedule.commit')
                AND NOT EXISTS (SELECT 1 FROM public.f01_context() c
                    WHERE c.purpose IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT')));
    """)
    for table in ("sensitive_audit", "outbox_events"):
        op.execute(f"""
            CREATE POLICY f01_api_b19_protected_evidence ON public.{table} AS RESTRICTIVE
                FOR INSERT TO carepulse_api WITH CHECK (NOT EXISTS (SELECT 1 FROM public.f01_context() c
                    WHERE c.purpose IN ('APPOINTMENT_BULK_PREVIEW','APPOINTMENT_BULK_COMMIT')))
        """)


def _snapshot() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b19_carried(p_day date,p_clock time) RETURNS timestamptz
        LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
            WITH offsets AS (
                SELECT DISTINCT ((probe AT TIME ZONE 'UTC') AT TIME ZONE 'Africa/Cairo')-probe AS utc_offset
                FROM (VALUES (p_day-1+time '12:00'),(p_day+1+time '12:00')) probes(probe)
            ), candidates AS (
                SELECT DISTINCT ((p_day+p_clock) AT TIME ZONE 'UTC')-utc_offset AS instant
                FROM offsets
            )
            SELECT CASE WHEN count(*)=1 THEN min(instant) END FROM candidates
                WHERE instant AT TIME ZONE 'Africa/Cairo'=p_day+p_clock;
        $$;

        CREATE FUNCTION public.f01_b19_candidate(p_org uuid,p_day date,p_row jsonb,p_mode text)
        RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE starts timestamptz; ends timestamptz;
        BEGIN
            IF p_row->>'status' NOT IN ('Booked','Waiting')
                OR p_row->>'classification'<>'ROUTINE' OR p_row->>'lane'<>'MAIN'
                OR p_row->>'booking_mode' IS DISTINCT FROM p_mode
                OR p_row->'destination_patient_conflict'='true'::jsonb
                OR (p_row->>'status'='Waiting' AND p_row->'queue_active' IS DISTINCT FROM 'true'::jsonb)
                OR (p_row->>'service_type'='FOLLOW_UP' AND (
                    p_row->>'claim_state' IS DISTINCT FROM 'ACTIVE'
                    OR p_row->'source_eligible' IS DISTINCT FROM 'true'::jsonb
                    OR p_row->>'approved_deadline' IS NULL
                    OR (p_day>(p_row->>'approved_deadline')::date AND p_row->>'extension_event_id' IS NULL))) THEN
                RETURN NULL;
            END IF;
            IF p_mode='EXACT_TIME' THEN
                starts:=public.f01_b19_carried(p_day,((p_row->>'starts_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time);
                ends:=public.f01_b19_carried(p_day,((p_row->>'ends_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time);
                IF starts IS NULL OR ends IS NULL OR starts>=ends THEN RETURN NULL; END IF;
            END IF;
            PERFORM public.b10_assert_followup_schedule(p_org,jsonb_build_object(
                'service_date',p_day,'booking_mode',p_mode,'starts_at',starts,'ends_at',ends));
            RETURN jsonb_build_object('starts_at',starts,'ends_at',ends);
        EXCEPTION WHEN check_violation THEN
            RETURN NULL;
        END $$;

        CREATE FUNCTION public.f01_b19_joint_conflicts(p_org uuid,p_day date,p_candidates jsonb,p_capacity integer)
        RETURNS uuid[] LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
            WITH proposed AS (
                SELECT key::uuid AS id,(value->>'starts_at')::timestamptz AS starts,
                    (value->>'ends_at')::timestamptz AS ends
                FROM jsonb_each(p_candidates) WHERE value->>'starts_at' IS NOT NULL
            ), existing AS (
                SELECT x.starts_at AS starts,x.ends_at AS ends FROM public.appointments x
                WHERE x.organization_id=p_org AND x.service_date=p_day AND x.booking_mode='EXACT_TIME'
                    AND x.status IN ('Booked','Waiting','In Examination','Completed')
            ), endpoints AS (
                SELECT starts AS instant FROM proposed UNION SELECT starts FROM existing
            ), overloaded AS (
                SELECT instant FROM endpoints e WHERE
                    (SELECT count(*) FROM proposed p WHERE p.starts<=e.instant AND p.ends>e.instant)
                    +(SELECT count(*) FROM existing x WHERE x.starts<=e.instant AND x.ends>e.instant)>p_capacity
            )
            SELECT coalesce(array_agg(p.id ORDER BY p.id),ARRAY[]::uuid[]) FROM proposed p
                WHERE EXISTS (SELECT 1 FROM overloaded e WHERE p.starts<=e.instant AND p.ends>e.instant);
        $$;

        CREATE FUNCTION public.f01_b19_bound_command(p_row jsonb,p_item jsonb,p_snapshot jsonb,p_overlap boolean)
        RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE command jsonb;
        BEGIN
            command:=jsonb_build_object('service_date',p_snapshot->'destination_date','reason',p_snapshot->'reason',
                'expected_appointment_revision',p_row->'revision','expected_schedule_revision',p_snapshot->'schedule_revision');
            IF p_row->>'status'='Waiting' THEN
                command:=command || jsonb_build_object('expected_queue_day_revision',p_row->'queue_day_revision');
            END IF;
            IF p_item->>'starts_at' IS NOT NULL THEN
                command:=command || jsonb_build_object('starts_at',p_item->'starts_at','ends_at',p_item->'ends_at');
                IF p_overlap THEN
                    command:=command || jsonb_build_object('expected_capacity_revision',p_snapshot->'capacity_revision');
                END IF;
            END IF;
            IF p_row->>'service_type'='FOLLOW_UP' THEN
                command:=command || jsonb_build_object('expected_claim_revision',p_row->'claim_revision',
                    'expected_source_decision_revision',p_row->'source_decision_revision',
                    'expected_visit_revision',p_row->'visit_revision','expected_followup_revision',p_row->'followup_revision',
                    'expected_authorization_revision',p_row->'authorization_revision');
                IF (p_snapshot->>'destination_date')::date>(p_row->>'approved_deadline')::date THEN
                    command:=command || jsonb_build_object('extension_event_id',p_row->'extension_event_id');
                END IF;
            END IF;
            RETURN command;
        END $$;

        CREATE FUNCTION public.f01_b19_assert_snapshot(snapshot jsonb) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; selection jsonb; rows jsonb; row_item jsonb; item jsonb; evidence jsonb;
            identifiers uuid[]; partition_ids uuid[]; candidates jsonb:='{}'::jsonb; candidate jsonb;
            conflicts uuid[]; destination date; expected_included boolean; included boolean;
            schedule jsonb; schedule_revision bigint; capacity_revision bigint; capacity integer;
            keys text[]:=ARRAY['original_service_date','destination_date','appointment_ids','reason','eligible_count',
                'excluded_count','included','excluded','security_revision','schedule_revision','capacity_revision','row_evidence'];
            row_keys text[]:=ARRAY['appointment_id','status','appointment_revision','source_local_start','source_local_end',
                'starts_at','ends_at'];
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.finished THEN
                RAISE EXCEPTION 'Bulk snapshot scope denied' USING ERRCODE='42501';
            END IF;
            selection:=public.f01_b19_selection();
            IF jsonb_typeof(snapshot) IS DISTINCT FROM 'object' OR NOT snapshot ?& keys
                OR snapshot - keys <> '{}'::jsonb
                OR snapshot->'original_service_date' IS DISTINCT FROM selection->'original_service_date'
                OR snapshot->'destination_date' IS DISTINCT FROM selection->'destination_date'
                OR snapshot->'appointment_ids' IS DISTINCT FROM selection->'appointment_ids'
                OR snapshot->'reason' IS DISTINCT FROM selection->'reason'
                OR snapshot->'security_revision' IS DISTINCT FROM to_jsonb(a.security_revision)
                OR jsonb_typeof(snapshot->'included') IS DISTINCT FROM 'array'
                OR jsonb_typeof(snapshot->'excluded') IS DISTINCT FROM 'array'
                OR snapshot->'eligible_count' IS DISTINCT FROM to_jsonb(jsonb_array_length(snapshot->'included'))
                OR snapshot->'excluded_count' IS DISTINCT FROM to_jsonb(jsonb_array_length(snapshot->'excluded')) THEN
                RAISE EXCEPTION 'Invalid bulk snapshot' USING ERRCODE='23514';
            END IF;
            SELECT array_agg(id::uuid ORDER BY id::uuid) INTO identifiers
                FROM jsonb_array_elements_text(selection->'appointment_ids') wanted(id);
            SELECT array_agg((part->>'appointment_id')::uuid ORDER BY (part->>'appointment_id')::uuid)
                INTO partition_ids FROM jsonb_array_elements((snapshot->'included')||(snapshot->'excluded')) part;
            IF partition_ids IS DISTINCT FROM identifiers THEN
                RAISE EXCEPTION 'Bulk partition differs from selection' USING ERRCODE='23514';
            END IF;
            destination:=(selection->>'destination_date')::date;
            SELECT h.revision,v.payload INTO STRICT schedule_revision,schedule FROM public.clinic_policy_heads h
                JOIN public.clinic_policy_versions v ON (v.organization_id,v.kind,v.version)=(h.organization_id,h.kind,h.current_version)
                WHERE h.organization_id=a.organization_id AND h.kind='SCHEDULE';
            SELECT h.revision,(v.payload->>'capacity')::integer INTO STRICT capacity_revision,capacity
                FROM public.clinic_policy_heads h JOIN public.clinic_policy_versions v
                    ON (v.organization_id,v.kind,v.version)=(h.organization_id,h.kind,h.current_version)
                WHERE h.organization_id=a.organization_id AND h.kind='CAPACITY';
            IF snapshot->'schedule_revision' IS DISTINCT FROM to_jsonb(schedule_revision)
                OR snapshot->'capacity_revision' IS DISTINCT FROM to_jsonb(capacity_revision) THEN
                RAISE EXCEPTION 'Stale bulk policy evidence' USING ERRCODE='40001';
            END IF;
            IF NOT (schedule->>'allow_overlap')::boolean THEN capacity:=1; END IF;
            rows:=public.f01_b19_rows();
            SELECT jsonb_agg(jsonb_build_object('id',r->'id','revision',r->'revision',
                'service_type',r->'service_type','classification',r->'classification','lane',r->'lane',
                'status',r->'status','patient_id',r->'patient_id','doctor_staff_id',r->'doctor_staff_id',
                'booking_mode',r->'booking_mode','starts_at',r->'starts_at','ends_at',r->'ends_at',
                'patient_revision',r->'patient_revision','queue_day_revision',r->'queue_day_revision',
                'claim_revision',r->'claim_revision','source_decision_revision',r->'source_decision_revision',
                'visit_revision',r->'visit_revision','followup_revision',r->'followup_revision',
                'authorization_revision',r->'authorization_revision') ORDER BY ordinality)
                INTO evidence FROM jsonb_array_elements(rows) WITH ORDINALITY e(r,ordinality);
            IF snapshot->'row_evidence' IS DISTINCT FROM evidence THEN
                RAISE EXCEPTION 'Stale bulk cohort revisions' USING ERRCODE='40001';
            END IF;
            FOR row_item IN SELECT * FROM jsonb_array_elements(rows) LOOP
                candidate:=public.f01_b19_candidate(a.organization_id,destination,row_item,schedule->>'booking_mode');
                IF candidate IS NOT NULL THEN
                    candidates:=candidates || jsonb_build_object(row_item->>'id',candidate);
                END IF;
            END LOOP;
            conflicts:=public.f01_b19_joint_conflicts(a.organization_id,destination,candidates,capacity);
            FOR row_item IN SELECT * FROM jsonb_array_elements(rows) LOOP
                candidate:=candidates->(row_item->>'id');
                expected_included:=candidate IS NOT NULL AND NOT ((row_item->>'id')::uuid=ANY(conflicts));
                SELECT part INTO item FROM jsonb_array_elements(snapshot->'included') part
                    WHERE part->>'appointment_id'=row_item->>'id';
                included:=FOUND;
                IF NOT included THEN
                    SELECT part INTO STRICT item FROM jsonb_array_elements(snapshot->'excluded') part
                        WHERE part->>'appointment_id'=row_item->>'id';
                END IF;
                IF included IS DISTINCT FROM expected_included
                    OR jsonb_typeof(item) IS DISTINCT FROM 'object' OR NOT item ?& row_keys
                    OR item->'appointment_revision' IS DISTINCT FROM row_item->'revision'
                    OR item->'status' IS DISTINCT FROM row_item->'status'
                    OR (item->>'source_local_start')::time IS DISTINCT FROM
                        ((row_item->>'starts_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time
                    OR (item->>'source_local_end')::time IS DISTINCT FROM
                        ((row_item->>'ends_at')::timestamptz AT TIME ZONE 'Africa/Cairo')::time
                    OR (item->>'starts_at')::timestamptz IS DISTINCT FROM (candidate->>'starts_at')::timestamptz
                    OR (item->>'ends_at')::timestamptz IS DISTINCT FROM (candidate->>'ends_at')::timestamptz THEN
                    RAISE EXCEPTION 'Bulk eligibility or interval changed' USING ERRCODE='40001';
                END IF;
                IF included THEN
                    IF item - row_keys - 'command'<>'{}'::jsonb
                        OR item->'command' IS DISTINCT FROM public.f01_b19_bound_command(
                            row_item,item,snapshot,(schedule->>'allow_overlap')::boolean) THEN
                        RAISE EXCEPTION 'Bulk row command mismatch' USING ERRCODE='23514';
                    END IF;
                ELSIF item - row_keys - 'reason'<>'{}'::jsonb
                    OR coalesce(item->>'reason','') NOT IN ('appointment not eligible','waiting queue evidence unavailable','service not eligible',
                        'patient already booked on destination day','follow-up entitlement unavailable',
                        'confirmed follow-up extension required','destination schedule unavailable','booking mode changed',
                        'destination local time unavailable','destination slot unavailable','destination time elapsed',
                        'destination capacity conflict')
                    OR (candidate IS NOT NULL AND item->>'reason'<>'destination capacity conflict') THEN
                    RAISE EXCEPTION 'Invalid bulk exclusion evidence' USING ERRCODE='23514';
                END IF;
            END LOOP;
        END $$;

        CREATE FUNCTION public.f01_b19_store_preview(snapshot jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; projection jsonb; preview_id uuid:=gen_random_uuid();
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_PREVIEW' OR a.stage<>'PRIMARY' THEN
                RAISE EXCEPTION 'Bulk preview write scope denied' USING ERRCODE='42501';
            END IF;
            projection:=public.f01_b19_preview_replay();
            IF projection IS NOT NULL THEN RETURN projection; END IF;
            PERFORM public.f01_b19_assert_snapshot(snapshot);
            projection:=snapshot || jsonb_build_object('preview_id',preview_id,'revision',1);
            INSERT INTO public.idempotency_receipts (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES (preview_id,'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,a.staff_id,
                'appointments.bulk_reschedule.preview',a.idempotency_key,a.digest,projection,1);
            RETURN projection;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b19_carried(date,time),
            public.f01_b19_candidate(uuid,date,jsonb,text),public.f01_b19_joint_conflicts(uuid,date,jsonb,integer),
            public.f01_b19_bound_command(jsonb,jsonb,jsonb,boolean),public.f01_b19_assert_snapshot(jsonb),
            public.f01_b19_store_preview(jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b19_store_preview(jsonb) TO carepulse_api;
    """)


def _reuse_b18() -> None:
    _rewrite(
        "public.d08_context(uuid,uuid,jsonb)",
        "ctx.purpose='APPOINTMENT_RESCHEDULE' AND ctx.stage='PRIMARY'",
        "ctx.purpose IN ('APPOINTMENT_RESCHEDULE','APPOINTMENT_BULK_COMMIT') AND ctx.stage='PRIMARY'",
    )
    # The existing writer still creates every per-Appointment domain/evidence
    # row. Only the enclosing B-19 command owns the single idempotency receipt.
    marker = """INSERT INTO public.idempotency_receipts (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,a.staff_id,
                'appointments.reschedule',a.idempotency_key,a.digest,projection,changed.revision);
            PERFORM public.f01_b18_mark(a.number,true);"""
    _rewrite(
        "public.f01_b18_apply()",
        marker,
        """IF NOT EXISTS (SELECT 1 FROM public.f01_b19_authority() b
                WHERE b.purpose='APPOINTMENT_BULK_COMMIT' AND b.stage='PRIMARY' AND b.active IS NOT NULL) THEN
                """
        + marker
        + """
            END IF;""",
    )


def _prepare() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b19_prepare() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; preview jsonb; ids uuid[]; destination date; kind_name text; reserved_number bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_COMMIT' OR a.stage<>'PRIMARY'
                OR a.number IS NOT NULL OR a.finished THEN
                RAISE EXCEPTION 'Bulk preparation scope denied' USING ERRCODE='42501';
            END IF;
            IF public.f01_b19_commit_replay() IS NOT NULL THEN
                RAISE EXCEPTION 'Bulk already committed' USING ERRCODE='CP001';
            END IF;
            preview:=public.f01_b19_selection();
            IF (preview->>'eligible_count')::integer<1 THEN
                RAISE EXCEPTION 'Empty included bulk cohort' USING ERRCODE='23514';
            END IF;
            destination:=(preview->>'destination_date')::date;
            SELECT array_agg(id::uuid ORDER BY id::uuid) INTO ids
                FROM jsonb_array_elements_text(preview->'appointment_ids') wanted(id);

            -- The B-18/B-10 lock prefix, across the complete original cohort.
            PERFORM p.id FROM public.patients p WHERE p.organization_id=a.organization_id
                AND EXISTS (SELECT 1 FROM public.appointments x WHERE x.organization_id=p.organization_id
                    AND x.patient_id=p.id AND x.id=ANY(ids))
                ORDER BY p.id FOR UPDATE OF p;
            FOREACH kind_name IN ARRAY ARRAY['SCHEDULE','CAPACITY','FOLLOWUP','PRICE'] LOOP
                PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id=a.organization_id
                    AND h.kind=kind_name FOR SHARE;
                IF NOT FOUND THEN RAISE EXCEPTION 'Bulk policy unavailable' USING ERRCODE='23514'; END IF;
            END LOOP;

            -- The counter is also the shared booking fence on an empty day.
            -- A deferred assignment and completion witness prevent committing preparation alone.
            INSERT INTO public.appointment_day_counters (organization_id,service_date,last_number)
            VALUES (a.organization_id,destination,1)
            ON CONFLICT (organization_id,service_date) DO UPDATE
                SET last_number=appointment_day_counters.last_number+1
            RETURNING last_number INTO reserved_number;

            PERFORM d.doctor_staff_id FROM public.doctor_controls d WHERE d.organization_id=a.organization_id
                AND EXISTS (SELECT 1 FROM public.appointments x WHERE x.organization_id=d.organization_id
                    AND x.doctor_staff_id=d.doctor_staff_id AND x.id=ANY(ids) AND x.status='Waiting')
                ORDER BY d.doctor_staff_id FOR UPDATE OF d;
            PERFORM q.id FROM public.queue_days q WHERE q.organization_id=a.organization_id
                AND EXISTS (SELECT 1 FROM public.appointments x WHERE x.organization_id=q.organization_id
                    AND (x.doctor_staff_id,x.service_date,x.lane)=(q.doctor_staff_id,q.service_date,q.lane)
                    AND x.id=ANY(ids) AND x.status='Waiting')
                ORDER BY q.doctor_staff_id,q.service_date,q.lane FOR UPDATE OF q;
            PERFORM x.id FROM public.appointments x WHERE x.organization_id=a.organization_id
                AND (x.id=ANY(ids) OR EXISTS (SELECT 1 FROM public.followup_claims cl
                    WHERE cl.organization_id=x.organization_id AND cl.source_appointment_id=x.id
                        AND cl.appointment_id=ANY(ids)))
                ORDER BY x.id FOR UPDATE OF x;
            PERFORM e.id FROM public.queue_entries e WHERE e.organization_id=a.organization_id
                AND e.appointment_id=ANY(ids) AND e.disposition='ACTIVE'
                ORDER BY e.id FOR UPDATE OF e;
            PERFORM v.id FROM public.visits v WHERE v.organization_id=a.organization_id
                AND EXISTS (SELECT 1 FROM public.followup_claims cl WHERE cl.organization_id=v.organization_id
                    AND cl.source_visit_id=v.id AND cl.appointment_id=ANY(ids))
                ORDER BY v.id FOR UPDATE OF v;
            PERFORM s.visit_id FROM public.followup_source_state s WHERE s.organization_id=a.organization_id
                AND EXISTS (SELECT 1 FROM public.followup_claims cl WHERE cl.organization_id=s.organization_id
                    AND cl.source_visit_id=s.visit_id AND cl.appointment_id=ANY(ids))
                ORDER BY s.visit_id FOR UPDATE OF s;
            PERFORM cl.id FROM public.followup_claims cl WHERE cl.organization_id=a.organization_id
                AND cl.appointment_id=ANY(ids) ORDER BY cl.id FOR UPDATE OF cl;

            -- All policy and resource evidence is freshly checked after the waits.
            PERFORM public.f01_b19_assert_snapshot(preview-'preview_id'-'revision');
            PERFORM public.f01_b19_stage(reserved_number,NULL,false);
            RETURN jsonb_build_object('preview_id',preview->'preview_id','prepared',true);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b19_prepare() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b19_prepare() TO carepulse_api;
    """)


def _apply() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b19_apply(snapshot jsonb) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; preview jsonb; bound jsonb; item jsonb; command jsonb; applied jsonb;
            moved jsonb:='[]'::jsonb; projection jsonb; reserved_number bigint;
            destination date; target uuid; queue_revision bigint; first_row boolean:=true;
        BEGIN
            SELECT * INTO a FROM public.f01_b19_authority();
            IF NOT FOUND OR a.purpose<>'APPOINTMENT_BULK_COMMIT' OR a.stage<>'PRIMARY'
                OR a.number IS NULL OR a.active IS NOT NULL OR a.finished THEN
                RAISE EXCEPTION 'Bulk application scope denied' USING ERRCODE='42501';
            END IF;
            preview:=public.f01_b19_read_preview();
            bound:=preview-'preview_id'-'revision';
            IF snapshot IS DISTINCT FROM bound AND snapshot IS DISTINCT FROM preview THEN
                RAISE EXCEPTION 'Bulk immutable snapshot mismatch' USING ERRCODE='40001';
            END IF;
            IF (bound->>'eligible_count')::integer<1 THEN
                RAISE EXCEPTION 'Empty included bulk cohort' USING ERRCODE='23514';
            END IF;
            PERFORM public.f01_b19_assert_snapshot(bound);
            destination:=(bound->>'destination_date')::date;
            reserved_number:=a.number;
            FOR item IN SELECT part FROM jsonb_array_elements(bound->'included') part
                ORDER BY (part->>'appointment_id')::uuid LOOP
                target:=(item->>'appointment_id')::uuid;
                command:=item->'command';
                IF NOT first_row THEN
                    UPDATE public.appointment_day_counters d SET last_number=d.last_number+1
                        WHERE d.organization_id=a.organization_id AND d.service_date=destination
                        RETURNING d.last_number INTO STRICT reserved_number;
                END IF;
                first_row:=false;
                -- A prior included Waiting row may have incremented this same
                -- locked QueueDay. The original bound revision was checked above.
                IF item->>'status'='Waiting' THEN
                    SELECT q.revision INTO STRICT queue_revision FROM public.queue_days q
                        JOIN public.appointments x ON x.organization_id=q.organization_id
                            AND (x.doctor_staff_id,x.service_date,x.lane)=(q.doctor_staff_id,q.service_date,q.lane)
                        WHERE x.organization_id=a.organization_id AND x.id=target;
                    command:=jsonb_set(command,'{expected_queue_day_revision}',to_jsonb(queue_revision));
                END IF;
                PERFORM public.f01_b19_stage(reserved_number,
                    jsonb_build_object('appointment_id',target,'command',command),false);
                applied:=public.f01_b18_apply();
                moved:=moved || jsonb_build_array(applied->'projection');
                -- Evaluate the inherited per-row queue closure witness while
                -- that row's exact authority and QueueDay revision are active.
                SET CONSTRAINTS public.f01_queue_rank_shift_final IMMEDIATE;
                SET CONSTRAINTS public.f01_queue_rank_shift_final DEFERRED;
            END LOOP;
            projection:=jsonb_build_object('preview_id',preview->'preview_id','moved',moved,
                'moved_count',jsonb_array_length(moved),'revision',1);
            INSERT INTO public.idempotency_receipts (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,a.staff_id,
                'appointments.bulk_reschedule.commit',a.idempotency_key,a.digest,projection,1);
            PERFORM public.f01_b19_stage(reserved_number,NULL,true);
            RETURN projection;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b19_apply(jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b19_apply(jsonb) TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _receipts_and_selection()
    _operational_reads()
    _snapshot()
    _reuse_b18()
    _prepare()
    _apply()


def downgrade() -> None:
    raise RuntimeError("B-19 migration is forward-only")
