"""Python compatibility admission gate for candidate API, worker, and sender.

Self-contained under apps/api/carepulse/ (copied by apps/api/Dockerfile).
Never imports ops/compat/control.py or uses maintenance credentials.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

CANDIDATE_HEAD = "0046_f01_audit_integrity"
SOURCE_HEAD = "0040_t173_numeric_money"

ServiceKind = Literal["api", "worker", "sender"]

_EXPECTED_ROLE_BY_SERVICE: dict[str, frozenset[str]] = {
    "api": frozenset({"carepulse_api", "carepulse_gate"}),
    "worker": frozenset({"carepulse_worker"}),
    "sender": frozenset({"carepulse_sender"}),
}


class CompatAdmissionError(RuntimeError):
    """Sanitized compatibility admission denial (no SQL/key/tenant leakage)."""

    def __init__(self, reason: str = "Compatibility admission denied") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CompatRuntimeConfig:
    """Trusted local runtime configuration for compatibility admission."""

    target_id: str
    manifest_id: str
    generation: Literal["source", "candidate"]
    expected_head: str
    service: ServiceKind
    db_name: str

    @classmethod
    def from_env(cls, service: ServiceKind) -> CompatRuntimeConfig | None:
        target_id = os.environ.get("CP_COMPAT_TARGET_ID", "").strip()
        if not target_id:
            return None
        generation = os.environ.get("CP_COMPAT_GENERATION", "").strip()
        if generation not in ("source", "candidate"):
            raise CompatAdmissionError("Invalid generation configuration")
        expected_head = os.environ.get("CP_COMPAT_EXPECTED_HEAD", "").strip()
        manifest_id = os.environ.get("CP_COMPAT_MANIFEST_ID", "").strip()
        db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
        if not expected_head or not manifest_id or not db_name:
            raise CompatAdmissionError("Incomplete compatibility configuration")
        return cls(
            target_id=target_id,
            manifest_id=manifest_id,
            generation=generation,  # type: ignore[arg-type]
            expected_head=expected_head,
            service=service,
            db_name=db_name,
        )


@dataclass(frozen=True)
class AdmissionPermit:
    """Active shared session permit held across an admitted invocation."""

    attempt_id: str
    target_id: str
    owner_epoch: int
    generation: str
    service: str
    permit_key: int


def permit_lock_key(target_id: str) -> int:
    """Deterministic 64-bit PostgreSQL advisory lock key matching ops/compat/control.py."""
    digest = hashlib.sha256(f"compat:permit:{target_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def is_compat_enabled() -> bool:
    """Return True when the process is configured for compatibility gating."""
    return bool(os.environ.get("CP_COMPAT_TARGET_ID", "").strip())


def _expected_phase(generation: str) -> str:
    return "SOURCE_OPEN" if generation == "source" else "TARGET_OPEN"


def _expected_manifest_col(generation: str) -> str:
    return "source_manifest_id" if generation == "source" else "target_manifest_id"


def verify_connection_identity(
    session_or_conn,
    *,
    config: CompatRuntimeConfig,
    expected_login: str | None = None,
) -> tuple[str, str]:
    """Verify actual DB, session/current role, and non-privileged role flags on a connection."""
    row = (
        session_or_conn.execute(
            text(
                """
            SELECT current_database() AS db_name,
                   session_user::text AS session_user,
                   current_user::text AS current_user,
                   r.rolsuper,
                   r.rolbypassrls,
                   r.rolcanlogin
            FROM pg_roles r
            WHERE r.rolname = current_user
            """
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CompatAdmissionError("Service identity unavailable")
    if row["db_name"] != config.db_name:
        raise CompatAdmissionError("Target database mismatch")
    allowed_roles = (
        {expected_login}
        if expected_login is not None
        else _EXPECTED_ROLE_BY_SERVICE[config.service]
    )
    if (
        row["session_user"] not in allowed_roles
        or row["current_user"] not in allowed_roles
        or row["session_user"] != row["current_user"]
    ):
        raise CompatAdmissionError("Unexpected effective database role")
    if row["rolsuper"] or row["rolbypassrls"] or not row["rolcanlogin"]:
        raise CompatAdmissionError("Invalid role privilege attributes")
    return str(row["session_user"]), str(row["db_name"])


def verify_control_and_head(
    session_or_conn,
    *,
    config: CompatRuntimeConfig,
    lock_for_share: bool = True,
) -> int:
    """Validate singleton alembic head and compat_control.target_control under short FOR SHARE."""
    revs = (
        session_or_conn.execute(text("SELECT version_num FROM public.alembic_version"))
        .scalars()
        .all()
    )
    if len(revs) != 1 or revs[0] != config.expected_head:
        raise CompatAdmissionError("Incompatible schema revision")

    lock_clause = " FOR SHARE" if lock_for_share else ""
    ctrl = (
        session_or_conn.execute(
            text(
                f"""
            SELECT target_id, db_name, phase, owner_epoch,
                   source_manifest_id, target_manifest_id, active_generation,
                   source_sealed
            FROM compat_control.target_control
            WHERE singleton_id = TRUE{lock_clause}
            """
            )
        )
        .mappings()
        .first()
    )
    if ctrl is None:
        raise CompatAdmissionError("Missing target control")
    if ctrl["target_id"] != config.target_id or ctrl["db_name"] != config.db_name:
        raise CompatAdmissionError("Target binding mismatch")
    if ctrl["phase"] != _expected_phase(config.generation):
        raise CompatAdmissionError("Service generation fenced")
    if ctrl["active_generation"] != config.generation:
        raise CompatAdmissionError("Active generation mismatch")
    if config.generation == "source" and ctrl["source_sealed"]:
        raise CompatAdmissionError("Source generation sealed")
    manifest_col = _expected_manifest_col(config.generation)
    if ctrl[manifest_col] != config.manifest_id:
        raise CompatAdmissionError("Manifest identity mismatch")
    return int(ctrl["owner_epoch"])


def acquire_invocation_permit(
    engine: Engine,
    *,
    config: CompatRuntimeConfig,
    expected_login: str | None = None,
    invocation_class: str = "http",
    correlation_id: str | None = None,
    record_attempt: bool = False,
) -> tuple[AdmissionPermit, object]:
    """Acquire shared session permit on a dedicated non-clinical connection, validate, and commit short check.

    Returns (permit, permit_connection). Caller MUST call release_invocation_permit()
    when the invocation/response/delivery completes.
    """
    key = permit_lock_key(config.target_id)
    conn = engine.connect()
    try:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock_shared(:k)"), {"k": key}
        ).scalar()
        if not acquired:
            raise CompatAdmissionError("Drain in progress")

        verify_connection_identity(conn, config=config, expected_login=expected_login)
        epoch = verify_control_and_head(conn, config=config, lock_for_share=True)
        attempt_id = str(uuid.uuid4())
        if record_attempt:
            conn.execute(
                text(
                    """
                    SELECT compat_control.record_own_attempt(
                        :attempt_id, :target_id, :epoch, :generation,
                        :service, :inv_class, :corr_id, NULL, 'PENDING'
                    )
                    """
                ),
                {
                    "attempt_id": attempt_id,
                    "target_id": config.target_id,
                    "epoch": epoch,
                    "generation": config.generation,
                    "service": config.service,
                    "inv_class": invocation_class,
                    "corr_id": correlation_id,
                },
            )
        conn.commit()
        permit = AdmissionPermit(
            attempt_id=attempt_id,
            target_id=config.target_id,
            owner_epoch=epoch,
            generation=config.generation,
            service=config.service,
            permit_key=key,
        )
        return permit, conn
    except Exception:
        try:
            conn.rollback()
        except SQLAlchemyError:
            pass
        try:
            conn.execute(text("SELECT pg_advisory_unlock_shared(:k)"), {"k": key})
            conn.commit()
        except SQLAlchemyError:
            pass
        conn.close()
        raise


def record_attempt_disposition(
    permit_conn,
    permit: AdmissionPermit,
    *,
    invocation_class: str,
    disposition: Literal["COMMITTED", "ROLLED_BACK", "SENT", "NOT_SENT", "UNKNOWN"],
    correlation_id: str | None = None,
    lease_generation: int | None = None,
) -> None:
    """Update the caller's own attempt disposition via the narrow SECURITY DEFINER interface."""
    try:
        permit_conn.execute(
            text(
                """
                SELECT compat_control.record_own_attempt(
                    :attempt_id, :target_id, :epoch, :generation,
                    :service, :inv_class, :corr_id, :lease_gen, :disposition
                )
                """
            ),
            {
                "attempt_id": permit.attempt_id,
                "target_id": permit.target_id,
                "epoch": permit.owner_epoch,
                "generation": permit.generation,
                "service": permit.service,
                "inv_class": invocation_class,
                "corr_id": correlation_id,
                "lease_gen": lease_generation,
                "disposition": disposition,
            },
        )
        permit_conn.commit()
    except SQLAlchemyError as exc:
        try:
            permit_conn.rollback()
        except SQLAlchemyError:
            pass
        raise CompatAdmissionError("Failed to record attempt disposition") from exc


def release_invocation_permit(permit_conn, permit: AdmissionPermit) -> None:
    """Release the shared session permit and close the permit connection."""
    try:
        permit_conn.execute(
            text("SELECT pg_advisory_unlock_shared(:k)"), {"k": permit.permit_key}
        )
        permit_conn.commit()
    except SQLAlchemyError:
        pass
    finally:
        permit_conn.close()


def check_pool_checkout(
    session: Session | object,
    *,
    config: CompatRuntimeConfig,
    expected_login: str | None = None,
    expected_epoch: int | None = None,
) -> int:
    """Verify target, effective role, head, and phase on an already-checked-out DB connection."""
    verify_connection_identity(session, config=config, expected_login=expected_login)
    epoch = verify_control_and_head(session, config=config, lock_for_share=False)
    if expected_epoch is not None and epoch != expected_epoch:
        raise CompatAdmissionError("Stale owner epoch on reused connection")
    return epoch


def probe_compat_readiness(engine: Engine, service: ServiceKind) -> bool:
    """Bounded readiness check for /health/ready and worker status."""
    config = CompatRuntimeConfig.from_env(service)
    if config is None:
        return True
    try:
        with engine.connect() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            verify_connection_identity(conn, config=config)
            verify_control_and_head(conn, config=config, lock_for_share=False)
        return True
    except (CompatAdmissionError, SQLAlchemyError):
        return False
