"""B-16: signed Visit clinical, read and terminal authority."""

from alembic import op

revision = "0060_b16_visits"
down_revision = "0059_b13_queue_policy_placement"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Missing unique B-16 marker in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def _identity() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.requests ADD COLUMN b16_body jsonb,
            ADD COLUMN b16_digest text;
    """)
    _rewrite(
        "f01_private.route_purpose(text,text)",
        "RETURN CASE",
        """RETURN CASE
                WHEN p_method='GET' AND p_path ~ ('^/v1/visits/' || uuid_pattern || '$')
                    THEN 'VISIT_READ'
                WHEN p_method='POST' AND p_path ~ ('^/v1/visits/' || uuid_pattern || '/complete$')
                    THEN 'VISIT_COMPLETE'
                WHEN p_method='POST' AND p_path ~ ('^/v1/visits/' || uuid_pattern || '/cancel-exceptional$')
                    THEN 'VISIT_CANCEL_EXCEPTIONAL'""",
    )
    _rewrite(
        "public.f01_register_proof_before_intake(bytea,text,bytea,bytea)",
        """OR f01_private.route_purpose(envelope->>3, envelope->>4)
                        IS DISTINCT FROM envelope->>14""",
        """OR NOT (
                        f01_private.route_purpose(envelope->>3,envelope->>4)
                            IS NOT DISTINCT FROM envelope->>14
                        OR (envelope->>3='PATCH' AND envelope->>14='VISIT_CLINICAL'
                            AND f01_private.route_purpose(envelope->>3,envelope->>4)='VISIT_FOLLOWUP')
                    )""",
    )
    op.get_bind().exec_driver_sql(
        r"""
        ALTER FUNCTION public.f01_register_proof(bytea,text,bytea,bytea)
            RENAME TO f01_register_proof_before_b16;
        REVOKE ALL ON FUNCTION public.f01_register_proof_before_b16(bytea,text,bytea,bytea)
            FROM PUBLIC,carepulse_gate;

        CREATE FUNCTION f01_private.b16_operation(purpose text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
            SELECT CASE purpose WHEN 'VISIT_CLINICAL' THEN 'visits.clinical'
                WHEN 'VISIT_COMPLETE' THEN 'visits.complete'
                WHEN 'VISIT_CANCEL_EXCEPTIONAL' THEN 'visits.cancel_exceptional' END;
        $$;

        CREATE FUNCTION f01_private.b16_revision(v json) RETURNS boolean
        LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
            SELECT coalesce(json_typeof(v)='number' AND v::text ~ '^(-?0|[1-9][0-9]{0,18})$'
                AND pg_input_is_valid(v::text,'bigint'),false);
        $$;

        CREATE FUNCTION f01_private.b16_reason(v text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
            SELECT CASE WHEN v !~ '[\x01-\x1f]' THEN nullif(btrim(v,
                U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
                || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'),'') END;
        $$;

        CREATE FUNCTION f01_private.b16_source_family(raw json) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
        DECLARE command jsonb := raw::jsonb; d jsonb := command->'followup_decision';
        BEGIN
            IF NOT command ?& ARRAY['followup_decision','expected_visit_revision']
                OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k
                    WHERE k NOT IN ('followup_decision','expected_visit_revision',
                        'expected_source_decision_revision','expected_followup_revision'))
                OR NOT f01_private.b16_revision(raw->'expected_visit_revision')
                OR ((command ? 'expected_source_decision_revision' OR command ? 'expected_followup_revision')
                    AND NOT (f01_private.b16_revision(raw->'expected_source_decision_revision')
                        AND f01_private.b16_revision(raw->'expected_followup_revision')))
                OR jsonb_typeof(d) IS DISTINCT FROM 'object' THEN
                RETURN false;
            END IF;
            RETURN coalesce(d ? 'kind' AND d->>'kind' IN ('DEFAULT','OPTED_OUT','DEADLINE')
                AND jsonb_typeof(d->'kind')='string'
                AND NOT EXISTS (SELECT 1 FROM jsonb_object_keys(d) k
                    WHERE k NOT IN ('kind','deadline_date','reason'))
                AND (d->>'reason' IS NULL OR (jsonb_typeof(d->'reason')='string'
                    AND f01_private.b16_reason(d->>'reason') IS NOT NULL))
                AND (d->>'kind'='DEFAULT' OR d->>'reason' IS NOT NULL)
                AND CASE WHEN d->>'kind'='DEADLINE' THEN
                    jsonb_typeof(d->'deadline_date')='string'
                    AND d->>'deadline_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                    AND (d->>'deadline_date')::date::text=d->>'deadline_date'
                ELSE d->>'deadline_date' IS NULL END,false);
        END $$;

        CREATE FUNCTION f01_private.b16_command(purpose text,raw json) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
        DECLARE
            command jsonb := raw::jsonb;
            clinical constant text[] := ARRAY['examination','findings','diagnosis',
                'medical_notes','treatment','tests','recommendation'];
            applicable constant text[] := ARRAY['expected_claim_revision','expected_source_decision_revision',
                'expected_followup_revision','expected_authorization_revision'];
            field text;
        BEGIN
            IF jsonb_typeof(command) IS DISTINCT FROM 'object'
                OR NOT f01_private.b16_revision(raw->'expected_visit_revision') THEN
                RAISE EXCEPTION 'Invalid Visit command' USING ERRCODE='22023';
            END IF;
            IF purpose='VISIT_CLINICAL' THEN
                IF NOT command ?| clinical OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k
                    WHERE k<>'expected_visit_revision' AND k<>ALL(clinical))
                    OR EXISTS (SELECT 1 FROM jsonb_each(command) e
                        WHERE e.key=ANY(clinical) AND jsonb_typeof(e.value) NOT IN ('string','null')) THEN
                    RAISE EXCEPTION 'Invalid clinical family' USING ERRCODE='22023';
                END IF;
                RETURN command;
            END IF;
            IF NOT f01_private.b16_revision(raw->'expected_appointment_revision')
                OR EXISTS (SELECT 1 FROM jsonb_object_keys(command) k
                    WHERE k NOT IN ('expected_visit_revision','expected_appointment_revision',
                        CASE purpose WHEN 'VISIT_COMPLETE' THEN 'examination_occurred' ELSE 'reason' END)
                        AND k<>ALL(applicable)) THEN
                RAISE EXCEPTION 'Invalid terminal family' USING ERRCODE='22023';
            END IF;
            FOREACH field IN ARRAY applicable LOOP
                IF command->>field IS NOT NULL AND NOT f01_private.b16_revision(raw->field) THEN
                    RAISE EXCEPTION 'Invalid terminal revision' USING ERRCODE='22023';
                END IF;
            END LOOP;
            IF purpose='VISIT_COMPLETE' THEN
                IF command->'examination_occurred' IS DISTINCT FROM 'true'::jsonb THEN
                    RAISE EXCEPTION 'Positive examination required' USING ERRCODE='22023';
                END IF;
            ELSIF jsonb_typeof(command->'reason') IS DISTINCT FROM 'string'
                OR f01_private.b16_reason(command->>'reason') IS NULL THEN
                RAISE EXCEPTION 'Exceptional cancellation reason required' USING ERRCODE='22023';
            ELSE
                command := jsonb_set(command,'{reason}',to_jsonb(f01_private.b16_reason(command->>'reason')));
            END IF;
            RETURN jsonb_strip_nulls(command);
        END $$;

        CREATE FUNCTION public.f01_register_proof(payload bytea,key_id text,mac bytea,body bytea)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE request_id bigint; req f01_private.requests%ROWTYPE; raw json; command jsonb;
        BEGIN
            request_id := public.f01_register_proof_before_b16(payload,key_id,mac,body);
            SELECT * INTO STRICT req FROM f01_private.requests r WHERE r.id=request_id;
            IF req.purpose='VISIT_FOLLOWUP' AND req.method='PATCH' THEN
                BEGIN
                    raw := convert_from(body,'UTF8')::json;
                    PERFORM f01_private.canonical_intake_json(raw);
                    IF json_typeof(raw) IS DISTINCT FROM 'object'
                        OR NOT f01_private.b16_source_family(raw) THEN
                        RAISE EXCEPTION 'Invalid source family' USING ERRCODE='22023';
                    END IF;
                EXCEPTION WHEN data_exception THEN
                    UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
                END;
                RETURN request_id;
            END IF;
            IF req.purpose<>'VISIT_READ' AND f01_private.b16_operation(req.purpose) IS NULL THEN
                RETURN request_id;
            END IF;
            BEGIN
                IF req.query<>'' OR req.target_id IS NULL THEN
                    RAISE EXCEPTION 'Invalid Visit target' USING ERRCODE='22023';
                END IF;
                IF req.purpose='VISIT_READ' THEN
                    IF octet_length(body)<>0 THEN
                        RAISE EXCEPTION 'Invalid Visit read body' USING ERRCODE='22023';
                    END IF;
                    UPDATE f01_private.requests r SET b16_body='{}'::jsonb WHERE r.id=request_id;
                ELSE
                    IF req.idempotency_key !~ '^[-A-Za-z0-9._:]{1,128}$' THEN
                        RAISE EXCEPTION 'Invalid Visit command envelope' USING ERRCODE='22023';
                    END IF;
                    raw := convert_from(body,'UTF8')::json;
                    PERFORM f01_private.canonical_intake_json(raw);
                    command := f01_private.b16_command(req.purpose,raw);
                    UPDATE f01_private.requests r SET b16_body=command,
                        b16_digest=encode(sha256(convert_to(f01_private.canonical_intake_json(
                            jsonb_build_object('operation',f01_private.b16_operation(req.purpose),
                                'visit_id',req.target_id,'body',command)::json),'UTF8')),'hex')
                    WHERE r.id=request_id;
                END IF;
            EXCEPTION WHEN data_exception THEN
                UPDATE f01_private.requests r SET body_scope_valid=false WHERE r.id=request_id;
            END;
            RETURN request_id;
        END $$;

        CREATE FUNCTION public.f01_b16_authority()
        RETURNS TABLE (organization_id uuid,staff_id uuid,role text,purpose text,stage text,
            target_id uuid,operation text,idempotency_key text,command jsonb,digest text)
        LANGUAGE sql VOLATILE SECURITY DEFINER ROWS 1 SET search_path=pg_catalog,pg_temp AS $$
            SELECT r.organization_id,r.staff_id,r.role,r.purpose,c.stage,r.target_id,
                f01_private.b16_operation(r.purpose),r.idempotency_key,r.b16_body,r.b16_digest
            FROM public.f01_context() c JOIN f01_private.requests r ON r.id=c.request_id
            JOIN public.staff_accounts s ON s.organization_id=r.organization_id AND s.id=r.staff_id
            WHERE r.body_scope_valid AND r.b16_body IS NOT NULL AND r.target_id IS NOT NULL
                AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
                AND s.role=r.role AND s.security_revision=r.security_revision
                AND ((r.purpose IN ('VISIT_READ','VISIT_COMPLETE') AND r.role IN ('DOCTOR_OWNER','RECEPTIONIST'))
                    OR (r.purpose IN ('VISIT_CLINICAL','VISIT_CANCEL_EXCEPTIONAL') AND r.role='DOCTOR_OWNER'))
                AND (c.stage='PRIMARY' OR (c.stage='RECEIPT_RECOVERY' AND r.b16_digest IS NOT NULL));
        $$;
        REVOKE ALL ON FUNCTION f01_private.b16_operation(text),f01_private.b16_revision(json),
            f01_private.b16_reason(text),f01_private.b16_source_family(json),f01_private.b16_command(text,json),
            public.f01_register_proof(bytea,text,bytea,bytea),public.f01_b16_authority() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_register_proof(bytea,text,bytea,bytea) TO carepulse_gate;
        GRANT EXECUTE ON FUNCTION public.f01_b16_authority() TO carepulse_migration;
        """,
        execution_options={"no_parameters": True},
    )
    marker = "OR req.purpose = 'VISIT_FOLLOWUP'"
    _rewrite(
        "public.f01_bind(bigint,text,integer,xid8)",
        marker,
        marker
        + """
                           OR (f01_private.b16_operation(req.purpose) IS NOT NULL
                               AND req.b16_digest IS NOT NULL)""",
    )
    op.execute("""
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)


def _clinical_storage() -> None:
    op.execute("ALTER TABLE public.visits ADD COLUMN recommendation text")
    _rewrite(
        "public.guard_visit()",
        "NEW.followup_decision) IS DISTINCT FROM",
        "NEW.followup_decision, NEW.recommendation) IS DISTINCT FROM",
    )
    _rewrite(
        "public.guard_visit()",
        "(NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        "(NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
    )
    for row in ("OLD", "NEW"):
        _rewrite(
            "public.guard_visit()",
            f"'followup_decision', {row}.followup_decision);",
            f"'followup_decision', {row}.followup_decision, 'recommendation', {row}.recommendation);",
        )
    _rewrite(
        "public.guard_visit()",
        "IF old_content = new_content THEN",
        """IF session_user='carepulse_api' AND NOT EXISTS (
                    SELECT 1 FROM public.f01_b16_authority() a
                    WHERE a.purpose='VISIT_CLINICAL' AND a.stage='PRIMARY'
                        AND a.organization_id=OLD.organization_id AND a.target_id=OLD.id
                        AND a.staff_id=OLD.doctor_staff_id
                        AND OLD.clinical_revision=(a.command->>'expected_visit_revision')::bigint
                        AND new_content=old_content || (a.command-'expected_visit_revision')
                ) THEN
                    RAISE EXCEPTION 'Clinical write scope denied' USING ERRCODE='42501';
                END IF;
                IF old_content = new_content THEN""",
    )
    _rewrite(
        "public.guard_visit()",
        "IF kind NOT IN",
        """IF session_user='carepulse_api' THEN
                    kind := 'CORRECTION';
                    justification := 'Doctor clinical revision';
                END IF;
                IF kind NOT IN""",
    )
    _rewrite(
        "public.guard_visit()",
        "OR NEW.followup_decision ->> 'kind' IS DISTINCT FROM 'DEFAULT'",
        """OR NEW.recommendation IS DISTINCT FROM OLD.recommendation
                        OR NEW.followup_decision ->> 'kind' IS DISTINCT FROM 'DEFAULT'""",
    )
    # Historical snapshots have no recommendation key; compare that absence as NULL.
    _rewrite(
        "public.d08_legacy_source(uuid,uuid)",
        "'followup_decision', NULL);",
        "'followup_decision', NULL, 'recommendation', NULL);",
    )
    _rewrite(
        "public.d08_legacy_source(uuid,uuid)",
        "r.old_values IS DISTINCT FROM prior",
        "(jsonb_build_object('recommendation',NULL) || r.old_values) IS DISTINCT FROM prior",
    )
    _rewrite(
        "public.d08_legacy_source(uuid,uuid)",
        "prior := r.new_values;",
        "prior := jsonb_build_object('recommendation',NULL) || r.new_values;",
    )
    _rewrite(
        "public.d08_legacy_source(uuid,uuid)",
        "'followup_decision', v.followup_decision)",
        "'followup_decision', v.followup_decision,'recommendation',v.recommendation)",
    )


def _scope() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b16_target() RETURNS public.visits
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; v public.visits%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND THEN RAISE EXCEPTION 'Visit authority denied' USING ERRCODE='42501'; END IF;
            SELECT vis.* INTO v FROM public.visits vis
            JOIN public.appointments x ON x.organization_id=vis.organization_id AND x.id=vis.appointment_id
                AND x.patient_id=vis.patient_id AND x.doctor_staff_id=vis.doctor_staff_id
                AND x.service_date=vis.service_date
            JOIN public.patients p ON p.organization_id=vis.organization_id AND p.id=vis.patient_id
            WHERE vis.organization_id=a.organization_id AND vis.id=a.target_id
                AND (a.role<>'DOCTOR_OWNER' OR a.staff_id=vis.doctor_staff_id)
                AND NOT EXISTS (SELECT 1 FROM public.followup_claims c
                    WHERE c.organization_id=vis.organization_id AND c.appointment_id=x.id
                    AND (c.patient_id<>vis.patient_id OR c.doctor_staff_id<>vis.doctor_staff_id
                        OR NOT EXISTS (SELECT 1 FROM public.visits sv
                            JOIN public.appointments sx ON sx.organization_id=sv.organization_id
                                AND sx.id=sv.appointment_id AND sx.patient_id=sv.patient_id
                                AND sx.doctor_staff_id=sv.doctor_staff_id
                            WHERE sv.organization_id=c.organization_id AND sv.id=c.source_visit_id
                                AND sx.id=c.source_appointment_id AND sv.patient_id=c.patient_id
                                AND sv.doctor_staff_id=c.doctor_staff_id)));
            IF NOT FOUND THEN RAISE EXCEPTION 'Visit unavailable' USING ERRCODE='P0002'; END IF;
            RETURN v;
        END $$;

        CREATE FUNCTION public.f01_b16_terminal_scope(p_org uuid,p_appointment uuid,p_revision bigint,p_operation text)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_b16_authority() a
                JOIN public.visits v ON v.organization_id=a.organization_id AND v.id=a.target_id
                WHERE a.stage='PRIMARY' AND a.organization_id=p_org AND v.appointment_id=p_appointment
                    AND (a.command->>'expected_appointment_revision')::bigint=p_revision
                    AND (a.role<>'DOCTOR_OWNER' OR a.staff_id=v.doctor_staff_id)
                    AND ((p_operation='COMPLETE' AND a.purpose='VISIT_COMPLETE')
                        OR (p_operation='CANCEL' AND a.purpose='VISIT_CANCEL_EXCEPTIONAL'
                            AND a.staff_id=v.doctor_staff_id)));
        $$;

        CREATE FUNCTION public.f01_b16_history_scope(h public.appointment_history)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
            SELECT EXISTS (SELECT 1 FROM public.f01_b16_authority() a
                JOIN public.visits v ON v.organization_id=a.organization_id AND v.id=a.target_id
                JOIN public.appointments x ON x.organization_id=v.organization_id AND x.id=v.appointment_id
                WHERE a.stage='PRIMARY' AND h.organization_id=a.organization_id AND h.appointment_id=x.id
                    AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                    AND h.appointment_revision=(a.command->>'expected_appointment_revision')::bigint+1
                    AND v.clinical_revision=(a.command->>'expected_visit_revision')::bigint
                    AND x.revision=h.appointment_revision AND x.status=h.new_status
                    AND h.old_status='In Examination'
                    AND h.old_service_date=x.service_date AND h.new_service_date=x.service_date
                    AND h.old_number=x.current_number AND h.new_number=x.current_number
                    AND h.price_snapshot_appointment_id=x.id AND h.replacement_appointment_id IS NULL
                    AND ((a.purpose='VISIT_COMPLETE' AND h.action='STATUS' AND h.new_status='Completed'
                            AND h.reason='Examination completed')
                        OR (a.purpose='VISIT_CANCEL_EXCEPTIONAL' AND h.action='DOCTOR_EXCEPTION'
                            AND h.new_status='Cancelled' AND h.reason=a.command->>'reason')));
        $$;
        REVOKE ALL ON FUNCTION public.f01_b16_target(),public.f01_b16_terminal_scope(uuid,uuid,bigint,text),
            public.f01_b16_history_scope(public.appointment_history) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b16_history_scope(public.appointment_history)
            TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
    """)
    _rewrite(
        "f01_private.guard_audit_appointment_history()",
        "IF session_user = 'carepulse_api' AND NOT EXISTS (",
        """IF session_user = 'carepulse_api' AND NOT public.f01_b16_history_scope(NEW)
                AND NOT EXISTS (""",
    )
    op.execute("RESET ROLE")
    for signature, operation in (
        (
            "public.d07_complete_examined_visit_raw(uuid,uuid,bigint,boolean)",
            "'COMPLETE'",
        ),
        (
            "public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)",
            "requested_operation",
        ),
    ):
        _rewrite(
            signature,
            "IF session_user <> 'carepulse_runtime'",
            f"""IF session_user <> 'carepulse_api'
               OR NOT public.f01_b16_terminal_scope(target_organization,target_appointment,
                    expected_revision,{operation})""",
        )
        _rewrite(
            signature,
            "current_setting('app.organization_id', true)",
            "public.f01_org()",
        )
        # Each occurrence is replaced in one pass because both are authority-bearing.
        connection = op.get_bind()
        definition = connection.exec_driver_sql(
            f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
        ).scalar_one()
        marker = "current_setting('app.staff_id', true)"
        if definition.count(marker) != 2:
            raise RuntimeError(f"Missing B-16 actor markers in {signature}")
        connection.exec_driver_sql(
            definition.replace(marker, "public.f01_staff()"),
            execution_options={"no_parameters": True},
        )
    _rewrite(
        "public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)",
        "RAISE EXCEPTION 'Exam Visit missing' USING ERRCODE = '23514';\n                END IF;",
        """RAISE EXCEPTION 'Exam Visit missing' USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM public.f01_b16_authority() a
                    JOIN public.visits v ON v.organization_id=a.organization_id AND v.id=a.target_id
                    WHERE v.id=retained_visit AND v.clinical_revision=(a.command->>'expected_visit_revision')::bigint) THEN
                    RAISE EXCEPTION 'Stale terminal Visit' USING ERRCODE='40001';
                END IF;""",
    )


def _projections() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b16_projection(v public.visits,x public.appointments)
        RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
            SELECT jsonb_build_object('visit_id',v.id,'appointment_id',x.id,'status',x.status,
                'visit_revision',v.clinical_revision,'appointment_revision',x.revision);
        $$;

        CREATE FUNCTION public.f01_b16_attestation(v public.visits) RETURNS jsonb
        LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
            SELECT jsonb_build_object('examination_occurred',c.examination_occurred,
                'actor_staff_id',c.actor_staff_id,'actor_role',c.actor_role,'completed_at',c.completed_at)
            FROM public.completion_attestations c
            WHERE c.organization_id=v.organization_id AND c.visit_id=v.id AND c.appointment_id=v.appointment_id;
        $$;

        CREATE FUNCTION public.f01_b16_read() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; v public.visits%ROWTYPE; x public.appointments%ROWTYPE;
            cl public.followup_claims%ROWTYPE; source_id uuid; projection jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND OR a.purpose<>'VISIT_READ' OR a.stage<>'PRIMARY' THEN
                RAISE EXCEPTION 'Visit read scope denied' USING ERRCODE='42501';
            END IF;
            v := public.f01_b16_target();
            SELECT * INTO STRICT x FROM public.appointments ax
                WHERE ax.organization_id=v.organization_id AND ax.id=v.appointment_id;
            SELECT * INTO cl FROM public.followup_claims c
                WHERE c.organization_id=v.organization_id AND c.appointment_id=x.id
                    AND c.patient_id=v.patient_id AND c.doctor_staff_id=v.doctor_staff_id;
            source_id := coalesce(cl.source_visit_id,CASE WHEN x.service_type='NEW_EXAMINATION'
                AND x.status='Completed' THEN v.id END);
            projection := public.f01_b16_projection(v,x) || jsonb_build_object(
                'patient_id',v.patient_id,'doctor_staff_id',v.doctor_staff_id,'service_date',v.service_date,
                'admitted_at',v.admitted_at,'completion_attestation',public.f01_b16_attestation(v),
                'source_visit_id',source_id,
                'source_decision_revision',CASE WHEN source_id IS NOT NULL THEN coalesce((
                    SELECT s.source_revision FROM public.followup_source_state s
                    WHERE s.organization_id=v.organization_id AND s.visit_id=source_id),0) END,
                'followup_revision',(SELECT h.revision FROM public.clinic_policy_heads h
                    WHERE h.organization_id=v.organization_id AND h.kind='FOLLOWUP' AND source_id IS NOT NULL),
                'claim_revision',cl.revision,
                'authorization_revision',CASE WHEN cl.id IS NOT NULL THEN (
                    SELECT coalesce(max(e.authorization_revision),0) FROM public.followup_authorization_events e
                    WHERE e.organization_id=v.organization_id AND e.claim_id=cl.id) END);
            IF a.role='DOCTOR_OWNER' THEN
                projection := projection || jsonb_build_object('examination',v.examination,'findings',v.findings,
                    'diagnosis',v.diagnosis,'medical_notes',v.medical_notes,'treatment',v.treatment,
                    'tests',v.tests,'recommendation',v.recommendation);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.f01_b16_authority() live WHERE live.purpose='VISIT_READ'
                AND live.stage='PRIMARY' AND live.target_id=v.id) THEN
                RAISE EXCEPTION 'Visit read authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',projection,'revision',v.clinical_revision);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b16_projection(public.visits,public.appointments),
            public.f01_b16_attestation(public.visits),public.f01_b16_read() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b16_read() TO carepulse_api;
    """)


def _receipts() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b16_read_receipt() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; v public.visits%ROWTYPE; r public.idempotency_receipts%ROWTYPE;
            projection jsonb; x public.appointments%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND OR a.operation IS NULL OR a.digest IS NULL THEN
                RAISE EXCEPTION 'Visit receipt scope denied' USING ERRCODE='42501';
            END IF;
            v := public.f01_b16_target();
            SELECT * INTO r FROM public.idempotency_receipts ir
            WHERE ir.scope_kind='TENANT' AND ir.scope_id=a.organization_id
                AND ir.organization_id=a.organization_id AND ir.actor_kind='STAFF'
                AND ir.actor_id=a.staff_id AND ir.staff_id=a.staff_id
                AND ir.operation=a.operation AND ir.idempotency_key=a.idempotency_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF r.request_digest<>a.digest THEN
                RAISE EXCEPTION 'Visit idempotency conflict' USING ERRCODE='CP001';
            END IF;
            projection := r.result_projection;
            IF projection->>'visit_id' IS DISTINCT FROM v.id::text
                OR projection->>'appointment_id' IS DISTINCT FROM v.appointment_id::text THEN
                RAISE EXCEPTION 'Visit receipt linkage denied' USING ERRCODE='42501';
            END IF;
            IF a.purpose='VISIT_CLINICAL' THEN
                IF EXISTS (SELECT 1 FROM jsonb_object_keys(projection) k WHERE k NOT IN
                    ('visit_id','appointment_id','status','visit_revision','appointment_revision'))
                    OR r.result_revision<>(a.command->>'expected_visit_revision')::bigint+1
                    OR projection->'visit_revision' IS DISTINCT FROM to_jsonb(r.result_revision)
                    OR NOT EXISTS (SELECT 1 FROM public.clinical_revisions cr
                        JOIN public.clinical_audit ca ON ca.organization_id=cr.organization_id
                            AND ca.correlation_id=cr.id::text AND ca.doctor_staff_id=cr.actor_staff_id
                            AND ca.clinical_revision=cr.revision
                        JOIN public.appointment_history h ON h.organization_id=cr.organization_id
                            AND h.appointment_id=v.appointment_id
                            AND h.appointment_revision=(projection->>'appointment_revision')::bigint
                        WHERE cr.organization_id=v.organization_id AND cr.visit_id=v.id
                            AND cr.revision=r.result_revision AND cr.actor_staff_id=a.staff_id
                            AND cr.actor_role=a.role AND cr.change_kind='CORRECTION'
                            AND cr.new_values=cr.old_values || (a.command-'expected_visit_revision')
                            AND h.new_status=projection->>'status') THEN
                    RAISE EXCEPTION 'Clinical receipt evidence denied' USING ERRCODE='42501';
                END IF;
            ELSE
                SELECT * INTO STRICT x FROM public.appointments ax
                    WHERE ax.organization_id=v.organization_id AND ax.id=v.appointment_id;
                IF r.result_revision<>(a.command->>'expected_appointment_revision')::bigint+1
                    OR projection->'appointment_revision' IS DISTINCT FROM to_jsonb(r.result_revision)
                    OR projection->'visit_revision' IS DISTINCT FROM a.command->'expected_visit_revision'
                    OR projection->'claim_revision' IS DISTINCT FROM coalesce((
                        SELECT to_jsonb(c.revision) FROM public.followup_claims c
                        WHERE c.organization_id=v.organization_id AND c.appointment_id=x.id
                            AND c.terminal_history_revision=x.revision
                            AND c.revision=(a.command->>'expected_claim_revision')::bigint+1
                            AND c.state=CASE a.purpose WHEN 'VISIT_COMPLETE' THEN 'CONSUMED' ELSE 'RELEASED' END
                    ),'null'::jsonb)
                    OR (x.service_type='FOLLOW_UP' AND projection->'claim_revision'='null'::jsonb)
                    OR x.revision<>r.result_revision OR projection->>'status' IS DISTINCT FROM x.status
                    OR NOT EXISTS (SELECT 1 FROM public.appointment_history h
                        WHERE h.organization_id=v.organization_id AND h.appointment_id=x.id
                            AND h.appointment_revision=x.revision AND h.actor_staff_id=a.staff_id AND h.actor_role=a.role
                            AND h.old_status='In Examination' AND h.new_status=x.status
                            AND ((a.purpose='VISIT_COMPLETE' AND h.action='STATUS' AND x.status='Completed')
                                OR (a.purpose='VISIT_CANCEL_EXCEPTIONAL' AND h.action='DOCTOR_EXCEPTION'
                                    AND x.status='Cancelled' AND h.reason=a.command->>'reason')))
                    OR NOT EXISTS (SELECT 1 FROM public.outbox_events e
                        WHERE e.organization_id=v.organization_id AND e.aggregate_type='APPOINTMENT'
                            AND e.aggregate_id=x.id::text AND e.event_type=CASE a.purpose
                                WHEN 'VISIT_COMPLETE' THEN 'VISIT_EXAMINATION_COMPLETED'
                                ELSE 'VISIT_EXAMINATION_CANCELLED' END
                            AND e.payload=jsonb_build_object('visit_id',v.id,'appointment_id',x.id,
                                'status',x.status,'appointment_revision',x.revision,
                                'actor_staff_id',a.staff_id,'actor_role',a.role))
                    OR EXISTS (SELECT 1 FROM jsonb_object_keys(projection) k WHERE k NOT IN
                        ('visit_id','appointment_id','status','visit_revision','appointment_revision',
                         'claim_revision','completion_attestation')) THEN
                    RAISE EXCEPTION 'Terminal receipt evidence denied' USING ERRCODE='42501';
                END IF;
                IF a.purpose='VISIT_COMPLETE' THEN
                    PERFORM public.assert_positive_exam_completion(v.organization_id,x.id,x.revision);
                    IF projection->'completion_attestation' IS DISTINCT FROM public.f01_b16_attestation(v) THEN
                        RAISE EXCEPTION 'Completion receipt evidence denied' USING ERRCODE='42501';
                    END IF;
                ELSIF projection ? 'completion_attestation' OR NOT EXISTS (
                    SELECT 1 FROM public.sensitive_audit au JOIN public.appointment_history h
                        ON h.organization_id=au.organization_id AND h.id::text=au.correlation_id
                    WHERE au.organization_id=v.organization_id AND h.appointment_id=x.id
                        AND h.appointment_revision=x.revision AND au.action='VISIT_EXAMINATION_CANCELLED'
                        AND au.actor_staff_id=a.staff_id AND au.actor_role=a.role
                        AND au.reason=a.command->>'reason' AND au.after_state->>'visit_id'=v.id::text
                        AND au.after_state->>'appointment_id'=x.id::text
                        AND au.after_state->>'revision'=x.revision::text
                        AND au.after_state->>'status'='Cancelled') THEN
                    RAISE EXCEPTION 'Cancellation receipt projection denied' USING ERRCODE='42501';
                END IF;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.f01_b16_authority() live
                WHERE live.digest=a.digest AND live.target_id=v.id) THEN
                RAISE EXCEPTION 'Visit receipt authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',projection,'revision',r.result_revision,'replayed',true);
        END $$;

        CREATE FUNCTION public.f01_b16_replay() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.f01_b16_authority() a
                WHERE a.stage='RECEIPT_RECOVERY' AND a.operation IS NOT NULL) THEN
                RAISE EXCEPTION 'Visit receipt recovery denied' USING ERRCODE='42501';
            END IF;
            RETURN public.f01_b16_read_receipt();
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b16_read_receipt(),public.f01_b16_replay() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b16_replay() TO carepulse_api;
    """)


def _clinical_writer() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b16_apply_clinical() RETURNS jsonb
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; v public.visits%ROWTYPE; x public.appointments%ROWTYPE; cr public.clinical_revisions%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.purpose<>'VISIT_CLINICAL' THEN
                RAISE EXCEPTION 'Clinical writer scope denied' USING ERRCODE='42501';
            END IF;
            v := public.f01_b16_target();
            PERFORM 1 FROM public.patients p WHERE p.organization_id=v.organization_id AND p.id=v.patient_id FOR UPDATE;
            SELECT * INTO STRICT x FROM public.appointments ax
                WHERE ax.organization_id=v.organization_id AND ax.id=v.appointment_id FOR UPDATE;
            SELECT * INTO STRICT v FROM public.visits vis
                WHERE vis.organization_id=a.organization_id AND vis.id=a.target_id FOR UPDATE;
            IF v.clinical_revision<>(a.command->>'expected_visit_revision')::bigint THEN
                RAISE EXCEPTION 'Stale clinical revision' USING ERRCODE='40001';
            END IF;
            IF x.status NOT IN ('In Examination','Completed','Cancelled')
                OR (x.status='Cancelled' AND NOT EXISTS (SELECT 1 FROM public.appointment_history h
                    WHERE h.organization_id=x.organization_id AND h.appointment_id=x.id
                        AND h.appointment_revision=x.revision AND h.action='DOCTOR_EXCEPTION'
                        AND h.old_status='In Examination' AND h.new_status='Cancelled')) THEN
                RAISE EXCEPTION 'Clinical Visit status denied' USING ERRCODE='23514';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM jsonb_each(a.command-'expected_visit_revision') e
                WHERE to_jsonb(v)->e.key IS DISTINCT FROM e.value) THEN
                RAISE EXCEPTION 'Clinical revision requires a change' USING ERRCODE='40001';
            END IF;
            UPDATE public.visits vis SET
                examination=CASE WHEN a.command ? 'examination' THEN a.command->>'examination' ELSE v.examination END,
                findings=CASE WHEN a.command ? 'findings' THEN a.command->>'findings' ELSE v.findings END,
                diagnosis=CASE WHEN a.command ? 'diagnosis' THEN a.command->>'diagnosis' ELSE v.diagnosis END,
                medical_notes=CASE WHEN a.command ? 'medical_notes' THEN a.command->>'medical_notes' ELSE v.medical_notes END,
                treatment=CASE WHEN a.command ? 'treatment' THEN a.command->>'treatment' ELSE v.treatment END,
                tests=CASE WHEN a.command ? 'tests' THEN a.command->>'tests' ELSE v.tests END,
                recommendation=CASE WHEN a.command ? 'recommendation' THEN a.command->>'recommendation' ELSE v.recommendation END
            WHERE vis.organization_id=v.organization_id AND vis.id=v.id RETURNING * INTO v;
            SELECT * INTO STRICT cr FROM public.clinical_revisions r
                WHERE r.organization_id=v.organization_id AND r.visit_id=v.id AND r.revision=v.clinical_revision;
            INSERT INTO public.clinical_audit
                (id,organization_id,doctor_staff_id,clinical_revision,correlation_id,occurred_at)
            VALUES (gen_random_uuid(),v.organization_id,a.staff_id,v.clinical_revision,cr.id::text,cr.occurred_at);
            RETURN public.f01_b16_projection(v,x);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b16_apply_clinical() FROM PUBLIC;
    """)


def _terminal_writer() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b16_cancel_unclaimed(v public.visits,x public.appointments,p_reason text)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; history_id uuid:=gen_random_uuid(); moment timestamptz;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND OR a.purpose<>'VISIT_CANCEL_EXCEPTIONAL' OR a.stage<>'PRIMARY'
                OR a.target_id<>v.id OR a.organization_id<>v.organization_id
                OR a.staff_id<>v.doctor_staff_id OR p_reason IS DISTINCT FROM a.command->>'reason' THEN
                RAISE EXCEPTION 'Exceptional cancellation scope denied' USING ERRCODE='42501';
            END IF;
            UPDATE public.appointments ax SET status='Cancelled',revision=x.revision+1
                WHERE ax.organization_id=x.organization_id AND ax.id=x.id;
            INSERT INTO public.appointment_history (id,organization_id,appointment_id,appointment_revision,
                action,old_status,new_status,old_service_date,new_service_date,old_number,new_number,
                actor_staff_id,actor_role,reason,price_snapshot_appointment_id)
            VALUES (history_id,x.organization_id,x.id,x.revision+1,'DOCTOR_EXCEPTION','In Examination','Cancelled',
                x.service_date,x.service_date,x.current_number,x.current_number,a.staff_id,a.role,p_reason,x.id)
            RETURNING occurred_at INTO moment;
            INSERT INTO public.sensitive_audit (id,organization_id,actor_staff_id,actor_role,action,reason,
                correlation_id,before_state,after_state,occurred_at)
            VALUES (gen_random_uuid(),x.organization_id,a.staff_id,a.role,'VISIT_EXAMINATION_CANCELLED',p_reason,
                history_id::text,jsonb_build_object('appointment_id',x.id,'visit_id',v.id,'revision',x.revision,'status','In Examination'),
                jsonb_build_object('appointment_id',x.id,'visit_id',v.id,'revision',x.revision+1,'status','Cancelled'),moment);
        END $$;

        CREATE FUNCTION public.f01_b16_apply_terminal() RETURNS jsonb
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; v public.visits%ROWTYPE; x public.appointments%ROWTYPE;
            cl public.followup_claims%ROWTYPE; followup_revision bigint; projection jsonb; terminal_command jsonb;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.purpose NOT IN ('VISIT_COMPLETE','VISIT_CANCEL_EXCEPTIONAL') THEN
                RAISE EXCEPTION 'Terminal Visit scope denied' USING ERRCODE='42501';
            END IF;
            v := public.f01_b16_target();
            PERFORM 1 FROM public.patients p WHERE p.organization_id=v.organization_id AND p.id=v.patient_id FOR UPDATE;
            PERFORM 1 FROM public.clinic_policy_heads h WHERE h.organization_id=v.organization_id
                AND h.kind='SCHEDULE' FOR SHARE;
            SELECT h.revision INTO STRICT followup_revision FROM public.clinic_policy_heads h
                WHERE h.organization_id=v.organization_id AND h.kind='FOLLOWUP' FOR SHARE;
            SELECT * INTO STRICT x FROM public.appointments ax
                WHERE ax.organization_id=v.organization_id AND ax.id=v.appointment_id;
            IF x.status<>'In Examination' OR x.revision<>(a.command->>'expected_appointment_revision')::bigint THEN
                RAISE EXCEPTION 'Stale terminal Appointment' USING ERRCODE='40001';
            END IF;
            SELECT * INTO cl FROM public.followup_claims c
                WHERE c.organization_id=v.organization_id AND c.appointment_id=x.id;
            IF x.service_type='FOLLOW_UP' THEN
                IF cl.id IS NULL OR cl.state<>'ACTIVE'
                    OR followup_revision IS DISTINCT FROM (a.command->>'expected_followup_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale terminal claim or policy' USING ERRCODE='40001';
                END IF;
                terminal_command := jsonb_build_object('operation',CASE a.purpose
                    WHEN 'VISIT_COMPLETE' THEN 'COMPLETE' ELSE 'CANCEL' END,
                    'expected_claim_revision',a.command->'expected_claim_revision',
                    'expected_source_revision',a.command->'expected_source_decision_revision',
                    'expected_authorization_revision',a.command->'expected_authorization_revision')
                    || CASE a.purpose WHEN 'VISIT_COMPLETE' THEN jsonb_build_object('examination_occurred',true)
                        ELSE jsonb_build_object('reason',a.command->>'reason') END;
                PERFORM public.transition_terminal_appointment(v.organization_id,x.id,x.revision,terminal_command);
            ELSE
                IF cl.id IS NOT NULL OR a.command ?| ARRAY['expected_claim_revision','expected_source_decision_revision',
                    'expected_followup_revision','expected_authorization_revision'] THEN
                    RAISE EXCEPTION 'Unexpected terminal claim revision' USING ERRCODE='23514';
                END IF;
                SELECT * INTO STRICT x FROM public.appointments ax
                    WHERE ax.organization_id=v.organization_id AND ax.id=v.appointment_id FOR UPDATE;
                SELECT * INTO STRICT v FROM public.visits vis
                    WHERE vis.organization_id=a.organization_id AND vis.id=a.target_id FOR UPDATE;
                IF x.status<>'In Examination' OR x.revision<>(a.command->>'expected_appointment_revision')::bigint
                    OR v.clinical_revision<>(a.command->>'expected_visit_revision')::bigint THEN
                    RAISE EXCEPTION 'Stale terminal revision' USING ERRCODE='40001';
                END IF;
                IF a.purpose='VISIT_COMPLETE' THEN
                    PERFORM public.d07_complete_examined_visit_raw(v.organization_id,x.id,x.revision,true);
                ELSE
                    PERFORM public.f01_b16_cancel_unclaimed(v,x,a.command->>'reason');
                END IF;
            END IF;
            SELECT * INTO STRICT x FROM public.appointments ax WHERE ax.organization_id=v.organization_id AND ax.id=v.appointment_id;
            SELECT * INTO STRICT v FROM public.visits vis WHERE vis.organization_id=a.organization_id AND vis.id=a.target_id;
            projection := public.f01_b16_projection(v,x) || jsonb_build_object('claim_revision',(
                SELECT c.revision FROM public.followup_claims c WHERE c.organization_id=v.organization_id AND c.appointment_id=x.id));
            IF a.purpose='VISIT_COMPLETE' THEN
                projection := projection || jsonb_build_object('completion_attestation',public.f01_b16_attestation(v));
            END IF;
            INSERT INTO public.outbox_events (id,organization_id,event_type,aggregate_type,aggregate_id,payload)
            VALUES (gen_random_uuid(),v.organization_id,CASE a.purpose
                WHEN 'VISIT_COMPLETE' THEN 'VISIT_EXAMINATION_COMPLETED' ELSE 'VISIT_EXAMINATION_CANCELLED' END,
                'APPOINTMENT',x.id::text,jsonb_build_object('visit_id',v.id,'appointment_id',x.id,
                    'status',x.status,'appointment_revision',x.revision,'actor_staff_id',a.staff_id,'actor_role',a.role));
            RETURN projection;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b16_cancel_unclaimed(public.visits,public.appointments,text),
            public.f01_b16_apply_terminal() FROM PUBLIC;
    """)


def _writers() -> None:
    _projections()
    _receipts()
    _clinical_writer()
    _terminal_writer()
    op.execute("""
        CREATE FUNCTION public.f01_b16_mutate() RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE a record; projection jsonb; replay jsonb; revision bigint;
        BEGIN
            SELECT * INTO a FROM public.f01_b16_authority();
            IF NOT FOUND OR a.stage<>'PRIMARY' OR a.operation IS NULL THEN
                RAISE EXCEPTION 'Visit command scope denied' USING ERRCODE='42501';
            END IF;
            replay := public.f01_b16_read_receipt();
            IF replay IS NOT NULL THEN RETURN replay; END IF;
            IF a.purpose='VISIT_CLINICAL' THEN
                projection := public.f01_b16_apply_clinical();
                revision := (projection->>'visit_revision')::bigint;
            ELSE
                projection := public.f01_b16_apply_terminal();
                revision := (projection->>'appointment_revision')::bigint;
            END IF;
            INSERT INTO public.idempotency_receipts
                (id,scope_kind,scope_id,actor_kind,actor_id,organization_id,staff_id,operation,idempotency_key,
                    request_digest,result_projection,result_revision)
            VALUES (gen_random_uuid(),'TENANT',a.organization_id,'STAFF',a.staff_id,a.organization_id,
                a.staff_id,a.operation,a.idempotency_key,a.digest,projection,revision);
            IF NOT EXISTS (SELECT 1 FROM public.f01_b16_authority() live
                WHERE live.stage='PRIMARY' AND live.digest=a.digest AND live.target_id=a.target_id) THEN
                RAISE EXCEPTION 'Visit command authority expired' USING ERRCODE='42501';
            END IF;
            RETURN jsonb_build_object('projection',projection,'revision',revision,'replayed',false);
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b16_mutate() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b16_mutate() TO carepulse_api;
    """)


def upgrade() -> None:
    _identity()
    _clinical_storage()
    _scope()
    _writers()


def downgrade() -> None:
    raise RuntimeError("B-16 Visit migration is forward-only")
