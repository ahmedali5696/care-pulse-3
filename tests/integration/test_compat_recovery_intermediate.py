"""T051: independently recover real intermediate heads after lost acknowledgements."""

from unittest.mock import patch

import pytest
from carepulse.compat.admission import (
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
)

from ops.compat.control import (
    ContentionError,
    assert_current_owner,
    claim_owner_epoch,
)
from ops.compat.drain import drain_and_settle, observe_drain_complete
from ops.compat.launch import mark_source_sealed_in_control
from ops.compat.manifest import CANDIDATE_HEAD, SOURCE_HEAD
from ops.compat.migrate import (
    advance_one_successor,
    enter_migrating_phase,
    observe_committed_successor,
)
from ops.compat.preflight import PUBLISHED_SUCCESSORS, run_source_preflight
from ops.compat.prerequisites import apply_missing_prerequisites
from ops.compat.probes import probe_alembic_head
from ops.compat.recover import ExecutedUS2Checkpoint, recover_owner
from ops.compat.verify import verify_fenced_security, verify_retained_evidence
from tests.integration.compat_ci_helpers import connect_role, read_secret
from tests.integration.compat_recovery_lab import (
    original_sender_engine,
    populated_0040_lab,
    seed_synthetic_0041_identity,
)

CHECKPOINT = ExecutedUS2Checkpoint(
    44424355381, "835a257aa8374c50d615bf7b75676cccb49137e7", 5, 5, True
)


def _login(role: str, dbname: str) -> dict[str, str | int]:
    return {
        "host": "db",
        "port": 5432,
        "dbname": dbname,
        "user": role,
        "password": read_secret(role),
    }


def test_resume_each_0041_to_0046_after_rollback_and_lost_commit_ack() -> None:
    with populated_0040_lab("compat_mid") as lab:
        initial_owner = connect_role("carepulse_migration", dbname=lab.dbname)
        admin = connect_role("carepulse_bootstrap", dbname=lab.dbname)
        observer = connect_role(
            "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
        )
        recovery_context = None
        try:
            preflight = run_source_preflight(
                observer,
                source_manifest=lab.source_manifest,
                target_manifest=lab.target_manifest,
                us1_checkpoint_executed=True,
            )
            owner_epoch = claim_owner_epoch(
                initial_owner,
                target_id=lab.target_id,
                expected_cluster_id=lab.cluster_id,
                expected_db_name=lab.dbname,
            ).owner_epoch
            drain_and_settle(
                initial_owner, target_id=lab.target_id, owner_epoch=owner_epoch
            )
            mark_source_sealed_in_control(
                initial_owner, target_id=lab.target_id, owner_epoch=owner_epoch
            )
            proof = observe_drain_complete(
                observer,
                target_id=lab.target_id,
                expected_epoch=owner_epoch,
                consumers_stopped=True,
                require_source_sealed=True,
            )
            apply_missing_prerequisites(
                admin,
                observer,
                owner_conn=initial_owner,
                preflight=preflight,
                drain_proof=proof,
                owner_epoch=owner_epoch,
                role_passwords={
                    r: read_secret(r)
                    for r in (
                        "carepulse_api",
                        "carepulse_web",
                        "carepulse_worker",
                        "carepulse_gate",
                    )
                },
            )
            enter_migrating_phase(
                initial_owner, target_id=lab.target_id, owner_epoch=owner_epoch
            )

            current = SOURCE_HEAD
            current_owner = initial_owner
            from ops.compat.migrate import command as migration_command

            original_upgrade = migration_command.upgrade
            for i, successor in enumerate(PUBLISHED_SUCCESSORS):

                def lost_before_commit(cfg, rev):
                    original_upgrade(cfg, rev)
                    raise RuntimeError("synthetic failure before committed successor")

                with (
                    patch(
                        "ops.compat.migrate.command.upgrade",
                        side_effect=lost_before_commit,
                    ),
                    pytest.raises(RuntimeError, match="synthetic failure before"),
                ):
                    advance_one_successor(
                        current_owner,
                        target_id=lab.target_id,
                        owner_epoch=owner_epoch,
                        expected_predecessor=current,
                        target_successor=successor,
                    )
                assert probe_alembic_head(observer) == current

                advance_one_successor(
                    current_owner,
                    target_id=lab.target_id,
                    owner_epoch=owner_epoch,
                    expected_predecessor=current,
                    target_successor=successor,
                )
                if successor == PUBLISHED_SUCCESSORS[0]:
                    seed_synthetic_0041_identity(admin)
                if recovery_context is None:
                    current_owner.close()
                else:
                    recovery_context.__exit__(None, None, None)
                    recovery_context = None

                # No client acknowledgement: inspect the actual committed
                # successor with a fresh observer before any new migration.
                following_context = recover_owner(
                    owner_connection=_login("carepulse_migration", lab.dbname),
                    observer_connection=_login("carepulse_bootstrap", lab.dbname),
                    source_manifest=lab.source_manifest,
                    target_manifest=lab.target_manifest,
                    executed_us2=CHECKPOINT,
                )
                takeover = following_context.__enter__()
                recovery_context = following_context
                # The owner vanished after the commit, before any successor
                # readback. Verify the durable head under the recovered epoch.
                observe_committed_successor(
                    observer,
                    target_id=lab.target_id,
                    expected_epoch=takeover.control.owner_epoch,
                    expected_head=successor,
                )
                security = verify_fenced_security(
                    observer,
                    source_manifest=lab.source_manifest,
                    target_manifest=lab.target_manifest,
                    expected_epoch=takeover.control.owner_epoch,
                    expected_head=successor,
                )
                verify_retained_evidence(
                    observer, baseline=lab.retained_baseline(), security=security
                )
                assert takeover.committed_head == successor
                assert takeover.control.owner_epoch == owner_epoch + 1
                assert takeover.control.drain_verified_epoch == owner_epoch + 1
                assert takeover.branch == (
                    "TARGET_VERIFY_ONLY"
                    if successor == CANDIDATE_HEAD
                    else "FORWARD_ONLY"
                )
                assert takeover.next_successor == (
                    None if successor == CANDIDATE_HEAD else PUBLISHED_SUCCESSORS[i + 1]
                )
                stale = connect_role("carepulse_migration", dbname=lab.dbname)
                try:
                    with pytest.raises(ContentionError):
                        assert_current_owner(
                            stale,
                            target_id=lab.target_id,
                            expected_epoch=owner_epoch,
                            lock_mode="NONE",
                        )
                finally:
                    stale.close()
                sender_cfg = CompatRuntimeConfig(
                    target_id=lab.target_id,
                    manifest_id=lab.source_manifest.manifest_id,
                    generation="source",
                    expected_head=SOURCE_HEAD,
                    service="sender",
                    db_name=lab.dbname,
                )
                sender = original_sender_engine(lab.dbname)
                try:
                    with pytest.raises(CompatAdmissionError):
                        acquire_invocation_permit(
                            sender,
                            config=sender_cfg,
                            expected_login="carepulse_sender",
                        )
                finally:
                    sender.dispose()
                current = successor
                current_owner = takeover.owner_conn
                owner_epoch = takeover.control.owner_epoch
        finally:
            if recovery_context is not None:
                recovery_context.__exit__(None, None, None)
            observer.close()
            admin.close()
            initial_owner.close()
