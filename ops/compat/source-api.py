"""Read-only predecessor ASGI & SQLAlchemy pool wrapper for immutable 0040 API images (T021).

Mounted read-only into the 0040 API container and launched via:
  python /run/compat-adapters/source-api.py
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import uvicorn
from carepulse.config import load_config
from carepulse.db import create_db_engine
from carepulse.main import app as inner_app
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

SOURCE_HEAD = "0040_t173_numeric_money"


def _permit_key(target_id: str) -> int:
    digest = hashlib.sha256(f"compat:permit:{target_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _compat_settings() -> tuple[str, str, str, str]:
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
        raise RuntimeError("Invalid source API compatibility binding")
    return target_id, manifest_id, generation, expected_head


def _verify_source_connection(
    conn, target_id: str, manifest_id: str, db_name: str
) -> int:
    row = (
        conn.execute(
            text(
                """
            SELECT current_database() AS db_name,
                   session_user::text AS session_user,
                   current_user::text AS current_user,
                   r.rolsuper, r.rolbypassrls, r.rolcanlogin
            FROM pg_roles r WHERE r.rolname = current_user
            """
            )
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or row["db_name"] != db_name
        or row["session_user"] != "carepulse_runtime"
        or row["current_user"] != "carepulse_runtime"
        or row["rolsuper"]
        or row["rolbypassrls"]
        or not row["rolcanlogin"]
    ):
        raise RuntimeError("Source API role/database mismatch")

    revs = (
        conn.execute(text("SELECT version_num FROM public.alembic_version"))
        .scalars()
        .all()
    )
    if revs != [SOURCE_HEAD]:
        raise RuntimeError("Source API schema revision mismatch")

    ctrl = (
        conn.execute(
            text(
                """
            SELECT target_id, db_name, phase, owner_epoch,
                   source_manifest_id, active_generation, source_sealed
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
        or ctrl["phase"] != "SOURCE_OPEN"
        or ctrl["active_generation"] != "source"
        or ctrl["source_sealed"]
        or ctrl["source_manifest_id"] != manifest_id
    ):
        raise RuntimeError("Source API fenced")
    return int(ctrl["owner_epoch"])


# Guard every pool checkout in the 0040 process.
@event.listens_for(Engine, "checkout")
def _guard_source_pool_checkout(
    dbapi_connection, _connection_record, _connection_proxy
):
    target_id, manifest_id, _, _ = _compat_settings()
    db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
    cur = dbapi_connection.cursor()
    try:
        cur.execute("SELECT current_database(), session_user::text, current_user::text")
        ident = cur.fetchone()
        if (
            not ident
            or ident[0] != db_name
            or ident[1] != "carepulse_runtime"
            or ident[2] != "carepulse_runtime"
        ):
            raise RuntimeError("Source pool role/database mismatch")
        cur.execute("SELECT version_num FROM public.alembic_version")
        revs = cur.fetchall()
        if len(revs) != 1 or revs[0][0] != SOURCE_HEAD:
            raise RuntimeError("Source pool head mismatch")
        cur.execute(
            """
            SELECT target_id, phase, active_generation, source_sealed, source_manifest_id
            FROM compat_control.target_control WHERE singleton_id = TRUE
            """
        )
        ctrl = cur.fetchone()
        if (
            not ctrl
            or ctrl[0] != target_id
            or ctrl[1] != "SOURCE_OPEN"
            or ctrl[2] != "source"
            or ctrl[3]
            or ctrl[4] != manifest_id
        ):
            raise RuntimeError("Source pool fenced")
    finally:
        cur.close()


class SourceCompatASGIGuard:
    """ASGI wrapper around immutable 0040 FastAPI app."""

    def __init__(self, asgi_app) -> None:
        self.asgi_app = asgi_app
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            self._engine = create_db_engine(load_config("api"))
        return self._engine

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or path in ("/health/live", "/health/ready"):
            await self.asgi_app(scope, receive, send)
            return

        target_id, manifest_id, _, _ = _compat_settings()
        db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
        key = _permit_key(target_id)
        engine = self._get_engine()
        conn = engine.connect()
        attempt_id = str(uuid.uuid4())
        epoch = 0
        locked = False
        try:
            if not conn.execute(
                text("SELECT pg_try_advisory_lock_shared(:k)"), {"k": key}
            ).scalar():
                raise RuntimeError("Drain in progress")
            locked = True
            epoch = _verify_source_connection(conn, target_id, manifest_id, db_name)
            conn.execute(
                text(
                    """
                    SELECT compat_control.record_own_attempt(
                        :aid, :tid, :epoch, 'source', 'api', 'http', NULL, NULL, 'PENDING'
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
                    conn.execute(
                        text("SELECT pg_advisory_unlock_shared(:k)"), {"k": key}
                    )
                    conn.commit()
                except SQLAlchemyError:
                    pass
            conn.close()
            body = json.dumps({"detail": "Service unavailable"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        status_code = 500

        async def tracked_send(message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.asgi_app(scope, receive, tracked_send)
        finally:
            try:
                conn.execute(
                    text(
                        """
                        SELECT compat_control.record_own_attempt(
                            :aid, :tid, :epoch, 'source', 'api', 'http', NULL, NULL, :disp
                        )
                        """
                    ),
                    {
                        "aid": attempt_id,
                        "tid": target_id,
                        "epoch": epoch,
                        "disp": "COMMITTED" if status_code < 500 else "ROLLED_BACK",
                    },
                )
                conn.commit()
            except SQLAlchemyError:
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


app = SourceCompatASGIGuard(inner_app)


if __name__ == "__main__":
    _compat_settings()
    uvicorn.run(app, host="0.0.0.0", port=8000)
