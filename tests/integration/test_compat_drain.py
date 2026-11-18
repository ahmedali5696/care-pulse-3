"""US2 ordered closure, PostgreSQL waiter/blocker locks, and controller contention tests (T040)."""

from __future__ import annotations

import threading
import time

import pytest
from carepulse.compat.admission import (
    SOURCE_HEAD,
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    release_invocation_permit,
)

from ops.compat.control import (
    ContentionError,
    claim_owner_epoch,
    inspect_as_competitor,
    release_exclusive_drain_permit,
    release_owner_lock,
)
from ops.compat.drain import (
    DrainError,
    commit_draining_fence,
    drain_and_settle,
    observe_drain_complete,
)
from ops.compat.launch import mark_source_sealed_in_control
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


def test_ordered_invocation_versus_drain_with_lock_observation() -> None:
    with disposable_database("compat_drain") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=True)
            grant_db_access(admin, dbname, source_only=True)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, SOURCE_HEAD)

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
                mig, manifest=src_m, expected_target_id=target_id
            )
        finally:
            mig.close()

        # 1. Acquire a shared invocation permit (simulating an in-flight booking/sender).
        src_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=src_m.manifest_id,
            generation="source",
            expected_head=SOURCE_HEAD,
            service="api",
            db_name=dbname,
        )
        rt_eng = make_sa_engine("carepulse_runtime", dbname=dbname)
        permit, pconn = acquire_invocation_permit(
            rt_eng,
            config=src_cfg,
            expected_login="carepulse_runtime",
            record_attempt=True,
        )

        # 2. Controller claims owner epoch and starts drain in a background thread.
        owner_conn = connect_role("carepulse_migration", dbname=dbname)
        ctrl = claim_owner_epoch(
            owner_conn,
            target_id=target_id,
            expected_cluster_id=cluster_id,
            expected_db_name=dbname,
        )

        # Competing controller cannot acquire owner lock; gets inspect-only access.
        comp_conn = connect_role("carepulse_migration", dbname=dbname)
        try:
            with pytest.raises(ContentionError):
                claim_owner_epoch(
                    comp_conn,
                    target_id=target_id,
                    expected_cluster_id=cluster_id,
                    expected_db_name=dbname,
                )
            inspected = inspect_as_competitor(comp_conn, target_id=target_id)
            assert inspected.owner_epoch == ctrl.owner_epoch
        finally:
            comp_conn.close()

        drain_done = threading.Event()
        drain_errors: list[BaseException] = []

        def _run_drain() -> None:
            try:
                drain_and_settle(
                    owner_conn,
                    target_id=target_id,
                    owner_epoch=ctrl.owner_epoch,
                    blocking=True,
                )
            except BaseException as exc:
                drain_errors.append(exc)
            finally:
                drain_done.set()

        t = threading.Thread(target=_run_drain, daemon=True)
        t.start()

        # Wait until DRAINING is committed and owner_conn is waiting on the advisory permit.
        obs = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            deadline = time.monotonic() + 10
            saw_waiter = False
            while time.monotonic() < deadline:
                row = obs.execute(
                    """
                    SELECT count(*) AS waiters
                    FROM pg_stat_activity a
                    WHERE a.datname = %s
                      AND a.wait_event_type = 'Lock'
                      AND a.wait_event = 'advisory'
                      AND cardinality(pg_blocking_pids(a.pid)) > 0
                    """,
                    (dbname,),
                ).fetchone()
                if row and int(row["waiters"]) > 0:
                    saw_waiter = True
                    break
                time.sleep(0.1)
            assert saw_waiter, "Expected controller drain to wait on held shared permit"

            # Fresh admission while DRAINING must be denied promptly without deadlock.
            with pytest.raises(CompatAdmissionError):
                acquire_invocation_permit(
                    rt_eng, config=src_cfg, expected_login="carepulse_runtime"
                )

            # Complete the earlier invocation -> drain finishes!
            from carepulse.compat.admission import record_attempt_disposition

            record_attempt_disposition(
                pconn, permit, invocation_class="http", disposition="COMMITTED"
            )
            release_invocation_permit(pconn, permit)
            assert drain_done.wait(timeout=10)
            assert not drain_errors

            # Before sealing, observe_drain_complete(require_source_sealed=True) fails.
            with pytest.raises(DrainError):
                observe_drain_complete(
                    obs,
                    target_id=target_id,
                    expected_epoch=ctrl.owner_epoch,
                    consumers_stopped=True,
                    require_source_sealed=True,
                )

            mark_source_sealed_in_control(
                owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
            )
            proof = observe_drain_complete(
                obs,
                target_id=target_id,
                expected_epoch=ctrl.owner_epoch,
                consumers_stopped=True,
                require_source_sealed=True,
            )
            assert proof.unresolved_work_count == 0
            assert proof.source_sealed is True
        finally:
            obs.close()
            release_exclusive_drain_permit(owner_conn, target_id)
            release_owner_lock(owner_conn, target_id)
            owner_conn.close()
            rt_eng.dispose()
