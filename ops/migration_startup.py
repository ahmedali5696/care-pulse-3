"""CI-only Alembic startup diagnostics without SQL or credential disclosure."""

import re
import traceback
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError


def failure_record(error: Exception) -> str:
    category = "other"
    state = "unavailable"
    if isinstance(error, DBAPIError):
        category = "database"
        sqlstate = getattr(error.orig, "sqlstate", None)
        if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
            state = sqlstate
    elif isinstance(error, RuntimeError):
        category = "runtime"
    location = "unavailable"
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.parent.parts[-3:] == ("db", "migrations", "versions") and re.fullmatch(
            r"[0-9]{4}_[a-z0-9_]+\.py", path.name
        ):
            location = f"{path.name}:{line}"
    return f"MIGRATION_STARTUP FAIL category={category} sqlstate={state} location={location}"


def main() -> int:
    try:
        command.upgrade(Config("alembic.ini"), "head")
    except Exception as error:  # noqa: BLE001 - fail-closed CI process boundary
        # This process boundary must fail without leaking exception text or SQL.
        print(failure_record(error), flush=True)
        return 1
    print("MIGRATION_STARTUP PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
