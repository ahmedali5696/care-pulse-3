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
    acquire_owner_lock,
    assert_current_owner,
    bind_refenced_owner_epoch,
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


@dataclass(frozen=True)
class FencedReopen:
    """Durable closure after an ambiguous reopen, with owner backend retained."""

    owner_conn: psycopg.Connection
    control: ControlRecord
    previous_epoch: int
    previous_generation: str
    committed_head: str
    unresolved_work: int
    surviving_sessions: int


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
        if control.phase not in ("CLOSED", "DRAINING", "MIGRATING", "VERIFYING"):
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
            # The predecessor epoch's drain stamp cannot authorize work in a
            # new controller epoch. Re-establish it only after a fresh observer
            # proves the exclusive permit, no unsettled work and no surviving
            # physical source/candidate pool (including idle connections).
            survivors = observer.execute(
                """
                SELECT count(*) AS n FROM pg_stat_activity
                WHERE datname=current_database()
                  AND usename IN ('carepulse_runtime', 'carepulse_api',
                                  'carepulse_web', 'carepulse_gate',
                                  'carepulse_auth', 'carepulse_worker',
                                  'carepulse_sender')
                """
            ).fetchone()["n"]
            if survivors or (head != SOURCE_HEAD and not after.source_sealed):
                raise RecoveryError(
                    "Unsealed predecessor pool or source is still reachable"
                )
            current = assert_current_owner(
                owner,
                target_id=binding.target_id,
                expected_epoch=claimed.owner_epoch,
                lock_mode="UPDATE",
            )
            if current != after or probe_alembic_head(owner) != head:
                owner.rollback()
                raise RecoveryError(
                    "Owner or durable head changed before drain revalidation"
                )
            _assert_no_unresolved_work(owner, binding.target_id)
            changed = owner.execute(
                """
                UPDATE compat_control.target_control
                SET drain_verified_epoch = %s, updated_at=now()
                WHERE singleton_id=TRUE AND target_id=%s AND owner_epoch=%s
                  AND phase=%s AND active_generation='none'
                RETURNING owner_epoch
                """,
                (
                    claimed.owner_epoch,
                    binding.target_id,
                    claimed.owner_epoch,
                    claimed.phase,
                ),
            ).fetchone()
            if not changed or changed["owner_epoch"] != claimed.owner_epoch:
                owner.rollback()
                raise RecoveryError("Drain proof rebind CAS failed")
            owner.commit()
            witnessed = read_control_record(observer)
            if (
                witnessed.drain_verified_epoch != claimed.owner_epoch
                or witnessed.owner_epoch != claimed.owner_epoch
                or probe_alembic_head(observer) != head
            ):
                raise RecoveryError("New owner epoch lacks durable drain proof")
            yield RecoveryTakeover(owner, witnessed, head, branch, successor)
        finally:
            observer.close()
    finally:
        # Closing the physical session drops both the owner and exclusive permits.
        owner.close()


@contextmanager
def recover_ambiguous_open(
    *,
    owner_connection: Mapping[str, str | int],
    observer_connection: Mapping[str, str | int],
    source_manifest: GenerationManifest,
    target_manifest: GenerationManifest,
    executed_us2: ExecutedUS2Checkpoint,
    expected_open_epoch: int,
    expected_open_generation: Literal["source", "candidate"],
) -> Iterator[FencedReopen]:
    """Re-fence an OPEN target and advance epoch in the SAME row-locked transaction.

    A lost reopen acknowledgement may hide accepted business work. Close new
    ingress first; report unsettled prior work for independent reconciliation.
    This call never resends mail, replays business work, or reopens traffic.
    """
    _require_checkpoint(executed_us2)
    verify_manifest_pair(source_manifest, target_manifest)
    if expected_open_generation not in ("source", "candidate"):
        raise RecoveryError("Unknown generation on ambiguous reopen")
    binding = source_manifest.target_binding
    if (
        owner_connection.get("dbname") != binding.db_name
        or observer_connection.get("dbname") != binding.db_name
        or owner_connection.get("user") != "carepulse_migration"
        or expected_open_epoch < 1
    ):
        raise RecoveryError("Ambiguous reopen target or owner binding is invalid")
    previous_phase = (
        "SOURCE_OPEN" if expected_open_generation == "source" else "TARGET_OPEN"
    )
    expected_head = (
        SOURCE_HEAD
        if expected_open_generation == "source"
        else "0046_f01_audit_integrity"
    )
    owner = open_nonpooled_connection(**owner_connection)
    try:
        observer = open_nonpooled_connection(**observer_connection, autocommit=True)
        try:
            probe_target_identity(
                observer,
                expected_cluster_id=binding.cluster_id,
                expected_db_name=binding.db_name,
            )
            owner.execute(
                "SELECT set_config('application_name', %s, false)",
                (f"compat-lost-open-{uuid.uuid4().hex}",),
            )
            owner.commit()
            marker = owner.execute(
                "SELECT current_setting('application_name') AS name"
            ).fetchone()["name"]
            owner.commit()
            if not observer.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid=%s AND datname=%s AND usename='carepulse_migration' AND application_name=%s",
                (owner.info.backend_pid, binding.db_name, marker),
            ).fetchone():
                raise RecoveryError("Owner backend is not on the observed cluster")
            if not acquire_owner_lock(owner, binding.target_id):
                raise RecoveryError("Previous controller is still owner; inspect only")
            current = read_control_record(owner, lock_mode="UPDATE")
            if (
                current.target_id != binding.target_id
                or current.cluster_id != binding.cluster_id
                or current.db_name != binding.db_name
                or current.source_manifest_id != source_manifest.manifest_id
                or current.target_manifest_id != target_manifest.manifest_id
                or current.owner_epoch != expected_open_epoch
                or current.phase != previous_phase
                or current.active_generation != expected_open_generation
                or probe_alembic_head(owner) != expected_head
            ):
                owner.rollback()
                raise RecoveryError("The observed OPEN event or revision has changed")
            row = owner.execute(
                """
                UPDATE compat_control.target_control
                SET owner_epoch=owner_epoch+1, phase='DRAINING',
                    active_generation='none', drain_verified_epoch=NULL,
                    updated_at=now()
                WHERE singleton_id=TRUE AND target_id=%s AND owner_epoch=%s
                  AND phase=%s AND active_generation=%s
                  AND source_manifest_id=%s AND target_manifest_id=%s
                  AND EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num=%s)
                RETURNING owner_epoch
                """,
                (
                    binding.target_id,
                    expected_open_epoch,
                    previous_phase,
                    expected_open_generation,
                    source_manifest.manifest_id,
                    target_manifest.manifest_id,
                    expected_head,
                ),
            ).fetchone()
            if not row or row["owner_epoch"] != expected_open_epoch + 1:
                owner.rollback()
                raise RecoveryError("Re-fence compare-and-set lost ownership")
            owner.commit()

            # The fence is now durable. Outstanding earlier work must settle
            # under its original attempt/lease before ANY later release.
            fenced = read_control_record(observer)
            if (
                fenced.target_id != binding.target_id
                or fenced.owner_epoch != expected_open_epoch + 1
                or fenced.phase != "DRAINING"
                or fenced.active_generation != "none"
                or fenced.drain_verified_epoch is not None
                or probe_alembic_head(observer) != expected_head
            ):
                raise RecoveryError("Lost closure acknowledgement; leave fenced")
            bind_refenced_owner_epoch(
                owner,
                target_id=binding.target_id,
                expected_epoch=fenced.owner_epoch,
            )
            unknown = observer.execute(
                "SELECT count(*) AS n FROM compat_control.work_evidence WHERE target_id=%s AND disposition IN ('PENDING','UNKNOWN')",
                (binding.target_id,),
            ).fetchone()["n"]
            claimed = observer.execute(
                "SELECT count(*) AS n FROM public.delivery_jobs WHERE state='CLAIMED'"
            ).fetchone()["n"]
            live = observer.execute(
                """
                SELECT count(*) AS n FROM pg_stat_activity
                WHERE datname=current_database()
                  AND usename IN ('carepulse_runtime','carepulse_api','carepulse_web',
                                  'carepulse_worker','carepulse_gate','carepulse_auth',
                                  'carepulse_sender')
                """
            ).fetchone()["n"]
            yield FencedReopen(
                owner,
                fenced,
                expected_open_epoch,
                expected_open_generation,
                expected_head,
                int(unknown) + int(claimed),
                int(live),
            )
        finally:
            observer.close()
    finally:
        owner.close()
