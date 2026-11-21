"""T051: independently recover real intermediate heads after lost acknowledgements."""

import json
from unittest.mock import patch
from urllib.request import urlopen

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
    read_control_record,
)
from ops.compat.drain import drain_and_settle, observe_drain_complete
from ops.compat.launch import mark_source_sealed_in_control
from ops.compat.manifest import CANDIDATE_HEAD, SOURCE_HEAD
from ops.compat.migrate import (
    advance_one_successor,
    enter_migrating_phase,
    observe_committed_successor,
)
from ops.compat.preflight import (
    PUBLISHED_SUCCESSORS,
    US1_EXECUTED_CHECKPOINT,
    run_source_preflight,
)
from ops.compat.prerequisites import apply_missing_prerequisites
from ops.compat.probes import probe_alembic_head
from ops.compat.recover import ExecutedUS2Checkpoint, recover_owner
from ops.compat.verify import verify_fenced_security, verify_retained_evidence
from tests.integration.compat_ci_helpers import connect_role, read_secret
from tests.integration.compat_host_checkpoint import checkpoint_restarted_jobs
from tests.integration.compat_recovery_lab import (
    original_sender_engine,
    populated_0040_lab,
    seed_synthetic_0041_identity,
)

CHECKPOINT = ExecutedUS2Checkpoint(
    44424355381, "835a257aa8374c50d615bf7b75676cccb49137e7", 5, 5, True
)


def _mailpit_count() -> int:
    with urlopen("http://mail:8025/api/v1/messages", timeout=6) as response:
        count = json.load(response)["total"]
    assert type(count) is int and count >= 0
    return count


def _retained_fingerprints(observer, lab) -> tuple[str, ...]:
    """Compare opaque fingerprints; never log original tenant rows or credentials."""
    tenants = (lab.inventory.org_a_id, lab.inventory.org_b_id)
    queries = (
        (
            "compat_control.work_evidence",
            "attempt_id",
            "target_id=%s",
            (lab.target_id,),
        ),
        ("public.delivery_jobs", "id", "organization_id IN (%s,%s)", tenants),
        ("public.outbox_events", "id", "organization_id IN (%s,%s)", tenants),
        ("public.sensitive_audit", "id", "organization_id IN (%s,%s)", tenants),
        ("public.idempotency_receipts", "id", "organization_id IN (%s,%s)", tenants),
        (
            "public.protected_delivery_material",
            "job_id",
            "job_id IN (SELECT id FROM public.delivery_jobs WHERE organization_id IN (%s,%s))",
            tenants,
        ),
    )
    return tuple(
        observer.execute(
            "SELECT md5(COALESCE(string_agg(to_jsonb(t)::text, '|' "
            f"ORDER BY t.{key}), '')) AS digest FROM {table} t WHERE {where}",
            params,
        ).fetchone()["digest"]
        for table, key, where, params in queries
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
                source_baseline=lab.retained_baseline(),
                us1_checkpoint=US1_EXECUTED_CHECKPOINT,
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
                initial_owner,
                observer,
                target_id=lab.target_id,
                owner_epoch=owner_epoch,
                preflight=preflight,
                drain_proof=proof,
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
                        observer,
                        target_id=lab.target_id,
                        owner_epoch=owner_epoch,
                        expected_predecessor=current,
                        target_successor=successor,
                        preflight=preflight,
                    )
                assert probe_alembic_head(observer) == current

                advance_one_successor(
                    current_owner,
                    observer,
                    target_id=lab.target_id,
                    owner_epoch=owner_epoch,
                    expected_predecessor=current,
                    target_successor=successor,
                    preflight=preflight,
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

                # A separate CI host must invoke the *built* source/candidate
                # workers and senders. No Docker socket enters the observer.
                # Keep the recovered exclusive drain permit held during probes.
                control = read_control_record(observer)
                assert (
                    control.target_id,
                    control.cluster_id,
                    control.db_name,
                    control.phase,
                    control.active_generation,
                    control.owner_epoch,
                    control.drain_verified_epoch,
                    control.source_manifest_id,
                    control.target_manifest_id,
                ) == (
                    lab.target_id,
                    lab.cluster_id,
                    lab.dbname,
                    "MIGRATING",
                    "none",
                    takeover.control.owner_epoch,
                    takeover.control.owner_epoch,
                    lab.source_manifest.manifest_id,
                    lab.target_manifest.manifest_id,
                )
                before = _retained_fingerprints(observer, lab)
                mail_before = _mailpit_count()
                result = checkpoint_restarted_jobs(
                    step=i,
                    head=successor,
                    db_name=lab.dbname,
                    target_id=lab.target_id,
                    cluster_id=lab.cluster_id,
                    source_manifest_id=lab.source_manifest.manifest_id,
                    candidate_manifest_id=lab.target_manifest.manifest_id,
                    owner_epoch=takeover.control.owner_epoch,
                    tenant_id=lab.inventory.org_a_id,
                    job_id=lab.inventory.delivery_job_a_id,
                )
                if (
                    result["mail_before"] != mail_before
                    or result["mail_after"] != _mailpit_count()
                ):
                    raise AssertionError("Image-backed sender changed Mailpit state")
                if before != _retained_fingerprints(observer, lab):
                    raise AssertionError(
                        "Image-backed restart changed retained work/evidence"
                    )
                assert read_control_record(observer) == control
                assert probe_alembic_head(observer) == successor
                print(
                    "T051_EVIDENCE"
                    f" head={successor}"
                    f" source_worker={result['workers']['source']['restart_container_id'][:16]}"
                    f" candidate_worker={result['workers']['candidate']['restart_container_id'][:16]}"
                    f" source_sender={result['senders']['source']['container_id'][:16]}"
                    f" candidate_sender={result['senders']['candidate']['container_id'][:16]}"
                    f" mailpit_before={result['mail_before']}"
                    f" mailpit_after={result['mail_after']}"
                    " persisted_effects_unchanged=true"
                )
                current = successor
                current_owner = takeover.owner_conn
                owner_epoch = takeover.control.owner_epoch
        finally:
            if recovery_context is not None:
                recovery_context.__exit__(None, None, None)
            observer.close()
            admin.close()
            initial_owner.close()
