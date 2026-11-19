"""T052: fenced 0046 retirement, candidate eligibility and lost-reopen recovery."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from carepulse.compat.admission import (
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    check_pool_checkout,
    record_attempt_disposition,
    release_invocation_permit,
)
from sqlalchemy.exc import SQLAlchemyError

from ops.compat.control import read_control_record, release_exclusive_drain_permit
from ops.compat.credentials import retire_legacy_runtime
from ops.compat.launch import LaunchBindingError
from ops.compat.manifest import CANDIDATE_HEAD
from ops.compat.probes import probe_alembic_head
from ops.compat.recover import (
    ExecutedUS2Checkpoint,
    RecoveryError,
    recover_ambiguous_open,
)
from ops.compat.reopen import reopen_fenced_generation
from ops.compat.verify import VerificationError, verify_fenced_security
from tests.integration.compat_ci_helpers import (
    make_sa_engine,
    read_secret,
    read_source_secret,
)
from tests.integration.compat_fixture import compare_retained_inventory
from tests.integration.compat_recovery_lab import (
    committed_0046_cutover,
    inspected_prelaunch_pair,
    populated_0040_lab,
)
from tests.integration.postgres_locks import wait_for_postgres_lock

CHECKPOINT = ExecutedUS2Checkpoint(
    44424355381, "835a257aa8374c50d615bf7b75676cccb49137e7", 5, 5, True
)


def _connection(role: str, dbname: str, password: str | None = None):
    return {
        "host": "db",
        "port": 5432,
        "dbname": dbname,
        "user": role,
        "password": password if password is not None else read_secret(role),
    }


def test_incomplete_0046_key_policy_and_unguarded_build_never_open() -> None:
    with (
        populated_0040_lab("compat_invalid") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        source, candidate, units = inspected_prelaunch_pair(lab)
        with pytest.raises(LaunchBindingError):
            # A broken launch identity is denied before any control mutation.
            reopen_fenced_generation(
                cutover.owner,
                cutover.observer,
                source_manifest=source,
                target_manifest=candidate,
                baseline=lab.retained_baseline(),
                owner_epoch=cutover.owner_epoch,
                generation="candidate",
                observed_units=units[:3],
                login_probes={},
            )
        assert read_control_record(cutover.observer).phase == "MIGRATING"

        # This is a disposable copy. Remove a required key policy to prove
        # that a plausible 0046 head alone cannot release clinical traffic.
        with cutover.admin.transaction():
            cutover.admin.execute("SET LOCAL ROLE carepulse_identity_owner")
            cutover.admin.execute("DELETE FROM f01_private.policy")
        with pytest.raises(VerificationError):
            verify_fenced_security(
                cutover.observer,
                source_manifest=source,
                target_manifest=candidate,
                expected_epoch=cutover.owner_epoch,
                expected_head=CANDIDATE_HEAD,
            )
        assert probe_alembic_head(cutover.observer) == CANDIDATE_HEAD
        assert read_control_record(cutover.observer).active_generation == "none"


def test_candidate_reopens_once_then_lost_ack_atomically_refences() -> None:
    with (
        populated_0040_lab("compat_target") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        source, candidate, units = inspected_prelaunch_pair(lab)
        retired = retire_legacy_runtime(
            cutover.admin,
            cutover.owner,
            cutover.observer,
            source_manifest=source,
            target_manifest=candidate,
            baseline=lab.retained_baseline(),
            owner_epoch=cutover.owner_epoch,
            original_shared_passwords={
                role: read_source_secret(role)
                for role in ("carepulse_auth", "carepulse_sender")
            },
            candidate_shared_passwords={
                role: read_secret(role)
                for role in ("carepulse_auth", "carepulse_sender")
            },
        )
        assert retired.legacy_login_disabled
        assert set(retired.rotated_shared_roles) == {
            "carepulse_auth",
            "carepulse_sender",
        }

        active_roles = (
            "carepulse_api",
            "carepulse_web",
            "carepulse_worker",
            "carepulse_gate",
            "carepulse_auth",
            "carepulse_sender",
            "carepulse_operator",
        )
        opened = reopen_fenced_generation(
            cutover.owner,
            cutover.observer,
            source_manifest=source,
            target_manifest=candidate,
            baseline=lab.retained_baseline(),
            owner_epoch=cutover.owner_epoch,
            generation="candidate",
            observed_units=units,
            login_probes={r: _connection(r, lab.dbname) for r in active_roles},
            predecessor_login_probes={
                "carepulse_runtime": _connection("carepulse_runtime", lab.dbname),
                **{
                    r: _connection(r, lab.dbname, read_source_secret(r))
                    for r in ("carepulse_auth", "carepulse_sender")
                },
            },
        )
        assert opened.phase == "TARGET_OPEN"
        assert release_exclusive_drain_permit(cutover.owner, lab.target_id)
        cfg = CompatRuntimeConfig(
            target_id=lab.target_id,
            manifest_id=candidate.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="api",
            db_name=lab.dbname,
        )
        api = make_sa_engine("carepulse_api", dbname=lab.dbname)
        try:
            permit, conn = acquire_invocation_permit(
                api,
                config=cfg,
                expected_login="carepulse_api",
                record_attempt=True,
            )
            try:
                record_attempt_disposition(
                    conn, permit, invocation_class="http", disposition="ROLLED_BACK"
                )
            finally:
                release_invocation_permit(conn, permit)
        finally:
            api.dispose()
        legacy = make_sa_engine("carepulse_runtime", dbname=lab.dbname)
        try:
            with pytest.raises(SQLAlchemyError), legacy.connect():
                pytest.fail("Retired predecessor role unexpectedly connected")
        finally:
            legacy.dispose()

        # Simulate the owner receiving no reply after its OPEN commit. The
        # new operator must fence in the same transaction as epoch increment.
        cutover.owner.close()
        with recover_ambiguous_open(
            owner_connection=_connection("carepulse_migration", lab.dbname),
            observer_connection=_connection("carepulse_bootstrap", lab.dbname),
            source_manifest=source,
            target_manifest=candidate,
            executed_us2=CHECKPOINT,
            expected_open_epoch=opened.owner_epoch,
            expected_open_generation="candidate",
        ) as fenced:
            assert fenced.control.owner_epoch == opened.owner_epoch + 1
            assert fenced.control.phase == "DRAINING"
            assert fenced.control.active_generation == "none"
            assert fenced.unresolved_work == 0
            assert fenced.surviving_sessions == 0
            assert probe_alembic_head(cutover.observer) == CANDIDATE_HEAD
            with (
                pytest.raises(RecoveryError),
                recover_ambiguous_open(
                    owner_connection=_connection("carepulse_migration", lab.dbname),
                    observer_connection=_connection("carepulse_bootstrap", lab.dbname),
                    source_manifest=source,
                    target_manifest=candidate,
                    executed_us2=CHECKPOINT,
                    expected_open_epoch=opened.owner_epoch,
                    expected_open_generation="candidate",
                ),
            ):
                pytest.fail("Competing owner cannot re-fence this event")
        assert read_control_record(cutover.observer).phase == "DRAINING"
        assert probe_alembic_head(cutover.observer) == CANDIDATE_HEAD


def test_lost_open_refence_orders_held_work_before_new_admission() -> None:
    with (
        populated_0040_lab("compat_target_race") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        source, candidate, units = inspected_prelaunch_pair(lab)
        retire_legacy_runtime(
            cutover.admin,
            cutover.owner,
            cutover.observer,
            source_manifest=source,
            target_manifest=candidate,
            baseline=lab.retained_baseline(),
            owner_epoch=cutover.owner_epoch,
            original_shared_passwords={
                role: read_source_secret(role)
                for role in ("carepulse_auth", "carepulse_sender")
            },
            candidate_shared_passwords={
                role: read_secret(role)
                for role in ("carepulse_auth", "carepulse_sender")
            },
        )
        active_roles = (
            "carepulse_api",
            "carepulse_web",
            "carepulse_worker",
            "carepulse_gate",
            "carepulse_auth",
            "carepulse_sender",
            "carepulse_operator",
        )
        opened = reopen_fenced_generation(
            cutover.owner,
            cutover.observer,
            source_manifest=source,
            target_manifest=candidate,
            baseline=lab.retained_baseline(),
            owner_epoch=cutover.owner_epoch,
            generation="candidate",
            observed_units=units,
            login_probes={r: _connection(r, lab.dbname) for r in active_roles},
            predecessor_login_probes={
                "carepulse_runtime": _connection("carepulse_runtime", lab.dbname),
                **{
                    r: _connection(r, lab.dbname, read_source_secret(r))
                    for r in ("carepulse_auth", "carepulse_sender")
                },
            },
        )
        assert opened.phase == "TARGET_OPEN"
        assert release_exclusive_drain_permit(cutover.owner, lab.target_id)
        cutover.owner.close()  # Lost OPEN acknowledgement also loses owner lock.

        http_cfg = CompatRuntimeConfig(
            target_id=lab.target_id,
            manifest_id=candidate.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="api",
            db_name=lab.dbname,
        )
        sender_cfg = CompatRuntimeConfig(
            target_id=lab.target_id,
            manifest_id=candidate.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="sender",
            db_name=lab.dbname,
        )
        worker_cfg = CompatRuntimeConfig(
            target_id=lab.target_id,
            manifest_id=candidate.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="worker",
            db_name=lab.dbname,
        )
        with ExitStack() as cleanup:
            api = make_sa_engine("carepulse_api", dbname=lab.dbname)
            sender = make_sa_engine("carepulse_sender", dbname=lab.dbname)
            worker = make_sa_engine("carepulse_worker", dbname=lab.dbname)
            observer_engine = make_sa_engine("carepulse_bootstrap", dbname=lab.dbname)
            for engine in (api, sender, worker, observer_engine):
                cleanup.callback(engine.dispose)
            http_permit, http_conn = acquire_invocation_permit(
                api,
                config=http_cfg,
                expected_login="carepulse_api",
                record_attempt=True,
            )
            cleanup.callback(release_invocation_permit, http_conn, http_permit)
            sender_permit, sender_conn = acquire_invocation_permit(
                sender,
                config=sender_cfg,
                expected_login="carepulse_sender",
                invocation_class="sender_claim",
                record_attempt=True,
            )
            cleanup.callback(release_invocation_permit, sender_conn, sender_permit)
            record_attempt_disposition(
                http_conn,
                http_permit,
                invocation_class="http",
                disposition="ROLLED_BACK",
            )
            record_attempt_disposition(
                sender_conn,
                sender_permit,
                invocation_class="sender_claim",
                disposition="UNKNOWN",
            )

            http_pid = http_conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            assert (
                http_conn.exec_driver_sql(
                    "SELECT phase FROM compat_control.read_target_control()"
                ).scalar_one()
                == "TARGET_OPEN"
            )
            # The original HTTP backend holds the row while both original
            # invocation permits remain held, including the uncertain sender.
            fenced_ready = threading.Event()
            release_recovery = threading.Event()
            fenced_states = []

            def _refence():
                with recover_ambiguous_open(
                    owner_connection=_connection("carepulse_migration", lab.dbname),
                    observer_connection=_connection("carepulse_bootstrap", lab.dbname),
                    source_manifest=source,
                    target_manifest=candidate,
                    executed_us2=CHECKPOINT,
                    expected_open_epoch=opened.owner_epoch,
                    expected_open_generation="candidate",
                ) as fenced:
                    fenced_states.append(fenced)
                    fenced_ready.set()
                    release_recovery.wait()

            def _new_admission():
                with pytest.raises(CompatAdmissionError):
                    acquired, connection = acquire_invocation_permit(
                        worker,
                        config=worker_cfg,
                        expected_login="carepulse_worker",
                    )
                    release_invocation_permit(connection, acquired)
                    pytest.fail("New work entered after the re-fence was queued")

            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    try:
                        recovery_future = pool.submit(_refence)
                        # Fresh PG activity snapshots are required while the
                        # owner session is starting and waiting on the row lock.
                        with observer_engine.connect().execution_options(
                            isolation_level="AUTOCOMMIT"
                        ) as lock_observer:
                            deadline = time.monotonic() + 15
                            recovery_pid = None
                            while recovery_pid is None and time.monotonic() < deadline:
                                if recovery_future.done():
                                    # Surface a controller startup/ownership error,
                                    # rather than misreporting it as a lock timeout.
                                    recovery_future.result()
                                recovery_pid = lock_observer.exec_driver_sql(
                                    "SELECT pid FROM pg_stat_activity WHERE datname = %s "
                                    "AND usename = 'carepulse_migration' "
                                    "AND %s = ANY(pg_blocking_pids(pid))",
                                    (lab.dbname, http_pid),
                                ).scalar_one_or_none()
                                if recovery_pid is None:
                                    time.sleep(0.02)
                            assert recovery_pid is not None, (
                                "Recovery owner backend not observed"
                            )
                            wait_for_postgres_lock(
                                lock_observer, recovery_pid, http_pid
                            )
                        assert not fenced_ready.is_set()
                    finally:
                        http_conn.commit()  # Release row lock; retain both permits.

                    assert fenced_ready.wait(timeout=15), "Re-fence did not commit"
                    # Reject a fresh real worker login after the durable fence;
                    # a reader begun before the commit may legitimately see OPEN.
                    _new_admission()
                    fenced = fenced_states[0]
                    assert fenced.control.owner_epoch == opened.owner_epoch + 1
                    assert fenced.control.phase == "DRAINING"
                    assert fenced.control.active_generation == "none"
                    assert fenced.unresolved_work == 1
                    assert fenced.surviving_sessions >= 2
                    with pytest.raises(CompatAdmissionError):
                        check_pool_checkout(
                            http_conn,
                            config=http_cfg,
                            expected_login="carepulse_api",
                            expected_epoch=opened.owner_epoch,
                        )
                    actual = cutover.observer.execute(
                        "SELECT attempt_id, disposition FROM compat_control.work_evidence "
                        "WHERE target_id=%s",
                        (lab.target_id,),
                    ).fetchall()
                    assert {r["attempt_id"]: r["disposition"] for r in actual} == {
                        http_permit.attempt_id: "ROLLED_BACK",
                        sender_permit.attempt_id: "UNKNOWN",
                    }
                    assert probe_alembic_head(cutover.observer) == CANDIDATE_HEAD
                    compare_retained_inventory(cutover.observer, lab.inventory)
                finally:
                    release_recovery.set()
                recovery_future.result(timeout=15)

            assert read_control_record(cutover.observer).phase == "DRAINING"
            with (
                pytest.raises(RecoveryError, match="changed"),
                recover_ambiguous_open(
                    owner_connection=_connection("carepulse_migration", lab.dbname),
                    observer_connection=_connection("carepulse_bootstrap", lab.dbname),
                    source_manifest=source,
                    target_manifest=candidate,
                    executed_us2=CHECKPOINT,
                    expected_open_epoch=opened.owner_epoch,
                    expected_open_generation="candidate",
                ),
            ):
                pytest.fail("Stale epoch unexpectedly reacquired OPEN authority")
