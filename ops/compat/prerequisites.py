"""Explicitly authorized missing-delta-only prerequisite preparation for populated cutover (T030).

Strictly fenced: refuses any role, schema, ownership, extension, or GRANT/REVOKE write
until T028 preflight, T031 owner/epoch, T032 drain, and T037 independent observer proof
all pass, and rechecks ownership + drain immediately before each write.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import psycopg
from psycopg import sql

from ops.compat.control import assert_current_owner
from ops.compat.drain import DrainVerification, observe_drain_complete
from ops.compat.preflight import PreflightReport
from ops.compat.probes import (
    probe_alembic_head,
    probe_role_attributes,
    probe_target_identity,
)
from ops.compat.manifest import SOURCE_HEAD


class PrerequisiteError(RuntimeError):
    """Raised when pre-T030 drain/owner/seal evidence is missing or prerequisite delta fails."""


@dataclass(frozen=True)
class PrerequisiteDeltaReport:
    """Summary of explicitly applied missing prerequisite delta."""

    target_id: str
    owner_epoch: int
    created_roles: tuple[str, ...]
    created_schemas: tuple[str, ...]
    verified_roles: tuple[str, ...]


def _recheck_fenced_preconditions(
    owner_conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    preflight: PreflightReport,
    drain_proof: DrainVerification,
    owner_epoch: int,
) -> None:
    """Recheck owner lock/epoch, 0040 head, DRAINING, seal, and zero unresolved work before each write."""
    ident = probe_target_identity(
        owner_conn,
        expected_cluster_id=preflight.cluster_id,
        expected_db_name=preflight.db_name,
    )
    if ident.db_name != preflight.db_name:
        raise PrerequisiteError("Target database mismatch")
    probe_alembic_head(owner_conn, expected_head=SOURCE_HEAD)

    ctrl = assert_current_owner(
        owner_conn,
        target_id=preflight.target_id,
        expected_epoch=owner_epoch,
        lock_mode="UPDATE",
    )
    if ctrl.phase != "DRAINING" or ctrl.active_generation != "none":
        raise PrerequisiteError(
            f"Prerequisites require DRAINING/none, observed {ctrl.phase}/{ctrl.active_generation}"
        )
    if not ctrl.source_sealed or not drain_proof.source_sealed:
        raise PrerequisiteError("Source launch/credential seal not observed (T037)")
    if (
        ctrl.drain_verified_epoch != owner_epoch
        or drain_proof.owner_epoch != owner_epoch
    ):
        raise PrerequisiteError("Drain verification epoch mismatch")

    fresh = observe_drain_complete(
        observer_conn,
        target_id=preflight.target_id,
        expected_epoch=owner_epoch,
        consumers_stopped=True,
        require_source_sealed=True,
    )
    if fresh.unresolved_work_count != 0 or fresh.active_product_sessions != 0:
        raise PrerequisiteError("Unresolved work or active product sessions detected")


def apply_missing_prerequisites(
    admin_owner_conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    preflight: PreflightReport,
    drain_proof: DrainVerification,
    owner_epoch: int,
    role_passwords: Mapping[str, str],
) -> PrerequisiteDeltaReport:
    """Apply only the explicitly missing F-01 prerequisite delta under verified drain & owner lock."""
    _recheck_fenced_preconditions(
        admin_owner_conn,
        observer_conn,
        preflight=preflight,
        drain_proof=drain_proof,
        owner_epoch=owner_epoch,
    )

    required_login_roles = (
        "carepulse_api",
        "carepulse_web",
        "carepulse_worker",
        "carepulse_gate",
    )
    for r in required_login_roles:
        if not role_passwords.get(r):
            raise PrerequisiteError(f"Missing password for prerequisite role {r}")

    existing = probe_role_attributes(
        admin_owner_conn,
        (*required_login_roles, "carepulse_identity_owner", "carepulse_policy_seed"),
    )
    for role_name, snap in existing.items():
        if snap.rolsuper or snap.rolbypassrls or snap.rolcreaterole or snap.rolcreatedb:
            raise PrerequisiteError(
                f"Incompatible existing role attributes on {role_name}"
            )

    created_roles: list[str] = []
    for role_name in required_login_roles:
        _recheck_fenced_preconditions(
            admin_owner_conn,
            observer_conn,
            preflight=preflight,
            drain_proof=drain_proof,
            owner_epoch=owner_epoch,
        )
        if role_name not in existing:
            admin_owner_conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(
                    sql.Identifier(role_name),
                    sql.Literal(role_passwords[role_name]),
                )
            )
            admin_owner_conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(preflight.db_name), sql.Identifier(role_name)
                )
            )
            created_roles.append(role_name)
        admin_owner_conn.commit()

    _recheck_fenced_preconditions(
        admin_owner_conn,
        observer_conn,
        preflight=preflight,
        drain_proof=drain_proof,
        owner_epoch=owner_epoch,
    )
    if "carepulse_identity_owner" not in existing:
        admin_owner_conn.execute(
            "CREATE ROLE carepulse_identity_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
        admin_owner_conn.execute(
            "GRANT carepulse_identity_owner TO carepulse_migration WITH SET TRUE, INHERIT FALSE"
        )
        created_roles.append("carepulse_identity_owner")
    admin_owner_conn.commit()

    created_schemas: list[str] = []
    _recheck_fenced_preconditions(
        admin_owner_conn,
        observer_conn,
        preflight=preflight,
        drain_proof=drain_proof,
        owner_epoch=owner_epoch,
    )
    schema_rows = admin_owner_conn.execute(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('f01_private', 'f01_crypto')"
    ).fetchall()
    existing_schemas = {
        r["schema_name"] if isinstance(r, dict) else r[0] for r in schema_rows
    }
    if "f01_private" not in existing_schemas:
        admin_owner_conn.execute(
            "CREATE SCHEMA f01_private AUTHORIZATION carepulse_identity_owner"
        )
        created_schemas.append("f01_private")
    if "f01_crypto" not in existing_schemas:
        admin_owner_conn.execute(
            "CREATE SCHEMA f01_crypto AUTHORIZATION carepulse_migration"
        )
        created_schemas.append("f01_crypto")
    admin_owner_conn.execute(
        "CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA f01_crypto"
    )
    admin_owner_conn.execute("REVOKE ALL ON SCHEMA f01_private, f01_crypto FROM PUBLIC")
    admin_owner_conn.execute(
        "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_crypto FROM PUBLIC"
    )
    admin_owner_conn.execute(
        "GRANT EXECUTE ON FUNCTION f01_crypto.hmac(bytea, bytea, text), f01_crypto.digest(bytea, text), f01_crypto.gen_random_bytes(integer) TO carepulse_identity_owner"
    )
    admin_owner_conn.execute(
        "GRANT USAGE ON SCHEMA f01_crypto TO carepulse_identity_owner"
    )
    admin_owner_conn.commit()

    # Re-probe effective privileges after applying the missing delta.
    post_snaps = probe_role_attributes(
        observer_conn, (*required_login_roles, "carepulse_identity_owner")
    )
    for r in (*required_login_roles, "carepulse_identity_owner"):
        snap = post_snaps.get(r)
        if snap is None or snap.rolsuper or snap.rolbypassrls:
            raise PrerequisiteError(f"Post-delta role verification failed for {r}")

    return PrerequisiteDeltaReport(
        target_id=preflight.target_id,
        owner_epoch=owner_epoch,
        created_roles=tuple(created_roles),
        created_schemas=tuple(created_schemas),
        verified_roles=tuple(sorted(post_snaps.keys())),
    )
