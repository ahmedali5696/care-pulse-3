"""Fenced controller takeover and conservative 0040-origin recovery classification (T043/T044).

The returned branch is a plan, not permission to migrate, restore credentials, or reopen.
Verification and release are separate T045-T048 operations.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

import psycopg

from ops.compat.control import (
    ControlRecord,
    acquire_exclusive_drain_permit,
    assert_current_owner,
    claim_owner_epoch,
    open_nonpooled_connection,
    read_control_record,
)
from ops.compat.manifest import SOURCE_HEAD, GenerationManifest, verify_manifest_pair
from ops.compat.migrate import EXPECTED_TRANSITIONS
from ops.compat.probes import probe_alembic_head, probe_target_identity
from ops.compat.work import list_work_attempts


class RecoveryError(RuntimeError):
    """Observed recovery state is unsafe or insufficiently verified."""


@dataclass(frozen=True)
class ExecutedUS2Checkpoint:
    """Trusted operator-supplied CI evidence; a completed task checkbox is insufficient."""

    run_id: int
    commit_sha: str
    passed: int
    total: int
    cleanup_passed: bool


RecoveryBranch = Literal["SOURCE_REVERIFY", "FORWARD_ONLY", "TARGET_VERIFY_ONLY"]


@dataclass(frozen=True)
class RecoveryTakeover:
    """Owner backend remains open and locked only inside recover_owner()'s context."""

    owner_conn: psycopg.Connection
    control: ControlRecord
    committed_head: str
    branch: RecoveryBranch
    next_successor: str | None


def _require_checkpoint(checkpoint: ExecutedUS2Checkpoint) -> None:
    # The verified executable US2 checkpoint, not a caller-controlled success boolean.
    if (
        not isinstance(checkpoint, ExecutedUS2Checkpoint)
        or checkpoint.run_id != 44424355381
        or checkpoint.commit_sha != "835a257aa8374c50d615bf7b75676cccb49137e7"
        or checkpoint.passed != 5
        or checkpoint.total != 5
        or checkpoint.cleanup_passed is not True
    ):
        raise RecoveryError("Executed T042 US2 checkpoint is missing or untrusted")


def _classify(control: ControlRecord, head: str) -> tuple[RecoveryBranch, str | None]:
    if control.owner_epoch < 1 or control.active_generation != "none":
        raise RecoveryError("Expected a previously owned, fenced cutover event")
    if control.phase in ("SOURCE_OPEN", "TARGET_OPEN"):
        raise RecoveryError(
            "Open or ambiguous reopen must be separately fenced under T049"
        )
    if control.phase not in ("CLOSED", "DRAINING", "MIGRATING", "VERIFYING"):
        raise RecoveryError("Unrecognized recovery phase")

    if head == SOURCE_HEAD:
        if control.phase == "VERIFYING":
            raise RecoveryError("0040 cannot be in candidate verification")
        # Zero successor commits is not proof of still-valid original credentials.
        return "SOURCE_REVERIFY", None

    if not control.source_sealed:
        raise RecoveryError("Committed successor without proven source seal")
    if head in EXPECTED_TRANSITIONS:
        if control.phase not in ("CLOSED", "MIGRATING"):
            raise RecoveryError("Intermediate revision has an inconsistent phase")
        return "FORWARD_ONLY", EXPECTED_TRANSITIONS[head]

    if head == "0046_f01_audit_integrity":
        if control.phase not in ("CLOSED", "MIGRATING", "VERIFYING"):
            raise RecoveryError("0046 has an inconsistent verification phase")
        return "TARGET_VERIFY_ONLY", None

    raise RecoveryError("Unsupported or unknown committed revision")


def _assert_no_unresolved_work(conn: psycopg.Connection, target_id: str) -> None:
    unresolved = list_work_attempts(conn, target_id=target_id, only_unresolved=True)
    if unresolved:
        raise RecoveryError(
            f"Recovery blocked by {len(unresolved)} pending/unknown attempts"
        )
    claimed = conn.execute(
        "SELECT count(*) AS n FROM public.delivery_jobs WHERE state = 'CLAIMED'"
    ).fetchone()["n"]
    if claimed:
        raise RecoveryError(f"Recovery blocked by {claimed} claimed sender jobs")
    active = conn.execute(
        """
        SELECT count(*) AS n FROM pg_stat_activity
        WHERE datname = current_database()
          AND usename IN ('carepulse_runtime', 'carepulse_api', 'carepulse_web',
                         'carepulse_gate', 'carepulse_auth', 'carepulse_worker',
                         'carepulse_sender')
          AND (state <> 'idle' OR xact_start IS NOT NULL)
        """
    ).fetchone()["n"]
    if active:
        raise RecoveryError(f"Recovery blocked by {active} active product backends")


@contextmanager
def recover_owner(
    *,
    owner_connection: Mapping[str, str | int],
    observer_connection: Mapping[str, str | int],
    source_manifest: GenerationManifest,
    target_manifest: GenerationManifest,
    executed_us2: ExecutedUS2Checkpoint,
) -> Iterator[RecoveryTakeover]:
    """Claim a fresh, nonpooled owner epoch for an already fenced 0040-origin event.

    Caller must supply verified, trusted manifest and CI evidence; the observer
    login needs pg_control_system and visibility into work/pg_stat_activity.
    Neither unknown work nor an open/ambiguous phase is automatically repaired.
    """
    _require_checkpoint(executed_us2)
    verify_manifest_pair(source_manifest, target_manifest)
    binding = source_manifest.target_binding
    required_connection_keys = {"host", "port", "dbname", "user", "password"}
    if (
        set(owner_connection) != required_connection_keys
        or set(observer_connection) != required_connection_keys
        or owner_connection.get("dbname") != binding.db_name
        or observer_connection.get("dbname") != binding.db_name
        or owner_connection.get("user") != "carepulse_migration"
    ):
        raise RecoveryError("Recovery connections do not match the reviewed target")

    owner = open_nonpooled_connection(**owner_connection)
    try:
        observer = open_nonpooled_connection(**observer_connection, autocommit=True)
        try:
            owner_role = owner.execute(
                "SELECT session_user::text AS login, current_user::text AS effective"
            ).fetchone()
            if owner_role != {
                "login": "carepulse_migration",
                "effective": "carepulse_migration",
            }:
                raise RecoveryError(
                    "Owner must use the direct migration login and role"
                )
            observed = probe_target_identity(
                observer,
                expected_cluster_id=binding.cluster_id,
                expected_db_name=binding.db_name,
            )
            if observed.session_user != observed.current_user:
                raise RecoveryError("Observer is running under a substituted role")
            # A unique session marker proves the owner backend is on the *same*
            # physical PostgreSQL cluster as the independent identity observer.
            marker = f"compat-recovery-{uuid.uuid4().hex}"
            owner.execute("SELECT set_config('application_name', %s, false)", (marker,))
            owner.commit()
            same_backend = observer.execute(
                """
                SELECT 1 FROM pg_stat_activity
                WHERE pid = %s AND datname = %s AND usename = 'carepulse_migration'
                  AND application_name = %s
                """,
                (owner.info.backend_pid, binding.db_name, marker),
            ).fetchone()
            if not same_backend:
                raise RecoveryError(
                    "Independent observer cannot identify the owner backend"
                )

            before = read_control_record(observer)
            owner_before = read_control_record(owner)
            head = probe_alembic_head(observer)
            if probe_alembic_head(owner) != head or owner_before != before:
                raise RecoveryError("Independent control/head observations disagree")
            if (
                before.target_id != binding.target_id
                or before.cluster_id != binding.cluster_id
                or before.db_name != binding.db_name
                or before.source_manifest_id != source_manifest.manifest_id
                or before.target_manifest_id != target_manifest.manifest_id
            ):
                raise RecoveryError(
                    "Existing 0040-origin target/event binding mismatch"
                )
            branch, successor = _classify(before, head)
            _assert_no_unresolved_work(observer, binding.target_id)

            # Existing control helper claims the session advisory lock and commits
            # exactly one new epoch; no phase, grant, key or domain data is changed.
            claimed = claim_owner_epoch(
                owner,
                target_id=binding.target_id,
                expected_cluster_id=binding.cluster_id,
                expected_db_name=binding.db_name,
            )
            if (
                claimed.owner_epoch != before.owner_epoch + 1
                or claimed.phase != before.phase
                or claimed.active_generation != "none"
            ):
                raise RecoveryError("Takeover changed the observed fenced phase")
            assert_current_owner(
                owner,
                target_id=binding.target_id,
                expected_epoch=claimed.owner_epoch,
                lock_mode="NONE",
            )
            if not acquire_exclusive_drain_permit(
                owner, binding.target_id, blocking=False
            ):
                raise RecoveryError("Earlier invocation still owns a work permit")

            after = read_control_record(observer)
            if (
                after != claimed
                or probe_alembic_head(observer) != head
                or _classify(after, head) != (branch, successor)
            ):
                raise RecoveryError("Durable head/control changed during takeover")
            _assert_no_unresolved_work(observer, binding.target_id)
            yield RecoveryTakeover(owner, claimed, head, branch, successor)
        finally:
            observer.close()
    finally:
        # Closing the physical session drops both the owner and exclusive permits.
        owner.close()
