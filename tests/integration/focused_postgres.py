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

    def pytest_runtest_logreport(self, report):
        for name, detail in report.user_properties:
            if name == "boundary_profile":
                self.details.append(detail)

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
            if (
                name == "test_bulk_commit_serializes_with_included_row_changes"
                and path == "tests/integration/test_w3_bulk_races.py"
                and hasattr(item, "callspec")
            ):
                case = item.callspec.params
                if (
                    case.get("competitor") in ("admit", "reschedule", "archive")
                    and type(case.get("bulk_first")) is bool
                ):
                    detail += (
                        f" | B19 case {case['competitor']}"
                        f" bulk_first={str(case['bulk_first']).lower()}"
                    )
            error = call.excinfo.value if call.excinfo else None
            if isinstance(error, PostgresLockNotObserved):
                detail += f" | {error}"
            if isinstance(error, DBAPIError):
                error = error.orig
            if isinstance(error, PostgresError) and re.fullmatch(
                r"[A-Z0-9]{5}", error.sqlstate or ""
            ):
                detail += f" | SQLSTATE {error.sqlstate}"
                # Publish only identifiers and source line numbers, never SQL, values or error text.
                constraint = error.diag.constraint_name or ""
                if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", constraint):
                    detail += f" | constraint {constraint}"
                for routine, line_number in re.findall(
                    r"PL/pgSQL function (?:public\.|f01_private\.)?([a-z][a-z0-9_]{0,62})\([^\n]*?\) line ([0-9]+) at ",
                    error.diag.context or "",
                ):
                    detail += f" | routine {routine}:{line_number}"
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
