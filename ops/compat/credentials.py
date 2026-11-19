"""Fenced legacy credential retirement after the durable 0046 upgrade (T047)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import psycopg
from psycopg import sql

from ops.compat.control import assert_current_owner, read_control_record
from ops.compat.manifest import CANDIDATE_HEAD, GenerationManifest
from ops.compat.probes import (
    probe_alembic_head,
    probe_role_attributes,
    probe_target_identity,
)
from ops.compat.verify import (
    RetainedBaseline,
    verify_fenced_security,
    verify_retained_evidence,
)


class CredentialError(RuntimeError):
    """Credential retirement is unsafe; keep both generations fenced."""


@dataclass(frozen=True)
class CredentialProof:
    target_id: str
    owner_epoch: int
    head: str
    legacy_login_disabled: bool
    active_product_sessions: int
    rotated_shared_roles: tuple[str, ...]


def retire_legacy_runtime(
    admin_conn: psycopg.Connection,
    owner_conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    source_manifest: GenerationManifest,
    target_manifest: GenerationManifest,
    baseline: RetainedBaseline,
    owner_epoch: int,
    original_shared_passwords: Mapping[str, str],
    candidate_shared_passwords: Mapping[str, str],
) -> CredentialProof:
    """Retire predecessor LOGIN and rotate two reviewed shared DB login passwords.

    All application/SMTP/worker sessions must already be absent. A lost DDL
    acknowledgement is resolved by independently inspecting role state; no
    signing or clinical encryption keys are modified.
    """
    binding = source_manifest.target_binding
    shared = {"carepulse_auth", "carepulse_sender"}
    if (
        set(original_shared_passwords) != shared
        or set(candidate_shared_passwords) != shared
        or any(
            not original_shared_passwords[r]
            or not candidate_shared_passwords[r]
            or original_shared_passwords[r] == candidate_shared_passwords[r]
            for r in shared
        )
    ):
        raise CredentialError(
            "Missing distinct reviewed source/candidate shared credentials"
        )
    if admin_conn.autocommit or owner_conn.autocommit:
        raise CredentialError(
            "Retirement requires transactional owner and admin sessions"
        )

    security = verify_fenced_security(
        observer_conn,
        source_manifest=source_manifest,
        target_manifest=target_manifest,
        expected_epoch=owner_epoch,
        expected_head=CANDIDATE_HEAD,
    )
    verify_retained_evidence(observer_conn, baseline=baseline, security=security)
    ctrl = assert_current_owner(
        owner_conn,
        target_id=binding.target_id,
        expected_epoch=owner_epoch,
        lock_mode="NONE",
    )
    if (
        ctrl.phase not in ("MIGRATING", "VERIFYING")
        or ctrl.active_generation != "none"
        or not ctrl.source_sealed
        or ctrl.drain_verified_epoch != owner_epoch
    ):
        raise CredentialError("Retirement requires a fully drained, fenced target")

    # Existing backends retain their privileges even after NOLOGIN. Require
    # independently stopped pools rather than silently killing live work.
    sessions = observer_conn.execute(
        """
        SELECT count(*) AS n FROM pg_stat_activity
        WHERE datname=current_database()
          AND usename IN ('carepulse_runtime','carepulse_api','carepulse_web',
                          'carepulse_worker','carepulse_gate','carepulse_auth',
                          'carepulse_sender')
          AND pid<>pg_backend_pid()
        """
    ).fetchone()["n"]
    if sessions:
        raise CredentialError("Unsealed predecessor or candidate backend remains")

    admin_identity = probe_target_identity(
        admin_conn,
        expected_cluster_id=binding.cluster_id,
        expected_db_name=binding.db_name,
    )
    if (
        admin_identity.session_user != "carepulse_bootstrap"
        or admin_identity.current_user != "carepulse_bootstrap"
    ):
        raise CredentialError(
            "Role retirement requires the direct reviewed bootstrap login"
        )

    try:
        locked = read_control_record(admin_conn, lock_mode="UPDATE")
        if locked != ctrl or probe_alembic_head(admin_conn) != CANDIDATE_HEAD:
            raise CredentialError("Retirement owner epoch, target or head changed")
        if not admin_conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid=%s AND datname=current_database()",
            (owner_conn.info.backend_pid,),
        ).fetchone():
            raise CredentialError("Owner backend was lost during credential retirement")
        actual = probe_role_attributes(admin_conn, ["carepulse_runtime"])[
            "carepulse_runtime"
        ]
        if actual.rolsuper or actual.rolbypassrls:
            raise CredentialError("Incompatible legacy role attributes")
        if admin_conn.execute(
            "SELECT has_schema_privilege('carepulse_runtime','public','USAGE') AS permitted"
        ).fetchone()["permitted"]:
            raise CredentialError("Legacy runtime still has clinical schema access")
        if actual.rolcanlogin:
            admin_conn.execute("ALTER ROLE carepulse_runtime NOLOGIN")
        for role_name in sorted(shared):
            admin_conn.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role_name),
                    sql.Literal(candidate_shared_passwords[role_name]),
                )
            )
        admin_conn.commit()
    except Exception:
        admin_conn.rollback()
        raise

    roles = probe_role_attributes(observer_conn, ("carepulse_runtime", *shared))
    if (
        roles["carepulse_runtime"].rolcanlogin
        or any(not roles[r].rolcanlogin for r in shared)
        or read_control_record(observer_conn) != ctrl
    ):
        raise CredentialError("Legacy retirement acknowledgement cannot be verified")
    return CredentialProof(
        binding.target_id, owner_epoch, CANDIDATE_HEAD, True, 0, tuple(sorted(shared))
    )
