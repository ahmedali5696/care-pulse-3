"""US1 compatibility matrix integration tests using T011 fixtures (T026)."""

from __future__ import annotations

import pytest
from carepulse.compat.admission import (
    CANDIDATE_HEAD,
    SOURCE_HEAD,
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    probe_compat_readiness,
    release_invocation_permit,
)
from sqlalchemy.exc import SQLAlchemyError

from ops.compat.manifest import SOURCE_HEAD as MANIFEST_SOURCE_HEAD
from ops.compat.probes import ProbeError, probe_alembic_head
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
from tests.integration.compat_matrix_fixture import (
    MatrixFixtureError,
    apply_fixture_only_0041_delta,
    initial_activate_matched_target,
    provision_target_control,
    snapshot_0040_baseline,
    step_matrix_head,
)

INTERMEDIATE_REVISIONS = (
    "0041_f01_runtime_identity",
    "0042_f01_effective_grants",
    "0043_f01_writer_boundary",
    "0044_f01_delivery_acl",
    "0045_f01_revocation_evidence",
)


@pytest.fixture(scope="module", autouse=True)
def _ci_guard() -> None:
    require_compat_ci()


def test_matched_0040_source_activation_and_candidate_denial(monkeypatch) -> None:
    with disposable_database("compat_m0040") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=True)
            grant_db_access(admin, dbname, source_only=True)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, SOURCE_HEAD)

        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            target_id = f"t-{dbname}"
            src_manifest = build_manifest(
                generation="source",
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
            )
            cand_manifest = build_manifest(
                generation="candidate",
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
            )
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_manifest.manifest_id,
                target_manifest_id=cand_manifest.manifest_id,
            )
            snap = snapshot_0040_baseline(mig, target_id=target_id)
            assert snap.head == MANIFEST_SOURCE_HEAD
            assert snap.phase == "CLOSED"

            epoch = initial_activate_matched_target(
                mig, manifest=src_manifest, expected_target_id=target_id
            )
            assert epoch == 1

            # Second initial activation must fail closed.
            with pytest.raises(MatrixFixtureError):
                initial_activate_matched_target(
                    mig, manifest=src_manifest, expected_target_id=target_id
                )
        finally:
            mig.close()

        # Source runtime login is admitted at 0040 / SOURCE_OPEN.
        src_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=src_manifest.manifest_id,
            generation="source",
            expected_head=SOURCE_HEAD,
            service="api",
            db_name=dbname,
        )
        rt_engine = make_sa_engine("carepulse_runtime", dbname=dbname)
        try:
            permit, pconn = acquire_invocation_permit(
                rt_engine,
                config=src_cfg,
                expected_login="carepulse_runtime",
                record_attempt=True,
            )
            assert permit.owner_epoch == 1
            release_invocation_permit(pconn, permit)
            with monkeypatch.context() as env:
                for name, value in {
                    "CP_COMPAT_TARGET_ID": target_id,
                    "CP_COMPAT_MANIFEST_ID": src_manifest.manifest_id,
                    "CP_COMPAT_GENERATION": "source",
                    "CP_COMPAT_EXPECTED_HEAD": SOURCE_HEAD,
                    "CP_DB_NAME": dbname,
                }.items():
                    env.setenv(name, value)
                assert probe_compat_readiness(rt_engine, "api") is True
        finally:
            rt_engine.dispose()

        # Candidate API is denied in SOURCE_OPEN / 0040.
        cand_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=cand_manifest.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="api",
            db_name=dbname,
        )
        api_engine = make_sa_engine("carepulse_api", dbname=dbname)
        try:
            with pytest.raises((CompatAdmissionError, SQLAlchemyError)):
                acquire_invocation_permit(
                    api_engine,
                    config=cand_cfg,
                    expected_login="carepulse_api",
                )
            assert (
                probe_compat_readiness(api_engine, "api") is True
            )  # env unset -> noop
        finally:
            api_engine.dispose()


def test_matched_0046_candidate_activation_and_source_denial() -> None:
    with disposable_database("compat_m0046") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=False)
            grant_db_access(admin, dbname, source_only=False)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, CANDIDATE_HEAD)

        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            target_id = f"t-{dbname}"
            src_manifest = build_manifest(
                generation="source",
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
            )
            cand_manifest = build_manifest(
                generation="candidate",
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
            )
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_manifest.manifest_id,
                target_manifest_id=cand_manifest.manifest_id,
            )
            epoch = initial_activate_matched_target(
                mig, manifest=cand_manifest, expected_target_id=target_id
            )
            assert epoch == 1
        finally:
            mig.close()

        # Candidate API, gate, worker, and sender succeed in TARGET_OPEN / 0046.
        for svc, role in (
            ("api", "carepulse_api"),
            ("worker", "carepulse_worker"),
            ("sender", "carepulse_sender"),
        ):
            cfg = CompatRuntimeConfig(
                target_id=target_id,
                manifest_id=cand_manifest.manifest_id,
                generation="candidate",
                expected_head=CANDIDATE_HEAD,
                service=svc,  # type: ignore[arg-type]
                db_name=dbname,
            )
            eng = make_sa_engine(role, dbname=dbname)
            try:
                permit, pconn = acquire_invocation_permit(
                    eng,
                    config=cfg,
                    expected_login=role,
                    record_attempt=(svc != "worker"),
                )
                assert permit.owner_epoch == 1
                release_invocation_permit(pconn, permit)
            finally:
                eng.dispose()

        # Source runtime is denied at 0046 / TARGET_OPEN.
        src_cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=src_manifest.manifest_id,
            generation="source",
            expected_head=SOURCE_HEAD,
            service="api",
            db_name=dbname,
        )
        rt_eng = make_sa_engine("carepulse_runtime", dbname=dbname)
        try:
            with pytest.raises((CompatAdmissionError, SQLAlchemyError)):
                acquire_invocation_permit(
                    rt_eng, config=src_cfg, expected_login="carepulse_runtime"
                )
        finally:
            rt_eng.dispose()


@pytest.mark.parametrize("intermediate_rev", INTERMEDIATE_REVISIONS)
def test_intermediate_heads_remain_fenced_for_both_generations(
    intermediate_rev: str,
) -> None:
    with disposable_database("compat_mid") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=True)
            grant_db_access(admin, dbname, source_only=True)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, SOURCE_HEAD)

        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            target_id = f"t-{dbname}"
            src_manifest = build_manifest(
                generation="source",
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
            )
            cand_manifest = build_manifest(
                generation="candidate",
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
            )
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_manifest.manifest_id,
                target_manifest_id=cand_manifest.manifest_id,
            )
            snap = snapshot_0040_baseline(mig, target_id=target_id)
            assert snap.head == MANIFEST_SOURCE_HEAD
            conn_priv = mig.execute(
                "SELECT has_database_privilege('carepulse_api', %s, 'CONNECT') AS ok "
                "WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'carepulse_api')",
                (dbname,),
            ).fetchone()
            assert conn_priv is None or not conn_priv["ok"]
        finally:
            mig.close()

        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            apply_fixture_only_0041_delta(
                admin,
                api_password=read_secret("carepulse_api"),
                web_password=read_secret("carepulse_web"),
                worker_password=read_secret("carepulse_worker"),
                gate_password=read_secret("carepulse_gate"),
            )
        finally:
            admin.close()

        step_matrix_head(
            host="db",
            port=5432,
            dbname=dbname,
            migration_password=read_secret("carepulse_migration"),
            target_revision=intermediate_rev,
        )

        mig2 = connect_role("carepulse_migration", dbname=dbname)
        try:
            assert probe_alembic_head(mig2) == intermediate_rev
            with pytest.raises((MatrixFixtureError, ProbeError)):
                initial_activate_matched_target(
                    mig2, manifest=cand_manifest, expected_target_id=target_id
                )
        finally:
            mig2.close()

        # Both source and candidate are denied at every intermediate head.
        for gen, role, m_id, head in (
            ("source", "carepulse_runtime", src_manifest.manifest_id, SOURCE_HEAD),
            ("candidate", "carepulse_api", cand_manifest.manifest_id, CANDIDATE_HEAD),
        ):
            cfg = CompatRuntimeConfig(
                target_id=target_id,
                manifest_id=m_id,
                generation=gen,  # type: ignore[arg-type]
                expected_head=head,
                service="api",
                db_name=dbname,
            )
            eng = make_sa_engine(role, dbname=dbname)
            try:
                with pytest.raises((CompatAdmissionError, SQLAlchemyError)):
                    acquire_invocation_permit(eng, config=cfg, expected_login=role)
            finally:
                eng.dispose()
