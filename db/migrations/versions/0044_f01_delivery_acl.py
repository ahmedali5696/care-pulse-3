"""Keep delivery lease ownership private and retain uncertain gate bindings."""

from alembic import op

revision = "0044_f01_delivery_acl"
down_revision = "0043_f01_writer_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve the pre-cutover projection; a table grant also exposes claim_owner.
    op.execute("REVOKE SELECT ON public.delivery_jobs FROM carepulse_api")
    op.execute("""
        GRANT SELECT (id, organization_id, staff_id, invitation_id, verification_id,
            recovery_operation_id, proof_reference, purpose, recipient, template,
            logical_key, proof_expires_at, state, attempts, not_before,
            claim_generation, claimed_at, lease_expires_at, last_outcome,
            created_at, updated_at)
        ON public.delivery_jobs TO carepulse_api
    """)
    # Unknown transaction status is not proof of commit or rollback.
    op.execute("GRANT CREATE ON SCHEMA public TO carepulse_identity_owner")
    op.execute("SET LOCAL ROLE carepulse_identity_owner")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.f01_cleanup() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE observed timestamptz; affected integer;
        BEGIN
            IF session_user <> 'carepulse_gate' THEN
                RAISE EXCEPTION 'cleanup denied' USING ERRCODE = '42501';
            END IF;
            observed := f01_private.gate_now();
            WITH expired AS (
                SELECT r.id FROM f01_private.requests r WHERE r.expires_at <= observed
                    AND NOT EXISTS (SELECT 1 FROM f01_private.bindings b
                        WHERE b.request_id = r.id
                        AND pg_xact_status(b.xid) IS DISTINCT FROM 'committed'
                        AND pg_xact_status(b.xid) IS DISTINCT FROM 'aborted')
                    FOR UPDATE SKIP LOCKED
            )
            DELETE FROM f01_private.requests r USING expired e WHERE r.id = e.id;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END $$;
    """)
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner")


def downgrade() -> None:
    raise RuntimeError("F-01 delivery ACL migration is forward-only")
