"""US2 populated 0040 -> 0046 six-successor upgrade, T030 fence, and negative preflight (T042)."""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest
from alembic import command as alembic_command
from carepulse.compat.admission import (
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
)
from sqlalchemy.exc import SQLAlchemyError

from ops.compat.control import (
    ContentionError,
    claim_owner_epoch,
    release_exclusive_drain_permit,
    release_owner_lock,
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
    PreflightError,
    run_source_preflight,
)
from ops.compat.prerequisites import PrerequisiteError, apply_missing_prerequisites
from ops.compat.probes import ProbeError, probe_alembic_head, probe_role_attributes
from tests.integration.compat_ci_helpers import (
    build_manifest,
    connect_role,
    disposable_database,
    ensure_roles_provisioned,
    get_cluster_id,
    grant_db_access,
    make_sa_engine,
    read_secret,
    require_compat_ci,
    upgrade_to_revision,
)
from tests.integration.compat_fixture import (
    compare_retained_inventory,
    seed_populated_0040_fixture,
)
from tests.integration.compat_matrix_fixture import (
    initial_activate_matched_target,
    provision_target_control,
)


@pytest.fixture(scope="module", autouse=True)
def _ci_guard() -> None:
    require_compat_ci()


def _assert_product_roles_cannot_edit_fence(observer, dbname: str) -> None:
    """Try protected-control mutations with each actual available product login."""
    roles = (
        "carepulse_runtime",
        "carepulse_api",
        "carepulse_web",
        "carepulse_worker",
        "carepulse_gate",
        "carepulse_auth",
        "carepulse_sender",
        "carepulse_operator",
    )
    snapshots = probe_role_attributes(observer, roles)
    before = observer.execute(
        "SELECT phase, owner_epoch, source_manifest_id, target_manifest_id "
        "FROM compat_control.target_control"
    ).fetchone()
    for role in roles:
        snap = snapshots.get(role)
        if snap is None or not snap.rolcanlogin:
            continue
        try:
            conn = connect_role(role, dbname=dbname)
        except psycopg.OperationalError:
            # A login lacking CONNECT is already fenced at the physical boundary.
            assert not observer.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT') AS allowed",
                (role, dbname),
            ).fetchone()["allowed"]
            continue
        try:
            for statement in (
                "UPDATE compat_control.target_control SET owner_epoch = 999",
                "UPDATE compat_control.target_control SET phase = 'TARGET_OPEN'",
                "UPDATE compat_control.work_evidence SET disposition = 'COMMITTED'",
                "SET ROLE carepulse_migration",
            ):
                with pytest.raises(psycopg.Error):
                    conn.execute(statement)
                conn.rollback()
        finally:
            conn.close()
    after = observer.execute(
        "SELECT phase, owner_epoch, source_manifest_id, target_manifest_id "
        "FROM compat_control.target_control"
    ).fetchone()
    assert after == before


def _assert_both_generations_fenced(dbname, target_id, src_manifest, target_manifest):
    for generation, login, manifest, expected_head in (
        ("source", "carepulse_runtime", src_manifest, SOURCE_HEAD),
        ("candidate", "carepulse_api", target_manifest, CANDIDATE_HEAD),
    ):
        cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=manifest.manifest_id,
            generation=generation,
            expected_head=expected_head,
            service="api",
            db_name=dbname,
        )
        engine = make_sa_engine(login, dbname=dbname)
        try:
            with pytest.raises((CompatAdmissionError, SQLAlchemyError)):
                acquire_invocation_permit(engine, config=cfg, expected_login=login)
        finally:
            engine.dispose()


def test_populated_0040_to_0046_cutover_and_t030_fencing() -> None:
    with disposable_database("compat_upg") as dbname:
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

        # Seed populated 0040 fixture as bootstrap/owner.
        seed_conn = connect_role("carepulse_bootstrap", dbname=dbname)
        try:
            inventory = seed_populated_0040_fixture(seed_conn)
        finally:
            seed_conn.close()

        owner_conn = connect_role("carepulse_migration", dbname=dbname)
        admin_owner = connect_role("carepulse_bootstrap", dbname=dbname)
        observer = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            # 1. Read-only preflight passes at 0040; negative preflight with wrong target fails cleanly.
            preflight = run_source_preflight(
                observer,
                source_manifest=src_m,
                target_manifest=cand_m,
                us1_checkpoint_executed=True,
            )
            with pytest.raises(PreflightError):
                run_source_preflight(
                    observer,
                    source_manifest=src_m,
                    target_manifest=cand_m,
                    us1_checkpoint_executed=False,
                )

            # 2. Claim owner epoch, close/drain, seal source, and obtain T037 proof.
            ctrl = claim_owner_epoch(
                owner_conn,
                target_id=target_id,
                expected_cluster_id=cluster_id,
                expected_db_name=dbname,
            )
            drain_and_settle(
                owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
            )
            mark_source_sealed_in_control(
                owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
            )
            drain_proof = observe_drain_complete(
                observer,
                target_id=target_id,
                expected_epoch=ctrl.owner_epoch,
                consumers_stopped=True,
                require_source_sealed=True,
            )
            _assert_both_generations_fenced(dbname, target_id, src_m, cand_m)
            _assert_product_roles_cannot_edit_fence(observer, dbname)

            # 3. T030 refuses stale epoch without mutating roles/schemas.
            with pytest.raises((PrerequisiteError, ContentionError)):
                apply_missing_prerequisites(
                    admin_owner,
                    observer,
                    owner_conn=owner_conn,
                    preflight=preflight,
                    drain_proof=drain_proof,
                    owner_epoch=ctrl.owner_epoch - 1,
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

            # 4. T030 succeeds under verified drain + current owner epoch.
            delta = apply_missing_prerequisites(
                admin_owner,
                observer,
                owner_conn=owner_conn,
                preflight=preflight,
                drain_proof=drain_proof,
                owner_epoch=ctrl.owner_epoch,
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
            assert "carepulse_identity_owner" in delta.verified_roles

            # 5. Enter MIGRATING and advance one published successor per commit + fresh observer readback.
            enter_migrating_phase(
                owner_conn, target_id=target_id, owner_epoch=ctrl.owner_epoch
            )

            # The first real successor must roll back if control is lost between
            # applying its DDL and committing the owner-held migration unit.
            original_upgrade = alembic_command.upgrade

            def lose_ack_before_commit(config, revision):
                original_upgrade(config, revision)
                raise RuntimeError("injected loss before migration commit")

            with (
                patch(
                    "ops.compat.migrate.command.upgrade",
                    side_effect=lose_ack_before_commit,
                ),
                pytest.raises(
                    RuntimeError, match="injected loss before migration commit"
                ),
            ):
                advance_one_successor(
                    owner_conn,
                    target_id=target_id,
                    owner_epoch=ctrl.owner_epoch,
                    expected_predecessor=SOURCE_HEAD,
                    target_successor=PUBLISHED_SUCCESSORS[0],
                )
            assert (
                probe_alembic_head(observer, expected_head=SOURCE_HEAD) == SOURCE_HEAD
            )
            compare_retained_inventory(observer, inventory)

            current_rev = SOURCE_HEAD
            for successor in PUBLISHED_SUCCESSORS:
                committed = advance_one_successor(
                    owner_conn,
                    target_id=target_id,
                    owner_epoch=ctrl.owner_epoch,
                    expected_predecessor=current_rev,
                    target_successor=successor,
                )
                assert committed == successor
                obs = observe_committed_successor(
                    observer,
                    target_id=target_id,
                    expected_epoch=ctrl.owner_epoch,
                    expected_head=successor,
                )
                assert obs.committed_head == successor
                compare_retained_inventory(observer, inventory)
                _assert_both_generations_fenced(dbname, target_id, src_m, cand_m)
                _assert_product_roles_cannot_edit_fence(observer, dbname)
                current_rev = successor

            assert current_rev == CANDIDATE_HEAD
            # A replay of an already committed unit must not apply it again.
            with pytest.raises(ProbeError):
                advance_one_successor(
                    owner_conn,
                    target_id=target_id,
                    owner_epoch=ctrl.owner_epoch,
                    expected_predecessor=PUBLISHED_SUCCESSORS[-2],
                    target_successor=CANDIDATE_HEAD,
                )
            owner_conn.rollback()
            assert (
                probe_alembic_head(observer, expected_head=CANDIDATE_HEAD)
                == CANDIDATE_HEAD
            )
            compare_retained_inventory(observer, inventory)
        finally:
            observer.close()
            admin_owner.close()
            release_exclusive_drain_permit(owner_conn, target_id)
            release_owner_lock(owner_conn, target_id)
            owner_conn.close()
