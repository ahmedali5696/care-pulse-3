"""Real 0040 source admission must recheck PostgreSQL privileges on live sessions."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from sqlalchemy import Engine, event, text

from ops.compat.preflight import (
    US1_EXECUTED_CHECKPOINT,
    PreflightError,
    run_source_preflight,
)
from tests.integration.compat_ci_helpers import (
    connect_role,
    make_sa_engine,
    read_source_secret,
)
from tests.integration.compat_recovery_lab import (
    original_sender_engine,
    populated_0040_lab,
)


def _source_adapter(filename: str):
    path = Path(__file__).resolve().parents[2] / "ops" / "compat" / filename
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(guard, path: str) -> tuple[int, bytes]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(guard({"type": "http", "path": path}, receive, send))
    return (
        next(m["status"] for m in messages if m["type"] == "http.response.start"),
        b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        ),
    )


def test_0040_source_privileges_fail_closed_on_surviving_connections(
    monkeypatch,
) -> None:
    with populated_0040_lab("compat_src_grant") as lab:
        for name, value in (
            ("CP_COMPAT_TARGET_ID", lab.target_id),
            ("CP_COMPAT_MANIFEST_ID", lab.source_manifest.manifest_id),
            ("CP_COMPAT_GENERATION", "source"),
            ("CP_COMPAT_EXPECTED_HEAD", "0040_t173_numeric_money"),
            ("CP_DB_NAME", lab.dbname),
        ):
            monkeypatch.setenv(name, value)

        jobs = _source_adapter("source-jobs.py")
        api = _source_adapter("source-api.py")
        admin = None
        runtime_engine = None

        def runtime_epoch(conn) -> int:
            return api._verify_source_connection(
                conn, lab.target_id, lab.source_manifest.manifest_id, lab.dbname
            )

        def job_epoch(conn, role: str) -> int:
            return jobs._verify_job_connection(
                conn, lab.target_id, lab.source_manifest.manifest_id, lab.dbname, role
            )

        try:
            admin = connect_role(
                "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
            )
            runtime_engine = make_sa_engine("carepulse_runtime", dbname=lab.dbname)
            with runtime_engine.connect() as surviving:
                epoch = runtime_epoch(surviving)
                assert job_epoch(surviving, "carepulse_runtime") == epoch
                assert surviving.execute(
                    text(
                        "SELECT has_table_privilege(current_user, 'public.patients', 'SELECT')"
                    )
                ).scalar_one()
                assert surviving.execute(
                    text(
                        "SELECT has_table_privilege(current_user, 'public.appointments', 'INSERT')"
                    )
                ).scalar_one()
                assert not surviving.execute(
                    text(
                        "SELECT has_table_privilege(current_user, 'public.\"account\"', 'UPDATE')"
                    )
                ).scalar_one()

                dispatches = []

                async def original_app(_scope, _receive, send):
                    dispatches.append("called")
                    await send(
                        {"type": "http.response.start", "status": 200, "headers": []}
                    )
                    await send({"type": "http.response.body", "body": b"ok"})

                guard = api.SourceCompatASGIGuard(original_app)
                guard._engine = runtime_engine
                assert _response(guard, "/probe") == (200, b"ok")
                assert _response(guard, "/health/ready") == (200, b"ok")
                assert len(dispatches) == 2
                surviving.rollback()  # Leave the backend alive without holding DDL locks.

                observer = connect_role(
                    "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
                )
                try:
                    preflight_args = {
                        "source_manifest": lab.source_manifest,
                        "target_manifest": lab.target_manifest,
                        "source_baseline": lab.retained_baseline(),
                        "us1_checkpoint": US1_EXECUTED_CHECKPOINT,
                    }
                    assert run_source_preflight(observer, **preflight_args).head == (
                        "0040_t173_numeric_money"
                    )

                    runtime_drifts = (
                        (
                            "GRANT UPDATE ON compat_control.target_control TO carepulse_runtime",
                            "REVOKE UPDATE ON compat_control.target_control FROM carepulse_runtime",
                        ),
                        (
                            "GRANT UPDATE (phase) ON compat_control.target_control TO carepulse_runtime",
                            "REVOKE UPDATE (phase) ON compat_control.target_control FROM carepulse_runtime",
                        ),
                        (
                            "REVOKE SELECT ON public.patients FROM carepulse_runtime",
                            "GRANT SELECT ON public.patients TO carepulse_runtime",
                        ),
                        (
                            "REVOKE USAGE ON SCHEMA public FROM carepulse_runtime",
                            "GRANT USAGE ON SCHEMA public TO carepulse_runtime",
                        ),
                        (
                            "GRANT CREATE ON SCHEMA public TO carepulse_runtime",
                            "REVOKE CREATE ON SCHEMA public FROM carepulse_runtime",
                        ),
                        (
                            f"GRANT CREATE ON DATABASE {lab.dbname} TO carepulse_runtime",
                            f"REVOKE CREATE ON DATABASE {lab.dbname} FROM carepulse_runtime",
                        ),
                        (
                            f"GRANT TEMP ON DATABASE {lab.dbname} TO carepulse_runtime",
                            f"REVOKE TEMP ON DATABASE {lab.dbname} FROM carepulse_runtime",
                        ),
                        (
                            'GRANT DELETE ON public."account" TO carepulse_runtime',
                            'REVOKE DELETE ON public."account" FROM carepulse_runtime',
                        ),
                        (
                            "ALTER ROLE carepulse_runtime CREATEDB",
                            "ALTER ROLE carepulse_runtime NOCREATEDB",
                        ),
                        (
                            "ALTER ROLE carepulse_runtime CREATEROLE",
                            "ALTER ROLE carepulse_runtime NOCREATEROLE",
                        ),
                        (
                            "ALTER ROLE carepulse_runtime BYPASSRLS",
                            "ALTER ROLE carepulse_runtime NOBYPASSRLS",
                        ),
                        (
                            "GRANT carepulse_policy_seed TO carepulse_runtime",
                            "REVOKE carepulse_policy_seed FROM carepulse_runtime",
                        ),
                    )
                    for grant, revoke in runtime_drifts:
                        try:
                            admin.execute(grant)
                            with pytest.raises(RuntimeError, match="privilege"):
                                runtime_epoch(surviving)
                            with pytest.raises(RuntimeError, match="privilege"):
                                job_epoch(surviving, "carepulse_runtime")
                            with pytest.raises(PreflightError):
                                run_source_preflight(observer, **preflight_args)

                            before = admin.execute(
                                "SELECT count(*) FROM compat_control.work_evidence "
                                "WHERE target_id=%s AND generation='source' AND service='api'",
                                (lab.target_id,),
                            ).fetchone()["count"]
                            assert _response(guard, "/probe")[0] == 503
                            assert _response(guard, "/health/ready")[0] == 503
                            after = admin.execute(
                                "SELECT count(*) FROM compat_control.work_evidence "
                                "WHERE target_id=%s AND generation='source' AND service='api'",
                                (lab.target_id,),
                            ).fetchone()["count"]
                            assert before == after
                            assert len(dispatches) == 2
                        finally:
                            surviving.rollback()
                            admin.execute(revoke)
                        assert runtime_epoch(surviving) == epoch
                        assert job_epoch(surviving, "carepulse_runtime") == epoch
                        surviving.rollback()
                    assert run_source_preflight(observer, **preflight_args).head == (
                        "0040_t173_numeric_money"
                    )

                    with psycopg.connect(
                        host="db",
                        port=5432,
                        dbname=lab.dbname,
                        user="carepulse_auth",
                        password=read_source_secret("carepulse_auth"),
                        autocommit=True,
                        row_factory=dict_row,
                    ) as auth:
                        identity = auth.execute(
                            "SELECT session_user, current_user, pg_backend_pid() AS pid"
                        ).fetchone()
                        assert identity["session_user"] == "carepulse_auth"
                        assert identity["current_user"] == "carepulse_auth"
                        assert auth.execute(
                            "SELECT has_table_privilege(current_user, 'public.\"user\"', 'SELECT') AS ok"
                        ).fetchone()["ok"]
                        assert auth.execute(
                            "SELECT has_table_privilege(current_user, 'public.\"session\"', 'INSERT') AS ok"
                        ).fetchone()["ok"]
                        assert not auth.execute(
                            "SELECT has_table_privilege(current_user, 'public.patients', 'SELECT') AS ok"
                        ).fetchone()["ok"]
                        auth_drifts = (
                            (
                                "GRANT SELECT ON public.patients TO carepulse_auth",
                                "REVOKE SELECT ON public.patients FROM carepulse_auth",
                            ),
                            (
                                'REVOKE INSERT ON public."session" FROM carepulse_auth',
                                'GRANT INSERT ON public."session" TO carepulse_auth',
                            ),
                            (
                                "GRANT CREATE ON SCHEMA public TO carepulse_auth",
                                "REVOKE CREATE ON SCHEMA public FROM carepulse_auth",
                            ),
                            (
                                "ALTER ROLE carepulse_auth CREATEROLE",
                                "ALTER ROLE carepulse_auth NOCREATEROLE",
                            ),
                        )
                        for grant, revoke in auth_drifts:
                            try:
                                admin.execute(grant)
                                assert (
                                    auth.execute(
                                        "SELECT pg_backend_pid() AS pid"
                                    ).fetchone()["pid"]
                                    == identity["pid"]
                                )
                                with pytest.raises(PreflightError):
                                    run_source_preflight(observer, **preflight_args)
                            finally:
                                admin.execute(revoke)
                            assert run_source_preflight(
                                observer, **preflight_args
                            ).head == ("0040_t173_numeric_money")
                finally:
                    observer.close()
        finally:
            if runtime_engine is not None:
                runtime_engine.dispose()
            event.remove(Engine, "checkout", api._guard_source_pool_checkout)
            if admin is not None:
                admin.close()

        admin = None
        sender_engine = None
        try:
            admin = connect_role(
                "carepulse_bootstrap", dbname=lab.dbname, autocommit=True
            )
            sender_engine = original_sender_engine(lab.dbname)
            with sender_engine.connect() as sender:
                epoch = job_epoch(sender, "carepulse_sender")
                assert sender.execute(
                    text(
                        "SELECT has_table_privilege(current_user, 'public.delivery_jobs', 'UPDATE')"
                    )
                ).scalar_one()
                assert sender.execute(
                    text(
                        "SELECT has_column_privilege(current_user, 'public.delivery_jobs', 'recipient', 'SELECT')"
                    )
                ).scalar_one()
                for table, privilege in (
                    ("public.patients", "SELECT"),
                    ("public.appointments", "INSERT"),
                    ("public.visits", "SELECT"),
                ):
                    assert not sender.execute(
                        text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar_one()
                sender.rollback()

                sender_drifts = (
                    (
                        "GRANT SELECT ON public.patients TO carepulse_sender",
                        "REVOKE SELECT ON public.patients FROM carepulse_sender",
                    ),
                    (
                        "REVOKE UPDATE ON public.delivery_jobs FROM carepulse_sender",
                        "GRANT UPDATE ON public.delivery_jobs TO carepulse_sender",
                    ),
                    (
                        "GRANT INSERT ON public.appointments TO carepulse_sender",
                        "REVOKE INSERT ON public.appointments FROM carepulse_sender",
                    ),
                    (
                        "GRANT UPDATE ON compat_control.target_control TO carepulse_sender",
                        "REVOKE UPDATE ON compat_control.target_control FROM carepulse_sender",
                    ),
                    (
                        "GRANT CREATE ON SCHEMA public TO carepulse_sender",
                        "REVOKE CREATE ON SCHEMA public FROM carepulse_sender",
                    ),
                    (
                        "ALTER ROLE carepulse_sender CREATEDB",
                        "ALTER ROLE carepulse_sender NOCREATEDB",
                    ),
                    (
                        "ALTER ROLE carepulse_sender CREATEROLE",
                        "ALTER ROLE carepulse_sender NOCREATEROLE",
                    ),
                    (
                        "ALTER ROLE carepulse_sender BYPASSRLS",
                        "ALTER ROLE carepulse_sender NOBYPASSRLS",
                    ),
                    (
                        "GRANT carepulse_policy_seed TO carepulse_sender",
                        "REVOKE carepulse_policy_seed FROM carepulse_sender",
                    ),
                )
                for grant, revoke in sender_drifts:
                    try:
                        admin.execute(grant)
                        with pytest.raises(RuntimeError, match="privilege"):
                            job_epoch(sender, "carepulse_sender")
                    finally:
                        sender.rollback()
                        admin.execute(revoke)
                    assert job_epoch(sender, "carepulse_sender") == epoch
                    sender.rollback()
        finally:
            if sender_engine is not None:
                sender_engine.dispose()
            if admin is not None:
                admin.close()
