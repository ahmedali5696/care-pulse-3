"""B-12: bind queue resources to the signed appointment or dated read partition."""

from alembic import op

revision = "0048_b12_queue_scope"
down_revision = "0047_b12_queue_checkin_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Definer lookup avoids recursive RLS between appointments and queue entries.
    # Only the authenticated request's target can determine the write partition.
    op.execute(r"""
        CREATE FUNCTION public.f01_queue_partition_matches(
            p_tenant uuid, p_doctor uuid, p_service_date date, p_lane text)
        RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = p_tenant::text AND c.stage = 'PRIMARY'
                  AND c.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                  AND (
                      (c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                          AND EXISTS (
                              SELECT 1 FROM public.appointments a
                              WHERE a.organization_id = p_tenant AND a.id = c.target_id
                                AND a.doctor_staff_id = p_doctor
                                AND a.service_date = p_service_date AND a.lane = p_lane
                          ))
                      OR (c.purpose = 'QUEUE_READ' AND c.method = 'GET'
                          AND c.path = '/v1/queue'
                          AND c.query ~ '^date=[0-9]{4}-[0-9]{2}-[0-9]{2}&lane=(MAIN|EMERGENCY)(&limit=[^&]*)?(&offset=[^&]*)?$'
                          AND split_part(c.query, '&', 1) = 'date=' || to_char(p_service_date, 'YYYY-MM-DD')
                          AND split_part(c.query, '&', 2) = 'lane=' || p_lane)
                  )
            );
        $$;

        CREATE FUNCTION public.f01_queue_active_exam_count(p_tenant uuid)
        RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = p_tenant::text AND c.stage = 'PRIMARY'
                  AND c.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                  AND c.purpose = 'QUEUE_READ' AND c.method = 'GET' AND c.path = '/v1/queue'
                  AND c.query ~ '^date=[0-9]{4}-[0-9]{2}-[0-9]{2}&lane=(MAIN|EMERGENCY)(&limit=[^&]*)?(&offset=[^&]*)?$'
            ) THEN
                RAISE EXCEPTION 'Queue examination count scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN (SELECT count(*) FROM public.appointments a
                    WHERE a.organization_id = p_tenant AND a.status = 'In Examination');
        END $$;

        REVOKE ALL ON FUNCTION public.f01_queue_partition_matches(uuid,uuid,date,text),
            public.f01_queue_active_exam_count(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_queue_partition_matches(uuid,uuid,date,text),
            public.f01_queue_active_exam_count(uuid) TO carepulse_api;
    """)

    # Retain the existing operation/column grants and narrow all queue row access.
    op.execute("""
        CREATE POLICY f01_api_queue_partition ON public.queue_days
            AS RESTRICTIVE FOR ALL TO carepulse_api
            USING (public.f01_queue_partition_matches(
                organization_id, doctor_staff_id, service_date, lane))
            WITH CHECK (public.f01_queue_partition_matches(
                organization_id, doctor_staff_id, service_date, lane));

        CREATE POLICY f01_api_queue_partition ON public.queue_entries
            AS RESTRICTIVE FOR ALL TO carepulse_api
            USING (disposition = 'ACTIVE' AND public.f01_queue_partition_matches(
                organization_id, doctor_staff_id, service_date, lane))
            WITH CHECK (disposition = 'ACTIVE' AND public.f01_queue_partition_matches(
                organization_id, doctor_staff_id, service_date, lane));

        CREATE POLICY f01_api_checkin_peer_read ON public.appointments
            FOR SELECT TO carepulse_api
            USING (status = 'Waiting' AND EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = appointments.organization_id::text
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND c.stage = 'PRIMARY'
                  AND EXISTS (
                      SELECT 1 FROM public.queue_entries e
                      WHERE e.organization_id = appointments.organization_id
                        AND e.appointment_id = appointments.id AND e.disposition = 'ACTIVE'
                  )
            ));

        CREATE POLICY f01_api_dated_queue_read ON public.appointments
            AS RESTRICTIVE FOR SELECT TO carepulse_api
            USING (NOT EXISTS (
                SELECT 1 FROM public.f01_context() c WHERE c.purpose = 'QUEUE_READ'
            ) OR (status = 'Waiting' AND EXISTS (
                SELECT 1 FROM public.queue_entries e
                WHERE e.organization_id = appointments.organization_id
                  AND e.appointment_id = appointments.id AND e.disposition = 'ACTIVE'
            )));

        CREATE POLICY f01_api_checkin_policy_lock ON public.clinic_policy_heads
            FOR UPDATE TO carepulse_api
            USING (kind IN ('SCHEDULE', 'QUEUE') AND EXISTS (
                SELECT 1 FROM public.f01_context() c
                WHERE c.organization_id = clinic_policy_heads.organization_id::text
                  AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST'
                  AND c.stage = 'PRIMARY' AND c.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
            )) WITH CHECK (false);
    """)

    for table in ("clinic_policy_heads", "clinic_policy_versions"):
        op.execute(f"""
            CREATE POLICY f01_api_queue_policy_scope ON public.{table}
                AS RESTRICTIVE FOR SELECT TO carepulse_api
                USING (NOT EXISTS (
                    SELECT 1 FROM public.f01_context() c
                    WHERE (c.purpose = 'QUEUE_CHECK_IN' AND (
                            c.stage <> 'PRIMARY' OR {table}.kind NOT IN ('SCHEDULE', 'QUEUE')))
                       OR (c.purpose = 'QUEUE_READ' AND (
                            c.stage <> 'PRIMARY' OR {table}.kind NOT IN ('QUEUE', 'CAPACITY', 'ADMISSION')))
                ));
        """)

    # A check-in cannot revise an existing queue without consuming one arrival.
    # D-06 independently checks the final arrival ledger against that counter.
    op.execute("""
        CREATE FUNCTION public.f01_guard_queue_arrival_increment()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user = 'carepulse_api' THEN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.arrival_counter <> 1 THEN
                        RAISE EXCEPTION 'Queue arrival increment scope denied' USING ERRCODE = '42501';
                    END IF;
                ELSIF NEW.arrival_counter <> OLD.arrival_counter + 1 THEN
                    RAISE EXCEPTION 'Queue arrival increment scope denied' USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_guard_queue_arrival_increment() FROM PUBLIC;
        CREATE TRIGGER f01_queue_arrival_increment
            BEFORE INSERT OR UPDATE ON public.queue_days FOR EACH ROW
            EXECUTE FUNCTION public.f01_guard_queue_arrival_increment();

        CREATE FUNCTION public.f01_assert_queue_rank_shift()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> 'carepulse_api' THEN
                RETURN NULL;
            END IF;
            IF OLD.routine_rank IS NULL OR NEW.routine_rank IS DISTINCT FROM OLD.routine_rank + 1
                OR NOT EXISTS (
                    SELECT 1 FROM public.queue_days q
                    JOIN public.queue_entries arrival
                      ON arrival.organization_id = q.organization_id AND arrival.queue_day_id = q.id
                     AND arrival.arrival_sequence = q.arrival_counter
                    JOIN public.appointments a
                      ON a.organization_id = arrival.organization_id AND a.id = arrival.appointment_id
                    JOIN public.appointment_history h
                      ON h.organization_id = a.organization_id AND h.appointment_id = a.id
                     AND h.appointment_revision = a.revision
                    CROSS JOIN public.f01_context() c
                    WHERE q.organization_id = NEW.organization_id AND q.id = NEW.queue_day_id
                      AND q.revision_transaction_id = pg_current_xact_id()
                      AND arrival.id <> NEW.id AND arrival.disposition = 'ACTIVE'
                      AND arrival.routine_rank <= OLD.routine_rank
                      AND a.status = 'Waiting' AND a.classification = 'ROUTINE'
                      AND c.organization_id = q.organization_id::text AND c.target_id = a.id
                      AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND c.stage = 'PRIMARY'
                      AND c.role IN ('DOCTOR_OWNER', 'RECEPTIONIST')
                      AND arrival.attested_by_staff_id::text = c.staff_id
                      AND h.action = 'CHECKED_IN' AND h.old_status = 'Booked' AND h.new_status = 'Waiting'
                      AND h.actor_staff_id::text = c.staff_id AND h.actor_role = c.role
                ) THEN
                RAISE EXCEPTION 'Queue rank shift scope denied' USING ERRCODE = '42501';
            END IF;
            RETURN NULL;
        END $$;
        REVOKE ALL ON FUNCTION public.f01_assert_queue_rank_shift() FROM PUBLIC;
        CREATE CONSTRAINT TRIGGER f01_queue_rank_shift_final
            AFTER UPDATE ON public.queue_entries DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.f01_assert_queue_rank_shift();
    """)


def downgrade() -> None:
    raise RuntimeError("B-12 queue resource scope migration is forward-only")
