"""Temporary hosted D-08 observation plugin; no test or application mutations."""

import linecache
import os
import re
from pathlib import Path

import pytest

_stream = None


def pytest_configure(config):
    global _stream
    # Keep progress visible while pytest captures ordinary test output.
    _stream = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")


def pytest_unconfigure(config):
    if _stream is not None:
        _stream.close()


def emit(message):
    print(f"D08-DIAG {message}", file=_stream, flush=True)


def sqlstate(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{5}", value)
        else None
    )


def exception_metadata(error):
    seen = set()
    pending = [error]
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        state = sqlstate(getattr(item, "sqlstate", None))
        emit(f"EXCEPTION {type(item).__name__} SQLSTATE={state or '-'}")
        if isinstance(item, AssertionError):
            assertion = str(item).splitlines()[0] if str(item) else ""
            if re.fullmatch(
                r"assert (?:True|False|None|[0-9]+|'[A-Z0-9]{5}')"
                r"(?: (?:==|!=|is|is not) (?:True|False|None|[0-9]+|'[A-Z0-9]{5}'))?",
                assertion,
            ):
                emit(f"ASSERT {assertion}")
        diag = getattr(item, "diag", None)
        for field in (
            "schema_name",
            "table_name",
            "constraint_name",
            "source_function",
        ):
            value = getattr(diag, field, None)
            if isinstance(value, str) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", value
            ):
                emit(f"PG_DIAG {field}={value}")
        pending.extend(
            getattr(item, name, None) for name in ("orig", "__cause__", "__context__")
        )


def pytest_collection_finish(session):
    for index, item in enumerate(session.items, 1):
        emit(f"COLLECT {index}/{len(session.items)} {item.nodeid}")


def pytest_runtest_logstart(nodeid, location):
    emit(f"START {nodeid}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    emit(f"CALL {item.nodeid}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    emit(f"TEARDOWN {item.nodeid}")


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    emit(f"FIXTURE START {fixturedef.argname} {request.node.nodeid}")
    yield
    emit(f"FIXTURE END {fixturedef.argname} {request.node.nodeid}")


def pytest_fixture_post_finalizer(fixturedef, request):
    emit(f"FIXTURE FINALIZED {fixturedef.argname} {request.node.nodeid}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    emit(f"RESULT {report.when} {report.outcome} {report.nodeid}")
    if not report.failed or call.excinfo is None:
        return
    # Never print exception text, SQL parameters, captured output, or local values.
    # Only static source, exception classes, SQLSTATEs and schema identifiers.
    try:
        exception_metadata(call.excinfo.value)
        for entry in call.excinfo.traceback:
            path = Path(entry.path)
            if not path.is_absolute():
                continue
            try:
                relative = path.relative_to(Path.cwd())
            except ValueError:
                continue
            number = entry.lineno + 1
            emit(
                f"FRAME {relative}:{number} {linecache.getline(str(path), number).strip()}"
            )
            if relative.parts[:2] != ("tests", "integration"):
                continue
            local = entry.frame.f_locals
            expected = sqlstate(local.get("expected_state"))
            denied = local.get("denied")
            if isinstance(denied, pytest.ExceptionInfo):
                try:
                    actual = sqlstate(
                        getattr(getattr(denied.value, "orig", None), "sqlstate", None)
                    )
                except AssertionError:
                    actual = None
                if actual or expected:
                    emit(
                        f"ASSERT SQLSTATE actual={actual or '-'} expected={expected or 'see source'}"
                    )
    except Exception as error:  # noqa: BLE001 - instrumentation must not replace test results.
        emit(f"METADATA UNAVAILABLE {type(error).__name__}")
