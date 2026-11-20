"""US2 ordered closure, PostgreSQL waiter/blocker locks, and controller contention tests (T040)."""

from __future__ import annotations

import os
import threading
import time
from contextlib import ExitStack

import psycopg
import pytest
from carepulse.compat.admission import (
    CANDIDATE_HEAD,
    SOURCE_HEAD,
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    record_attempt_disposition,
    release_invocation_permit,
)

from apps.worker.status import probe_status, process_identity, write_status
from ops.compat.control import (
    ContentionError,
    advisory_key,
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


def test_matching_epoch_cannot_mutate_without_claimed_session_owner_lock() -> None:
    with disposable_database("compat_owner") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=True)
            grant_db_access(admin, dbname, source_only=True)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, SOURCE_HEAD)
        target_id = f"t-{dbname}"
        src = build_manifest(
            generation="source",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        cand = build_manifest(
            generation="candidate",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        setup = connect_role("carepulse_migration", dbname=dbname)
        try:
            provision_target_control(
                setup,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src.manifest_id,
                target_manifest_id=cand.manifest_id,
            )
        finally:
            setup.close()

        owner = connect_role("carepulse_migration", dbname=dbname)
        competitor = connect_role("carepulse_migration", dbname=dbname)
        try:
            claimed = claim_owner_epoch(
                owner,
                target_id=target_id,
                expected_cluster_id=cluster_id,
                expected_db_name=dbname,
            )

            # Same role, target and current epoch do not grant another backend control.
            with pytest.raises(ContentionError, match="claimed owner"):
                commit_draining_fence(
                    competitor, target_id=target_id, owner_epoch=claimed.owner_epoch
                )
            assert (
                inspect_as_competitor(competitor, target_id=target_id).phase == "CLOSED"
            )

            # An expired claimant cannot substitute a transaction advisory lock.
            assert release_owner_lock(owner, target_id)
            owner.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (advisory_key("owner", target_id),),
            )
            with pytest.raises(ContentionError, match="claimed owner"):
                commit_draining_fence(
                    owner, target_id=target_id, owner_epoch=claimed.owner_epoch
                )
            assert (
                inspect_as_competitor(competitor, target_id=target_id).phase == "CLOSED"
            )
        finally:
            competitor.close()
            owner.rollback()
            owner.close()


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
        with ExitStack() as setup:
            rt_eng = make_sa_engine("carepulse_runtime", dbname=dbname)
            setup.callback(rt_eng.dispose)
            permit, pconn = acquire_invocation_permit(
                rt_eng,
                config=src_cfg,
                expected_login="carepulse_runtime",
                record_attempt=True,
            )
            setup.callback(release_invocation_permit, pconn, permit)
            http_pid = pconn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            sender_cfg = CompatRuntimeConfig(
                target_id=target_id,
                manifest_id=src_m.manifest_id,
                generation="source",
                expected_head=SOURCE_HEAD,
                service="sender",
                db_name=dbname,
            )
            sender_eng = make_sa_engine("carepulse_sender", dbname=dbname)
            setup.callback(sender_eng.dispose)
            sender_permit, sender_conn = acquire_invocation_permit(
                sender_eng,
                config=sender_cfg,
                expected_login="carepulse_sender",
                invocation_class="sender_claim",
                record_attempt=True,
            )
            setup.callback(release_invocation_permit, sender_conn, sender_permit)
            sender_pid = sender_conn.exec_driver_sql(
                "SELECT pg_backend_pid()"
            ).scalar_one()

            # 2. Controller claims owner epoch and starts drain in a background thread.
            owner_conn = connect_role("carepulse_migration", dbname=dbname)
            setup.callback(owner_conn.close)
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
                except (DrainError, RuntimeError) as exc:
                    drain_errors.append(exc)
                finally:
                    drain_done.set()

            t = threading.Thread(target=_run_drain, daemon=True)
            t.start()

            # After observer acquisition the existing outer finally owns cleanup.
            obs = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
            setup.pop_all()

        # Wait until DRAINING is committed and the owner is blocked by BOTH admitted holders.
        try:
            try:
                deadline = time.monotonic() + 10
                saw_waiter = False
                while time.monotonic() < deadline:
                    row = obs.execute(
                        """
                        SELECT pg_blocking_pids(a.pid) AS blockers
                        FROM pg_stat_activity a
                        WHERE a.pid = %s AND a.datname = %s
                          AND a.wait_event_type = 'Lock'
                          AND a.wait_event = 'advisory'
                        """,
                        (owner_conn.info.backend_pid, dbname),
                    ).fetchone()
                    if row and {http_pid, sender_pid}.issubset(set(row["blockers"])):
                        saw_waiter = True
                        break
                    time.sleep(0.1)
                assert saw_waiter, (
                    "Expected controller drain to wait on held shared permit"
                )

                # Fresh admission while DRAINING must be denied promptly without deadlock.
                with pytest.raises(CompatAdmissionError):
                    acquire_invocation_permit(
                        rt_eng, config=src_cfg, expected_login="carepulse_runtime"
                    )

                # Known HTTP commit and known sender NOT_SENT settle before drain.
                record_attempt_disposition(
                    pconn, permit, invocation_class="http", disposition="COMMITTED"
                )
                record_attempt_disposition(
                    sender_conn,
                    sender_permit,
                    invocation_class="sender_claim",
                    disposition="NOT_SENT",
                )
            finally:
                release_invocation_permit(pconn, permit)
                release_invocation_permit(sender_conn, sender_permit)

            assert drain_done.wait(timeout=10)
            t.join(timeout=2)
            assert not drain_errors
            attempts = obs.execute(
                "SELECT attempt_id, service, disposition FROM compat_control.work_evidence "
                "WHERE attempt_id IN (%s, %s)",
                (permit.attempt_id, sender_permit.attempt_id),
            ).fetchall()
            assert {
                row["attempt_id"]: (row["service"], row["disposition"])
                for row in attempts
            } == {
                permit.attempt_id: ("api", "COMMITTED"),
                sender_permit.attempt_id: ("sender", "NOT_SENT"),
            }

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
            # A completed invocation can leave a pooled PostgreSQL backend idle.
            # It remains a valid old login after NOLOGIN and must block migration.
            with pytest.raises(DrainError, match="Surviving product sessions"):
                observe_drain_complete(
                    obs,
                    target_id=target_id,
                    expected_epoch=ctrl.owner_epoch,
                    consumers_stopped=True,
                    require_source_sealed=True,
                )
            rt_eng.dispose()
            sender_eng.dispose()
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
            if not t.is_alive():
                release_exclusive_drain_permit(owner_conn, target_id)
                release_owner_lock(owner_conn, target_id)
            owner_conn.close()
            rt_eng.dispose()
            sender_eng.dispose()


def test_close_wins_before_sender_admission_and_worker_heartbeat_is_not_authority(
    tmp_path,
) -> None:
    with disposable_database("compat_close_first") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=False)
            grant_db_access(admin, dbname, source_only=False)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, CANDIDATE_HEAD)
        target_id = f"t-{dbname}"
        src = build_manifest(
            generation="source",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        cand = build_manifest(
            generation="candidate",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        setup = connect_role("carepulse_migration", dbname=dbname)
        try:
            provision_target_control(
                setup,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src.manifest_id,
                target_manifest_id=cand.manifest_id,
            )
            initial_activate_matched_target(
                setup, manifest=cand, expected_target_id=target_id
            )
        finally:
            setup.close()

        sender_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=cand.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="sender",
            db_name=dbname,
        )
        sender_eng = make_sa_engine("carepulse_sender", dbname=dbname)
        worker_eng = make_sa_engine("carepulse_worker", dbname=dbname)
        owner = connect_role("carepulse_migration", dbname=dbname)
        observer = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        attempt_done = threading.Event()
        denials: list[BaseException] = []
        admitted: list[str] = []

        def _sender_attempt() -> None:
            try:
                permit, conn = acquire_invocation_permit(
                    sender_eng,
                    config=sender_cfg,
                    expected_login="carepulse_sender",
                    invocation_class="sender_claim",
                    record_attempt=True,
                )
                admitted.append(permit.attempt_id)
                release_invocation_permit(conn, permit)
            except CompatAdmissionError as exc:
                denials.append(exc)
            finally:
                attempt_done.set()

        thread = threading.Thread(target=_sender_attempt, daemon=True)
        try:
            claimed = claim_owner_epoch(
                owner,
                target_id=target_id,
                expected_cluster_id=cluster_id,
                expected_db_name=dbname,
            )
            # The owner wins the control-row lock before the sender's admission read.
            owner.execute(
                "SELECT 1 FROM compat_control.target_control WHERE singleton_id FOR UPDATE"
            )
            thread.start()

            deadline = time.monotonic() + 10
            waiter = None
            while time.monotonic() < deadline:
                waiter = observer.execute(
                    """
                    SELECT a.pid, a.wait_event_type, pg_blocking_pids(a.pid) AS blockers
                    FROM pg_stat_activity a
                    WHERE a.datname = %s AND a.usename = 'carepulse_sender'
                      AND %s = ANY(pg_blocking_pids(a.pid))
                    """,
                    (dbname, owner.info.backend_pid),
                ).fetchone()
                if waiter is not None:
                    break
                time.sleep(0.02)
            assert waiter is not None, "Sender never waited on the owner's control row"
            assert waiter["wait_event_type"] == "Lock"
            assert owner.info.backend_pid in waiter["blockers"]
            assert not attempt_done.is_set()

            closed = commit_draining_fence(
                owner, target_id=target_id, owner_epoch=claimed.owner_epoch
            )
            assert closed.phase == "DRAINING" and closed.active_generation == "none"
            assert attempt_done.wait(timeout=10)
            thread.join(timeout=2)
            assert not admitted
            assert len(denials) == 1 and isinstance(denials[0], CompatAdmissionError)
            assert (
                observer.execute(
                    "SELECT count(*) AS n FROM compat_control.work_evidence WHERE target_id = %s",
                    (target_id,),
                ).fetchone()["n"]
                == 0
            )

            # A live idle-worker heartbeat remains distinct from sender admission.
            heartbeat = tmp_path / "idle-worker.json"
            write_status(heartbeat, process_identity(os.getpid()), ready=False)
            assert probe_status(heartbeat, "live", expected_pid=os.getpid())
            assert not probe_status(heartbeat, "ready", expected_pid=os.getpid())
            with pytest.raises(CompatAdmissionError):
                acquire_invocation_permit(
                    worker_eng,
                    config=sender_cfg,
                    expected_login="carepulse_sender",
                    invocation_class="sender_claim",
                )
            worker = connect_role("carepulse_worker", dbname=dbname)
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute(
                        "SELECT compat_control.record_own_attempt("
                        "'forged-sender', %s, %s, 'candidate', 'sender', "
                        "'sender_claim', NULL, NULL, 'PENDING')",
                        (target_id, claimed.owner_epoch),
                    )
            finally:
                worker.close()
            assert (
                observer.execute(
                    "SELECT count(*) AS n FROM compat_control.work_evidence WHERE target_id = %s",
                    (target_id,),
                ).fetchone()["n"]
                == 0
            )
        finally:
            owner.rollback()
            if thread.is_alive():
                thread.join(timeout=10)
            release_owner_lock(owner, target_id)
            observer.close()
            owner.close()
            sender_eng.dispose()
            worker_eng.dispose()
