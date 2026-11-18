"""Durable opaque outstanding work evidence and disposition tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg

WorkDisposition = Literal[
    "PENDING", "COMMITTED", "ROLLED_BACK", "SENT", "NOT_SENT", "UNKNOWN"
]
SETTLED_DISPOSITIONS: frozenset[str] = frozenset(
    {"COMMITTED", "ROLLED_BACK", "SENT", "NOT_SENT"}
)
UNRESOLVED_DISPOSITIONS: frozenset[str] = frozenset({"PENDING", "UNKNOWN"})


class WorkEvidenceError(RuntimeError):
    """Raised when work evidence is missing, contradictory, or unresolvable."""


@dataclass(frozen=True)
class WorkAttemptRecord:
    """One persisted attempt or external-effect reference in compat_control.work_evidence."""

    attempt_id: str
    target_id: str
    owner_epoch: int
    generation: str
    service: str
    invocation_class: str
    correlation_id: str | None
    lease_generation: int | None
    disposition: WorkDisposition
    created_by_role: str


def list_work_attempts(
    conn: psycopg.Connection,
    *,
    target_id: str,
    only_unresolved: bool = False,
) -> list[WorkAttemptRecord]:
    """Read work_evidence rows for a target."""
    clause = " AND disposition IN ('PENDING', 'UNKNOWN')" if only_unresolved else ""
    rows = conn.execute(
        f"""
        SELECT attempt_id, target_id, owner_epoch, generation, service,
               invocation_class, correlation_id, lease_generation, disposition,
               created_by_role
        FROM compat_control.work_evidence
        WHERE target_id = %s{clause}
        ORDER BY created_at ASC, attempt_id ASC
        """,
        (target_id,),
    ).fetchall()
    result: list[WorkAttemptRecord] = []
    for r in rows:
        def _get(k: str, i: int, row=r):
            return row[k] if isinstance(row, dict) else row[i]

        result.append(
            WorkAttemptRecord(
                attempt_id=_get("attempt_id", 0),
                target_id=_get("target_id", 1),
                owner_epoch=int(_get("owner_epoch", 2)),
                generation=_get("generation", 3),
                service=_get("service", 4),
                invocation_class=_get("invocation_class", 5),
                correlation_id=_get("correlation_id", 6),
                lease_generation=(
                    int(_get("lease_generation", 7))
                    if _get("lease_generation", 7) is not None
                    else None
                ),
                disposition=_get("disposition", 8),
                created_by_role=_get("created_by_role", 9),
            )
        )
    return result


def has_unresolved_work(conn: psycopg.Connection, *, target_id: str) -> bool:
    """Return True if any PENDING or UNKNOWN work exists for the target."""
    return len(list_work_attempts(conn, target_id=target_id, only_unresolved=True)) > 0


def assert_all_work_settled(conn: psycopg.Connection, *, target_id: str) -> None:
    """Fail closed if any PENDING or UNKNOWN work remains."""
    unresolved = list_work_attempts(conn, target_id=target_id, only_unresolved=True)
    if unresolved:
        summary = ", ".join(
            f"{w.service}:{w.invocation_class}:{w.disposition}" for w in unresolved[:5]
        )
        raise WorkEvidenceError(
            f"Unresolved work blocks transition ({len(unresolved)}): {summary}"
        )


def mark_work_unknown(
    conn: psycopg.Connection,
    *,
    attempt_id: str,
    target_id: str,
) -> None:
    """Mark an interrupted or timed-out attempt as UNKNOWN (never auto-settled)."""
    cur = conn.execute(
        """
        UPDATE compat_control.work_evidence
        SET disposition = 'UNKNOWN',
            updated_at = now()
        WHERE attempt_id = %s AND target_id = %s AND disposition IN ('PENDING', 'UNKNOWN')
        """,
        (attempt_id, target_id),
    )
    if cur.rowcount != 1:
        raise WorkEvidenceError(
            f"Cannot mark attempt {attempt_id} UNKNOWN (missing or already settled)"
        )
    if not conn.autocommit:
        conn.commit()


def resolve_work_with_proof(
    conn: psycopg.Connection,
    *,
    attempt_id: str,
    target_id: str,
    proven_disposition: Literal["COMMITTED", "ROLLED_BACK", "SENT", "NOT_SENT"],
    correlation_id: str | None = None,
) -> None:
    """Settle an attempt ONLY with an independently proven outcome (never on timeout/lease expiry)."""
    if proven_disposition not in SETTLED_DISPOSITIONS:
        raise WorkEvidenceError(f"Invalid settled disposition: {proven_disposition!r}")
    cur = conn.execute(
        """
        UPDATE compat_control.work_evidence
        SET disposition = %s,
            correlation_id = COALESCE(%s, correlation_id),
            updated_at = now()
        WHERE attempt_id = %s AND target_id = %s
        """,
        (proven_disposition, correlation_id, attempt_id, target_id),
    )
    if cur.rowcount != 1:
        raise WorkEvidenceError(
            f"Attempt {attempt_id} not found for target {target_id}"
        )
    if not conn.autocommit:
        conn.commit()
