"""Shared disposable PostgreSQL/Compose helpers for PRE-W3-COMPAT integration suites."""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy import URL, create_engine

from ops.compat.manifest import (
    CANDIDATE_HEAD,
    IMMUTABLE_SOURCE_SHA,
    SOURCE_HEAD,
    GenerationManifest,
    parse_manifest,
)
from ops.compat.probes import probe_target_identity

SECRET_FILES: dict[str, str] = {
    "carepulse_bootstrap": "/run/secrets/postgres_password",
    "carepulse_migration": "/run/secrets/migration_password",
    "carepulse_api": "/run/secrets/api_db_password",
    "carepulse_gate": "/run/secrets/gate_db_password",
    "carepulse_web": "/run/secrets/web_db_password",
    "carepulse_worker": "/run/secrets/worker_db_password",
    "carepulse_auth": "/run/secrets/auth_db_password",
    "carepulse_operator": "/run/secrets/operator_db_password",
    "carepulse_sender": "/run/secrets/sender_db_password",
    "carepulse_runtime": "/run/secrets/source_runtime_db_password",
}


def require_compat_ci() -> None:
    """Fail closed outside disposable CI (never silently skip mandatory CI checks)."""
    if os.getenv("CI") != "true":
        raise RuntimeError(
            "PRE-W3-COMPAT integration tests require disposable Linux CI"
        )
    project = os.environ.get("CP_CI_PROJECT", "")
    if not re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project):
        raise RuntimeError(f"Invalid CP_CI_PROJECT: {project!r}")


def read_secret(role: str) -> str:
    path = Path(SECRET_FILES[role])
    return path.read_text(encoding="utf-8").strip()


def connect_role(
    role: str, *, dbname: str = "carepulse", autocommit: bool = False
) -> psycopg.Connection:
    return psycopg.connect(
        host="db",
        port=5432,
        dbname=dbname,
        user=role,
        password=read_secret(role),
        autocommit=autocommit,
        connect_timeout=5,
        row_factory=dict_row,
    )


def make_sa_engine(role: str, *, dbname: str = "carepulse"):
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=role,
            password=read_secret(role),
            host="db",
            port=5432,
            database=dbname,
        ),
        isolation_level="READ COMMITTED",
        hide_parameters=True,
    )


def ensure_roles_provisioned(
    conn: psycopg.Connection, *, source_only: bool = False
) -> None:
    """Provision PostgreSQL roles using real secrets (0040 historical or full 0046)."""
    if source_only:
        role_defs = {
            "carepulse_migration": read_secret("carepulse_migration"),
            "carepulse_runtime": read_secret("carepulse_runtime"),
            "carepulse_operator": read_secret("carepulse_operator"),
            "carepulse_sender": read_secret("carepulse_sender"),
            "carepulse_auth": read_secret("carepulse_auth"),
        }
    else:
        role_defs = {
            role: read_secret(role)
            for role in (
                "carepulse_migration",
                "carepulse_runtime",
                "carepulse_api",
                "carepulse_web",
                "carepulse_worker",
                "carepulse_gate",
                "carepulse_operator",
                "carepulse_sender",
                "carepulse_auth",
            )
        }

    for role_name, pw in role_defs.items():
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)
        ).fetchone()
        if not exists:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(pw))
            )
        else:
            conn.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role_name), sql.Literal(pw)
                )
            )

    if not source_only:
        for nologin in ("carepulse_policy_seed", "carepulse_identity_owner"):
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (nologin,)
            ).fetchone()
            if not exists:
                conn.execute(
                    f"CREATE ROLE {nologin} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            conn.execute(
                f"GRANT {nologin} TO carepulse_migration WITH SET TRUE, INHERIT FALSE"
            )
    conn.commit()


def grant_db_access(
    conn: psycopg.Connection, dbname: str, *, source_only: bool = False
) -> None:
    conn.execute(f"REVOKE ALL ON DATABASE {dbname} FROM PUBLIC")
    roles = (
        [
            "carepulse_migration",
            "carepulse_runtime",
            "carepulse_operator",
            "carepulse_sender",
            "carepulse_auth",
        ]
        if source_only
        else [
            "carepulse_migration",
            "carepulse_runtime",
            "carepulse_api",
            "carepulse_web",
            "carepulse_worker",
            "carepulse_gate",
            "carepulse_operator",
            "carepulse_sender",
            "carepulse_auth",
        ]
    )
    conn.execute(f"GRANT CONNECT ON DATABASE {dbname} TO {', '.join(roles)}")
    conn.execute(f"GRANT CREATE ON DATABASE {dbname} TO carepulse_migration")
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    conn.execute("ALTER SCHEMA public OWNER TO carepulse_migration")
    conn.execute(
        "GRANT USAGE ON SCHEMA public TO carepulse_runtime, carepulse_sender, carepulse_auth"
    )
    if not source_only:
        conn.execute(
            "CREATE SCHEMA IF NOT EXISTS f01_private AUTHORIZATION carepulse_identity_owner"
        )
        conn.execute(
            "CREATE SCHEMA IF NOT EXISTS f01_crypto AUTHORIZATION carepulse_migration"
        )
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA f01_crypto")
        conn.execute("REVOKE ALL ON SCHEMA f01_private, f01_crypto FROM PUBLIC")
        conn.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA f01_crypto FROM PUBLIC")
        conn.execute(
            "GRANT EXECUTE ON FUNCTION f01_crypto.hmac(bytea, bytea, text), f01_crypto.digest(bytea, text), f01_crypto.gen_random_bytes(integer) TO carepulse_identity_owner"
        )
        conn.execute("GRANT USAGE ON SCHEMA f01_crypto TO carepulse_identity_owner")
    conn.commit()


@contextmanager
def disposable_database(prefix: str = "compat"):
    """Create and tear down an isolated disposable PostgreSQL database."""
    dbname = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = connect_role("carepulse_bootstrap", dbname="carepulse", autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {dbname}")
        yield dbname
    finally:
        admin.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (dbname,),
        )
        admin.execute(f"DROP DATABASE IF EXISTS {dbname}")
        admin.close()


def upgrade_to_revision(dbname: str, revision: str) -> None:
    engine = make_sa_engine("carepulse_migration", dbname=dbname)
    try:
        cfg = Config("/srv/alembic.ini")
        with engine.connect() as sa_conn:
            cfg.attributes["connection"] = sa_conn
            command.upgrade(cfg, revision)
            sa_conn.commit()
    finally:
        engine.dispose()


def build_manifest(
    *,
    generation: str,
    target_id: str,
    cluster_id: str,
    db_name: str,
) -> GenerationManifest:
    is_source = generation == "source"
    head = SOURCE_HEAD if is_source else CANDIDATE_HEAD
    sha = IMMUTABLE_SOURCE_SHA if is_source else "b" * 40
    img_hex = "a" * 64 if is_source else "b" * 64
    adp_hex = "c" * 64 if is_source else "d" * 64
    return parse_manifest(
        {
            "manifest_id": f"{generation}-{head[:4]}",
            "generation": generation,
            "source_sha": sha,
            "expected_head": head,
            "target_binding": {
                "target_id": target_id,
                "cluster_id": cluster_id,
                "db_name": db_name,
            },
            "participants": {
                "web": {
                    "image_digest": f"sha256:{img_hex}",
                    "runnable_target": "runtime",
                    "entry_command": ["node", "ingress.mjs"],
                    "adapter_digest": f"sha256:{adp_hex}",
                    "adapter_mount_path": "/run/compat-adapters",
                    "adapter_import_path": "compat/admission.ts",
                    "packaging_verified": True,
                    "trusted_peers": [
                        f"http://{'source' if is_source else 'candidate'}-api:8000"
                    ],
                    "allowed_credentials": (
                        ["carepulse_runtime", "carepulse_auth"]
                        if is_source
                        else ["carepulse_web", "carepulse_auth"]
                    ),
                },
                "api": {
                    "image_digest": f"sha256:{img_hex}",
                    "runnable_target": "api",
                    "entry_command": ["python", "-m", "uvicorn", "carepulse.main:app"],
                    "adapter_digest": f"sha256:{adp_hex}",
                    "adapter_mount_path": "/run/compat-adapters",
                    "adapter_import_path": "carepulse.compat.admission",
                    "packaging_verified": True,
                    "trusted_peers": [],
                    "allowed_credentials": (
                        ["carepulse_runtime"]
                        if is_source
                        else ["carepulse_api", "carepulse_gate"]
                    ),
                },
                "worker": {
                    "image_digest": f"sha256:{img_hex}",
                    "runnable_target": "worker",
                    "entry_command": ["python", "apps/worker/main.py"],
                    "adapter_digest": f"sha256:{adp_hex}",
                    "adapter_mount_path": "/run/compat-adapters",
                    "adapter_import_path": "carepulse.compat.admission",
                    "packaging_verified": True,
                    "trusted_peers": [],
                    "allowed_credentials": (
                        ["carepulse_runtime"] if is_source else ["carepulse_worker"]
                    ),
                },
                "sender": {
                    "image_digest": f"sha256:{img_hex}",
                    "runnable_target": "sender",
                    "entry_command": [
                        "python",
                        "-m",
                        "carepulse.notifications.email_sender",
                    ],
                    "adapter_digest": f"sha256:{adp_hex}",
                    "adapter_mount_path": "/run/compat-adapters",
                    "adapter_import_path": "carepulse.compat.admission",
                    "packaging_verified": True,
                    "trusted_peers": ["mail:1025"],
                    "allowed_credentials": ["carepulse_sender"],
                },
            },
        }
    )


def get_cluster_id(conn: psycopg.Connection) -> str:
    return probe_target_identity(conn).cluster_id
