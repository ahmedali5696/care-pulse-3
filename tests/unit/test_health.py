"""Deterministic health deadlines and process heartbeat boundary checks."""

import json
import os
import subprocess

import pytest
from carepulse import health

from apps.worker import status


def test_liveness_has_no_dependency_call(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Liveness attempted a dependency probe")

    monkeypatch.setattr(subprocess, "run", forbidden)
    response = health.live()
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "alive"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("outcome", ["ready", "failed", "timeout", "os_error"])
def test_readiness_is_sanitized_and_timeout_is_unready(monkeypatch, outcome):
    def dependency_process(*args, **kwargs):
        assert kwargs["timeout"] == health.PROBE_DEADLINE_SECONDS
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("synthetic-secret-not-for-response", 4)
        if outcome == "os_error":
            raise OSError("synthetic-secret-not-for-response")
        return subprocess.CompletedProcess(["probe"], 0 if outcome == "ready" else 1)

    monkeypatch.setattr(subprocess, "run", dependency_process)
    response = health.ready()
    assert response.status_code == (200 if outcome == "ready" else 503)
    assert json.loads(response.body) == {
        "status": "ready" if outcome == "ready" else "not_ready"
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def heartbeat(monkeypatch, tmp_path):
    # /proc metadata and monotonic time are OS boundaries, not worker logic.
    monkeypatch.setattr(status, "process_identity", lambda pid: "boot:current-start")
    monkeypatch.setattr(status.time, "monotonic", lambda: 100.0)
    path = tmp_path / "status.json"
    status.write_status(path, "boot:current-start", True)
    return path


@pytest.mark.parametrize(
    ("age", "expected"),
    [(-0.001, False), (0, True), (10, True), (10.001, False)],
)
def test_heartbeat_accepts_only_inclusive_freshness_window(heartbeat, age, expected):
    saved = json.loads(heartbeat.read_text())
    saved["heartbeat"] = 100.0 - age
    heartbeat.write_text(json.dumps(saved))
    assert status.probe_status(heartbeat, "live", os.getpid()) is expected


@pytest.mark.parametrize("invalid", [True, "100", float("nan"), float("inf")])
def test_invalid_heartbeat_is_rejected(heartbeat, invalid):
    saved = json.loads(heartbeat.read_text())
    saved["heartbeat"] = invalid
    heartbeat.write_text(json.dumps(saved))
    assert not status.probe_status(heartbeat, "live", os.getpid())


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("pid", -1),
        ("pid", True),
        ("started", "previous-process"),
        ("alive", False),
        ("ready", "true"),
    ],
)
def test_mismatched_or_invalid_process_state_is_rejected(heartbeat, field, invalid):
    saved = json.loads(heartbeat.read_text())
    saved[field] = invalid
    heartbeat.write_text(json.dumps(saved))
    assert not status.probe_status(heartbeat, "live", os.getpid())


def test_fresh_live_process_can_be_unready_and_recover(heartbeat):
    status.write_status(heartbeat, "boot:current-start", False)
    assert status.probe_status(heartbeat, "live", os.getpid())
    assert not status.probe_status(heartbeat, "ready", os.getpid())
    status.write_status(heartbeat, "boot:current-start", True)
    assert status.probe_status(heartbeat, "ready", os.getpid())


def test_restart_rejects_old_identity_until_fresh_matching_status(heartbeat):
    status.write_status(heartbeat, "boot:previous-start", True)
    assert not status.probe_status(heartbeat, "live", os.getpid())
    status.write_status(heartbeat, "boot:current-start", True)
    assert status.probe_status(heartbeat, "ready", os.getpid())


def test_dead_process_and_missing_or_malformed_status_fail(heartbeat, monkeypatch):
    def dead_process(pid):
        raise FileNotFoundError("Synthetic terminated process")

    monkeypatch.setattr(status, "process_identity", dead_process)
    assert not status.probe_status(heartbeat, "live", os.getpid())
    heartbeat.write_text("malformed")
    assert not status.probe_status(heartbeat, "ready", os.getpid())
    heartbeat.unlink()
    assert not status.probe_status(heartbeat, "live", os.getpid())
