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
        sender_eng = make_sa_engine("carepulse_sender", dbname=dbname)
        try:
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
                lease_generation=1,
            )
            release_invocation_permit(pconn, permit)

            # Even after lease expiry or restart, claim_delivery_job refuses to reclaim UNKNOWN job_id!
            with sender_eng.begin() as conn:
                claimed = claim_delivery_job(
                    conn, ClaimPolicy(timedelta(seconds=30), 3), job_id=job_id
                )
                assert claimed is None
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

            # Once independently proven SENT, work settles and drain succeeds.
            resolve_work_with_proof(
                owner_conn,
                attempt_id=permit.attempt_id,
                target_id=target_id,
                proven_disposition="SENT",
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
