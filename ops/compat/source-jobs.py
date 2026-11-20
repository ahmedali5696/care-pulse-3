"""Read-only predecessor worker & one-shot sender startup wrappers for immutable 0040 images (T022).

Launched via:
  python /run/compat-adapters/source-jobs.py worker
  python /run/compat-adapters/source-jobs.py sender --organization-id <uuid> [--job-id <uuid>]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid

from carepulse.config import load_config
from carepulse.db import create_db_engine
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

SOURCE_HEAD = "0040_t173_numeric_money"


def _permit_key(target_id: str) -> int:
    digest = hashlib.sha256(f"compat:permit:{target_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _compat_settings() -> tuple[str, str]:
    target_id = os.environ.get("CP_COMPAT_TARGET_ID", "").strip()
    manifest_id = os.environ.get("CP_COMPAT_MANIFEST_ID", "").strip()
    generation = os.environ.get("CP_COMPAT_GENERATION", "").strip()
    expected_head = os.environ.get("CP_COMPAT_EXPECTED_HEAD", "").strip()
    if (
        not target_id
        or not manifest_id
        or generation != "source"
        or expected_head != SOURCE_HEAD
    ):
        raise RuntimeError("Invalid source job compatibility binding")
    return target_id, manifest_id


def _verify_job_connection(
    conn, target_id: str, manifest_id: str, db_name: str, expected_role: str
) -> int:
    row = (
        conn.execute(
            text(
                """
            SELECT current_database() AS db_name,
                   session_user::text AS session_user,
                   current_user::text AS current_user
            """
            )
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or row["db_name"] != db_name
        or row["session_user"] != expected_role
        or row["current_user"] != expected_role
    ):
        raise RuntimeError("Source job role/database mismatch")
    ctrl = (
        conn.execute(
            text(
                """
            SELECT target_id, db_name, phase, owner_epoch,
                   source_manifest_id, active_generation, source_sealed, alembic_head
            FROM compat_control.read_target_control()
            """
            )
        )
        .mappings()
        .first()
    )
    if (
        ctrl is None
        or ctrl["target_id"] != target_id
        or ctrl["db_name"] != db_name
        or ctrl["alembic_head"] != SOURCE_HEAD
        or ctrl["phase"] != "SOURCE_OPEN"
        or ctrl["active_generation"] != "source"
        or ctrl["source_sealed"]
        or ctrl["source_manifest_id"] != manifest_id
    ):
        raise RuntimeError("Source job fenced")
    return int(ctrl["owner_epoch"])


def _run_source_worker() -> int:
    """Preserve original heartbeat-only worker with compatibility-guarded ready probe."""
    from apps.worker.status import (
        HEARTBEAT_INTERVAL_SECONDS,
        STATUS_PATH,
        process_identity,
        write_status,
    )

    load_config("worker")
    target_id, manifest_id = _compat_settings()
    db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
    engine = create_db_engine(load_config("worker"))
    started = process_identity(os.getpid())
    write_status(STATUS_PATH, started, False)
    try:
        while True:
            ready = False
            try:
                with engine.connect() as conn:
                    _verify_job_connection(
                        conn, target_id, manifest_id, db_name, "carepulse_runtime"
                    )
                ready = True
            except Exception:  # noqa: BLE001 - unknown probe failures must mark worker unready
                ready = False
            write_status(STATUS_PATH, started, ready)
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    finally:
        engine.dispose()


def _run_source_sender(argv: list[str]) -> int:
    """Guard historical one-shot sender entry, claim, transport, and settlement."""
    from carepulse.notifications import email_sender

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--organization-id", required=True, type=uuid.UUID)
    parser.add_argument("--job-id", type=uuid.UUID)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print("Delivery unavailable")
        return 1

    settings = email_sender._settings()
    target_id, manifest_id = _compat_settings()
    db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
    key = _permit_key(target_id)
    engine = create_engine(settings.connection_url, hide_parameters=True)
    conn = engine.connect()
    attempt_id = str(uuid.uuid4())
    reservation = str(args.job_id) if args.job_id else f"org:{args.organization_id}"
    epoch = 0
    locked = False
    try:
        if not conn.execute(
            text("SELECT pg_try_advisory_lock_shared(:k)"), {"k": key}
        ).scalar():
            raise RuntimeError("Drain in progress")
        locked = True
        epoch = _verify_job_connection(
            conn, target_id, manifest_id, db_name, "carepulse_sender"
        )
        conn.execute(
            text(
                """
                SELECT compat_control.record_own_attempt(
                    :aid, :tid, :epoch, 'source', 'sender', 'sender_claim', :corr, NULL, 'PENDING'
                )
                """
            ),
            {"aid": attempt_id, "tid": target_id, "epoch": epoch, "corr": reservation},
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - fail closed on any sender admission error
        try:
            conn.rollback()
        except SQLAlchemyError:
            pass
        if locked:
            try:
                conn.execute(text("SELECT pg_advisory_unlock_shared(:k)"), {"k": key})
                conn.commit()
            except SQLAlchemyError:
                pass
        conn.close()
        engine.dispose()
        print("Delivery unavailable")
        return 1

    disposition = "UNKNOWN"
    claimed = None
    transport = "NOT_STARTED"
    result = None
    original_tenant = email_sender._tenant
    original_claim = email_sender.claim_delivery_job
    original_node = email_sender._node_call
    original_send_one = email_sender.send_one_job

    class UncertainTransport(Exception):
        pass

    def tenant_with_reservation(connection, organization_id):
        original_tenant(connection, organization_id)
        connection.execute(
            text("SELECT set_config('app.compat_attempt_id', :aid, true)"),
            {"aid": attempt_id},
        )

    def reserved_claim(connection, policy, job_id=None):
        nonlocal claimed
        if connection.scalar(
            text("SELECT compat_control.has_unknown_sender_attempt(:job, :org)"),
            {"job": str(job_id) if job_id else None, "org": str(args.organization_id)},
        ):
            return None
        claim = original_claim(connection, policy, job_id)
        if claim is not None:
            # Preserve exact job and generation atomically with the original claim.
            connection.execute(
                text("""
                    SELECT compat_control.record_own_attempt(
                        :aid, :tid, :epoch, 'source', 'sender', 'sender_claim',
                        :job, :lease, 'PENDING'
                    )
                """),
                {
                    "aid": attempt_id,
                    "tid": target_id,
                    "epoch": epoch,
                    "job": str(claim.id),
                    "lease": claim.generation,
                },
            )
        claimed = claim
        return claim

    def observed_node(payload, timeout):
        nonlocal transport
        acknowledged = original_node(payload, timeout)
        if payload is not None:
            if not acknowledged:
                transport = "AMBIGUOUS"
                # The 0040 adapter does not distinguish lost SMTP ACK from failure.
                # Stop it before it schedules a possibly duplicate delivery.
                raise UncertainTransport
            transport = "SENT"
        return acknowledged

    def observed_send_one(*params):
        nonlocal result
        result = original_send_one(*params)
        return result

    try:
        email_sender._tenant = tenant_with_reservation
        email_sender.claim_delivery_job = reserved_claim
        email_sender._node_call = observed_node
        email_sender.send_one_job = observed_send_one
        sys.argv = ["carepulse.notifications.email_sender", *argv]
        try:
            rc = email_sender.main()
        except Exception:  # noqa: BLE001 - historical sender may lose its DB/SMTP ACK
            # A lost transport or settlement ACK must not be translated to NOT_SENT.
            print("Delivery unavailable")
            rc = 1
        if rc == 0 and claimed is None and result == "NOT_DUE":
            disposition = "NOT_SENT"
        elif rc == 0 and claimed is not None:
            with engine.begin() as observer:
                original_tenant(observer, args.organization_id)
                row = observer.execute(
                    text("""
                        SELECT state, last_outcome, claim_generation
                        FROM public.delivery_jobs WHERE id = :job
                    """),
                    {"job": claimed.id},
                ).one_or_none()
            expected = {
                "SENT": "SMTP_ACCEPTED",
                "READY": "RETRY_SCHEDULED",
                "FAILED": "ATTEMPTS_EXHAUSTED",
            }
            if result == "STALE" and transport == "NOT_STARTED":
                disposition = "NOT_SENT"
            elif (
                row is not None
                and result in expected
                and row.state == result
                and row.last_outcome == expected[result]
                and row.claim_generation == claimed.generation
            ):
                if result == "SENT" and transport == "SENT":
                    disposition = "SENT"
                elif result != "SENT" and transport == "NOT_STARTED":
                    disposition = "NOT_SENT"
        return rc
    finally:
        email_sender._tenant = original_tenant
        email_sender.claim_delivery_job = original_claim
        email_sender._node_call = original_node
        email_sender.send_one_job = original_send_one
        try:
            conn.execute(
                text(
                    """
                    SELECT compat_control.record_own_attempt(
                    :aid, :tid, :epoch, 'source', 'sender', 'sender_claim',
                    :job, :lease, :disp
                    )
                    """
                ),
                {
                    "aid": attempt_id,
                    "tid": target_id,
                    "epoch": epoch,
                    "disp": disposition,
                    "job": str(claimed.id) if claimed is not None else reservation,
                    "lease": claimed.generation if claimed is not None else None,
                },
            )
            conn.commit()
        except SQLAlchemyError:
            pass
        try:
            conn.execute(text("SELECT pg_advisory_unlock_shared(:k)"), {"k": key})
            conn.commit()
        except SQLAlchemyError:
            pass
        conn.close()
        engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("worker", "sender"):
        raise SystemExit(1)
    mode = sys.argv[1]
    if mode == "worker":
        raise SystemExit(_run_source_worker())
    raise SystemExit(_run_source_sender(sys.argv[2:]))
