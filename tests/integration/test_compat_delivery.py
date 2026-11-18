"""US2 sender uncertainty, ambiguous SMTP settlement, and lease-expiry non-reclaim (T041)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from carepulse.compat.admission import (
    CANDIDATE_HEAD,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    record_attempt_disposition,
    release_invocation_permit,
)
from carepulse.notifications.evidence import ClaimPolicy, claim_delivery_job

from ops.compat.control import claim_owner_epoch, release_owner_lock
from ops.compat.drain import drain_and_settle
from ops.compat.work import (
    WorkEvidenceError,
    assert_all_work_settled,
    resolve_work_with_proof,
)
from tests.integration.compat_ci_helpers import (
    build_manifest,
    connect_role,
    disposable_database,
    ensure_roles_provisioned,
    get_cluster_id,
    grant_db_access,
    make_sa_engine,
    require_compat_ci,
    upgrade_to_revision,
)
from tests.integration.compat_matrix_fixture import (
    initial_activate_matched_target,
    provision_target_control,
)


@pytest.fixture(scope="module", autouse=True)
def _ci_guard() -> None:
    require_compat_ci()


def _seed_claimable_job(conn, job_id: uuid.UUID) -> uuid.UUID:
    """A real due, proof-backed job; UNKNOWN must block a valid reclaim."""
    organization_id, operator_id, invitation_id = (uuid.uuid4() for _ in range(3))
    recipient = f"compat-{job_id.hex}@example.invalid"
    conn.execute(
        """
        INSERT INTO public.operator_identities (id, credential_digest, issued_by_reference)
        VALUES (%s, %s, 'compat-delivery-synthetic')
        """,
        (operator_id, f"synthetic:{operator_id}"),
    )
    conn.execute(
        "INSERT INTO public.organizations (id, branch, created_by_operator_id) "
        "VALUES (%s, 'Synthetic compat delivery', %s)",
        (organization_id, operator_id),
    )
    conn.execute(
        """
        INSERT INTO public.invitations
            (id, organization_id, purpose, target_email, proof_digest,
             issued_by_operator_id, expires_at)
        VALUES (%s, %s, 'FIRST_OWNER', %s, %s, %s,
                clock_timestamp() + interval '1 hour')
        """,
        (invitation_id, organization_id, recipient, "a" * 64, operator_id),
    )
    conn.execute(
        """
        INSERT INTO public.delivery_jobs
            (id, organization_id, invitation_id, proof_reference,
             purpose, recipient, template, logical_key, proof_expires_at)
        VALUES (%s, %s, %s, %s,
                'FIRST_OWNER', %s, 'FIRST_OWNER_INVITE', %s,
                clock_timestamp() + interval '1 hour')
        """,
        (
            job_id,
            organization_id,
            invitation_id,
            str(invitation_id),
            recipient,
            str(invitation_id),
        ),
    )
    conn.execute(
        """
        INSERT INTO public.protected_delivery_material
            (job_id, delivery_key_id, ciphertext_base64url)
        VALUES (%s, 'compat-synthetic-key', %s)
        """,
        (job_id, "A" * 512),
    )
    conn.commit()
    return organization_id


def test_ambiguous_sender_attempt_blocks_reclaim_and_drain() -> None:
    with disposable_database("compat_deliv") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=False)
            grant_db_access(admin, dbname, source_only=False)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, CANDIDATE_HEAD)

        target_id = f"t-{dbname}"
        src_m = build_manifest(
            generation="source",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        cand_m = build_manifest(
            generation="candidate",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_m.manifest_id,
                target_manifest_id=cand_m.manifest_id,
            )
            initial_activate_matched_target(
                mig, manifest=cand_m, expected_target_id=target_id
            )
        finally:
            mig.close()

        # Sender records a claimed attempt that ends in ambiguous UNKNOWN outcome.
        sender_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=cand_m.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="sender",
            db_name=dbname,
        )
        job_id = uuid.uuid4()
        fixture_conn = connect_role("carepulse_migration", dbname=dbname)
        try:
            organization_id = _seed_claimable_job(fixture_conn, job_id)
        finally:
            fixture_conn.close()
        sender_eng = make_sa_engine("carepulse_sender", dbname=dbname)
        try:
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                # The same job was genuinely eligible and claimable before
                # transport became ambiguous; never test an absent job.
                original = claim_delivery_job(
                    conn, ClaimPolicy(timedelta(seconds=30), 3), job_id=job_id
                )
                assert original is not None and original.id == job_id

            # Simulate a crashed sender's lease expiry in this disposable test
            # database without relaxing the production claim/lease guards.
            migration = connect_role("carepulse_migration", dbname=dbname)
            try:
                migration.execute(
                    "ALTER TABLE public.delivery_jobs DISABLE TRIGGER delivery_jobs_guard"
                )
                migration.execute(
                    "UPDATE public.delivery_jobs SET "
                    "claimed_at = clock_timestamp() - interval '2 hours', "
                    "lease_expires_at = clock_timestamp() - interval '1 hour' "
                    "WHERE id = %s",
                    (job_id,),
                )
                migration.execute(
                    "ALTER TABLE public.delivery_jobs ENABLE TRIGGER delivery_jobs_guard"
                )
                migration.commit()
            finally:
                migration.close()

            permit, pconn = acquire_invocation_permit(
                sender_eng,
                config=sender_cfg,
                expected_login="carepulse_sender",
                invocation_class="sender_claim",
                correlation_id=str(job_id),
                record_attempt=True,
            )
            record_attempt_disposition(
                pconn,
                permit,
                invocation_class="sender_claim",
                disposition="UNKNOWN",
                correlation_id=str(job_id),
                lease_generation=original.generation,
            )
            release_invocation_permit(pconn, permit)

            # Even after lease expiry or restart, claim_delivery_job refuses to reclaim UNKNOWN job_id!
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                claimed = claim_delivery_job(
                    conn, ClaimPolicy(timedelta(seconds=30), 3), job_id=job_id
                )
                assert claimed is None
            with sender_eng.begin() as conn:
                conn.exec_driver_sql(
                    "SELECT set_config('app.organization_id', %s, true)",
                    (str(organization_id),),
                )
                assert (
                    claim_delivery_job(conn, ClaimPolicy(timedelta(seconds=30), 3))
                    is None
                )
            verify = connect_role("carepulse_migration", dbname=dbname)
            try:
                job = verify.execute(
                    "SELECT state, claim_generation, attempts FROM public.delivery_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
                assert job["state"] == "CLAIMED"
                assert job["claim_generation"] == original.generation
                assert job["attempts"] == 1
            finally:
                verify.close()
        finally:
            sender_eng.dispose()

        # Controller drain MUST fail closed while UNKNOWN work exists.
        owner_conn = connect_role("carepulse_migration", dbname=dbname)
        try:
            ctrl = claim_owner_epoch(
                owner_conn,
                target_id=target_id,
                expected_cluster_id=cluster_id,
                expected_db_name=dbname,
            )
            with pytest.raises(WorkEvidenceError):
                drain_and_settle(
                    owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
                )

            # No SMTP transport was invoked by this synthetic negative case.
            resolve_work_with_proof(
                owner_conn,
                attempt_id=permit.attempt_id,
                target_id=target_id,
                proven_disposition="NOT_SENT",
                correlation_id=str(job_id),
            )
            assert_all_work_settled(owner_conn, target_id=target_id)
            settled_ctrl = drain_and_settle(
                owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
            )
            assert settled_ctrl.drain_verified_epoch == ctrl.owner_epoch
        finally:
            release_owner_lock(owner_conn, target_id)
            owner_conn.close()
