"""B-20 worker-only cutoff, constrained system evidence and durable receipts."""

from alembic import op

revision = "0072_b20_worker"
down_revision = "0071_b20_staff"
branch_labels = None
depends_on = None


def _evidence() -> None:
    # Retain both existing actor shapes verbatim. SYSTEM is only valid for this
    # operation; the actor UUID identifies the worker service, never a staff row.
    op.execute("""
        ALTER TABLE public.appointment_history ALTER COLUMN actor_staff_id DROP NOT NULL;
        ALTER TABLE public.appointment_history DROP CONSTRAINT appointment_history_actor_role_check;
        ALTER TABLE public.appointment_history ADD CONSTRAINT appointment_history_actor_role_check CHECK ((
            (actor_role IN ('DOCTOR_OWNER','RECEPTIONIST') AND actor_staff_id IS NOT NULL)
            OR (actor_role='SYSTEM' AND actor_staff_id IS NULL AND action='STATUS'
                AND old_status='Booked' AND new_status='No-show' AND reason='Configured No-show cutoff'
                AND old_service_date=new_service_date AND old_number=new_number
                AND replacement_appointment_id IS NULL)) IS TRUE);

        ALTER TABLE public.idempotency_receipts DROP CONSTRAINT idempotency_receipts_actor_kind_check;
        ALTER TABLE public.idempotency_receipts ADD CONSTRAINT idempotency_receipts_actor_kind_check
            CHECK (actor_kind IN ('STAFF','OPERATOR','SYSTEM'));
        DO $$
        DECLARE shape record;
        BEGIN
            SELECT conname,pg_get_expr(conbin,conrelid) AS expression INTO STRICT shape
            FROM pg_constraint WHERE conrelid='public.idempotency_receipts'::regclass
                AND contype='c' AND pg_get_expr(conbin,conrelid) LIKE '%OPERATOR_CREATE%'
                AND pg_get_expr(conbin,conrelid) LIKE '%actor_id%';
            EXECUTE format('ALTER TABLE public.idempotency_receipts DROP CONSTRAINT %I',shape.conname);
            EXECUTE format('ALTER TABLE public.idempotency_receipts ADD CONSTRAINT %I CHECK ((%s) OR (
                scope_kind=''TENANT'' AND actor_kind=''SYSTEM'' AND organization_id IS NOT NULL
                AND scope_id=organization_id AND staff_id IS NULL AND operator_id IS NULL
                AND actor_id=''00000000-0000-4000-8000-000000000020''::uuid
                AND operation=''appointments.cutoff_no_show'' AND result_organization_id IS NULL
                    AND result_projection IS NOT NULL
                    AND jsonb_typeof(result_projection)=''object'' AND result_revision IS NOT NULL
                    AND result_projection->>''appointment_id''=idempotency_key
                    AND result_projection->>''appointment_revision''=result_revision::text
                    AND result_projection->>''status''=''No-show''
                    AND result_projection ?& ARRAY[''appointment_id'',''status'',''service_date'',
                        ''appointment_revision'',''claim_revision'']
                    AND result_projection - ARRAY[''appointment_id'',''status'',''service_date'',
                        ''appointment_revision'',''claim_revision'']=''{}''::jsonb
                    AND jsonb_typeof(result_projection->''service_date'')=''string''
                    AND jsonb_typeof(result_projection->''appointment_revision'')=''number''
                    AND jsonb_typeof(result_projection->''claim_revision'') IN (''number'',''null'')) IS TRUE)',shape.conname,shape.expression);
        END $$;
    """)
    bind = op.get_bind()
    definition = bind.exec_driver_sql(
        "SELECT pg_get_functiondef('public.guard_appointment_history_insert()'::regprocedure)"
    ).scalar_one()
    marker = "IF NOT EXISTS (\n                SELECT 1 FROM public.staff_accounts s"
    end = "AND s.id = NEW.actor_staff_id AND s.role = NEW.actor_role\n            ) OR NOT EXISTS ("
    if definition.count(marker) != 1 or definition.count(end) != 1:
        raise RuntimeError("Unexpected historical actor guard")
    definition = definition.replace(
        marker,
        """IF NOT ((NEW.actor_role='SYSTEM' AND NEW.actor_staff_id IS NULL
                AND session_user='carepulse_worker' AND current_user='carepulse_migration') OR EXISTS (
                SELECT 1 FROM public.staff_accounts s""",
    ).replace(
        end,
        "AND s.id = NEW.actor_staff_id AND s.role = NEW.actor_role\n            )) OR NOT EXISTS (",
    )
    bind.exec_driver_sql(definition, execution_options={"no_parameters": True})


def _cutoff_authority() -> None:
    # The DB issues a capability for each selected row. The existing F-01 key
    # stays with its non-login owner; no application receives key material.
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        CREATE FUNCTION public.f01_b20_cutoff_token(p_tenant uuid,p_day date,p_version bigint,
            p_target uuid,p_revision bigint) RETURNS bytea
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE signing_key bytea;
        BEGIN
            IF session_user <> 'carepulse_worker' THEN
                RAISE EXCEPTION 'Worker scope denied' USING ERRCODE='42501';
            END IF;
            SELECT k.secret INTO signing_key FROM f01_private.keys k
                WHERE k.status='ACTIVE' ORDER BY k.key_id LIMIT 1 FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Worker scope unavailable' USING ERRCODE='42501'; END IF;
            RETURN f01_crypto.hmac(convert_to(jsonb_build_array('carepulse-cutoff-v1',
                current_database(),session_user::text,p_tenant,p_day,p_version,p_target,p_revision)::text,
                'UTF8'),signing_key,'sha256');
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b20_cutoff_token(uuid,date,bigint,uuid,bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b20_cutoff_token(uuid,date,bigint,uuid,bigint) TO carepulse_migration;
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)
    op.execute("""
        CREATE FUNCTION public.b20_cutoff_instant(p_day date,p_time time) RETURNS timestamptz
        LANGUAGE plpgsql STABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
        DECLARE wall timestamp:=p_day+p_time; chosen timestamptz;
            forward_wall timestamp; low_seconds integer:=0; high_seconds integer; middle integer;
            probe timestamp;
        BEGIN
            -- PostgreSQL chooses standard time for Cairo's repeated fall-back hour.
            chosen:=wall AT TIME ZONE 'Africa/Cairo';
            forward_wall:=chosen AT TIME ZONE 'Africa/Cairo';
            IF forward_wall=wall THEN RETURN chosen; END IF;
            high_seconds:=extract(epoch FROM forward_wall-wall)::integer;
            IF high_seconds<=0 THEN RAISE EXCEPTION 'Cutoff timezone resolution failed' USING ERRCODE='22023'; END IF;
            WHILE high_seconds-low_seconds>1 LOOP
                middle:=(low_seconds+high_seconds)/2;
                probe:=wall+make_interval(secs=>middle);
                IF (probe AT TIME ZONE 'Africa/Cairo') AT TIME ZONE 'Africa/Cairo'=probe THEN
                    high_seconds:=middle;
                ELSE low_seconds:=middle;
                END IF;
            END LOOP;
            RETURN (wall+make_interval(secs=>high_seconds)) AT TIME ZONE 'Africa/Cairo';
        END $$;

        CREATE FUNCTION public.b20_cutoff_lock_organization(p_tenant uuid,p_day date,p_version bigint)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            IF session_user<>'carepulse_worker' OR current_user<>'carepulse_migration'
                OR current_setting('transaction_isolation')<>'read committed'
                OR p_tenant IS NULL OR p_day IS NULL OR NOT isfinite(p_day)
                OR p_version IS NULL OR p_version<1 THEN
                RAISE EXCEPTION 'Worker cutoff denied' USING ERRCODE='42501';
            END IF;
            PERFORM 1 FROM public.organizations o WHERE o.id=p_tenant AND o.state='ACTIVE' FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Worker cutoff denied' USING ERRCODE='42501'; END IF;
        END $$;

        CREATE FUNCTION public.b20_cutoff_authorize(p_tenant uuid,p_day date,p_version bigint)
        RETURNS timestamptz LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE cutoff jsonb; resolved timestamptz;
        BEGIN
            PERFORM public.b20_cutoff_lock_organization(p_tenant,p_day,p_version);
            SELECT v.payload->'no_show_cutoff' INTO cutoff FROM public.clinic_policy_heads h
                JOIN public.clinic_policy_versions v ON v.organization_id=h.organization_id
                    AND v.kind=h.kind AND v.version=h.current_version
                WHERE h.organization_id=p_tenant AND h.kind='SCHEDULE' AND h.current_version=p_version FOR SHARE OF h;
            IF NOT FOUND OR cutoff->'enabled' IS DISTINCT FROM 'true'::jsonb OR cutoff->>'local_time' IS NULL THEN
                RAISE EXCEPTION 'Worker cutoff policy unavailable' USING ERRCODE='23514';
            END IF;
            resolved:=public.b20_cutoff_instant(p_day,(cutoff->>'local_time')::time);
            IF resolved IS NULL OR clock_timestamp()<resolved THEN
                RAISE EXCEPTION 'Worker cutoff is not due' USING ERRCODE='23514';
            END IF;
            RETURN resolved;
        END $$;
        REVOKE ALL ON FUNCTION public.b20_cutoff_instant(date,time),
            public.b20_cutoff_lock_organization(uuid,date,bigint),
            public.b20_cutoff_authorize(uuid,date,bigint) FROM PUBLIC;
    """)


def _worker_writers() -> None:
    op.execute("""
        CREATE FUNCTION public.f01_b20_cutoff_candidates(p_tenant uuid,p_day date,p_version bigint)
        RETURNS TABLE(appointment_id uuid,appointment_revision bigint,scope_token bytea)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN
            PERFORM public.b20_cutoff_authorize(p_tenant,p_day,p_version);
            RETURN QUERY SELECT a.id,a.revision,
                public.f01_b20_cutoff_token(p_tenant,p_day,p_version,a.id,a.revision)
                FROM public.appointments a
                WHERE a.organization_id=p_tenant AND a.service_date=p_day AND a.status='Booked'
                ORDER BY a.id LIMIT 100;
        END $$;

        CREATE FUNCTION public.f01_b20_cutoff_appointment(p_tenant uuid,p_day date,p_version bigint,
            p_target uuid,p_expected_revision bigint,p_token bytea) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        DECLARE x public.appointments%ROWTYPE; cl public.followup_claims%ROWTYPE;
            r public.idempotency_receipts%ROWTYPE; digest text; projection jsonb; resolved timestamptz;
            worker_id constant uuid:='00000000-0000-4000-8000-000000000020';
        BEGIN
            IF p_target IS NULL OR p_expected_revision IS NULL OR p_expected_revision<0 THEN
                RAISE EXCEPTION 'Worker cutoff target denied' USING ERRCODE='42501';
            END IF;
            PERFORM public.b20_cutoff_lock_organization(p_tenant,p_day,p_version);
            IF p_token IS NULL OR p_token IS DISTINCT FROM
                public.f01_b20_cutoff_token(p_tenant,p_day,p_version,p_target,p_expected_revision) THEN
                RAISE EXCEPTION 'Worker cutoff scope denied' USING ERRCODE='42501';
            END IF;
            SELECT * INTO x FROM public.appointments a WHERE a.organization_id=p_tenant
                AND a.service_date=p_day AND a.id=p_target;
            IF NOT FOUND THEN RAISE EXCEPTION 'Worker cutoff target unavailable' USING ERRCODE='P0002'; END IF;
            digest:=encode(sha256(convert_to(jsonb_build_array('appointments.cutoff_no_show',
                p_tenant,p_day,p_version,p_target,p_expected_revision)::text,'UTF8')),'hex');
            SELECT * INTO r FROM public.idempotency_receipts i WHERE i.scope_kind='TENANT'
                AND i.scope_id=p_tenant AND i.organization_id=p_tenant AND i.actor_kind='SYSTEM'
                AND i.actor_id=worker_id AND i.operation='appointments.cutoff_no_show' AND i.idempotency_key=p_target::text;
            IF FOUND THEN
                PERFORM public.b20_cutoff_authorize(p_tenant,p_day,p_version);
                IF r.request_digest<>digest THEN RAISE EXCEPTION 'Cutoff receipt conflict' USING ERRCODE='CP001'; END IF;
                SELECT * INTO cl FROM public.followup_claims c WHERE c.organization_id=p_tenant
                    AND c.appointment_id=p_target;
                IF r.result_projection IS DISTINCT FROM jsonb_build_object(
                        'appointment_id',p_target,'status','No-show','service_date',p_day,
                        'appointment_revision',p_expected_revision+1,'claim_revision',cl.revision)
                    OR r.result_revision IS DISTINCT FROM p_expected_revision+1
                    OR NOT EXISTS(SELECT 1 FROM public.appointment_history h WHERE h.organization_id=p_tenant
                        AND h.appointment_id=p_target AND h.appointment_revision=r.result_revision
                        AND h.actor_role='SYSTEM' AND h.actor_staff_id IS NULL AND h.action='STATUS'
                        AND h.old_status='Booked' AND h.new_status='No-show'
                        AND h.reason='Configured No-show cutoff' AND h.new_service_date=p_day
                        AND (cl.id IS NULL OR (cl.state='RELEASED'
                            AND cl.terminal_history_revision=h.appointment_revision AND cl.terminal_at=h.occurred_at)))
                    OR (x.service_type='FOLLOW_UP') IS DISTINCT FROM (cl.id IS NOT NULL) THEN
                    RAISE EXCEPTION 'Cutoff receipt evidence denied' USING ERRCODE='42501';
                END IF;
                RETURN r.result_projection;
            END IF;
            x:=public.b20_lock_unarrived(p_tenant,p_target);
            IF x.service_date<>p_day OR x.revision<>p_expected_revision THEN
                RAISE EXCEPTION 'Stale cutoff target' USING ERRCODE='40001';
            END IF;
            resolved:=public.b20_cutoff_authorize(p_tenant,p_day,p_version);
            cl:=public.b20_absence_claim(x);
            projection:=public.b20_apply_no_show(x,cl,NULL,'SYSTEM','Configured No-show cutoff',
                p_version,resolved);
            INSERT INTO public.idempotency_receipts(id,scope_kind,scope_id,actor_kind,actor_id,organization_id,
                operation,idempotency_key,request_digest,result_projection,result_revision)
            VALUES(gen_random_uuid(),'TENANT',p_tenant,'SYSTEM',worker_id,p_tenant,
                'appointments.cutoff_no_show',p_target::text,digest,projection,x.revision+1);
            RETURN projection;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_b20_cutoff_candidates(uuid,date,bigint),
            public.f01_b20_cutoff_appointment(uuid,date,bigint,uuid,bigint,bytea) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_b20_cutoff_candidates(uuid,date,bigint),
            public.f01_b20_cutoff_appointment(uuid,date,bigint,uuid,bigint,bytea) TO carepulse_worker;
    """)


def upgrade() -> None:
    _evidence()
    _cutoff_authority()
    _worker_writers()


def downgrade() -> None:
    raise RuntimeError("B-20 worker migration is forward-only")
