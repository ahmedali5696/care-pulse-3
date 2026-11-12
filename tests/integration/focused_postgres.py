"""Run a CI-approved pytest target and publish only structured diagnostics."""

import os
import re
import sys
from pathlib import Path

import pytest
from postgres_locks import PostgresLockNotObserved
from psycopg import Error as PostgresError
from sqlalchemy.exc import DBAPIError


class Diagnostic:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0
        self.details = []

    def pytest_collectreport(self, report):
        if report.failed:
            path = Path(report.nodeid).as_posix()
            if not re.fullmatch(r"tests/integration/test_[a-z0-9_]+\.py", path):
                path = "test module unavailable"
            self.details.append(f"FAILED collection {path}")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.passed and report.when == "call":
            self.passed += 1
        elif report.failed or report.skipped:
            self.failed += report.failed
            self.skipped += report.skipped
            path, line, _ = report.location
            path = Path(path).as_posix()
            if call.excinfo:
                for entry in reversed(call.excinfo.traceback):
                    if entry.path == item.path:
                        line = entry.lineno
                        break
            name = item.originalname if isinstance(item, pytest.Function) else ""
            location = "test location unavailable"
            if (
                re.fullmatch(r"tests/integration/test_[a-z0-9_]+\.py", path)
                and re.fullmatch(r"test_[a-z0-9_]+", name or "")
                and line is not None
            ):
                location = f"{path}:{line + 1} {name}"
            detail = f"{report.outcome.upper()} {report.when} {location}"
            error = call.excinfo.value if call.excinfo else None
            if isinstance(error, PostgresLockNotObserved):
                detail += f" | {error}"
            if isinstance(error, DBAPIError):
                error = error.orig
            if isinstance(error, PostgresError) and re.fullmatch(
                r"[A-Z0-9]{5}", error.sqlstate or ""
            ):
                detail += f" | SQLSTATE {error.sqlstate}"
            self.details.append(detail)
        return report


def main(target):
    diagnostic = Diagnostic()
    # Suppress raw pytest, driver and subprocess output, including setup errors.
    with open(os.devnull, "wb") as sink:
        stdout, stderr = os.dup(1), os.dup(2)
        try:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            status = pytest.main(
                ["-p", "no:cacheprovider", "--capture=fd", target], plugins=[diagnostic]
            )
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.dup2(stdout, 1)
            os.dup2(stderr, 2)
            os.close(stdout)
            os.close(stderr)
    for detail in diagnostic.details:
        print(detail)
    print(
        f"Focused PostgreSQL: {diagnostic.passed} passed, {diagnostic.failed} failed, "
        f"{diagnostic.skipped} skipped; pytest exit {int(status)}"
    )
    if not diagnostic.passed and not diagnostic.details:
        print("FAIL no passing tests or test reports; check selection and runner setup")
    return int(
        status != pytest.ExitCode.OK
        or diagnostic.failed
        or diagnostic.skipped
        or not diagnostic.passed
        or ("::" in target and diagnostic.passed != 1)
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
