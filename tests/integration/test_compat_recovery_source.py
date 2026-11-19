"""T050: real 0040-origin recovery before any successor commit."""

import pytest
from carepulse.compat.admission import (
    CompatRuntimeConfig,
    acquire_invocation_permit,
    record_attempt_disposition,
    release_invocation_permit,
)

from ops.compat.control import claim_owner_epoch, read_control_record
from ops.compat.drain import commit_draining_fence, drain_and_settle
from ops.compat.launch import mark_source_sealed_in_control
from ops.compat.manifest import SOURCE_HEAD
from ops.compat.recover import (
    ExecutedUS2Checkpoint,
    RecoveryError,
    recover_owner,
)
from ops.compat.verify import verify_fenced_security, verify_retained_evidence
from tests.integration.compat_ci_helpers import (
    connect_role,
    make_sa_engine,
    read_secret,
)
from tests.integration.compat_recovery_lab import populated_0040_lab

CHECKPOINT = ExecutedUS2Checkpoint(
    44424355381, "835a257aa8374c50d615bf7b75676cccb49137e7", 5, 5, True
)


def _connections(dbname: str):
    return (
        {
            "host": "db",
            "port": 5432,
            "dbname": dbname,
            "user": "carepulse_migration",
            "password": read_secret("carepulse_migration"),
        },
        {
            "host": "db",
            "port": 5432,
            "dbname": dbname,
            "user": "carepulse_bootstrap",
            "password": read_secret("carepulse_bootstrap"),
        },
    )


def test_recover_0040_after_controller_loss_preserves_source_authority() -> None:
    with populated_0040_lab("compat_src") as lab:
        prior = connect_role("carepulse_migration", dbname=lab.dbname)
        try:
            epoch = claim_owner_epoch(
                prior,
                target_id=lab.target_id,
                expected_cluster_id=lab.cluster_id,
                expected_db_name=lab.dbname,
            ).owner_epoch
            drain_and_settle(prior, target_id=lab.target_id, owner_epoch=epoch)
            mark_source_sealed_in_control(
                prior, target_id=lab.target_id, owner_epoch=epoch
            )
        finally:
            # A crashed controller loses both session advisory locks.
            prior.close()

        owner_opts, observer_opts = _connections(lab.dbname)
        with recover_owner(
            owner_connection=owner_opts,
            observer_connection=observer_opts,
            source_manifest=lab.source_manifest,
            target_manifest=lab.target_manifest,
            executed_us2=CHECKPOINT,
        ) as recovered:
            assert recovered.committed_head == SOURCE_HEAD
            assert recovered.branch == "SOURCE_REVERIFY"
            assert recovered.next_successor is None
            assert recovered.control.owner_epoch == epoch + 1
            assert recovered.control.drain_verified_epoch == epoch + 1
            observer = connect_role(
                "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
            )
            try:
                security = verify_fenced_security(
                    observer,
                    source_manifest=lab.source_manifest,
                    target_manifest=lab.target_manifest,
                    expected_epoch=recovered.control.owner_epoch,
                    expected_head=SOURCE_HEAD,
                )
                retained = verify_retained_evidence(
                    observer, baseline=lab.retained_baseline(), security=security
                )
                assert retained.checked_rows >= 20
                assert read_control_record(observer).phase == "DRAINING"
                assert (
                    observer.execute(
                        "SELECT rolcanlogin FROM pg_roles WHERE rolname='carepulse_runtime'"
                    ).fetchone()["rolcanlogin"]
                    is True
                )
            finally:
                observer.close()


def test_unknown_original_http_outcome_blocks_any_recovery_epoch() -> None:
    with populated_0040_lab("compat_unknown") as lab:
        cfg = CompatRuntimeConfig(
            target_id=lab.target_id,
            manifest_id=lab.source_manifest.manifest_id,
            generation="source",
            expected_head=SOURCE_HEAD,
            service="api",
            db_name=lab.dbname,
        )
        engine = make_sa_engine("carepulse_runtime", dbname=lab.dbname)
        try:
            permit, permit_conn = acquire_invocation_permit(
                engine,
                config=cfg,
                expected_login="carepulse_runtime",
                record_attempt=True,
            )
            try:
                record_attempt_disposition(
                    permit_conn,
                    permit,
                    invocation_class="http",
                    disposition="UNKNOWN",
                )
            finally:
                release_invocation_permit(permit_conn, permit)
        finally:
            engine.dispose()

        previous = connect_role("carepulse_migration", dbname=lab.dbname)
        try:
            epoch = claim_owner_epoch(
                previous,
                target_id=lab.target_id,
                expected_cluster_id=lab.cluster_id,
                expected_db_name=lab.dbname,
            ).owner_epoch
            commit_draining_fence(previous, target_id=lab.target_id, owner_epoch=epoch)
        finally:
            previous.close()

        owner_opts, observer_opts = _connections(lab.dbname)
        with (
            pytest.raises(RecoveryError, match="pending/unknown"),
            recover_owner(
                owner_connection=owner_opts,
                observer_connection=observer_opts,
                source_manifest=lab.source_manifest,
                target_manifest=lab.target_manifest,
                executed_us2=CHECKPOINT,
            ),
        ):
            pytest.fail("Uncertain original work cannot authorize takeover")
        observer = connect_role(
            "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
        )
        try:
            state = read_control_record(observer)
            assert state.phase == "DRAINING"
            assert state.owner_epoch == epoch
            assert (
                observer.execute(
                    "SELECT count(*) AS n FROM compat_control.work_evidence WHERE target_id=%s AND disposition='UNKNOWN'",
                    (lab.target_id,),
                ).fetchone()["n"]
                == 1
            )
        finally:
            observer.close()
