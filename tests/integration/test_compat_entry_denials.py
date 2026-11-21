"""US1 entry denials, control-mutation negative proofs, and Compose isolation (T027)."""

from __future__ import annotations

import json
import os

import psycopg
import pytest
from carepulse.compat.admission import (
    CANDIDATE_HEAD,
    SOURCE_HEAD,
    CompatAdmissionError,
    CompatRuntimeConfig,
    acquire_invocation_permit,
    check_pool_checkout,
    probe_compat_readiness,
    release_invocation_permit,
)

from ops.compat.launch import verify_compose_isolation
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


def test_compose_compat_topology_has_no_bypass_paths() -> None:
    compose_path = os.environ["CP_COMPAT_COMPOSE_CONFIG_PATH"]
    with open(compose_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    verify_compose_isolation(doc)


@pytest.mark.parametrize(
    "head,roles_to_test",
    [
        (
            SOURCE_HEAD,
            (
                "carepulse_runtime",
                "carepulse_auth",
                "carepulse_sender",
                "carepulse_operator",
            ),
        ),
        (
            CANDIDATE_HEAD,
            (
                "carepulse_api",
                "carepulse_gate",
                "carepulse_web",
                "carepulse_auth",
                "carepulse_worker",
                "carepulse_sender",
                "carepulse_operator",
            ),
        ),
    ],
)
def test_real_logins_cannot_mutate_control_or_other_owner_work(
    head: str, roles_to_test: tuple[str, ...]
) -> None:
    with disposable_database("compat_deny") as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(admin, source_only=False)
            grant_db_access(admin, dbname, source_only=(head == SOURCE_HEAD))
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()

        upgrade_to_revision(dbname, head)

        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            target_id = f"t-{dbname}"
            gen = "source" if head == SOURCE_HEAD else "candidate"
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
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_m.manifest_id,
                target_manifest_id=cand_m.manifest_id,
            )
            initial_activate_matched_target(
                mig,
                manifest=src_m if gen == "source" else cand_m,
                expected_target_id=target_id,
            )
            # Seed another-owner work_evidence row as migration owner.
            mig.execute(
                """
                INSERT INTO compat_control.work_evidence (
                    attempt_id, target_id, owner_epoch, generation, service,
                    invocation_class, disposition, created_by_role
                ) VALUES (
                    'other-owner-attempt', %s, 1, %s, 'api', 'http', 'PENDING', 'carepulse_migration'
                )
                """,
                (target_id, gen),
            )
            mig.commit()
        finally:
            mig.close()

        for role in roles_to_test:
            conn = connect_role(role, dbname=dbname)
            try:
                for stmt in (
                    "UPDATE compat_control.target_control SET phase = 'CLOSED'",
                    "UPDATE compat_control.target_control SET owner_epoch = 999",
                    "DELETE FROM compat_control.target_control",
                    "INSERT INTO compat_control.work_evidence (attempt_id, target_id, owner_epoch, generation, service, invocation_class, disposition, created_by_role) VALUES ('forged', 'x', 1, 'candidate', 'api', 'http', 'PENDING', 'x')",
                    "UPDATE compat_control.work_evidence SET disposition = 'COMMITTED' WHERE attempt_id = 'other-owner-attempt'",
                    "TRUNCATE compat_control.work_evidence",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute(stmt)
                    conn.rollback()

                # Attempt to overwrite another role's attempt via record_own_attempt
                if role not in (
                    "carepulse_worker",
                    "carepulse_gate",
                    "carepulse_operator",
                ):
                    with pytest.raises(psycopg.Error):
                        conn.execute(
                            """
                            SELECT compat_control.record_own_attempt(
                                'other-owner-attempt', %s, 1, %s, 'api', 'http', NULL, NULL, 'COMMITTED'
                            )
                            """,
                            (target_id, gen),
                        )
                    conn.rollback()
            finally:
                conn.close()


def test_denies_missing_multiple_unknown_head_and_stale_pool(monkeypatch) -> None:
    with disposable_database("compat_meta") as dbname:
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
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=src_m.manifest_id,
                target_manifest_id=cand_m.manifest_id,
            )
            initial_activate_matched_target(
                mig, manifest=cand_m, expected_target_id=target_id
            )
        finally:
            mig.close()

        cfg = CompatRuntimeConfig(
            target_id=target_id,
            manifest_id=cand_m.manifest_id,
            generation="candidate",
            expected_head=CANDIDATE_HEAD,
            service="api",
            db_name=dbname,
        )
        api_engine = make_sa_engine("carepulse_api", dbname=dbname)
        try:
            # 1. Valid permit works, then phase closes -> reused pool checkout denies!
            permit, pconn = acquire_invocation_permit(
                api_engine, config=cfg, expected_login="carepulse_api"
            )
            release_invocation_permit(pconn, permit)

            for name, value in {
                "CP_COMPAT_TARGET_ID": target_id,
                "CP_COMPAT_MANIFEST_ID": cand_m.manifest_id,
                "CP_COMPAT_GENERATION": "candidate",
                "CP_COMPAT_EXPECTED_HEAD": CANDIDATE_HEAD,
                "CP_DB_NAME": dbname,
            }.items():
                monkeypatch.setenv(name, value)
            assert probe_compat_readiness(api_engine, "api") is True

            # Effective grants drift while an earlier pooled login survives.
            with api_engine.connect() as surviving:
                mig = connect_role("carepulse_migration", dbname=dbname)
                try:
                    mig.execute(
                        "GRANT UPDATE ON compat_control.target_control TO carepulse_api"
                    )
                    mig.commit()
                    with pytest.raises(CompatAdmissionError, match="privilege"):
                        check_pool_checkout(
                            surviving, config=cfg, expected_login="carepulse_api"
                        )
                    assert probe_compat_readiness(api_engine, "api") is False
                finally:
                    mig.execute(
                        "REVOKE UPDATE ON compat_control.target_control FROM carepulse_api"
                    )
                    mig.commit()
                    mig.close()
                assert (
                    check_pool_checkout(
                        surviving, config=cfg, expected_login="carepulse_api"
                    )
                    == 1
                )
            assert probe_compat_readiness(api_engine, "api") is True

            mig = connect_role("carepulse_migration", dbname=dbname)
            try:
                mig.execute("UPDATE compat_control.target_control SET phase = 'CLOSED'")
                mig.commit()
            finally:
                mig.close()

            with (
                api_engine.connect() as reused_conn,
                pytest.raises(CompatAdmissionError),
            ):
                check_pool_checkout(
                    reused_conn, config=cfg, expected_login="carepulse_api"
                )
            assert probe_compat_readiness(api_engine, "api") is False

            # 2. Wrong service login (carepulse_gate pretending to be carepulse_api) denies
            gate_engine = make_sa_engine("carepulse_gate", dbname=dbname)
            try:
                with pytest.raises(CompatAdmissionError):
                    acquire_invocation_permit(
                        gate_engine, config=cfg, expected_login="carepulse_api"
                    )
                assert probe_compat_readiness(gate_engine, "api") is False
            finally:
                gate_engine.dispose()

            # 3. Multiple/unknown alembic_version rows deny
            mig = connect_role("carepulse_migration", dbname=dbname)
            try:
                mig.execute(
                    "UPDATE compat_control.target_control SET phase = 'TARGET_OPEN'"
                )
                mig.execute(
                    "INSERT INTO public.alembic_version (version_num) VALUES ('9999_unknown')"
                )
                mig.commit()
            finally:
                mig.close()

            with pytest.raises(CompatAdmissionError):
                acquire_invocation_permit(
                    api_engine, config=cfg, expected_login="carepulse_api"
                )
        finally:
            api_engine.dispose()
