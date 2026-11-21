"""US1-only disposable compatibility matrix fixtures and initial matched activation.

Restricted to test fixture provisioning; never invokes T030 or production cutover.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from ops.compat.control import (
    acquire_owner_lock,
    read_control_record,
    release_owner_lock,
)
from ops.compat.manifest import (
    CANDIDATE_HEAD,
    SOURCE_HEAD,
    GenerationManifest,
)
from ops.compat.probes import (
    probe_alembic_head,
    probe_role_attributes,
    probe_target_identity,
)
from ops.compat.work import assert_all_work_settled

COMPAT_CONTROL_SQL = Path(__file__).resolve().parents[2] / "db" / "compat-control.sql"


class MatrixFixtureError(RuntimeError):
    """Raised when matrix fixture provisioning or initial activation fails closed."""


@dataclass(frozen=True)
class MatrixTargetSnapshot:
    """Recorded baseline snapshot for a disposable matrix target."""

    target_id: str
    head: str
    phase: str
    owner_epoch: int
    roles: frozenset[str]


def _apply_compat_control_sql(conn: psycopg.Connection) -> None:
    sql_text = COMPAT_CONTROL_SQL.read_text(encoding="utf-8")
    # Strip psql metacommands when executing via psycopg.
    cleaned_lines = [
        line for line in sql_text.splitlines() if not line.lstrip().startswith("\\")
    ]
    conn.execute("\n".join(cleaned_lines))
    conn.commit()


def provision_target_control(
    conn: psycopg.Connection,
    *,
    target_id: str,
    cluster_id: str,
    db_name: str,
    source_manifest_id: str,
    target_manifest_id: str,
) -> None:
    """Provision compat_control schema/table in CLOSED state (no default-open)."""
    _apply_compat_control_sql(conn)
    conn.execute(
        """
        INSERT INTO compat_control.target_control (
            singleton_id, target_id, cluster_id, db_name, phase, owner_epoch,
            source_manifest_id, target_manifest_id, active_generation, source_sealed
        ) VALUES (
            TRUE, %s, %s, %s, 'CLOSED', 0, %s, %s, 'none', FALSE
        )
        ON CONFLICT (singleton_id) DO NOTHING
        """,
        (target_id, cluster_id, db_name, source_manifest_id, target_manifest_id),
    )
    conn.commit()


def snapshot_0040_baseline(
    conn: psycopg.Connection, *, target_id: str
) -> MatrixTargetSnapshot:
    """Snapshot and prove the original 0040 role/head baseline before any fixture delta."""
    head = probe_alembic_head(conn, expected_head=SOURCE_HEAD)
    ctrl = read_control_record(conn)
    if ctrl.target_id != target_id or ctrl.phase != "CLOSED":
        raise MatrixFixtureError(
            f"Expected CLOSED control for {target_id}, got {ctrl.phase}"
        )
    role_rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname LIKE 'carepulse_%' ORDER BY rolname"
    ).fetchall()
    roles = frozenset(
        r[0] if not isinstance(r, dict) else r["rolname"] for r in role_rows
    )
    return MatrixTargetSnapshot(
        target_id=target_id,
        head=head,
        phase=ctrl.phase,
        owner_epoch=ctrl.owner_epoch,
        roles=roles,
    )


def apply_fixture_only_0041_delta(
    conn: psycopg.Connection,
    *,
    api_password: str,
    web_password: str,
    worker_password: str,
    gate_password: str,
) -> None:
    """Apply the narrowly scoped fixture-only missing role/schema/extension delta for 0041-0045 matrix heads."""
    # Never wholesale replay db/roles.sql; only add missing split roles/schemas/extension.
    for role_name, password in (
        ("carepulse_api", api_password),
        ("carepulse_web", web_password),
        ("carepulse_worker", worker_password),
        ("carepulse_gate", gate_password),
    ):
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)
        ).fetchone()
        if not exists:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(password))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(conn.info.dbname), sql.Identifier(role_name)
                )
            )
    for nologin_role in ("carepulse_policy_seed", "carepulse_identity_owner"):
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (nologin_role,)
        ).fetchone()
        if not exists:
            conn.execute(
                f"CREATE ROLE {nologin_role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            conn.execute(
                f"GRANT {nologin_role} TO carepulse_migration WITH SET TRUE, INHERIT FALSE"
            )
    conn.execute(
        "CREATE SCHEMA IF NOT EXISTS f01_private AUTHORIZATION carepulse_identity_owner"
    )
    conn.execute(
        "CREATE SCHEMA IF NOT EXISTS f01_crypto AUTHORIZATION carepulse_migration"
    )
    conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA f01_crypto")
    conn.execute("REVOKE ALL ON SCHEMA f01_private, f01_crypto FROM PUBLIC")
    conn.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_crypto FROM PUBLIC")
    conn.execute(
        "GRANT EXECUTE ON FUNCTION f01_crypto.hmac(bytea, bytea, text), f01_crypto.digest(bytea, text), f01_crypto.gen_random_bytes(integer) TO carepulse_identity_owner"
    )
    conn.execute("GRANT USAGE ON SCHEMA f01_crypto TO carepulse_identity_owner")
    conn.commit()


def step_matrix_head(
    *,
    host: str,
    port: int,
    dbname: str,
    migration_password: str,
    target_revision: str,
) -> str:
    """Step an isolated 0040-origin matrix copy to a published 0041-0045 revision (stays CLOSED)."""
    if target_revision not in {
        "0041_f01_runtime_identity",
        "0042_f01_effective_grants",
        "0043_f01_writer_boundary",
        "0044_f01_delivery_acl",
        "0045_f01_revocation_evidence",
    }:
        raise MatrixFixtureError(
            f"Invalid intermediate matrix target: {target_revision}"
        )
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_migration",
            password=migration_password,
            host=host,
            port=port,
            database=dbname,
        )
    )
    try:
        cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with engine.connect() as sa_conn:
            cfg.attributes["connection"] = sa_conn
            command.upgrade(cfg, target_revision)
            sa_conn.commit()
    finally:
        engine.dispose()
    return target_revision


def initial_activate_matched_target(
    conn: psycopg.Connection,
    *,
    manifest: GenerationManifest,
    expected_target_id: str,
) -> int:
    """Single-use, operator-owned initial CLOSED -> SOURCE_OPEN/0040 or TARGET_OPEN/0046 activation.

    Strictly restricted to US1 fresh matched targets before launching product services.
    Refuses intermediates, wrong targets, second transitions, or outstanding work.
    """
    if manifest.target_binding.target_id != expected_target_id:
        raise MatrixFixtureError("Manifest target_id mismatch")
    ident = probe_target_identity(
        conn,
        expected_cluster_id=manifest.target_binding.cluster_id,
        expected_db_name=manifest.target_binding.db_name,
    )
    head = probe_alembic_head(conn, expected_head=manifest.expected_head)
    if head not in (SOURCE_HEAD, CANDIDATE_HEAD):
        raise MatrixFixtureError(f"Intermediate head {head} cannot be activated")

    if not acquire_owner_lock(conn, expected_target_id):
        raise MatrixFixtureError("Could not acquire owner lock for initial activation")
    try:
        ctrl = read_control_record(conn, lock_mode="UPDATE")
        if ctrl.target_id != expected_target_id or ctrl.db_name != ident.db_name:
            raise MatrixFixtureError("Control target mismatch")
        if ctrl.phase != "CLOSED" or ctrl.owner_epoch != 0:
            raise MatrixFixtureError(
                "Initial activation allowed only once from CLOSED at epoch 0"
            )
        assert_all_work_settled(conn, target_id=expected_target_id)

        if manifest.generation == "source":
            if ctrl.source_manifest_id != manifest.manifest_id:
                raise MatrixFixtureError("Source manifest_id mismatch")
            new_phase = "SOURCE_OPEN"
            required_roles = ("carepulse_runtime", "carepulse_auth", "carepulse_sender")
        else:
            if ctrl.target_manifest_id != manifest.manifest_id:
                raise MatrixFixtureError("Target manifest_id mismatch")
            new_phase = "TARGET_OPEN"
            required_roles = (
                "carepulse_api",
                "carepulse_web",
                "carepulse_worker",
                "carepulse_gate",
                "carepulse_auth",
                "carepulse_sender",
            )

        snaps = probe_role_attributes(conn, required_roles)
        for role_name in required_roles:
            snap = snaps.get(role_name)
            if (
                snap is None
                or not snap.rolcanlogin
                or snap.rolsuper
                or snap.rolbypassrls
            ):
                raise MatrixFixtureError(f"Role {role_name} not ready for {new_phase}")

        conn.execute(
            """
            UPDATE compat_control.target_control
            SET phase = %s,
                owner_epoch = 1,
                active_generation = %s,
                updated_at = now()
            WHERE singleton_id = TRUE AND target_id = %s AND phase = 'CLOSED' AND owner_epoch = 0
            """,
            (new_phase, manifest.generation, expected_target_id),
        )
        conn.commit()
        return 1
    except Exception:
        conn.rollback()
        raise
    finally:
        release_owner_lock(conn, expected_target_id)
