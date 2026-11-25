"""Container-free contract checks for the comparison-only partition."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ops import (
    foundation_acceptance,
    ordinary_partition,
)
from ops import (
    profile_ordinary_report as report,
)
from ops.test_profile_ordinary import profiling_env  # noqa: F401


@pytest.fixture
def partition_env(profiling_env):  # noqa: F811
    return profiling_env | {
        "INPUT_ORDINARY_PROFILE_SHA": "",
        "INPUT_ORDINARY_PARTITION_SHA": profiling_env["GITHUB_SHA"],
    }


def test_partition_has_only_comparison_lanes_and_no_receipt(partition_env, tmp_path):
    result = foundation_acceptance.validate_request(partition_env)
    assert result["mode"] == "ordinary-partition"
    assert json.loads(result["matrix"]) == list(report.PARTITIONS)
    receipt = tmp_path / foundation_acceptance.RECEIPT_FILE
    with pytest.raises(ValueError):
        foundation_acceptance.seal(receipt, partition_env)
    with pytest.raises(ValueError):
        foundation_acceptance.aggregate(tmp_path, partition_env)
    assert not receipt.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"INPUT_ORDINARY_PARTITION_SHA": ""},
        {"INPUT_ORDINARY_PARTITION_SHA": "b" * 40},
        {"INPUT_ORDINARY_PROFILE_SHA": "a" * 40},
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
    ],
)
def test_partition_rejects_conflicts_and_wrong_context(partition_env, override):
    with pytest.raises(ValueError):
        foundation_acceptance.validate_request(partition_env | override)


def frozen_controller():
    return subprocess.check_output(
        ["git", "show", f"{ordinary_partition.REVIEWED}:ops/check.ps1"], text=True
    )


def controller_region(source, start, end):
    return source[source.index(start) : source.index(end, source.index(start))]


def pre_service_commands(source):
    return controller_region(
        source,
        "profile_checkpoint identity_seed:end\n",
        "profile_checkpoint service_startup:start\n",
    ).removeprefix("profile_checkpoint identity_seed:end\n")


def selector_setup(source):
    return controller_region(
        source, "historical_nodes=(\n", "run_historical_migrations() (\n"
    ) + controller_region(
        source,
        "# Current-head B-04 uses the same split",
        "profile_checkpoint migration:start",
    )


def record_pre_service(source, variant, expected):
    setup = r"""
set -euo pipefail
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT
private="$scratch"
foundation_lane=ordinary
identity_fixture_args=(-e CP_DB_IDENTITY_KEY_ID)
CP_CI_PROJECT=carepulse-ci-123-16
CP_RUNTIME_DB_PASSWORD_FILE=runtime
CP_WEB_DB_PASSWORD_SOURCE_FILE=web
CP_WORKER_DB_PASSWORD_SOURCE_FILE=worker
CP_AUTH_DB_PASSWORD_SOURCE_FILE=auth
CP_OPERATOR_SECRET_FILE=operator
CP_SENDER_DB_PASSWORD_SOURCE_FILE=sender
CP_MIGRATION_SECRET_FILE=migration
CP_DB_PASSWORD_FILE=postgres
profile_variant=control
profile_head=0056_b14_target_binding
profile_dir="$scratch"
partition_db=false
partition_stack=false
case "$VARIANT" in
  partition-db) partition_db=true ;;
  partition-stack) partition_stack=true ;;
esac
docker() {
  if [[ "$*" == *"postgres --version"* ]]; then echo 'postgres 17.11'; return; fi
  printf 'COMMAND' >> "$scratch/actual"
  for arg in "$@"; do printf '\t%s' "${arg//$scratch/PRIVATE}" >> "$scratch/actual"; done
  printf '\n' >> "$scratch/actual"
}
python3() { :; }
profile_checkpoint() { :; }
"""
    if expected is None:
        # The frozen controller's commands are recorded to stdout, not executed.
        final = 'cat "$scratch/actual"\n'
    else:
        final = (
            "cat > \"$scratch/expected\" <<'EXPECTED'\n"
            + expected
            + '\nEXPECTED\ncmp "$scratch/actual" "$scratch/expected"\n'
        )
    # DB's intentional exit must not prevent comparing its recorded commands.
    script = (
        f"VARIANT={variant}\n"
        + setup
        + selector_setup(source)
        + "(\n"
        + pre_service_commands(source)
        + "\n)\n"
        + final
    )
    bash = (
        str(
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Git/bin/bash.exe"
        )
        if sys.platform == "win32"
        else "bash"
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-s"],
        input=script.encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    return result.stdout.decode().splitlines()


def test_exact_pre_service_commands_and_service_scenarios_are_preserved():
    baseline = frozen_controller()
    candidate = Path("ops/check.ps1").read_text(encoding="utf-8")
    assert selector_setup(candidate) == selector_setup(baseline)
    serial = record_pre_service(baseline, "partition-control", None)
    assert len(serial) == 18
    selected = {
        re.search(r"foundation-results/([a-z0-9_-]+)\.xml", command)[1]: command
        for command in serial
    }
    assert list(selected) == list(report.PRE_SERVICE)
    for variant in report.PARTITIONS:
        expected = "\n".join(
            selected[name]
            for name in report.report_inventory(variant)
            if name in selected
        )
        record_pre_service(candidate, variant, expected)
    start = "profile_checkpoint service_startup:start\n"
    old_end = 'if [ "$foundation_full" = true ] || [ "$foundation_profile" = true ] || [ "${CP_B04_SPIKE:-false}" = true ]; then\n  foundation_tests='
    new_end = 'if [ "$partition_stack" = true ]; then\n  test "$health_case_count"'
    assert controller_region(candidate, start, new_end) == controller_region(
        baseline, start, old_end
    )


def comparison_payload(variant, context):
    counts = report.report_inventory(variant)
    return {
        "kind": report.PARTITION_KIND,
        "variant": variant,
        "baseline_sha": report.BASELINE,
        "profiler_sha256": hashlib.sha256(
            Path("ops/profile_ordinary.py").read_bytes()
        ).hexdigest(),
        "tested_sha": context["sha"],
        "run_id": context["run_id"],
        "attempt": context["attempt"],
        "observed_migration_head": report.HEAD,
        "runner_os": "ubuntu24",
        "runner_image": "20261122.23.1",
        "cleanup": "verified",
        "zero_skips": True,
        "mandatory_cases": sum(counts.values()),
        "mandatory_report_counts": counts,
        "focus": {},
        "controller_elapsed_seconds": 100,
        "report_identity_sha256": {name: "a" * 64 for name in counts},
    }


@pytest.fixture
def comparison(tmp_path, partition_env):
    context = foundation_acceptance.github_context(
        partition_env, profiling_request=True
    )
    paths = {}
    for variant in report.PARTITIONS:
        directory = (
            tmp_path
            / f"ordinary-partition-{variant}-{context['run_id']}-{context['attempt']}-{context['sha']}"
        )
        directory.mkdir()
        path = directory / "ordinary-profile-timing.json"
        path.write_text(json.dumps(comparison_payload(variant, context)))
        paths[variant] = path
    return tmp_path, context, paths


def test_comparison_requires_exact_case_union(comparison):
    folder, context, _ = comparison
    assert set(ordinary_partition.compare(folder, context)) == set(report.PARTITIONS)
    assert sum(report.report_inventory("partition-db").values()) == 446
    assert sum(report.report_inventory("partition-stack").values()) == 605


@pytest.mark.parametrize(
    "change",
    [
        {"tested_sha": "b" * 40},
        {"profiler_sha256": "b" * 64},
        {"attempt": "2"},
        {"zero_skips": False},
        {"cleanup": "unverified"},
        {"kind": report.KIND},
        {"mandatory_cases": 445},
        {"runner_image": "20261123.1.1"},
        {"report_identity_sha256": {}},
    ],
)
def test_comparison_rejects_invalid_lane(comparison, change):
    folder, context, paths = comparison
    path = paths["partition-db"]
    payload = json.loads(path.read_text()) | change
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        ordinary_partition.compare(folder, context)


def test_same_count_different_parameter_identity_is_rejected(comparison):
    folder, context, paths = comparison
    path = paths["partition-db"]
    payload = json.loads(path.read_text())
    payload["report_identity_sha256"]["f01"] = "b" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        ordinary_partition.compare(folder, context)
    path.unlink()
    with pytest.raises(ValueError):
        ordinary_partition.compare(folder, context)


def test_report_identity_retains_parameter_names(tmp_path):
    path = tmp_path / "f01.xml"

    def write(first, second):
        path.write_text(
            f'<testsuite tests="2" failures="0" errors="0" skipped="0"><testcase classname="test_a" name="{first}"/><testcase classname="test_a" name="{second}"/></testsuite>'
        )

    write("test_case[a]", "test_case[b]")
    original = report.identity_digest(tmp_path, "f01", 2)
    write("test_case[b]", "test_case[a]")
    assert report.identity_digest(tmp_path, "f01", 2) == original
    write("test_case[a]", "test_case[c]")
    assert report.identity_digest(tmp_path, "f01", 2) != original
    write("test_case[a]", "test_case[a]")
    with pytest.raises(ValueError):
        report.identity_digest(tmp_path, "f01", 2)


def test_report_identity_retains_tap_names(tmp_path):
    path = tmp_path / "web-adapter.tap"
    counts = "# tests 2\n# pass 2\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n"
    path.write_text("    ok 1 - child\nok 1 - parent\n" + counts)
    original = report.identity_digest(tmp_path, "web-adapter", 2)
    path.write_text("    ok 1 - other\nok 1 - parent\n" + counts)
    assert report.identity_digest(tmp_path, "web-adapter", 2) != original
    path.write_text("ok 1 - parent\nok 1 - parent\n" + counts)
    with pytest.raises(ValueError):
        report.identity_digest(tmp_path, "web-adapter", 2)
