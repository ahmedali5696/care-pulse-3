"""Verify the frozen Ordinary comparison, never a Foundation receipt."""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from ops import profile_ordinary_report as report
from ops.foundation_acceptance import _read_json, github_context

REVIEWED = "7f54927dbffd9df4f2d6da75fbebb880226cfdb0"
CHANGED_FILES = {
    ".github/workflows/foundation.yml",
    "ops/check.ps1",
    "ops/foundation_acceptance.py",
    "ops/profile_ordinary_report.py",
    "ops/ordinary_partition.py",
    "ops/test_ordinary_partition.py",
    "docs/full-foundation-scalability.md",
}
SECRET_ENV = (
    "CP_DB_PASSWORD_FILE",
    "CP_RUNTIME_DB_PASSWORD_FILE",
    "CP_API_DB_PASSWORD_SOURCE_FILE",
    "CP_WEB_DB_PASSWORD_SOURCE_FILE",
    "CP_WORKER_DB_PASSWORD_SOURCE_FILE",
    "CP_GATE_DB_PASSWORD_SOURCE_FILE",
    "CP_DB_IDENTITY_KEY_SOURCE_FILE",
    "CP_AUTH_DB_PASSWORD_SOURCE_FILE",
    "CP_SERVICE_KEY_SOURCE_FILE",
    "CP_AUTH_SECRET_SOURCE_FILE",
    "CP_FILE_ENCRYPTION_KEY_SOURCE_FILE",
    "CP_MIGRATION_SECRET_FILE",
    "CP_OPERATOR_SECRET_FILE",
    "CP_OPERATOR_CREDENTIAL_SOURCE_FILE",
    "CP_SENDER_DB_PASSWORD_SOURCE_FILE",
)


def verify_frozen_tree():
    parent = subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip()
    report.require(parent == REVIEWED)
    paths = subprocess.check_output(
        ["git", "diff", "--name-only", REVIEWED, "HEAD"], text=True
    ).splitlines()
    report.require(set(paths) <= CHANGED_FILES)
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], check=True)


def scan(private, evidence):
    paths = [Path(os.environ[key]) for key in SECRET_ENV]
    report.require(all(path.parent == private for path in paths))
    secrets = [path.read_bytes().strip() for path in paths]
    report.require(len(set(secrets)) == 15)
    report.require(all(re.fullmatch(rb"[0-9a-f]{64}", value) for value in secrets))
    canaries = private / "partition-canaries"
    with canaries.open("xb") as stream:
        stream.write(b"\n".join(secrets) + b"\n")
    result = subprocess.run(
        [
            "git",
            "grep",
            "-I",
            "-q",
            "-f",
            str(canaries),
            "--",
            "apps",
            "tests",
            "ops",
            "compose.yaml",
        ],
        check=False,
    )
    report.require(result.returncode == 1)
    for name in ("report.json", "timings.tsv"):
        data = (evidence / name).read_bytes()
        report.require(not any(secret in data for secret in secrets))


def validate_lane(payload, variant, context):
    inventory = report.report_inventory(variant)
    expected = {
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
        "cleanup": "verified",
        "zero_skips": True,
        "mandatory_cases": sum(inventory.values()),
        "mandatory_report_counts": inventory,
        "focus": {},
    }
    report.require(all(payload.get(key) == value for key, value in expected.items()))
    report.require(list(payload["mandatory_report_counts"]) == list(inventory))
    digests = payload["report_identity_sha256"]
    report.require(set(digests) == set(inventory))
    report.require(
        all(re.fullmatch(r"[0-9a-f]{64}", value) for value in digests.values())
    )
    report.identifier(payload["runner_image"], r"[0-9]+(?:\.[0-9]+)+")
    report.seconds(payload["controller_elapsed_seconds"])
    return digests


def compare(folder, context):
    suffix = f"{context['run_id']}-{context['attempt']}-{context['sha']}"
    names = {
        variant: f"ordinary-partition-{variant}-{suffix}"
        for variant in report.PARTITIONS
    }
    report.require(folder.is_dir() and not folder.is_symlink())
    report.require({path.name for path in folder.iterdir()} == set(names.values()))
    lanes = {}
    for variant, name in names.items():
        directory = folder / name
        report.require(directory.is_dir() and not directory.is_symlink())
        report.require(
            {path.name for path in directory.iterdir()}
            == {"ordinary-profile-timing.json"}
        )
        payload = _read_json(directory / "ordinary-profile-timing.json", 64 * 1024)
        report.require(isinstance(payload, dict))
        lanes[variant] = (payload, validate_lane(payload, variant, context))
    control, db, stack = (lanes[variant] for variant in report.PARTITIONS)
    report.require(not (db[1].keys() & stack[1].keys()))
    report.require(control[1] == db[1] | stack[1])
    report.require(len({lane[0]["runner_image"] for lane in lanes.values()}) == 1)
    return {
        variant: lane[0]["controller_elapsed_seconds"]
        for variant, lane in lanes.items()
    }


def main(argv):
    if len(argv) == 3 and argv[0] == "scan":
        scan(Path(argv[1]), Path(argv[2]))
    elif len(argv) == 2 and argv[0] == "compare":
        context = github_context(os.environ, profiling_request=True)
        report.require(context["event"] == "workflow_dispatch")
        report.require(context["repository"] == "ahmedali5696/care-pulse-3")
        report.require(context["ref"] == "refs/heads/feature/ci-scalability")
        times = compare(Path(argv[1]), context)
        print(
            "Ordinary comparison only: exact report/case parity, zero skips, cleanup verified"
        )
        print(f"Controller seconds: {times}")
    else:
        raise SystemExit("Invalid Ordinary partition command")


if __name__ == "__main__":
    main(sys.argv[1:])
