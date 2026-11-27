"""B-12: require all signed check-in evidence in the transition transaction."""

from alembic import op

revision = "0050_b12_checkin_completeness"
down_revision = "0049_b12_checkin_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
        ALTER TABLE f01_private.activations
            ADD COLUMN checkin_audit_recorded boolean NOT NULL DEFAULT false,
            ADD COLUMN checkin_outbox_recorded boolean NOT NULL DEFAULT false,
            ADD COLUMN checkin_receipt_recorded boolean NOT NULL DEFAULT false;

        -- 0049 validates each evidence row before INSERT. Record only successful
        -- inserts, in the same transaction as the private transition mark.
        CREATE FUNCTION public.f01_record_checkin_evidence()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> 'carepulse_api' THEN RETURN NULL; END IF;
            UPDATE f01_private.activations a SET
                checkin_audit_recorded = a.checkin_audit_recorded OR TG_TABLE_NAME = 'sensitive_audit',
                checkin_outbox_recorded = a.checkin_outbox_recorded OR TG_TABLE_NAME = 'outbox_events',
                checkin_receipt_recorded = a.checkin_receipt_recorded OR TG_TABLE_NAME = 'idempotency_receipts'
            FROM public.f01_context() c
            WHERE c.request_id = a.request_id AND c.stage = a.stage
              AND c.purpose = 'QUEUE_CHECK_IN' AND c.method = 'POST' AND c.stage = 'PRIMARY'
              AND a.checkin_applied_revision IS NOT NULL
              AND NOT CASE TG_TABLE_NAME
                  WHEN 'sensitive_audit' THEN a.checkin_audit_recorded
                  WHEN 'outbox_events' THEN a.checkin_outbox_recorded
                  WHEN 'idempotency_receipts' THEN a.checkin_receipt_recorded
                  ELSE true END;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Check-in evidence recording denied' USING ERRCODE = '42501';
            END IF;
            RETURN NULL;
        END $$;

        CREATE FUNCTION f01_private.assert_checkin_evidence_complete()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM f01_private.activations a
                WHERE a.request_id = NEW.request_id AND a.stage = NEW.stage
                  AND a.checkin_applied_revision = NEW.checkin_applied_revision
                  AND a.checkin_audit_recorded AND a.checkin_outbox_recorded
                  AND a.checkin_receipt_recorded) THEN
                RAISE EXCEPTION 'Check-in commit evidence incomplete' USING ERRCODE = '42501';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER f01_checkin_evidence_committed
            AFTER UPDATE ON f01_private.activations DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW WHEN (OLD.checkin_applied_revision IS NULL
                AND NEW.checkin_applied_revision IS NOT NULL)
            EXECUTE FUNCTION f01_private.assert_checkin_evidence_complete();
        REVOKE ALL ON FUNCTION f01_private.assert_checkin_evidence_complete(),
            public.f01_record_checkin_evidence() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.f01_record_checkin_evidence() TO carepulse_migration;
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;

        CREATE TRIGGER f01_record_checkin_audit AFTER INSERT ON public.sensitive_audit
            FOR EACH ROW WHEN (NEW.action = 'appointments.check_in')
            EXECUTE FUNCTION public.f01_record_checkin_evidence();
        CREATE TRIGGER f01_record_checkin_outbox AFTER INSERT ON public.outbox_events
            FOR EACH ROW WHEN (NEW.event_type = 'PATIENT_CHECKED_IN')
            EXECUTE FUNCTION public.f01_record_checkin_evidence();
        CREATE TRIGGER f01_record_checkin_receipt AFTER INSERT ON public.idempotency_receipts
            FOR EACH ROW WHEN (NEW.operation = 'appointments.check_in')
            EXECUTE FUNCTION public.f01_record_checkin_evidence();
    """)


def downgrade() -> None:
    raise RuntimeError("B-12 check-in completeness migration is forward-only")
