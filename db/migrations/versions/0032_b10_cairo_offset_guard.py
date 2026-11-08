"""B-10: require non-UTC timed inputs to use Cairo's actual UTC offset."""

from alembic import op

revision = "0032_b10_cairo_offset_guard"
down_revision = "0031_b10_writer_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER FUNCTION public.b10_assert_followup_schedule(uuid,jsonb)
            RENAME TO b10_schedule_without_offset_guard;
        REVOKE ALL ON FUNCTION public.b10_schedule_without_offset_guard(uuid,jsonb)
            FROM PUBLIC, carepulse_runtime;

        CREATE FUNCTION public.b10_assert_followup_schedule(org uuid, booking jsonb)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            endpoint_key text;
            supplied text;
            offset_text text;
            supplied_minutes integer;
            cairo_minutes integer;
            instant timestamptz;
        BEGIN
            PERFORM public.b10_schedule_without_offset_guard(org, booking);
            IF booking->>'booking_mode' <> 'EXACT_TIME' THEN
                RETURN;
            END IF;
            FOREACH endpoint_key IN ARRAY ARRAY['starts_at','ends_at'] LOOP
                supplied := booking->>endpoint_key;
                offset_text := right(supplied, 6);
                -- Explicit UTC is valid even when Cairo's offset is nonzero.
                IF right(supplied, 1) = 'Z'
                   OR offset_text IN ('+00:00','-00:00') THEN
                    CONTINUE;
                END IF;
                instant := supplied::timestamptz;
                supplied_minutes := (
                    substring(offset_text, 2, 2)::integer * 60
                    + substring(offset_text, 5, 2)::integer
                ) * CASE WHEN left(offset_text, 1) = '-' THEN -1 ELSE 1 END;
                cairo_minutes := extract(epoch FROM (
                    (instant AT TIME ZONE 'Africa/Cairo')
                    - (instant AT TIME ZONE 'UTC')
                ))::integer / 60;
                IF supplied_minutes <> cairo_minutes THEN
                    RAISE EXCEPTION 'Timed endpoint UTC offset disagrees with Cairo'
                        USING ERRCODE = '23514';
                END IF;
            END LOOP;
        END $$;
        REVOKE ALL ON FUNCTION public.b10_assert_followup_schedule(uuid,jsonb)
            FROM PUBLIC, carepulse_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("B-10 Cairo offset guard migration is forward-only")
