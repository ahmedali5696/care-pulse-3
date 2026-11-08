"""B-10: expose only verified legacy source status to signed operational readers.

Clinical revision contents and historical Doctor reasons remain inaccessible through
this read seam. The original D-08 provenance checker retains its revoked runtime ACL.
"""

from alembic import op

revision = "0030_b10_source_proof"
down_revision = "0029_b10_followup_writers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.b10_operational_source_proof(org uuid, source_visit uuid)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            actor uuid;
            proof jsonb;
            decision jsonb;
            visible_decision jsonb;
            completion date;
        BEGIN
            IF session_user <> 'carepulse_runtime'
               OR current_setting('app.organization_id', true) IS DISTINCT FROM org::text
               OR NULLIF(current_setting('app.staff_id', true), '') IS NULL THEN
                RAISE EXCEPTION 'Source proof context denied' USING ERRCODE = '42501';
            END IF;
            actor := current_setting('app.staff_id', true)::uuid;
            PERFORM 1 FROM public.organizations
            WHERE id = org AND state = 'ACTIVE';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source proof clinic denied' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
            WHERE s.organization_id = org AND s.id = actor AND s.enabled AND s.verified
              AND s.reset_block_operation_id IS NULL AND u."emailVerified"
              AND u.email = s.verified_email
              AND s.role IN ('DOCTOR_OWNER', 'RECEPTIONIST');
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source proof actor denied' USING ERRCODE = '42501';
            END IF;
            PERFORM 1 FROM public.visits v
                JOIN public.appointments a
                  ON (a.organization_id, a.id) = (v.organization_id, v.appointment_id)
            WHERE v.organization_id = org AND v.id = source_visit
              AND (v.patient_id, v.doctor_staff_id) =
                  (a.patient_id, a.doctor_staff_id);
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            proof := public.d08_legacy_source(org, source_visit);
            IF proof->>'state' IS DISTINCT FROM 'VERIFIED' THEN
                RETURN jsonb_build_object('state', 'ANCESTRY_UNKNOWN');
            END IF;
            decision := NULLIF(proof->'decision', 'null'::jsonb);
            IF decision IS NOT NULL THEN
                SELECT (completed_at AT TIME ZONE 'Africa/Cairo')::date
                    INTO completion FROM public.completion_attestations
                WHERE organization_id = org AND visit_id = source_visit;
                IF completion IS NULL THEN
                    RETURN jsonb_build_object('state', 'ANCESTRY_UNKNOWN');
                END IF;
                PERFORM public.d08_validate_decision(decision, completion);
                visible_decision := jsonb_build_object('kind', decision->>'kind');
                IF decision->>'kind' = 'DEADLINE' THEN
                    visible_decision := visible_decision || jsonb_build_object(
                        'deadline_date', decision->>'deadline_date');
                END IF;
            END IF;
            RETURN jsonb_build_object(
                'state', 'VERIFIED', 'decision', visible_decision,
                'actor_staff_id', proof->>'actor_staff_id',
                'actor_role', proof->>'actor_role');
        END $$;
        REVOKE ALL ON FUNCTION public.b10_operational_source_proof(uuid,uuid)
            FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.b10_operational_source_proof(uuid,uuid)
            TO carepulse_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("B-10 source proof migration is forward-only")
