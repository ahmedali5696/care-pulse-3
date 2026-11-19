"""T052: fenced 0046 retirement, candidate eligibility and lost-reopen recovery."""

import pytest
from carepulse.compat.admission import (
    CompatRuntimeConfig,
    acquire_invocation_permit,
    record_attempt_disposition,
    release_invocation_permit,
)
from sqlalchemy.exc import SQLAlchemyError

from ops.compat.control import read_control_record
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
from tests.integration.compat_recovery_lab import (
    committed_0046_cutover,
    inspected_prelaunch_pair,
    populated_0040_lab,
)

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
