"""B-06 T123: honor the current versioned FOLLOWUP window on new DEFAULT claims."""

from alembic import op

revision = "0024_b06_followup_window"
down_revision = "0023_m1_operational_manager"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The published D-08 guard uses +7 on every new DEFAULT claim. Replace its
    # function in a forward revision: existing claim rows, their frozen prices,
    # policy snapshots and all D-08 triggers remain untouched.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_followup_claim() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            source_row record;
            target_row record;
            price_provenance jsonb;
            source_completion timestamptz;
            booking_actor uuid;
            current_policy_version bigint;
            current_followup_payload jsonb;
            current_window numeric;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Follow-up evidence cannot be deleted' USING ERRCODE = '23514';
            ELSIF TG_OP = 'UPDATE' THEN
                IF OLD.state <> 'ACTIVE' OR NEW.state NOT IN ('RELEASED','CONSUMED')
                   OR NEW.revision <> OLD.revision + 1
                   OR (to_jsonb(NEW) - 'state' - 'revision' -
                       'terminal_history_revision' - 'terminal_at') IS DISTINCT FROM
                      (to_jsonb(OLD) - 'state' - 'revision' -
                       'terminal_history_revision' - 'terminal_at') THEN
                    RAISE EXCEPTION 'Follow-up claim evidence is immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.state <> 'ACTIVE' OR NEW.revision <> 1 OR
               NEW.terminal_history_revision IS NOT NULL OR NEW.terminal_at IS NOT NULL THEN
                RAISE EXCEPTION 'Claim must begin as an active reservation'
                    USING ERRCODE = '23514';
            END IF;
            -- Lock the current policy before source Appointment/Visit; this
            -- matches the booking transaction's publication lock order.
            SELECT h.current_version, v.payload
            INTO current_policy_version, current_followup_payload
            FROM public.clinic_policy_heads h
            JOIN public.clinic_policy_versions v
              ON (v.organization_id, v.kind, v.version) =
                 (h.organization_id, h.kind, h.current_version)
            WHERE h.organization_id = NEW.organization_id
              AND h.kind = 'FOLLOWUP'
            FOR SHARE OF h;
            IF current_policy_version IS NULL
               OR NEW.policy_snapshot ->> 'policy_revision'
                    IS DISTINCT FROM current_policy_version::text THEN
                RAISE EXCEPTION 'Follow-up policy version is stale'
                    USING ERRCODE = '23514';
            END IF;
            SELECT a.id, a.organization_id, a.patient_id, a.doctor_staff_id,
                   a.status, a.service_type, a.classification,
                   v.followup_decision, v.clinical_revision
            INTO source_row FROM public.visits v
            JOIN public.appointments a ON a.organization_id = v.organization_id
                                      AND a.id = v.appointment_id
            WHERE v.organization_id = NEW.organization_id AND v.id = NEW.source_visit_id
              AND a.id = NEW.source_appointment_id
            FOR UPDATE OF a, v;
            IF NOT FOUND OR source_row.status <> 'Completed'
               OR source_row.service_type <> 'NEW_EXAMINATION'
               OR source_row.classification NOT IN ('ROUTINE','URGENT','EMERGENCY')
               OR source_row.patient_id <> NEW.patient_id
               OR source_row.doctor_staff_id <> NEW.doctor_staff_id THEN
                RAISE EXCEPTION 'Invalid completed follow-up source' USING ERRCODE = '23514';
            END IF;
            -- Doctor-authored source decisions remain mandatory until the
            -- independently authorized D-07/D-08 operational provenance cutover.
            IF source_row.clinical_revision < 1
               OR COALESCE(source_row.followup_decision ->> 'kind', '')
                      NOT IN ('DEFAULT','DEADLINE') THEN
                RAISE EXCEPTION 'Doctor follow-up authorization required'
                    USING ERRCODE = '23514';
            END IF;
            SELECT h.occurred_at INTO source_completion
            FROM public.appointment_history h
            WHERE h.organization_id = NEW.organization_id
              AND h.appointment_id = NEW.source_appointment_id
              AND h.new_status = 'Completed'
            ORDER BY h.appointment_revision DESC LIMIT 1;
            IF source_completion IS NULL OR NEW.completion_local_date <>
                (source_completion AT TIME ZONE 'Africa/Cairo')::date THEN
                RAISE EXCEPTION 'Incorrect Cairo completion day' USING ERRCODE = '23514';
            END IF;
            SELECT h.actor_staff_id INTO booking_actor
            FROM public.appointment_history h
            WHERE h.organization_id = NEW.organization_id
              AND h.appointment_id = NEW.appointment_id AND h.action = 'CREATED';

            IF source_row.followup_decision ->> 'kind' = 'DEFAULT' THEN
                -- Verify the live version, not a caller-selected historical
                -- policy snapshot or deadline.
                IF jsonb_typeof(current_followup_payload -> 'last_day_inclusive')
                    IS DISTINCT FROM 'number' THEN
                    RAISE EXCEPTION 'Follow-up default window is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
                current_window :=
                    (current_followup_payload ->> 'last_day_inclusive')::numeric;
                IF current_window < 0 OR current_window > 365
                   OR current_window <> trunc(current_window)
                   OR NEW.approved_deadline IS DISTINCT FROM
                      NEW.completion_local_date + current_window::integer THEN
                    RAISE EXCEPTION 'Follow-up default window is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            IF (source_row.followup_decision ->> 'kind' = 'DEADLINE'
                AND (btrim(COALESCE(source_row.followup_decision ->> 'reason', '')) = ''
                     OR source_row.followup_decision ->> 'deadline_date'
                        IS DISTINCT FROM NEW.approved_deadline::text))
               OR NEW.policy_snapshot ->> 'source_visit_id'
                    IS DISTINCT FROM NEW.source_visit_id::text
               OR NEW.policy_snapshot ->> 'doctor_staff_id'
                    IS DISTINCT FROM NEW.doctor_staff_id::text
               OR NEW.policy_snapshot ->> 'source_revision'
                    IS DISTINCT FROM source_row.clinical_revision::text
               OR NEW.policy_snapshot ->> 'actor_staff_id'
                    IS DISTINCT FROM booking_actor::text
               OR NEW.policy_snapshot -> 'authorized' IS DISTINCT FROM 'true'::jsonb
               OR COALESCE((NEW.policy_snapshot ->> 'policy_revision') ~ '^[1-9][0-9]*$', false) = false THEN
                RAISE EXCEPTION 'Follow-up authorization snapshot is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
            SELECT a.patient_id, a.doctor_staff_id, a.service_type,
                   a.classification, a.service_date
            INTO target_row FROM public.appointments a
            WHERE a.organization_id = NEW.organization_id AND a.id = NEW.appointment_id;
            SELECT s.entitlement_provenance INTO price_provenance
            FROM public.appointment_price_snapshots s
            WHERE s.organization_id = NEW.organization_id AND s.appointment_id = NEW.appointment_id;
            IF NOT FOUND OR target_row.patient_id IS DISTINCT FROM NEW.patient_id
               OR target_row.doctor_staff_id IS DISTINCT FROM NEW.doctor_staff_id
               OR target_row.service_type IS DISTINCT FROM 'FOLLOW_UP'
               OR target_row.classification IS DISTINCT FROM 'ROUTINE'
               OR target_row.service_date < NEW.completion_local_date
               OR target_row.service_date > NEW.approved_deadline
               OR price_provenance IS DISTINCT FROM NEW.policy_snapshot THEN
                RAISE EXCEPTION 'Follow-up target or frozen evidence mismatch'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("B-06 follow-up window migration is forward-only")
