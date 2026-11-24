"""Host-owned compatibility handshake must reject unsafe observer requests."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ops.compat import ci_host
from ops.compat.ci_host import Supervisor


def test_disposable_host_rejects_default_database_before_docker(monkeypatch):
    def no_docker(*_args, **_kwargs):
        pytest.fail("Unsafe request reached Docker/PostgreSQL host")

    monkeypatch.setattr("ops.compat.ci_host.compose", no_docker)
    host = Supervisor.__new__(Supervisor)
    with pytest.raises(RuntimeError, match="Invalid disposable database"):
        host.validate(
            {
                "version": 1,
                "kind": "booking_source",
                "db_name": "carepulse",
                "target_id": "t-carepulse",
                "source_manifest_id": "source-0040",
            }
        )


def test_host_refuses_forged_owner_and_head(monkeypatch):
    monkeypatch.setattr(
        "ops.compat.ci_host.compose",
        lambda *_args, **_kwargs: (
            "t-compat_mid_abcdefghijkl|123456|compat_mid_abcdefghijkl|MIGRATING|5|"
            "source-0040|candidate-0046|none|true|0041_f01_runtime_identity"
        ),
    )
    host = Supervisor.__new__(Supervisor)
    request = {
        "version": 1,
        "kind": "intermediate",
        "step": 0,
        "head": "0041_f01_runtime_identity",
        "db_name": "compat_mid_abcdefghijkl",
        "target_id": "t-compat_mid_abcdefghijkl",
        "cluster_id": "123456",
        "source_manifest_id": "source-0040",
        "candidate_manifest_id": "candidate-0046",
        "owner_epoch": 4,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }
    with pytest.raises(RuntimeError, match="owner epoch"):
        host.validate(request)


def test_host_uses_explicit_boolean_encoding_and_denies_unsealed_source(monkeypatch):
    request = {
        "version": 1,
        "kind": "intermediate",
        "step": 0,
        "head": "0041_f01_runtime_identity",
        "db_name": "compat_mid_abcdefghijkl",
        "target_id": "t-compat_mid_abcdefghijkl",
        "cluster_id": "123456",
        "source_manifest_id": "source-0040",
        "candidate_manifest_id": "candidate-0046",
        "owner_epoch": 4,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }

    def sealed_query(*args, **_kwargs):
        assert "CASE WHEN t.source_sealed THEN 'true' ELSE 'false' END" in args[-1]
        return (
            "t-compat_mid_abcdefghijkl|123456|compat_mid_abcdefghijkl|MIGRATING|4|"
            "source-0040|candidate-0046|none|false|0041_f01_runtime_identity"
        )

    monkeypatch.setattr("ops.compat.ci_host.compose", sealed_query)
    with pytest.raises(RuntimeError, match="source-seal"):
        Supervisor.__new__(Supervisor).validate(request)


@pytest.mark.parametrize("image_matches", [True, False])
def test_sender_attests_actual_container_image_and_removes_it(
    monkeypatch, image_matches
):
    image = "sha256:" + "a" * 64
    container = "b" * 64
    removed = []
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    monkeypatch.setattr(
        "ops.compat.ci_host.command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="Delivery unavailable\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        "ops.compat.ci_host.required",
        lambda *_args, **_kwargs: (
            container + "|" + (image if image_matches else "sha256:" + "c" * 64)
        ),
    )
    host = Supervisor.__new__(Supervisor)
    host.observed = {"services": {"source-sender": {"image_id": image}}}
    host.remove = removed.append
    request = {
        "source_manifest_id": "source-0040",
        "db_name": "compat_mid_abcdefghijkl",
        "target_id": "t-compat_mid_abcdefghijkl",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }
    if image_matches:
        result = host.sender("source-sender", request, "d" * 24)
        assert result["container_id"] == container
        assert result["image_id"] == image
    else:
        with pytest.raises(RuntimeError, match="image mismatches"):
            host.sender("source-sender", request, "d" * 24)
    assert removed == ["carepulse-ci-123-1-" + "d" * 12 + "-source-sender"]


@pytest.mark.parametrize("remove_ok", [True, False])
def test_source_stop_requires_verified_container_removal(monkeypatch, remove_ok):
    image = "sha256:" + "a" * 64
    cid = "b" * 64
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    monkeypatch.setattr(
        "ops.compat.ci_host.compose",
        lambda *_args, **_kwargs: (
            "t-compat_demo|compat_demo|SOURCE_OPEN|1|source-0040|source|false"
        ),
    )

    def docker(args, **_kwargs):
        if args[:3] == ("docker", "inspect", "--format"):
            return SimpleNamespace(returncode=0, stdout=f"{cid}|{image}|no", stderr="")
        if args[:3] == ("docker", "rm", "-f"):
            return SimpleNamespace(
                returncode=0 if remove_ok else 1,
                stdout=cid if remove_ok else "",
                stderr="",
            )
        if args[:2] == ("docker", "inspect"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[:2] == ("docker", "ps"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        pytest.fail(f"unexpected Docker request: {args}")

    monkeypatch.setattr("ops.compat.ci_host.command", docker)
    host = Supervisor.__new__(Supervisor)
    request = {
        "version": 1,
        "id": "a" * 24,
        "kind": "booking_source",
        "db_name": "compat_demo",
        "target_id": "t-compat_demo",
        "source_manifest_id": "source-0040",
        "owner_epoch": 1,
    }
    host.active = {request["id"]: (cid, request)}
    host.sealed_source_targets = set()
    host.stopped_source_ids = {}
    host.observed = {"services": {"source-api": {"image_id": image}}}
    if not remove_ok:
        with pytest.raises(RuntimeError, match="command failed"):
            host.stop_source(request)
        assert request["id"] in host.active
        return
    proof = host.stop_source(request)
    assert proof["status"] == "stopped"
    assert proof["owner_epoch"] == 1
    assert proof["source_container_id"] == cid
    assert proof["launch_sealed"] is False
    assert not host.active
    assert host.stopped_source_ids["t-compat_demo"] == cid
    with pytest.raises(RuntimeError, match="sealed"):
        host.launch("source-api", request, name="unsafe-restart")


@pytest.mark.parametrize(
    ("record_phase", "head", "running", "allowed"),
    [
        ("DRAINING", "0040_t173_numeric_money", "", True),
        ("MIGRATING", "0042_f01_effective_grants", "", True),
        ("MIGRATING", "0042_f01_effective_grants", "candidate-api|cafe123", False),
        ("SOURCE_OPEN", "0040_t173_numeric_money", "", False),
        ("DRAINING", "0040_t173_numeric_money", "source-worker|cafe123", False),
    ],
)
def test_host_drain_proof_requires_sealed_epoch_and_absent_consumers(
    monkeypatch,
    record_phase,
    head,
    running,
    allowed,
):
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    monkeypatch.setattr(
        "ops.compat.ci_host.compose",
        lambda *_args, **_kwargs: (
            f"t-compat_demo|123|compat_demo|{record_phase}|2|"
            f"source-0040|candidate-0046|none|true|{head}"
        ),
    )
    monkeypatch.setattr(
        "ops.compat.ci_host.required",
        lambda *_args, **_kwargs: running,
    )
    host = Supervisor.__new__(Supervisor)
    host.active = {}
    host.sealed_source_targets = set()
    host.stopped_source_ids = {}
    request = {
        "version": 1,
        "kind": "drain_proof",
        "id": "a" * 24,
        "db_name": "compat_demo",
        "target_id": "t-compat_demo",
        "cluster_id": "123",
        "source_manifest_id": "source-0040",
        "candidate_manifest_id": "candidate-0046",
        "owner_epoch": 2,
        "head": head,
    }
    if not allowed:
        with pytest.raises(RuntimeError):
            host.drain_proof(request)
        return
    response = host.drain_proof(request)
    assert response["ok"] is True
    assert response["running_consumer_count"] == 0
    assert response["owner_epoch"] == 2
    assert response["launch_sealed"] is True
    assert response["stop_phase"] == record_phase
    assert len(response["stopped_services"]) == 8


def _request_host(tmp_path, kind="intermediate", **fields):
    host = Supervisor.__new__(Supervisor)
    host.directory = tmp_path / "requests"
    host.directory.mkdir()
    host.response_directory = tmp_path / "replies"
    host.response_directory.mkdir()
    host.active = {}
    host.processed = set()
    host.stopped = False
    token = "a" * 24
    request = {"version": 1, "id": token, "kind": kind, **fields}
    (host.directory / f"{token}.request.json").write_text(json.dumps(request))
    return host, host.response_directory / f"{token}.response.json"


def _diagnostics(capsys):
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    records = [
        json.loads(line.removeprefix("CI_HOST_DIAGNOSTIC "))
        for line in lines
        if line.startswith("CI_HOST_DIAGNOSTIC ")
    ]
    for record in records:
        assert record.keys() <= {
            "event",
            "checkpoint",
            "operation_phase",
            "generation",
            "failure_kind",
            "operation_category",
            "timestamp",
            "elapsed_seconds",
            "deadline_seconds",
            "exit_code",
            "exception_class",
            "expected_head",
            "response_kind",
        }
        assert record["timestamp"] > 0
        assert record["elapsed_seconds"] >= 0
    assert len(records) == len(lines)
    return records, captured.out + captured.err


@pytest.mark.parametrize("generation", ["source", "candidate"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_intermediate_timeout_records_primary_before_cleanup(
    tmp_path, monkeypatch, capsys, generation, cleanup_fails
):
    secret = "sensitive-command-and-output"
    host, reply = _request_host(tmp_path, head=ci_host.SUCCESSORS[0], db_name=secret)
    host.validate = lambda _: None
    host.mail_count = lambda: 0
    host.launch = lambda *_args, **_kwargs: secret
    actual_worker = host.worker
    host.worker = lambda service, request, token: (
        actual_worker(service, request, token)
        if service.startswith(generation + "-")
        else {}
    )
    host.sender = lambda *_args: {}
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    removed = []

    def run(args, **kwargs):
        if args[:3] == ("docker", "rm", "-f"):
            removed.append(args[-1])
            return SimpleNamespace(
                returncode=17 if cleanup_fails else 0, stdout=secret, stderr=secret
            )
        assert kwargs["timeout"] == 3
        raise subprocess.TimeoutExpired(args, 3, output=secret, stderr=secret)

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    expected = RuntimeError if cleanup_fails else subprocess.TimeoutExpired
    with pytest.raises(expected):
        host.run()
    assert removed == [secret]
    assert json.loads(reply.read_text()) == {
        "version": 1,
        "status": "blocked",
        "ok": False,
    }
    assert host.stopped
    records, output = _diagnostics(capsys)
    failures = [record for record in records if record["event"] == "failure"]
    assert len(failures) == (2 if cleanup_fails else 1)
    primary = failures[0]
    assert primary["exception_class"] == "TimeoutExpired"
    assert primary["failure_kind"] == "primary"
    assert primary["operation_phase"] == "intermediate"
    assert primary["generation"] == generation
    assert primary["expected_head"] == ci_host.SUCCESSORS[0]
    assert primary["checkpoint"].startswith("worker:")
    assert primary["deadline_seconds"] == 3
    assert "exit_code" not in primary
    if cleanup_fails:
        cleanup = failures[1]
        assert cleanup["failure_kind"] == "cleanup"
        assert cleanup["exception_class"] == "RuntimeError"
        assert cleanup["generation"] == generation
        assert cleanup["deadline_seconds"] == 10
        assert cleanup["exit_code"] == 17
        assert cleanup["checkpoint"].startswith("remove:")
    assert secret not in output


@pytest.mark.parametrize("primary_fails", [False, True])
@pytest.mark.parametrize("blocked_fails", [False, True])
def test_response_emission_failure_preserves_original_evidence(
    tmp_path, monkeypatch, capsys, primary_fails, blocked_fails
):
    secret = "private-exception-payload"
    host, reply = _request_host(tmp_path, kind="booking_source")
    primary = ValueError(secret)
    normal_error = OSError(secret)
    blocked_error = PermissionError(secret)
    actual_emit = ci_host.emit

    def book(_request):
        if primary_fails:
            raise primary
        return {"status": "ready"}

    def emit(path, payload):
        if payload["status"] != "blocked":
            raise normal_error
        if blocked_fails:
            raise blocked_error
        actual_emit(path, payload)

    host.book_source = book
    monkeypatch.setattr(ci_host, "emit", emit)
    expected = (
        blocked_error if blocked_fails else primary if primary_fails else normal_error
    )
    with pytest.raises(type(expected)) as caught:
        host.run()
    assert caught.value is expected
    assert host.stopped is not blocked_fails
    if not blocked_fails:
        assert json.loads(reply.read_text()) == {
            "version": 1,
            "status": "blocked",
            "ok": False,
        }
    else:
        assert not reply.exists()
    records, output = _diagnostics(capsys)
    failures = [record for record in records if record["event"] == "failure"]
    assert len(failures) == (2 if blocked_fails else 1)
    assert failures[0]["exception_class"] == (
        "ValueError" if primary_fails else "OSError"
    )
    assert failures[0]["failure_kind"] == (
        "primary" if primary_fails else "response_emission"
    )
    if not primary_fails:
        assert failures[0]["response_kind"] == "normal"
    if blocked_fails:
        assert failures[1]["failure_kind"] == "response_emission"
        assert failures[1]["response_kind"] == "blocked"
        assert failures[1]["exception_class"] == "PermissionError"
    assert all(record["generation"] == "source" for record in failures)
    assert all(record["operation_phase"] == "booking_source" for record in failures)
    assert secret not in output


def test_main_cleanup_cannot_erase_primary_diagnostic(tmp_path, monkeypatch, capsys):
    secret = "do-not-print-this-exception"
    host, reply = _request_host(tmp_path, kind="booking_source")

    def fail(_request):
        raise RuntimeError(secret)

    def close():
        raise OSError(secret)

    host.book_source = fail
    host.close = close
    monkeypatch.setattr(ci_host, "Supervisor", lambda *_args: host)
    monkeypatch.setattr(ci_host.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["ci_host", "requests", "observed", "replies"])
    with pytest.raises(OSError):
        ci_host.main()
    assert json.loads(reply.read_text())["status"] == "blocked"
    records, output = _diagnostics(capsys)
    failures = [record for record in records if record["event"] == "failure"]
    assert [(r["failure_kind"], r["exception_class"]) for r in failures] == [
        ("primary", "RuntimeError"),
        ("cleanup", "OSError"),
    ]
    assert failures[1]["operation_phase"] == "shutdown"
    assert secret not in output


def test_cli_startup_failure_has_no_raw_traceback(tmp_path):
    requests = tmp_path / "requests"
    requests.mkdir()
    replies = tmp_path / "replies"
    replies.mkdir()
    secret = "private-observed-file"
    observed = tmp_path / secret
    observed.write_text("not-json")
    env = {
        **os.environ,
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "GITHUB_REPOSITORY": "ahmedali5696/care-pulse-3",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/diagnostic-test",
        "CP_CI_PROJECT": "carepulse-ci-123-1",
        "COMPOSE_PROJECT_NAME": "carepulse-ci-123-1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ops.compat.ci_host",
            str(requests),
            str(observed),
            str(replies),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert secret not in result.stdout
    assert "Traceback" not in result.stdout
    record = json.loads(result.stdout.removeprefix("CI_HOST_DIAGNOSTIC "))
    assert record["exception_class"] == "JSONDecodeError"
    assert record["operation_phase"] == "startup"
    assert record["failure_kind"] == "primary"


def test_poll_deadline_and_expected_probe_exit_are_distinct(
    tmp_path, monkeypatch, capsys
):
    host, reply = _request_host(
        tmp_path,
        kind="booking_source",
        db_name="compat_demo",
        target_id="t-compat_demo",
    )
    host.validate = lambda _: None
    host.sealed_source_targets = set()
    host.launch = lambda *_args, **_kwargs: "private-container"
    removed = []
    host.remove = removed.append
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    clock = [0.0]
    monkeypatch.setattr(ci_host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        ci_host.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def run(_args, **kwargs):
        assert kwargs["timeout"] == 4
        clock[0] += 4
        return SimpleNamespace(
            returncode=1, stdout="private-stdout", stderr="private-stderr"
        )

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Source image did not become live"):
        host.run()
    assert json.loads(reply.read_text())["status"] == "blocked"
    assert removed == ["private-container"]
    records, output = _diagnostics(capsys)
    results = [record for record in records if record["event"] == "subprocess_result"]
    assert len(results) == 3
    assert all(
        r["exit_code"] == 1 and r["deadline_seconds"] == 4 and "failure_kind" not in r
        for r in results
    )
    failure = records[-1]
    assert failure["failure_kind"] == "primary"
    assert failure["deadline_seconds"] == 12
    assert failure["elapsed_seconds"] >= 12
    assert failure["checkpoint"].startswith("book_source:")
    assert "private-" not in output


def test_sender_denial_failure_records_exit_code_without_streams(
    tmp_path, monkeypatch, capsys
):
    secret = "sensitive-sender-output"
    image = "sha256:" + "a" * 64
    host, reply = _request_host(
        tmp_path,
        head=ci_host.SUCCESSORS[2],
        source_manifest_id="source-0040",
        db_name="compat_demo",
        target_id="t-compat_demo",
        tenant_id=secret,
        job_id=secret,
    )
    host.validate = lambda _: None
    host.mail_count = lambda: 0
    host.worker = lambda *_args: {}
    host.observed = {"services": {"source-sender": {"image_id": image}}}
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    removed = []
    host.remove = removed.append

    def run(args, **_kwargs):
        if args[:2] == ("docker", "inspect"):
            return SimpleNamespace(
                returncode=0, stdout="b" * 64 + "|" + image, stderr=secret
            )
        return SimpleNamespace(returncode=9, stdout=secret, stderr=secret)

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="sender denial mismatch"):
        host.run()
    assert json.loads(reply.read_text())["status"] == "blocked"
    assert len(removed) == 1
    records, output = _diagnostics(capsys)
    failure = [r for r in records if r["event"] == "failure"]
    assert len(failure) == 1
    assert failure[0]["checkpoint"] == "sender:denial_mismatch"
    assert failure[0]["generation"] == "source"
    assert failure[0]["exit_code"] == 9
    assert failure[0]["expected_head"] == ci_host.SUCCESSORS[2]
    assert secret not in output


def test_primary_blocked_response_and_shutdown_failures_all_remain_visible(
    tmp_path, monkeypatch, capsys
):
    secret = "private-three-way-failure"
    host, _reply = _request_host(tmp_path, kind="drain_proof", head=secret)

    def drain(_request):
        raise ValueError(secret)

    def emit(*_args):
        raise FileExistsError(secret)

    def close():
        raise PermissionError(secret)

    host.drain_proof = drain
    host.close = close
    monkeypatch.setattr(ci_host, "emit", emit)
    monkeypatch.setattr(ci_host, "Supervisor", lambda *_args: host)
    monkeypatch.setattr(ci_host.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["ci_host", "requests", "observed", "replies"])
    with pytest.raises(PermissionError):
        ci_host.main()
    assert not host.stopped
    records, output = _diagnostics(capsys)
    assert [(r["failure_kind"], r["exception_class"]) for r in records] == [
        ("primary", "ValueError"),
        ("response_emission", "FileExistsError"),
        ("cleanup", "PermissionError"),
    ]
    assert records[0]["operation_phase"] == "drain_proof"
    assert records[0]["generation"] == "both"
    assert all("expected_head" not in r for r in records)
    assert secret not in output


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_launch_image_mismatch_is_recorded_before_removal(
    tmp_path, monkeypatch, capsys, cleanup_fails
):
    secret = "private-container-id"
    host, reply = _request_host(
        tmp_path,
        kind="booking_source",
        db_name="compat_demo",
        target_id="t-compat_demo",
        source_manifest_id="source-0040",
    )
    host.validate = lambda _: None
    host.sealed_source_targets = set()
    host.observed = {"services": {"source-api": {"image_id": "expected"}}}
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    removed = []

    def run(args, **_kwargs):
        if args[:3] == ("docker", "rm", "-f"):
            removed.append(args[-1])
            return SimpleNamespace(
                returncode=6 if cleanup_fails else 0, stdout="", stderr=secret
            )
        if args[:2] == ("docker", "inspect"):
            return SimpleNamespace(returncode=0, stdout="unexpected", stderr=secret)
        return SimpleNamespace(returncode=0, stdout=secret, stderr="")

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    expected = "command failed" if cleanup_fails else "runtime image mismatches"
    with pytest.raises(RuntimeError, match=expected):
        host.run()
    assert json.loads(reply.read_text())["status"] == "blocked"
    assert removed == [secret]
    records, output = _diagnostics(capsys)
    failures = [r for r in records if r["event"] == "failure"]
    assert len(failures) == (2 if cleanup_fails else 1)
    assert failures[0]["checkpoint"] == "launch:image_mismatch"
    assert failures[0]["failure_kind"] == "primary"
    if cleanup_fails:
        assert failures[1]["failure_kind"] == "cleanup"
        assert failures[1]["exit_code"] == 6
    assert secret not in output


def test_diagnostic_sink_failure_does_not_change_blocked_response(
    tmp_path, monkeypatch
):
    host, reply = _request_host(tmp_path, kind="booking_source")
    primary = ValueError("private")

    def fail(_request):
        raise primary

    def unavailable(*_args, **_kwargs):
        raise BrokenPipeError("private")

    host.book_source = fail
    monkeypatch.setattr("builtins.print", unavailable)
    with pytest.raises(ValueError) as caught:
        host.run()
    assert caught.value is primary
    assert host.stopped
    assert json.loads(reply.read_text()) == {
        "version": 1,
        "status": "blocked",
        "ok": False,
    }


@pytest.mark.parametrize(("probe_name", "exit_code"), [("live", 7), ("ready", 0)])
@pytest.mark.parametrize("generation", ["source", "candidate"])
def test_worker_probe_assertion_reports_its_own_exit_and_deadline(
    tmp_path, monkeypatch, capsys, probe_name, exit_code, generation
):
    host, reply = _request_host(tmp_path, head=ci_host.SUCCESSORS[1])
    host.validate = lambda _: None
    host.mail_count = lambda: 0
    host.launch = lambda *_args, **_kwargs: "private-container"
    host.remove = lambda _: None
    worker = host.worker
    host.worker = lambda service, request, token: (
        worker(service, request, token) if service.startswith(generation + "-") else {}
    )
    host.sender = lambda *_args: {}
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    secret = "private-probe-stream"

    def run(args, **kwargs):
        if args[3] == "cat":
            return SimpleNamespace(
                returncode=0, stdout='{"alive":true,"ready":false}', stderr=secret
            )
        assert kwargs["timeout"] == ci_host.RUN_TIMEOUT
        return SimpleNamespace(
            returncode=exit_code if args[-1] == probe_name else 0,
            stdout=secret,
            stderr=secret,
        )

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        host.run()
    assert json.loads(reply.read_text())["status"] == "blocked"
    records, output = _diagnostics(capsys)
    failures = [r for r in records if r["event"] == "failure"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["generation"] == generation
    assert failure["operation_phase"] == "intermediate"
    assert failure["failure_kind"] == "primary"
    assert failure["exit_code"] == exit_code
    assert failure["deadline_seconds"] == ci_host.RUN_TIMEOUT
    assert failure["checkpoint"].startswith("worker:")
    assert secret not in output


@pytest.mark.parametrize("primary_fails", [False, True])
def test_actual_atomic_emit_collision_preserves_first_failure(
    tmp_path, capsys, primary_fails
):
    host, reply = _request_host(tmp_path, kind="booking_source")
    secret = "private-collision-payload"
    temp = reply.with_suffix(reply.suffix + ".tmp")
    temp.write_text(secret)

    def book(_request):
        if primary_fails:
            raise KeyError(secret)
        return {"status": "ready"}

    host.book_source = book
    with pytest.raises(FileExistsError):
        host.run()
    assert not reply.exists()
    assert temp.read_text() == secret
    records, output = _diagnostics(capsys)
    failures = [r for r in records if r["event"] == "failure"]
    assert len(failures) == 2
    assert failures[0]["exception_class"] == (
        "KeyError" if primary_fails else "FileExistsError"
    )
    assert failures[0]["failure_kind"] == (
        "primary" if primary_fails else "response_emission"
    )
    assert failures[1]["failure_kind"] == "response_emission"
    assert failures[1]["response_kind"] == "blocked"
    assert failures[1]["exception_class"] == "FileExistsError"
    assert failures[1]["checkpoint"].startswith("emit:")
    assert all("exit_code" not in r and "deadline_seconds" not in r for r in failures)
    assert secret not in output


def test_real_subprocess_streams_are_not_published(capsys):
    secret = 'CI_HOST_DIAGNOSTIC {"private":"subprocess-canary"}'
    args = [
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1]); print(sys.argv[1], file=sys.stderr); sys.exit(13)",
        secret,
    ]
    with (
        ci_host._diagnostic_scope("intermediate", generation="candidate"),
        pytest.raises(RuntimeError, match="CI-host command failed"),
    ):
        ci_host.required(args, timeout=5)
    records, output = _diagnostics(capsys)
    assert len(records) == 2
    assert records[0]["event"] == "subprocess_result"
    assert records[1]["failure_kind"] == "primary"
    assert all(r["exit_code"] == 13 and r["deadline_seconds"] == 5 for r in records)
    assert all(r["generation"] == "candidate" for r in records)
    assert secret not in output


@pytest.mark.parametrize("base", [RuntimeError, Exception])
def test_unknown_exception_class_uses_safe_base_or_fallback(capsys, base):
    private_type = type("PrivateExceptionCanary", (base,), {})
    error = private_type("private-exception-message")
    with (
        pytest.raises(private_type) as caught,
        ci_host._diagnostic_scope("intermediate", generation="source"),
    ):
        raise error
    assert caught.value is error
    records, output = _diagnostics(capsys)
    assert len(records) == 1
    assert records[0]["exception_class"] == (
        "RuntimeError" if base is RuntimeError else "OtherException"
    )
    assert "PrivateExceptionCanary" not in output
    assert "private-exception-message" not in output
    assert "exit_code" not in records[0] and "deadline_seconds" not in records[0]


@pytest.mark.parametrize("kind", ["drain_proof", "stop_source"])
def test_source_container_presence_assertion_has_source_exit_evidence(
    tmp_path, monkeypatch, capsys, kind
):
    image = "sha256:" + "a" * 64
    cid = "b" * 64
    token = "a" * 24
    target = "t-compat_demo"
    secret = "private-container-probe"
    host, reply = _request_host(
        tmp_path,
        kind="booking_source" if kind == "stop_source" else kind,
        db_name="compat_demo",
        target_id=target,
        source_manifest_id="source-0040",
        candidate_manifest_id="candidate-0046",
        cluster_id="123",
        owner_epoch=2,
        head=ci_host.SOURCE_HEAD,
    )
    host.sealed_source_targets = set()
    host.stopped_source_ids = {target: cid}
    host.observed = {"services": {"source-api": {"image_id": image}}}
    if kind == "stop_source":
        request_path = host.directory / f"{token}.request.json"
        host.active[token] = (cid, json.loads(request_path.read_text()))
        request_path.rename(host.directory / f"{token}.stop.json")
        reply = host.response_directory / f"{token}.stopped.json"
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")

    def run(args, **_kwargs):
        if args[:2] == ("docker", "compose"):
            value = (
                f"{target}|compat_demo|DRAINING|2|source-0040|none|false"
                if kind == "stop_source"
                else f"{target}|123|compat_demo|DRAINING|2|source-0040|candidate-0046|none|true|{ci_host.SOURCE_HEAD}"
            )
        elif args[:3] == ("docker", "inspect", "--format"):
            value = f"{cid}|{image}|no"
        elif args[:2] == ("docker", "inspect"):
            value = secret
        else:
            value = ""
        return SimpleNamespace(returncode=0, stdout=value, stderr=secret)

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="(still exists|restarted)"):
        host.run()
    assert json.loads(reply.read_text()) == {
        "version": 1,
        "status": "blocked",
        "ok": False,
    }
    records, output = _diagnostics(capsys)
    failures = [r for r in records if r["event"] == "failure"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["operation_phase"] == kind
    assert failure["generation"] == "source"
    assert failure["exit_code"] == 0
    assert failure["deadline_seconds"] == ci_host.RUN_TIMEOUT
    assert failure["checkpoint"].startswith(kind + ":")
    assert secret not in output


def test_real_failure_chain_retains_execution_before_emission_and_cleanup(
    tmp_path, monkeypatch, capsys
):
    secret = "private-nested-exception"
    host, reply = _request_host(tmp_path, head=ci_host.SUCCESSORS[2])
    host.validate = lambda _: None
    host.mail_count = lambda: 0
    host.launch = lambda *_args, **_kwargs: "worker-container"
    host.active["source-token"] = ("source-container", {})
    reply.with_suffix(reply.suffix + ".tmp").write_text(secret)
    monkeypatch.setenv("CP_CI_PROJECT", "carepulse-ci-123-1")
    removed = []

    def run(args, **kwargs):
        if args[:3] == ("docker", "rm", "-f"):
            removed.append(args[-1])
            return SimpleNamespace(
                returncode=17 if args[-1] == "worker-container" else 19,
                stdout=secret,
                stderr=secret,
            )
        raise subprocess.TimeoutExpired(
            args, kwargs["timeout"], output=secret, stderr=secret
        )

    monkeypatch.setattr(ci_host.subprocess, "run", run)
    monkeypatch.setattr(ci_host, "Supervisor", lambda *_args: host)
    monkeypatch.setattr(ci_host.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["ci_host", "requests", "observed", "replies"])
    with pytest.raises(RuntimeError, match="CI-host command failed"):
        ci_host.main()
    assert removed == ["worker-container", "source-container"]
    assert not reply.exists()
    records, output = _diagnostics(capsys)
    failures = [r for r in records if r["event"] == "failure"]
    assert [(r["failure_kind"], r["exception_class"]) for r in failures] == [
        ("primary", "TimeoutExpired"),
        ("cleanup", "RuntimeError"),
        ("response_emission", "FileExistsError"),
        ("cleanup", "RuntimeError"),
    ]
    assert "exit_code" not in failures[0]
    assert failures[0]["deadline_seconds"] == 3
    assert failures[1]["exit_code"] == 17
    assert failures[2]["response_kind"] == "blocked"
    assert "deadline_seconds" not in failures[2]
    assert failures[3]["operation_phase"] == "shutdown"
    assert failures[3]["exit_code"] == 19
    assert secret not in output
