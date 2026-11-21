"""Read-only predecessor ASGI & SQLAlchemy pool wrapper for immutable 0040 API images (T021).

Mounted read-only into the 0040 API container and launched via:
  python /run/compat-adapters/source-api.py
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import uvicorn
from carepulse.config import load_config
from carepulse.db import create_db_engine
from carepulse.main import app as inner_app
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

SOURCE_HEAD = "0040_t173_numeric_money"

# Consult the PostgreSQL login on every admission and pool checkout. Source
# image code is immutable, so the reviewed adapter must enforce its 0040 ACL.
_SOURCE_ROLE_PROBE = """
    SELECT current_database(), session_user::text, current_user::text,
           COALESCE((
               SELECT r.rolcanlogin AND NOT (
                   r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
                   OR r.rolcreatedb OR r.rolinherit OR r.rolreplication
                   OR EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid)
               )
               FROM pg_roles r WHERE r.rolname = current_user
           ), FALSE)
           AND has_database_privilege(current_user, current_database(), 'CONNECT')
           AND NOT has_database_privilege(current_user, current_database(), 'CREATE')
           AND NOT has_database_privilege(current_user, current_database(), 'TEMPORARY')
           AND has_schema_privilege(current_user, 'public', 'USAGE')
           AND NOT has_schema_privilege(current_user, 'public', 'CREATE')
           AND has_schema_privilege(current_user, 'compat_control', 'USAGE')
           AND NOT has_schema_privilege(current_user, 'compat_control', 'CREATE')
           AND has_table_privilege(current_user, 'compat_control.target_control', 'SELECT')
           AND has_function_privilege(current_user, 'compat_control.read_target_status()', 'EXECUTE')
           AND has_function_privilege(current_user, 'compat_control.read_target_control()', 'EXECUTE')
           AND has_function_privilege(
               current_user,
               'compat_control.record_own_attempt(text,text,bigint,text,text,text,text,bigint,text)',
               'EXECUTE'
           )
           AND NOT has_function_privilege(
               current_user, 'compat_control.has_unknown_sender_attempt(text,text)', 'EXECUTE'
           )
           AND NOT EXISTS (
               SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'compat_control' AND c.relkind IN ('r','p','v','m')
                 AND (
                   has_table_privilege(current_user, c.oid, 'INSERT')
                   OR has_table_privilege(current_user, c.oid, 'UPDATE')
                   OR has_table_privilege(current_user, c.oid, 'DELETE')
                   OR has_table_privilege(current_user, c.oid, 'TRUNCATE')
                   OR has_any_column_privilege(current_user, c.oid, 'INSERT')
                   OR has_any_column_privilege(current_user, c.oid, 'UPDATE')
                   OR has_any_column_privilege(current_user, c.oid, 'REFERENCES')
                   OR (c.relname <> 'target_control'
                       AND has_any_column_privilege(current_user, c.oid, 'SELECT'))
                 )
           )
           AND NOT EXISTS (
               SELECT 1 FROM pg_namespace n
               WHERE n.nspname NOT IN ('public', 'compat_control', 'pg_catalog', 'information_schema')
                 AND n.nspname NOT LIKE 'pg_%'
                 AND (has_schema_privilege(current_user, n.oid, 'USAGE')
                      OR has_schema_privilege(current_user, n.oid, 'CREATE'))
           )
           AND has_table_privilege(current_user, 'public.patients', 'SELECT')
           AND has_table_privilege(current_user, 'public.appointments', 'INSERT')
           AND has_table_privilege(current_user, 'public.delivery_jobs', 'UPDATE')
           AND NOT has_table_privilege(current_user, 'public.patients', 'DELETE')
           AND NOT has_table_privilege(current_user, 'public.appointments', 'DELETE')
           AND NOT has_table_privilege(current_user, 'public.visits', 'DELETE')
           AND NOT has_any_column_privilege(current_user, 'public."account"', 'SELECT')
           AND NOT has_any_column_privilege(current_user, 'public."account"', 'UPDATE')
           AND NOT has_any_column_privilege(current_user, 'public."account"', 'INSERT')
           AND NOT has_table_privilege(current_user, 'public."account"', 'DELETE')
           AND NOT has_any_column_privilege(current_user, 'public."session"', 'SELECT')
           AND NOT has_any_column_privilege(current_user, 'public."session"', 'UPDATE')
           AND NOT has_any_column_privilege(current_user, 'public."session"', 'INSERT')
           AND NOT has_table_privilege(current_user, 'public."session"', 'DELETE')
           AS privileges_ok
"""


def _assert_source_role(row, db_name: str) -> None:
    if (
        row is None
        or row[0] != db_name
        or row[1] != "carepulse_runtime"
        or row[2] != "carepulse_runtime"
        or row[3] is not True
    ):
        raise RuntimeError("Source API effective privilege mismatch")


def _permit_key(target_id: str) -> int:
    digest = hashlib.sha256(f"compat:permit:{target_id}".encode()).digest()
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
    _assert_source_role(conn.execute(text(_SOURCE_ROLE_PROBE)).first(), db_name)

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
        cur.execute(_SOURCE_ROLE_PROBE)
        _assert_source_role(cur.fetchone(), db_name)
        cur.execute(
            """
            SELECT target_id, db_name, phase, active_generation, source_sealed,
                   source_manifest_id, alembic_head
            FROM compat_control.read_target_status()
            """
        )
        ctrl = cur.fetchone()
        if (
            not ctrl
            or ctrl[0] != target_id
            or ctrl[1] != db_name
            or ctrl[2] != "SOURCE_OPEN"
            or ctrl[3] != "source"
            or ctrl[4]
            or ctrl[5] != manifest_id
            or ctrl[6] != SOURCE_HEAD
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
        if scope.get("type") != "http" or path == "/health/live":
            await self.asgi_app(scope, receive, send)
            return

        if path == "/health/ready":
            try:
                target_id, manifest_id, _, _ = _compat_settings()
                with self._get_engine().connect() as connection:
                    _verify_source_connection(
                        connection,
                        target_id,
                        manifest_id,
                        os.environ.get("CP_DB_NAME", "carepulse").strip(),
                    )
            except Exception:  # noqa: BLE001 - readiness must deny every unknown guard failure
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
                await send({"type": "http.response.body", "body": b'{"ready":false}'})
                return
            await self.asgi_app(scope, receive, send)
            return

        target_id, manifest_id, _, _ = _compat_settings()
        db_name = os.environ.get("CP_DB_NAME", "carepulse").strip()
        key = _permit_key(target_id)
        engine = self._get_engine()
        conn = None
        attempt_id = str(uuid.uuid4())
        epoch = 0
        locked = False
        try:
            conn = engine.connect()
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
        except Exception:  # noqa: BLE001 - no failed admission may reach the source app
            if conn is not None:
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

        try:
            await self.asgi_app(scope, receive, send)
        finally:
            # Preserve PENDING: an ASGI response cannot prove commit or rollback.
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
