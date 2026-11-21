"""Maintenance close, exclusive drain permits, and independent drain verification (T032, T037)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg

from ops.compat.control import (
    ControlRecord,
    acquire_exclusive_drain_permit,
    advisory_key,
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
    claimed_delivery_jobs: int
    consumers_stopped: bool


_CONSUMERS = frozenset(
    f"{generation}-{service}"
    for generation in ("source", "candidate")
    for service in ("api", "web", "worker", "sender")
)


def assert_host_consumers_stopped(
    observer_conn: psycopg.Connection, ctrl: ControlRecord
) -> None:
    """Require actual host-owned Docker and database shutdown evidence.

    The observer has no Docker socket. A separate CI-host confirms the
    current target, epoch, head, seal and absence of running consumers.
    """
    directory_str = os.environ.get("CP_COMPAT_T051_IPC_DIR", "")
    directory = Path(directory_str) if directory_str else None
    if (
        os.environ.get("CI") != "true"
        or directory is None
        or not directory.is_dir()
        or directory.is_symlink()
    ):
        raise DrainError("Independent host shutdown observer is unavailable")
    heads = observer_conn.execute(
        "SELECT version_num FROM public.alembic_version"
    ).fetchall()
    if len(heads) != 1:
        raise DrainError("Head is not uniquely available for host shutdown proof")
    head = heads[0]["version_num"] if isinstance(heads[0], dict) else heads[0][0]
    nonce = uuid4().hex[:24]
    payload = {
        "version": 1,
        "id": nonce,
        "kind": "drain_proof",
        "db_name": ctrl.db_name,
        "target_id": ctrl.target_id,
        "cluster_id": ctrl.cluster_id,
        "source_manifest_id": ctrl.source_manifest_id,
        "candidate_manifest_id": ctrl.target_manifest_id,
        "owner_epoch": ctrl.owner_epoch,
        "head": head,
    }
    temporary = directory / f"{nonce}.pending"
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, directory / f"{nonce}.request.json")
    replies_str = os.environ.get("CP_COMPAT_T051_REPLY_DIR", "")
    replies = Path(replies_str) if replies_str else None
    if replies is None or not replies.is_dir() or replies.is_symlink():
        raise DrainError("Independent host response mount is unavailable")
    reply_path = replies / f"{nonce}.response.json"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if reply_path.is_file():
            try:
                response = json.loads(reply_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DrainError("Invalid host shutdown evidence") from exc
            if (
                any(response.get(k) != v for k, v in payload.items())
                or response.get("ok") is not True
                or response.get("status") != "stopped"
                or response.get("stop_phase") != ctrl.phase
                or response.get("launch_sealed") is not True
                or response.get("running_consumer_count") != 0
                or frozenset(response.get("stopped_services", ())) != _CONSUMERS
                or len(response.get("stopped_services", ())) != len(_CONSUMERS)
            ):
                raise DrainError("Host shutdown evidence mismatches current target")
            return
        time.sleep(0.05)
    raise DrainError("Host shutdown evidence was not delivered")


def assert_exclusive_drain_permit(conn: psycopg.Connection, target_id: str) -> None:
    """Only the current owner backend may authorize DDL under a held drain lock."""
    key = advisory_key("permit", target_id)
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype='advisory' AND pid=pg_backend_pid()
              AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
              AND classid::bigint=%s AND objid::bigint=%s
              AND objsubid=1 AND mode='ExclusiveLock' AND granted
        ) AS held
        """,
        ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),
    ).fetchone()
    if not row or not row["held"]:
        raise DrainError("Current owner no longer holds exclusive drain permit")


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
    if current.phase not in ("CLOSED", "SOURCE_OPEN", "TARGET_OPEN", "DRAINING"):
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

    assert_exclusive_drain_permit(conn, target_id)

    assert_current_owner(
        conn, target_id=target_id, expected_epoch=owner_epoch, lock_mode="UPDATE"
    )
    assert_all_work_settled(conn, target_id=target_id)
    claimed = conn.execute(
        "SELECT count(*) AS n FROM public.delivery_jobs WHERE state = 'CLAIMED'"
    ).fetchone()
    if (claimed["n"] if isinstance(claimed, dict) else claimed[0]) != 0:
        raise DrainError("Claimed sender jobs prevent completion of drain")

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
    consumers_stopped: bool,
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

    claimed_row = observer_conn.execute(
        "SELECT count(*) AS n FROM public.delivery_jobs WHERE state = 'CLAIMED'"
    ).fetchone()
    claimed = int(claimed_row["n"] if isinstance(claimed_row, dict) else claimed_row[0])
    if claimed:
        raise DrainError("Claimed sender jobs remain during drain")

    sess_row = observer_conn.execute(
        """
        SELECT count(*) AS c
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND usename IN ('carepulse_runtime', 'carepulse_api', 'carepulse_web', 'carepulse_gate', 'carepulse_auth', 'carepulse_worker', 'carepulse_sender')
          AND pid <> pg_backend_pid()
        """
    ).fetchone()
    active_sessions = int(sess_row["c"] if isinstance(sess_row, dict) else sess_row[0])
    if active_sessions != 0:
        raise DrainError(
            f"Surviving product sessions remain during drain: {active_sessions}"
        )

    assert_host_consumers_stopped(observer_conn, ctrl)

    return DrainVerification(
        target_id=ctrl.target_id,
        owner_epoch=ctrl.owner_epoch,
        phase=ctrl.phase,
        active_generation=ctrl.active_generation,
        source_sealed=ctrl.source_sealed,
        active_product_sessions=active_sessions,
        unresolved_work_count=unresolved,
        claimed_delivery_jobs=claimed,
        consumers_stopped=consumers_stopped,
    )
