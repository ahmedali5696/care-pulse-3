"""Single-unit owner-held Alembic migration and independent observer readback (T038, T039)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import tuple_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from ops.compat.control import assert_current_owner, read_control_record
from ops.compat.drain import (
    DrainVerification,
    assert_exclusive_drain_permit,
    observe_drain_complete,
)
from ops.compat.preflight import (
    PUBLISHED_SUCCESSORS,
    PreflightReport,
    verify_populated_source_basis,
)
from ops.compat.probes import (
    probe_alembic_head,
    probe_effective_table_privileges,
    probe_rls_and_triggers,
    probe_target_identity,
)
from ops.compat.work import assert_all_work_settled


class MigrationStepError(RuntimeError):
    """Raised when a migration step or independent observer readback fails."""


class _BorrowedOwnerDbapi:
    """Expose tuple-row DBAPI cursors without handing off the owner's session."""

    def __init__(self, owner: psycopg.Connection) -> None:
        self._owner = owner

    def cursor(self, *args, **kwargs):
        kwargs.setdefault("row_factory", tuple_row)
        return self._owner.cursor(*args, **kwargs)

    def close(self) -> None:
        # SQLAlchemy must never terminate the controller's advisory-lock session.
        pass

    def __getattr__(self, name: str):
        return getattr(self._owner, name)


EXPECTED_TRANSITIONS: dict[str, str] = {
    "0040_t173_numeric_money": "0041_f01_runtime_identity",
    "0041_f01_runtime_identity": "0042_f01_effective_grants",
    "0042_f01_effective_grants": "0043_f01_writer_boundary",
    "0043_f01_writer_boundary": "0044_f01_delivery_acl",
    "0044_f01_delivery_acl": "0045_f01_revocation_evidence",
    "0045_f01_revocation_evidence": "0046_f01_audit_integrity",
}


@dataclass(frozen=True)
class StepObservation:
    """Independently observed state after one committed successor."""

    target_id: str
    owner_epoch: int
    committed_head: str
    phase: str


def _recheck_migration_boundary(
    owner_conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    target_id: str,
    owner_epoch: int,
    expected_head: str,
    expected_phase: str,
    preflight: PreflightReport,
) -> None:
    """Independently recheck the sealed target and retained source before DDL."""
    ctrl = assert_current_owner(
        owner_conn,
        target_id=target_id,
        expected_epoch=owner_epoch,
        lock_mode="UPDATE",
    )
    assert_exclusive_drain_permit(owner_conn, target_id)
    ident = probe_target_identity(
        observer_conn,
        expected_cluster_id=preflight.cluster_id,
        expected_db_name=preflight.db_name,
    )
    observed = read_control_record(observer_conn)
    if (
        ident.db_name != preflight.db_name
        or ctrl != observed
        or ctrl.phase != expected_phase
        or ctrl.active_generation != "none"
        or not ctrl.source_sealed
        or ctrl.drain_verified_epoch != owner_epoch
        or ctrl.source_manifest_id != preflight.source_manifest_id
        or ctrl.target_manifest_id != preflight.target_manifest_id
    ):
        raise MigrationStepError(
            "Sealed owner, source manifest, or drain proof changed"
        )
    probe_alembic_head(owner_conn, expected_head=expected_head)
    probe_alembic_head(observer_conn, expected_head=expected_head)
    assert_all_work_settled(observer_conn, target_id=target_id)
    claimed = observer_conn.execute(
        "SELECT count(*) AS n FROM public.delivery_jobs WHERE state='CLAIMED'"
    ).fetchone()["n"]
    sessions = observer_conn.execute(
        """
        SELECT count(*) AS n FROM pg_stat_activity
        WHERE datname=current_database() AND usename IN
          ('carepulse_runtime','carepulse_api','carepulse_web','carepulse_worker',
           'carepulse_gate','carepulse_auth','carepulse_sender')
        """
    ).fetchone()["n"]
    if claimed or sessions:
        raise MigrationStepError(
            "Claims or surviving service sessions prevent migration"
        )
    verify_populated_source_basis(
        observer_conn,
        preflight.source_baseline,
        preflight.target_id,
        preflight.cluster_id,
        preflight.db_name,
    )


def enter_migrating_phase(
    conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    target_id: str,
    owner_epoch: int,
    preflight: PreflightReport,
    drain_proof: DrainVerification,
) -> None:
    """Transition DRAINING -> MIGRATING under current owner lock and verified drain."""
    ctrl = assert_current_owner(
        conn, target_id=target_id, expected_epoch=owner_epoch, lock_mode="UPDATE"
    )
    if ctrl.phase not in ("DRAINING", "MIGRATING"):
        raise MigrationStepError(f"Cannot enter MIGRATING from {ctrl.phase}")
    if ctrl.drain_verified_epoch != owner_epoch:
        raise MigrationStepError("Drain not verified for current owner epoch")
    if drain_proof.owner_epoch != owner_epoch or not drain_proof.consumers_stopped:
        raise MigrationStepError("No verified stopped-consumer proof")
    observe_drain_complete(
        observer_conn,
        target_id=target_id,
        expected_epoch=owner_epoch,
        consumers_stopped=drain_proof.consumers_stopped,
        require_source_sealed=True,
    )
    _recheck_migration_boundary(
        conn,
        observer_conn,
        target_id=target_id,
        owner_epoch=owner_epoch,
        expected_head=preflight.head,
        expected_phase=ctrl.phase,
        preflight=preflight,
    )
    assert_all_work_settled(conn, target_id=target_id)
    conn.execute(
        """
        UPDATE compat_control.target_control
        SET phase = 'MIGRATING',
            active_generation = 'none',
            updated_at = now()
        WHERE singleton_id = TRUE AND target_id = %s AND owner_epoch = %s
        """,
        (target_id, owner_epoch),
    )
    conn.commit()


def advance_one_successor(
    owner_conn: psycopg.Connection,
    observer_conn: psycopg.Connection,
    *,
    target_id: str,
    owner_epoch: int,
    expected_predecessor: str,
    target_successor: str,
    preflight: PreflightReport,
    alembic_ini: Path | None = None,
) -> str:
    """Advance exactly one published successor on the same owner-lock backend connection (T038)."""
    if target_successor not in PUBLISHED_SUCCESSORS:
        raise MigrationStepError(f"Unapproved successor revision: {target_successor!r}")
    if EXPECTED_TRANSITIONS.get(expected_predecessor) != target_successor:
        raise MigrationStepError(
            f"Invalid transition {expected_predecessor} -> {target_successor}"
        )

    ctrl = assert_current_owner(
        owner_conn, target_id=target_id, expected_epoch=owner_epoch, lock_mode="UPDATE"
    )
    if ctrl.phase != "MIGRATING" or ctrl.active_generation != "none":
        raise MigrationStepError(
            f"Migration requires MIGRATING/none, observed {ctrl.phase}/{ctrl.active_generation}"
        )
    _recheck_migration_boundary(
        owner_conn,
        observer_conn,
        target_id=target_id,
        owner_epoch=owner_epoch,
        expected_head=expected_predecessor,
        expected_phase="MIGRATING",
        preflight=preflight,
    )
    actual_head = probe_alembic_head(owner_conn, expected_head=expected_predecessor)
    assert actual_head == expected_predecessor
    owner_conn.commit()

    # Supply the exact same owner-lock backend connection to Alembic (no child migrator).
    ini_path = alembic_ini or (Path(__file__).resolve().parents[2] / "alembic.ini")
    cfg = Config(str(ini_path))
    sa_engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: _BorrowedOwnerDbapi(owner_conn),
        poolclass=NullPool,
        use_native_hstore=False,
        hide_parameters=True,
    )
    try:
        # Alembic's env.py has its own begin_transaction(). Supply an already
        # active outer unit so a failure after its DDL cannot commit before the
        # owner acknowledges this successor.
        with sa_engine.connect() as sa_conn, sa_conn.begin():
            cfg.attributes["connection"] = sa_conn
            command.upgrade(cfg, target_successor)
    except Exception:
        if not owner_conn.closed and not owner_conn.autocommit:
            owner_conn.rollback()
        raise
    finally:
        sa_engine.dispose()

    return target_successor


def observe_committed_successor(
    observer_conn: psycopg.Connection,
    *,
    target_id: str,
    expected_epoch: int,
    expected_head: str,
) -> StepObservation:
    """Independently read back committed revision and per-revision effective security (T039)."""
    actual_head = probe_alembic_head(observer_conn, expected_head=expected_head)
    ctrl = observer_conn.execute(
        """
        SELECT target_id, phase, owner_epoch
        FROM compat_control.target_control
        WHERE singleton_id = TRUE
        """
    ).fetchone()
    if ctrl is None:
        raise MigrationStepError("Missing control record on observer readback")
    t_id = ctrl["target_id"] if isinstance(ctrl, dict) else ctrl[0]
    phase = ctrl["phase"] if isinstance(ctrl, dict) else ctrl[1]
    epoch = int(ctrl["owner_epoch"] if isinstance(ctrl, dict) else ctrl[2])
    if t_id != target_id or epoch != expected_epoch:
        raise MigrationStepError("Observer readback target/epoch mismatch")

    if actual_head >= "0042_f01_effective_grants":
        privs = probe_effective_table_privileges(
            observer_conn,
            "carepulse_runtime",
            "public.alembic_version",
            ("SELECT", "INSERT", "UPDATE", "DELETE"),
        )
        if any(privs.values()):
            raise MigrationStepError(
                "carepulse_runtime retained unexpected privileges on public.alembic_version at >=0042"
            )
    if actual_head >= "0041_f01_runtime_identity":
        probe_rls_and_triggers(
            observer_conn,
            required_force_rls_tables=("public.organizations", "public.patients"),
        )

    return StepObservation(
        target_id=t_id,
        owner_epoch=epoch,
        committed_head=actual_head,
        phase=phase,
    )
