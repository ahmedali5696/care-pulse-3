"""Target-specific advisory owner lock and shared/exclusive invocation permits."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.rows import dict_row

ControlPhase = Literal[
    "CLOSED",
    "SOURCE_OPEN",
    "DRAINING",
    "MIGRATING",
    "VERIFYING",
    "TARGET_OPEN",
]
VALID_PHASES: frozenset[str] = frozenset(
    {"CLOSED", "SOURCE_OPEN", "DRAINING", "MIGRATING", "VERIFYING", "TARGET_OPEN"}
)


class ControlError(RuntimeError):
    """Raised when target control state, lock ownership, or permit acquisition fails."""


class ContentionError(ControlError):
    """Raised when a competing controller or active drain blocks acquisition."""


@dataclass(frozen=True)
class ControlRecord:
    """Snapshot of compat_control.target_control."""

    target_id: str
    cluster_id: str
    db_name: str
    phase: ControlPhase
    owner_epoch: int
    source_manifest_id: str
    target_manifest_id: str
    active_generation: str
    source_sealed: bool
    drain_verified_epoch: int | None


def advisory_key(namespace: str, target_id: str) -> int:
    """Derive a deterministic signed 64-bit PostgreSQL advisory lock key."""
    digest = hashlib.sha256(f"compat:{namespace}:{target_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def open_nonpooled_connection(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    autocommit: bool = False,
) -> psycopg.Connection:
    """Open a direct, nonpooled PostgreSQL connection for controller or observer use."""
    return psycopg.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        autocommit=autocommit,
        connect_timeout=5,
        row_factory=dict_row,
    )


def acquire_owner_lock(conn: psycopg.Connection, target_id: str) -> bool:
    """Attempt non-blocking session-scoped advisory owner lock on this nonpooled connection."""
    key = advisory_key("owner", target_id)
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()
    if not conn.autocommit:
        conn.commit()
    return bool(row and row["acquired"])


def release_owner_lock(conn: psycopg.Connection, target_id: str) -> bool:
    """Release the session-scoped advisory owner lock."""
    key = advisory_key("owner", target_id)
    row = conn.execute("SELECT pg_advisory_unlock(%s) AS released", (key,)).fetchone()
    if not conn.autocommit:
        conn.commit()
    return bool(row and row["released"])


def acquire_shared_permit(conn: psycopg.Connection, target_id: str) -> bool:
    """Try non-blocking shared session permit for an admitted invocation."""
    key = advisory_key("permit", target_id)
    row = conn.execute(
        "SELECT pg_try_advisory_lock_shared(%s) AS acquired", (key,)
    ).fetchone()
    return bool(row and row["acquired"])


def release_shared_permit(conn: psycopg.Connection, target_id: str) -> bool:
    """Release the shared session permit at invocation completion."""
    key = advisory_key("permit", target_id)
    row = conn.execute(
        "SELECT pg_advisory_unlock_shared(%s) AS released", (key,)
    ).fetchone()
    return bool(row and row["released"])


def acquire_exclusive_drain_permit(
    conn: psycopg.Connection, target_id: str, *, blocking: bool = True
) -> bool:
    """Acquire exclusive drain permit after committing DRAINING under row lock."""
    key = advisory_key("permit", target_id)
    if blocking:
        conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        if not conn.autocommit:
            conn.commit()
        return True
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()
    if not conn.autocommit:
        conn.commit()
    return bool(row and row["acquired"])


def release_exclusive_drain_permit(conn: psycopg.Connection, target_id: str) -> bool:
    """Release the exclusive drain permit."""
    key = advisory_key("permit", target_id)
    row = conn.execute("SELECT pg_advisory_unlock(%s) AS released", (key,)).fetchone()
    if not conn.autocommit:
        conn.commit()
    return bool(row and row["released"])


def read_control_record(
    conn: psycopg.Connection, *, lock_mode: Literal["NONE", "SHARE", "UPDATE"] = "NONE"
) -> ControlRecord:
    """Read the singleton control row with optional short row lock."""
    suffix = ""
    if lock_mode == "SHARE":
        suffix = " FOR SHARE"
    elif lock_mode == "UPDATE":
        suffix = " FOR UPDATE"
    rows = conn.execute(
        f"""
        SELECT target_id, cluster_id, db_name, phase, owner_epoch,
               source_manifest_id, target_manifest_id, active_generation,
               source_sealed, drain_verified_epoch
        FROM compat_control.target_control
        WHERE singleton_id = TRUE{suffix}
        """
    ).fetchall()
    if len(rows) != 1:
        raise ControlError(f"Expected exactly 1 target_control row, found {len(rows)}")
    row = rows[0]
    if row["phase"] not in VALID_PHASES:
        raise ControlError(f"Invalid control phase: {row['phase']!r}")
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
        drain_verified_epoch=(
            int(row["drain_verified_epoch"])
            if row["drain_verified_epoch"] is not None
            else None
        ),
    )


def claim_owner_epoch(
    conn: psycopg.Connection,
    *,
    target_id: str,
    expected_cluster_id: str,
    expected_db_name: str,
) -> ControlRecord:
    """Acquire owner advisory lock and monotonically increment owner_epoch under row lock."""
    if not acquire_owner_lock(conn, target_id):
        raise ContentionError(
            f"Competing controller already holds owner lock for {target_id}"
        )
    try:
        current = read_control_record(conn, lock_mode="UPDATE")
        if current.target_id != target_id:
            raise ControlError(
                f"Target mismatch: expected {target_id}, got {current.target_id}"
            )
        if (
            current.cluster_id != expected_cluster_id
            or current.db_name != expected_db_name
        ):
            raise ControlError(
                "Cluster/database identity mismatch while claiming owner epoch"
            )
        row = conn.execute(
            """
            UPDATE compat_control.target_control
            SET owner_epoch = owner_epoch + 1,
                updated_at = now()
            WHERE singleton_id = TRUE AND target_id = %s
            RETURNING target_id, cluster_id, db_name, phase, owner_epoch,
                      source_manifest_id, target_manifest_id, active_generation,
                      source_sealed, drain_verified_epoch
            """,
            (target_id,),
        ).fetchone()
        conn.commit()
        assert row is not None
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
            drain_verified_epoch=(
                int(row["drain_verified_epoch"])
                if row["drain_verified_epoch"] is not None
                else None
            ),
        )
    except Exception:
        if not conn.autocommit:
            conn.rollback()
        release_owner_lock(conn, target_id)
        raise


def assert_current_owner(
    conn: psycopg.Connection,
    *,
    target_id: str,
    expected_epoch: int,
    lock_mode: Literal["NONE", "UPDATE"] = "UPDATE",
) -> ControlRecord:
    """Verify this nonpooled controller still owns the target lock and current epoch."""
    current = read_control_record(conn, lock_mode=lock_mode)
    if current.target_id != target_id:
        if not conn.autocommit:
            conn.rollback()
        raise ContentionError(
            f"Target mismatch: expected {target_id}, got {current.target_id}"
        )
    if current.owner_epoch != expected_epoch:
        if not conn.autocommit:
            conn.rollback()
        raise ContentionError(
            f"Superseded controller epoch: held {expected_epoch}, current {current.owner_epoch}"
        )
    return current


def inspect_as_competitor(conn: psycopg.Connection, *, target_id: str) -> ControlRecord:
    """Read-only inspection for a contending controller that does not hold the owner lock."""
    current = read_control_record(conn, lock_mode="NONE")
    if current.target_id != target_id:
        raise ControlError(
            f"Target mismatch: expected {target_id}, got {current.target_id}"
        )
    return current
