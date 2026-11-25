"""Validate fixed-baseline Ordinary profiling evidence without exporting raw reports."""

import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from itertools import pairwise
from pathlib import Path
from xml.etree import ElementTree

BASELINE = "0c325ce80a999b79f7e442b39c4b0ecbc55fea9d"
HEAD = "0056_b14_target_binding"
KIND = "PROFILING_ONLY_NOT_ACCEPTANCE"
FOCUS = (
    "f01",
    "d08",
    "w3_checkin",
    "w3_queue_scope",
    "w3_admission_confirmations",
    "b09",
    "b10",
)
# Fixed report order and counts from baseline run 45621749596, attempt 1.
PRE_SERVICE = {
    "f01": 68,
    "d08": 322,
    "d06": 19,
    "w3_receipt_boundary": 4,
    "w3_checkin": 8,
    "w3_checkin_races": 8,
    "w3_queue_scope": 3,
    "w3_intake": 8,
    "w3_intake_races": 15,
    "w3_admission_controls": 2,
    "w3_admission_confirmations": 8,
    "b06": 61,
    "b07": 71,
    "b08": 34,
    "b09": 77,
    "b10": 143,
    "d11": 4,
    "b05-backend": 33,
}
SERVICE = {
    "topology-seed": 3,
    "topology-verify": 3,
    "secrets": 38,
    "health-1": 3,
    **{f"health-{i}": 1 for i in range(2, 8)},
    "auth-mail": 19,
    "sessions": 43,
    "b05-invited": 2,
    "w3_bff": 4,
    "web-ingress": 2,
    "sender": 4,
    "recovery": 27,
    "owner-invite": 1,
    "web-adapter": 6,
    "sender-mail-down": 1,
    "health-8": 1,
}
TAP = {"web-ingress", "web-adapter"}
REPORTS = PRE_SERVICE | SERVICE
PHASES = ("setup", "call", "teardown")
PARTITION_KIND = "ORDINARY_COMPARISON_ONLY_NOT_ACCEPTANCE"
DB_REPORTS = {
    "f01",
    "d08",
    "w3_receipt_boundary",
    "w3_checkin",
    "w3_checkin_races",
    "w3_queue_scope",
    "w3_intake",
    "w3_intake_races",
    "w3_admission_controls",
    "w3_admission_confirmations",
}
PARTITIONS = ("partition-control", "partition-db", "partition-stack")


def report_inventory(variant):
    require(variant in ("control", "instrumented", *PARTITIONS))
    if variant == "partition-db":
        return {name: count for name, count in REPORTS.items() if name in DB_REPORTS}
    if variant == "partition-stack":
        return {
            name: count for name, count in REPORTS.items() if name not in DB_REPORTS
        }
    return REPORTS


def identity_digest(folder, name, count):
    """Hash complete case identities, retaining parameters but not exporting them."""
    if name in TAP:
        tap_count(folder, name, count)
        identities = re.findall(
            r"(?m)^([ ]*ok [0-9]+ - .+)$",
            (folder / f"{name}.tap").read_text(),
        )
        require(len(identities) == count)
    else:
        identities = [
            (case.attrib.get("classname", ""), case.attrib["name"])
            for case in junit_cases(folder, name, count)
        ]
    require(len(set(identities)) == count)
    return hashlib.sha256(
        json.dumps(sorted(identities), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def require(condition):
    if not condition:
        raise ValueError("Invalid or incomplete profiling evidence")


def seconds(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    return value


def identifier(value, pattern=r"[A-Za-z_][A-Za-z0-9_]*"):
    require(isinstance(value, str) and re.fullmatch(pattern, value))
    return value


def junit_cases(folder, name, count):
    root = ElementTree.parse(folder / f"{name}.xml").getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    require(suites and all(suite.tag == "testsuite" for suite in suites))
    require(sum(int(suite.attrib["tests"]) for suite in suites) == count)
    require(
        all(
            int(suite.attrib[field]) == 0
            for suite in suites
            for field in ("failures", "errors", "skipped")
        )
    )
    cases = [case for suite in suites for case in suite.findall("testcase")]
    require(len(cases) == count)
    require(
        not any(
            case.find(field) is not None
            for case in cases
            for field in ("failure", "error", "skipped")
        )
    )
    return cases


def tap_count(folder, name, count):
    matches = re.findall(
        r"(?m)^# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)$",
        (folder / f"{name}.tap").read_text(),
    )
    require(
        len(matches) == 6
        and dict(matches)
        == {
            "tests": str(count),
            "pass": str(count),
            "fail": "0",
            "cancelled": "0",
            "skipped": "0",
            "todo": "0",
        }
    )


def case_timing(case):
    require(set(case) == {"file", "test", "phases"})
    require(set(case["phases"]) == set(PHASES))
    timings = {}
    for phase in PHASES:
        entry = case["phases"][phase]
        require(set(entry) == {"seconds", "outcome"} and entry["outcome"] == "passed")
        timings[phase] = seconds(entry["seconds"])
    return {
        "file": identifier(case["file"], r"test_[A-Za-z0-9_]+\.py"),
        "test": identifier(case["test"], r"test_[A-Za-z0-9_]+"),
        "seconds": timings,
    }


def fixture_timings(profile):
    setup = {}
    for name, entry in profile["fixture_setup"].items():
        require(set(entry) == {"count", "seconds"})
        require(type(entry["count"]) is int and entry["count"] > 0)
        setup[identifier(name)] = {
            "count": entry["count"],
            "seconds": seconds(entry["seconds"]),
        }
    teardown = {
        identifier(name): seconds(value)
        for name, value in profile["generator_teardown_seconds"].items()
    }
    require(setup and set(teardown) <= set(setup))
    return {"fixture_setup": setup, "generator_teardown_seconds": teardown}


def focus_timings(folder, name, cases):
    profile = json.loads((folder / f"profile-{name}.json").read_text())
    require(
        profile["kind"] == KIND
        and profile["pytest_exit"] == 0
        and profile["selected"] == len(cases)
    )
    timings = [case_timing(case) for case in profile["cases_in_execution_order"]]
    # JUnit parameter IDs remain private. Compare only the source file/function multiset.
    expected = Counter(
        (
            next(
                part
                for part in case.attrib["classname"].split(".")
                if part.startswith("test_")
            )
            + ".py",
            case.attrib["name"].split("[", 1)[0],
        )
        for case in cases
    )
    require(Counter((case["file"], case["test"]) for case in timings) == expected)
    return {
        "cases_in_execution_order": timings,
        "phase_totals_seconds": {
            phase: round(sum(case["seconds"][phase] for case in timings), 6)
            for phase in PHASES
        },
        **fixture_timings(profile),
    }


def collect(folder, output, variant, head):
    require(head == HEAD)
    reports = report_inventory(variant)
    require({p.stem for p in folder.glob("*.xml")} == set(reports) - TAP)
    require({p.stem for p in folder.glob("*.tap")} == set(reports) & TAP)
    expected_profiles = (
        {f"profile-{name}.json" for name in FOCUS}
        if variant == "instrumented"
        else set()
    )
    require({p.name for p in folder.glob("profile-*.json")} == expected_profiles)
    focus = {}
    for name, count in reports.items():
        if name in TAP:
            tap_count(folder, name, count)
            continue
        cases = junit_cases(folder, name, count)
        if variant == "instrumented" and name in FOCUS:
            focus[name] = focus_timings(folder, name, cases)
    evidence = {
        "variant": variant,
        "observed_migration_head": head,
        "mandatory_report_counts": reports,
        "focus": focus,
    }
    if variant in PARTITIONS:
        evidence["report_identity_sha256"] = {
            name: identity_digest(folder, name, count)
            for name, count in reports.items()
        }
    write_json(output, evidence)


def expected_events(variant="control"):
    yield "controller:start"
    for name in (
        "pulls",
        "build",
        "database_ready",
        "role_bootstrap",
        "migration",
        "identity_seed",
    ):
        yield f"{name}:start"
        yield f"{name}:end"
    for name in report_inventory(variant):
        if name == "topology-seed":
            yield "service_startup:start"
            yield "service_startup:end"
        prefix = name if name in TAP | {"owner-invite"} else f"pytest:{name}"
        yield f"{prefix}:start"
        yield f"{prefix}:end" if name in TAP | {"owner-invite"} else f"{prefix}:end:0"
    yield "controller:validated"
    yield "cleanup:start"
    yield "cleanup:end"


def orchestration_timings(path, variant="control"):
    rows = [line.split("\t") for line in path.read_text().splitlines()]
    require([label for label, _ in rows] == list(expected_events(variant)))
    ticks = [int(tick) for _, tick in rows]
    require(all(0 <= a <= b for a, b in pairwise(ticks)))
    spans = {}
    for index, (label, _) in enumerate(rows):
        if label.endswith(":start") and label != "controller:start":
            spans[label.removesuffix(":start")] = round(
                (ticks[index + 1] - ticks[index]) / 1e9, 6
            )
    total = (ticks[-1] - ticks[0]) / 1e9
    return {
        "controller_elapsed_seconds": round(total, 6),
        "interval_seconds": spans,
        "unattributed_orchestration_seconds": round(total - sum(spans.values()), 6),
    }


def write_json(output, evidence):
    with output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, allow_nan=False, indent=2)
        stream.write("\n")


def publish(folder, output):
    evidence = json.loads((folder / "report.json").read_text())
    variant = evidence["variant"]
    require(not (Path(os.environ["RUNNER_TEMP"]) / "foundation-ci-private").exists())
    evidence.update(orchestration_timings(folder / "timings.tsv", variant))
    evidence.update(
        {
            "kind": PARTITION_KIND if variant in PARTITIONS else KIND,
            "baseline_sha": BASELINE,
            "tested_sha": identifier(os.environ["GITHUB_SHA"], r"[0-9a-f]{40}"),
            "profiler_sha256": hashlib.sha256(
                Path("ops/profile_ordinary.py").read_bytes()
            ).hexdigest(),
            "run_id": identifier(os.environ["GITHUB_RUN_ID"], r"[0-9]+"),
            "attempt": identifier(os.environ["GITHUB_RUN_ATTEMPT"], r"[0-9]+"),
            "runner_image": identifier(
                os.environ["ImageVersion"], r"[0-9]+(?:\.[0-9]+)+"
            ),
            "runner_os": identifier(os.environ["ImageOS"], r"ubuntu24"),
            "cleanup": "verified",
            "zero_skips": True,
            "mandatory_cases": sum(report_inventory(variant).values()),
        }
    )
    write_json(output, evidence)


if __name__ == "__main__":
    if sys.argv[1] == "collect" and len(sys.argv) == 6:
        collect(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5])
    elif sys.argv[1] == "publish" and len(sys.argv) == 4:
        publish(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise SystemExit("Invalid profiling report command")
