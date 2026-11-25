"""Local, container-free verification of the disposable profiling experiment."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from ops import foundation_acceptance
from ops import profile_ordinary_report as report


def run_probe(tmp_path, source):
    test = tmp_path / "test_probe.py"
    test.write_text(source, encoding="utf-8")
    output = tmp_path / "profile-probe.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ops.profile_ordinary",
            str(output),
            "-q",
            "-p",
            "no:cacheprovider",
            f"--rootdir={tmp_path}",
            f"--junitxml={tmp_path / 'probe.xml'}",
            str(test),
        ],
        capture_output=True,
        check=False,
    )
    return result, output


def test_real_pytest_profile_redacts_ids_and_times_fixture(tmp_path):
    result, output = run_probe(
        tmp_path,
        """
import pytest
import time
@pytest.fixture
def resource():
    yield 1
    time.sleep(0.01)
@pytest.mark.parametrize('number', [1, 2], ids=['PRIVATE_CANARY_A', 'PRIVATE_CANARY_B'])
def test_case(resource, number):
    assert resource <= number
""",
    )
    assert result.returncode == 0
    assert "PRIVATE_CANARY" not in output.read_text()
    cases = report.junit_cases(tmp_path, "probe", 2)
    timings = report.focus_timings(tmp_path, "probe", cases)
    assert len(timings["cases_in_execution_order"]) == 2
    assert timings["fixture_setup"]["resource"]["count"] == 2
    assert timings["generator_teardown_seconds"]["resource"] > 0
    assert set(timings["phase_totals_seconds"]) == set(report.PHASES)


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [
        ("def test_case(): assert False, 'PRIVATE_CANARY'", 1),
        ("def test_case(): pytest.skip('PRIVATE_CANARY')", 1),
        (
            "@pytest.mark.xfail(reason='PRIVATE_CANARY')\ndef test_case(): assert False",
            1,
        ),
        ("@pytest.mark.xfail(reason='PRIVATE_CANARY')\ndef test_case(): pass", 1),
        ("def helper(): pass", 5),
    ],
)
def test_pytest_failures_and_missing_tests_are_not_success(tmp_path, body, exit_code):
    result, output = run_probe(tmp_path, "import pytest\n" + body)
    assert result.returncode == exit_code
    assert "PRIVATE_CANARY" not in output.read_text()


def make_inventory(folder, variant):
    for name, count in report.REPORTS.items():
        if name in report.TAP:
            stats = {
                "tests": count,
                "pass_": count,
                "fail": 0,
                "cancelled": 0,
                "skipped": 0,
                "todo": 0,
            }
            (folder / f"{name}.tap").write_text(
                "".join(
                    f"# {key.rstrip('_')} {value}\n" for key, value in stats.items()
                )
            )
            continue
        suite = ET.Element(
            "testsuite", tests=str(count), failures="0", errors="0", skipped="0"
        )
        for i in range(count):
            case = ET.SubElement(
                suite,
                "testcase",
                classname="test_probe",
                name=f"test_case[PRIVATE_CANARY_{i}]",
            )
            ET.SubElement(case, "system-out").text = "PRIVATE_CANARY"
        ET.ElementTree(suite).write(folder / f"{name}.xml")
        if variant == "instrumented" and name in report.FOCUS:
            payload = {
                "kind": report.KIND,
                "pytest_exit": 0,
                "selected": count,
                "cases_in_execution_order": [
                    {
                        "file": "test_probe.py",
                        "test": "test_case",
                        "phases": {
                            phase: {"seconds": 0.1, "outcome": "passed"}
                            for phase in report.PHASES
                        },
                    }
                    for _ in range(count)
                ],
                "fixture_setup": {"fixture": {"count": count, "seconds": 1}},
                "generator_teardown_seconds": {"fixture": 1},
            }
            (folder / f"profile-{name}.json").write_text(json.dumps(payload))


@pytest.mark.parametrize("variant", ["control", "instrumented"])
def test_complete_inventory_and_publish_only_after_cleanup(
    tmp_path, monkeypatch, variant
):
    make_inventory(tmp_path, variant)
    report.collect(tmp_path, tmp_path / "report.json", variant, report.HEAD)
    (tmp_path / "timings.tsv").write_text(
        "".join(
            f"{event}\t{i * 1000000000}\n"
            for i, event in enumerate(report.expected_events())
        )
    )
    for key, value in {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "2",
        "ImageVersion": "20261116.133.1",
        "ImageOS": "ubuntu24",
    }.items():
        monkeypatch.setenv(key, value)
    private = tmp_path / "foundation-ci-private"
    private.mkdir()
    output = tmp_path / "published.json"
    with pytest.raises(ValueError):
        report.publish(tmp_path, output)
    assert not output.exists()
    private.rmdir()
    report.publish(tmp_path, output)
    payload = json.loads(output.read_text())
    assert payload["mandatory_cases"] == 1051
    assert payload["kind"] == report.KIND
    assert payload["cleanup"] == "verified"
    assert set(payload["focus"]) == (
        set(report.FOCUS) if variant == "instrumented" else set()
    )
    assert "PRIVATE_CANARY" not in output.read_text()


@pytest.mark.parametrize(
    "defect", ["missing", "count", "skip", "tap", "profile", "head"]
)
def test_invalid_inventory_is_rejected(tmp_path, defect):
    make_inventory(tmp_path, "instrumented")
    path = tmp_path / "f01.xml"
    head = report.HEAD
    if defect == "missing":
        path.unlink()
    elif defect in ("count", "skip"):
        tree = ET.parse(path)
        tree.getroot().set("tests" if defect == "count" else "skipped", "1")
        tree.write(path)
    elif defect == "tap":
        (tmp_path / "web-ingress.tap").write_text("# tests 2\n")
    elif defect == "profile":
        (tmp_path / "profile-f01.json").unlink()
    else:
        head = "wrong"
    output = tmp_path / "report.json"
    with pytest.raises(ValueError):
        report.collect(tmp_path, output, "instrumented", head)
    assert not output.exists()


def test_out_of_order_or_failed_invocation_is_rejected(tmp_path):
    events = list(report.expected_events())
    index = events.index("pytest:f01:end:0")
    for changed in (
        events[:index] + events[index + 1 :],
        events[:index] + ["pytest:f01:end:1"] + events[index + 1 :],
    ):
        path = tmp_path / "timings.tsv"
        path.write_text("".join(f"{event}\t{i}\n" for i, event in enumerate(changed)))
        with pytest.raises(ValueError):
            report.orchestration_timings(path)


@pytest.mark.parametrize(
    "override",
    [
        {"GITHUB_REF": "refs/heads/feature/ci-scalability"},
        {"GITHUB_HEAD_REF": "feature/ci-scalability"},
        {"GITHUB_WORKFLOW": "Ordinary Profiling Experiment (NOT ACCEPTANCE)"},
    ],
)
def test_acceptance_context_rejects_experiment(override):
    env = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/development",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "ahmedali5696/care-pulse-3",
        "GITHUB_WORKFLOW_REF": "ahmedali5696/care-pulse-3/.github/workflows/foundation.yml@refs/heads/development",
    }
    with pytest.raises(ValueError, match="profiling-only"):
        foundation_acceptance.github_context(env | override)


@pytest.fixture
def profiling_env():
    repo = "ahmedali5696/care-pulse-3"
    ref = "refs/heads/feature/ci-scalability"
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": ref,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": repo,
        "GITHUB_WORKFLOW": "Foundation",
        "GITHUB_WORKFLOW_REF": f"{repo}/.github/workflows/foundation.yml@{ref}",
        "INPUT_ORDINARY_PROFILE_SHA": "a" * 40,
    }


def test_authorized_profile_normalizes_only_two_nonacceptance_lanes(
    profiling_env, tmp_path
):
    request = foundation_acceptance.validate_request(profiling_env)
    assert request["mode"] == "ordinary-profile"
    assert json.loads(request["matrix"]) == ["profile-control", "profile-instrumented"]
    assert request["candidate_sha"] == profiling_env["GITHUB_SHA"]
    receipt = tmp_path / foundation_acceptance.RECEIPT_FILE
    with pytest.raises(ValueError, match="profiling-only"):
        foundation_acceptance.seal(receipt, profiling_env)
    assert not receipt.exists()
    with pytest.raises(ValueError, match="profiling-only"):
        foundation_acceptance.aggregate(tmp_path, profiling_env)


@pytest.mark.parametrize(
    "override",
    [
        {"INPUT_ORDINARY_PROFILE_SHA": ""},
        {"INPUT_ORDINARY_PROFILE_SHA": "b" * 40},
        {"INPUT_ORDINARY_PROFILE_SHA": "a" * 7},
        {"INPUT_B04_SPIKE": "true"},
        {"INPUT_T133_TERMINAL": "true"},
        {"INPUT_T133_SHA": "a" * 40},
        {"INPUT_POSTGRES_DIAGNOSTIC": "compat-us3"},
        {"INPUT_POSTGRES_DIAGNOSTIC": "w3-checkin"},
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {
            "GITHUB_REF": "refs/heads/development",
            "GITHUB_WORKFLOW_REF": "ahmedali5696/care-pulse-3/.github/workflows/foundation.yml@refs/heads/development",
        },
        {
            "GITHUB_REPOSITORY": "other/repo",
            "GITHUB_WORKFLOW_REF": "other/repo/.github/workflows/foundation.yml@refs/heads/feature/ci-scalability",
        },
        {
            "GITHUB_WORKFLOW_REF": "ahmedali5696/care-pulse-3/.github/workflows/other.yml@refs/heads/feature/ci-scalability"
        },
    ],
)
def test_profile_rejects_unapproved_dispatch_context(profiling_env, override):
    with pytest.raises(ValueError):
        foundation_acceptance.validate_request(profiling_env | override)


def bash_probe(script):
    bash = (
        str(
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Git/bin/bash.exe"
        )
        if sys.platform == "win32"
        else shutil.which("bash")
    )
    assert bash and Path(bash).is_file()
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-s"],
        input=script.encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()


# The fake external CLI records argv and exit status only. No actual Docker
# invocation or database access is possible in these local shell checks.
FAKE_DOCKER = r"""
set -euo pipefail
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT
mkdir "$scratch/bin"
cat > "$scratch/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$FAKE_LOG"
case "$1" in
  ps) if [ "${FAKE_RESIDUAL:-0}" = 1 ]; then echo residual; fi ;;
  compose) exit "${FAKE_STATUS:-0}" ;;
esac
SH
chmod +x "$scratch/bin/docker"
export PATH="$scratch/bin:$PATH" FAKE_LOG="$scratch/argv"
test "$(command -v docker)" = "$scratch/bin/docker"
source ops/ordinary_profile.sh
profile_clock() { printf '1'; }
"""


def test_shell_forwards_original_arguments_and_failure():
    bash_probe(
        FAKE_DOCKER
        + r"""
profile_dir="$scratch"
profile_variant=instrumented
args=(compose run --rm test python -m pytest -q --junitxml=/tmp/foundation-results/f01.xml 'tests/test_example.py::test_case[one two]' --deselect=other)
export FAKE_STATUS=7
status=0
docker "${args[@]}" | cat || status=$?
test "$status" = 7
printf '%s\n' compose run --rm test python -m ops.profile_ordinary /tmp/foundation-results/profile-f01.json -q --junitxml=/tmp/foundation-results/f01.xml 'tests/test_example.py::test_case[one two]' --deselect=other > "$scratch/expected"
cmp "$FAKE_LOG" "$scratch/expected"
grep -Fx $'pytest:f01:end:7\t1' "$scratch/timings.tsv"
profile_variant=control
export FAKE_STATUS=0
: > "$FAKE_LOG"
docker "${args[@]}"
printf '%s\n' "${args[@]}" > "$scratch/expected"
cmp "$FAKE_LOG" "$scratch/expected"
profile_variant=instrumented
: > "$FAKE_LOG"
docker compose run test python -m pytest --junitxml=/tmp/foundation-results/d06.xml
printf '%s\n' compose run test python -m pytest --junitxml=/tmp/foundation-results/d06.xml > "$scratch/expected"
cmp "$FAKE_LOG" "$scratch/expected"
"""
    )


def test_actual_controller_cleanup_checks_scope_and_preserves_failure():
    controller = Path("ops/check.ps1").read_text()
    cleanup = re.search(r"(?ms)^cleanup\(\) \{\n.*?^\}", controller)
    assert cleanup is not None
    bash_probe(
        FAKE_DOCKER
        + "\n"
        + cleanup[0]
        + r"""
export GITHUB_RUN_ID=123 GITHUB_RUN_ATTEMPT=1
export CP_CI_PROJECT=carepulse-ci-123-14 COMPOSE_PROJECT_NAME=carepulse-ci-123-14
export RUNNER_TEMP="$scratch"
profile_dir="$scratch"
profile_variant=control
foundation_profile=true
foundation_full=false
foundation_coverage_verified=true
foundation_tests=1051
focused_target=''
private="$RUNNER_TEMP/foundation-ci-private"
mkdir "$scratch/unrelated"
for scenario in success failed residual wrong_project; do
  mkdir "$private"
  : > "$FAKE_LOG"
  status=0
  (
    trap cleanup EXIT
    case "$scenario" in
      failed) exit 7 ;;
      residual) export FAKE_RESIDUAL=1 ;;
      wrong_project) COMPOSE_PROJECT_NAME=unrelated ;;
    esac
  ) || status=$?
  case "$scenario" in
    success) test "$status" = 0 ;;
    failed) test "$status" = 7 ;;
    residual|wrong_project) test "$status" = 1 ;;
  esac
  test -d "$scratch/unrelated"
  if [ "$scenario" = wrong_project ]; then
    test ! -s "$FAKE_LOG"
    test -d "$private"
    rmdir "$private"
  else
    test ! -e "$private"
    grep -Fx -- '--volumes' "$FAKE_LOG"
    test "$(grep -c '^label=com.docker.compose.project=carepulse-ci-123-14$' "$FAKE_LOG")" = 3
    test "$(grep -c '^label=com.docker.compose.project=carepulse-ci-123-01$' "$FAKE_LOG")" = 3
  fi
done
"""
    )
