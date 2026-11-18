"""Maintenance close, exclusive drain permits, and independent drain verification (T032, T037)."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ops.compat.control import (
    ControlRecord,
    acquire_exclusive_drain_permit,
    assert_current_owner,
    read_control_record,
)
from ops.compat.work import assert_all_work_settled


class DrainError(RuntimeError):
    """Raised when close, drain, or independent drain verification fails closed."""


@dataclass(frozen=True)
class DrainVerification:
    """Independently observed drain-complete proof bound to target and owner epoch."""

    target_id: str
    owner_epoch: int
    phase: str
    active_generation: str
    source_sealed: bool
    active_product_sessions: int
    unresolved_work_count: int


def commit_draining_fence(
    conn: psycopg.Connection,
    *,
    target_id: str,
    owner_epoch: int,
) -> ControlRecord:
    """Commit DRAINING under admission row lock BEFORE waiting on exclusive work permit."""
    current = assert_current_owner(
        conn, target_id=target_id, expected_epoch=owner_epoch, lock_mode="UPDATE"
    )
    if current.phase not in ("CLOSED", "SOURCE_OPEN", "DRAINING"):
        raise DrainError(f"Cannot enter DRAINING from phase {current.phase}")

    row = conn.execute(
        """
        UPDATE compat_control.target_control
        SET phase = 'DRAINING',
            active_generation = 'none',
            drain_verified_epoch = NULL,
            updated_at = now()
        WHERE singleton_id = TRUE AND target_id = %s AND owner_epoch = %s
        RETURNING target_id, cluster_id, db_name, phase, owner_epoch,
                  source_manifest_id, target_manifest_id, active_generation,
                  source_sealed, drain_verified_epoch
        """,
        (target_id, owner_epoch),
    ).fetchone()
    if row is None:
        conn.rollback()
        raise DrainError("Failed to commit DRAINING under current owner epoch")
    conn.commit()
    return ControlRecord(
        target_id=row["target_id"],
        cluster_id=row["cluster_id"],
        db_name=row["db_name"],
        phase=row["phase"],
        owner_epoch=int(row["owner_epoch"]),
        source_manifest_id=row["source_manifest_id"],
        target_manifest_id=row["target_manifest_id"],
        active_generation=row["active_generation"],
        source_sealed=bool(row["source_sealed"]),
        drain_verified_epoch=None,
    )


def drain_and_settle(
    conn: psycopg.Connection,
    *,
    target_id: str,
    owner_epoch: int,
    blocking: bool = True,
) -> ControlRecord:
    """Commit DRAINING, wait for exclusive permit, verify zero unresolved work, and record drain_verified_epoch."""
    commit_draining_fence(conn, target_id=target_id, owner_epoch=owner_epoch)
    if not acquire_exclusive_drain_permit(conn, target_id, blocking=blocking):
        raise DrainError("Active invocation permits still held")

    assert_current_owner(
        conn, target_id=target_id, expected_epoch=owner_epoch, lock_mode="UPDATE"
    )
    assert_all_work_settled(conn, target_id=target_id)

    conn.execute(
        """
        UPDATE compat_control.target_control
        SET drain_verified_epoch = %s,
            updated_at = now()
        WHERE singleton_id = TRUE AND target_id = %s AND owner_epoch = %s
        """,
        (owner_epoch, target_id, owner_epoch),
    )
    conn.commit()
    return read_control_record(conn)


def observe_drain_complete(
    observer_conn: psycopg.Connection,
    *,
    target_id: str,
    expected_epoch: int,
    consumers_stopped: bool = True,
    require_source_sealed: bool = False,
) -> DrainVerification:
    """Read-only independent observer verification of drain completion before T030."""
    if not consumers_stopped:
        raise DrainError("Autonomous consumers have not been confirmed stopped")

    ctrl = read_control_record(observer_conn, lock_mode="NONE")
    if ctrl.target_id != target_id or ctrl.owner_epoch != expected_epoch:
        raise DrainError("Drain observer target/epoch mismatch")
    if ctrl.phase != "DRAINING" or ctrl.active_generation != "none":
        raise DrainError(
            f"Expected DRAINING/none, observed {ctrl.phase}/{ctrl.active_generation}"
        )
    if ctrl.drain_verified_epoch != expected_epoch:
        raise DrainError("drain_verified_epoch not recorded for current owner epoch")
    if require_source_sealed and not ctrl.source_sealed:
        raise DrainError("Source launch/credential seal not yet recorded")

    work_row = observer_conn.execute(
        """
        SELECT count(*) AS c
        FROM compat_control.work_evidence
        WHERE target_id = %s AND disposition IN ('PENDING', 'UNKNOWN')
        """,
        (target_id,),
    ).fetchone()
    unresolved = int(work_row["c"] if isinstance(work_row, dict) else work_row[0])
    if unresolved != 0:
        raise DrainError(f"Unresolved work remains: {unresolved} attempts")

    sess_row = observer_conn.execute(
        """
        SELECT count(*) AS c
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND usename IN ('carepulse_runtime', 'carepulse_api', 'carepulse_web', 'carepulse_gate', 'carepulse_auth', 'carepulse_worker', 'carepulse_sender')
          AND pid <> pg_backend_pid()
          AND state <> 'idle'
        """
    ).fetchone()
    active_sessions = int(sess_row["c"] if isinstance(sess_row, dict) else sess_row[0])
    if active_sessions != 0:
        raise DrainError(
            f"Active product transactions remain during drain: {active_sessions}"
        )

    return DrainVerification(
        target_id=ctrl.target_id,
        owner_epoch=ctrl.owner_epoch,
        phase=ctrl.phase,
        active_generation=ctrl.active_generation,
        source_sealed=ctrl.source_sealed,
        active_product_sessions=active_sessions,
        unresolved_work_count=unresolved,
    )
