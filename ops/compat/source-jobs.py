"""Read-only predecessor worker & one-shot sender startup wrappers for immutable 0040 images (T022).

Launched via:
  python /run/compat-adapters/source-jobs.py worker
  python /run/compat-adapters/source-jobs.py sender --organization-id <uuid> [--job-id <uuid>]
"""

from __future__ import annotations

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
    digest = hashlib.sha256(f"compat:permit:{target_id}".encode("utf-8")).digest()
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
    revs = (
        conn.execute(text("SELECT version_num FROM public.alembic_version"))
        .scalars()
        .all()
    )
    if revs != [SOURCE_HEAD]:
        raise RuntimeError("Source job schema revision mismatch")
    ctrl = (
        conn.execute(
            text(
                """
            SELECT target_id, db_name, phase, owner_epoch,
                   source_manifest_id, active_generation, source_sealed
            FROM compat_control.target_control
            WHERE singleton_id = TRUE FOR SHARE
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
            except Exception:
                ready = False
            write_status(STATUS_PATH, started, ready)
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    finally:
        engine.dispose()


def _run_source_sender(argv: list[str]) -> int:
    """Guard historical one-shot sender entry, claim, transport, and settlement."""
    from carepulse.notifications import email_sender

    settings = email_sender._settings()
    target_id, manifest_id = _compat_settings()
    db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
    key = _permit_key(target_id)
    engine = create_engine(settings.connection_url, hide_parameters=True)
    conn = engine.connect()
    attempt_id = str(uuid.uuid4())
    epoch = 0
    locked = False
    try:
        if not conn.execute(
            text("SELECT pg_try_advisory_lock_shared(:k)"), {"k": key}
        ).scalar():
            print("Delivery unavailable")
            return 1
        locked = True
        epoch = _verify_job_connection(
            conn, target_id, manifest_id, db_name, "carepulse_sender"
        )
        conn.execute(
            text(
                """
                SELECT compat_control.record_own_attempt(
                    :aid, :tid, :epoch, 'source', 'sender', 'sender_claim', NULL, NULL, 'PENDING'
                )
                """
            ),
            {"aid": attempt_id, "tid": target_id, "epoch": epoch},
        )
        conn.commit()
    except Exception:
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
    try:
        sys.argv = ["carepulse.notifications.email_sender", *argv]
        rc = email_sender.main()
        disposition = "SENT" if rc == 0 else "NOT_SENT"
        return rc
    finally:
        try:
            conn.execute(
                text(
                    """
                    SELECT compat_control.record_own_attempt(
                        :aid, :tid, :epoch, 'source', 'sender', 'sender_claim', NULL, NULL, :disp
                    )
                    """
                ),
                {
                    "aid": attempt_id,
                    "tid": target_id,
                    "epoch": epoch,
                    "disp": disposition,
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
