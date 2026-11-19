"""T050: real 0040-origin recovery before any successor commit."""

import json
from pathlib import Path

import psycopg
import pytest
from carepulse.compat.admission import (
    CompatRuntimeConfig,
    acquire_invocation_permit,
    record_attempt_disposition,
    release_invocation_permit,
)

from ops.compat.control import (
    claim_owner_epoch,
    read_control_record,
    release_exclusive_drain_permit,
)
from ops.compat.drain import commit_draining_fence, drain_and_settle
from ops.compat.launch import ObservedRuntimeUnit, mark_source_sealed_in_control
from ops.compat.manifest import SOURCE_HEAD
from ops.compat.recover import (
    ExecutedUS2Checkpoint,
    RecoveryError,
    recover_owner,
)
from ops.compat.reopen import reopen_fenced_generation
from ops.compat.verify import verify_fenced_security, verify_retained_evidence
from tests.integration.compat_ci_helpers import (
    connect_role,
    make_sa_engine,
    read_secret,
    read_source_secret,
)
from tests.integration.compat_recovery_lab import (
    inspected_prelaunch_pair,
    populated_0040_lab,
)

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


def _inspected_source_units(source_manifest):
    observed = json.loads(
        Path("/tmp/compat-observed.json").read_text(encoding="utf-8")
    )["services"]
    adapters = {
        "api": "source-api.py",
        "web": "source-web.cjs",
        "worker": "source-jobs.py",
        "sender": "source-jobs.py",
    }
    db_secrets = {
        "source_runtime_db_password": "carepulse_runtime",
        "source_auth_db_password": "carepulse_auth",
        "source_sender_db_password": "carepulse_sender",
    }
    adapter_dir = Path(__file__).resolve().parents[2] / "ops" / "compat"
    units = []
    for service, expected in source_manifest.participants.items():
        record = observed[f"source-{service}"]
        mounted = frozenset(db_secrets[s] for s in record["secrets"] if s in db_secrets)
        assert mounted == expected.allowed_credentials
        peer = (
            (record["api_peer"],)
            if service == "web"
            else ((f"{record['smtp_peer']}:1025",) if service == "sender" else ())
        )
        adapter = adapter_dir / adapters[service]
        units.append(
            ObservedRuntimeUnit(
                service=service,
                generation="source",
                image_digest=record["image_id"],
                entry_command=tuple(record["entry"]),
                adapter_file=adapter,
                adapter_mount_readonly=bool(record["adapter_readonly"]),
                import_reachable=adapter.is_file(),
                configured_peers=peer,
                mounted_credentials=mounted,
                target_id=source_manifest.target_binding.target_id,
                smtp_egress_allowed=bool(record["smtp_peer"]),
            )
        )
    return tuple(units)


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

                source, candidate, _ = inspected_prelaunch_pair(lab)
                units = _inspected_source_units(source)
                logins = {
                    role: {
                        "host": "db",
                        "port": 5432,
                        "dbname": lab.dbname,
                        "user": role,
                        "password": (
                            read_source_secret(role)
                            if role in ("carepulse_auth", "carepulse_sender")
                            else read_secret(role)
                        ),
                    }
                    for role in (
                        "carepulse_runtime",
                        "carepulse_auth",
                        "carepulse_sender",
                        "carepulse_operator",
                    )
                }
                wrong_password = read_secret("carepulse_auth")
                assert wrong_password != logins["carepulse_auth"]["password"]
                with pytest.raises(psycopg.OperationalError) as denied:
                    reopen_fenced_generation(
                        recovered.owner_conn,
                        observer,
                        source_manifest=source,
                        target_manifest=candidate,
                        baseline=lab.retained_baseline(),
                        owner_epoch=recovered.control.owner_epoch,
                        generation="source",
                        observed_units=units,
                        login_probes={
                            **logins,
                            "carepulse_auth": {
                                **logins["carepulse_auth"],
                                "password": wrong_password,
                            },
                        },
                    )
                # libpq can omit SQLSTATE on startup authentication failure.
                assert denied.value.sqlstate == "28P01" or (
                    "FATAL:" in str(denied.value)
                    and 'password authentication failed for user "carepulse_auth"'
                    in str(denied.value)
                )
                assert read_control_record(observer).phase == "DRAINING"

                reopened = reopen_fenced_generation(
                    recovered.owner_conn,
                    observer,
                    source_manifest=source,
                    target_manifest=candidate,
                    baseline=lab.retained_baseline(),
                    owner_epoch=recovered.control.owner_epoch,
                    generation="source",
                    observed_units=units,
                    login_probes=logins,
                )
                assert reopened.phase == "SOURCE_OPEN"
                state = read_control_record(observer)
                assert state.active_generation == "source"
                assert state.source_sealed is False
                assert release_exclusive_drain_permit(
                    recovered.owner_conn, lab.target_id
                )
                engine = make_sa_engine("carepulse_runtime", dbname=lab.dbname)
                try:
                    cfg = CompatRuntimeConfig(
                        target_id=lab.target_id,
                        manifest_id=source.manifest_id,
                        generation="source",
                        expected_head=SOURCE_HEAD,
                        service="api",
                        db_name=lab.dbname,
                    )
                    permit, connection = acquire_invocation_permit(
                        engine,
                        config=cfg,
                        expected_login="carepulse_runtime",
                        record_attempt=True,
                    )
                    try:
                        record_attempt_disposition(
                            connection,
                            permit,
                            invocation_class="http",
                            disposition="ROLLED_BACK",
                        )
                    finally:
                        release_invocation_permit(connection, permit)
                finally:
                    engine.dispose()
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
