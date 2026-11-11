"""Process liveness and deadline-limited foundation dependency readiness."""

import subprocess
import sys

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from carepulse.config import ConfigError, Consumer, load_config
from carepulse.db import create_db_engine, transaction

PROBE_DEADLINE_SECONDS = 4
COMPATIBLE_REVISION = "0036_b10_confirmation_parity"
router = APIRouter()


def probe_readiness(consumer: Consumer) -> bool:
    # Killing a probe also bounds DNS, pool waits and blocked driver reads.
    try:
        probe = subprocess.run(
            [sys.executable, "-m", "carepulse.health", consumer],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_DEADLINE_SECONDS,
            check=False,
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _database_ready(consumer: Consumer) -> bool:
    engine = create_db_engine(load_config(consumer))
    try:
        with transaction(engine) as connection:
            connection.execute(text("SET LOCAL statement_timeout = 2000"))
            revisions = connection.scalars(
                text("SELECT version_num FROM public.alembic_version")
            ).all()
            return revisions == [COMPATIBLE_REVISION]
    finally:
        engine.dispose()


@router.get("/health/live")
def live() -> JSONResponse:
    return JSONResponse({"status": "alive"}, headers={"Cache-Control": "no-store"})


@router.get("/health/ready")
def ready() -> JSONResponse:
    healthy = probe_readiness("api")
    return JSONResponse(
        {"status": "ready" if healthy else "not_ready"},
        status_code=200 if healthy else 503,
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    if sys.argv[1:] not in (["api"], ["worker"]):
        raise SystemExit(1)
    try:
        raise SystemExit(0 if _database_ready(sys.argv[1]) else 1)
    except (ConfigError, SQLAlchemyError, OSError):
        raise SystemExit(1) from None
