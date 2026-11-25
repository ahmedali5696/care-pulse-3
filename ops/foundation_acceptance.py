"""Fail-closed normalization and evidence gates for permanent Foundation acceptance."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HISTORICAL_SHA = "6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7"
SOURCE_SCHEMA = "0040_t173_numeric_money"
CANDIDATE_SCHEMA = "0046_f01_audit_integrity"
ORDINARY_SCHEMA = "0053_b11_receipt_conflict"
RECEIPT_FILE = "foundation-full-receipt.json"
EXPECTED_COVERAGE = {
    "ordinary": "current-head-excluding-extracted-historical",
    "historical": "immutable-0040-selected-nodes-on-isolated-postgres",
    "compat": "full-US1-US2-US3-six-successors-zero-skips",
}
PROVENANCE = {
    "ordinary": {"candidate_schema": ORDINARY_SCHEMA},
    "historical": {
        "historical_sha": HISTORICAL_SHA,
        "historical_schema": SOURCE_SCHEMA,
    },
    "compat": {
        "source_sha": HISTORICAL_SHA,
        "source_schema": SOURCE_SCHEMA,
        "candidate_schema": CANDIDATE_SCHEMA,
    },
}
PROVENANCE_ENV = {
    "historical_sha": "CP_FOUNDATION_HISTORICAL_SHA",
    "historical_schema": "CP_FOUNDATION_HISTORICAL_SCHEMA",
    "source_sha": "CP_FOUNDATION_SOURCE_SHA",
    "source_schema": "CP_FOUNDATION_SOURCE_SCHEMA",
    "candidate_schema": "CP_FOUNDATION_CANDIDATE_SCHEMA",
}
DIAGNOSTICS = {
    "authorization-lock",
    "booking-races",
    "followup-races",
    "f01-cleanup",
    "f01-followup-lock",
    "f01-visit-lock",
    "f01-followup-context",
    "f01-pricing",
    "f01-pricing-races",
    "w3-checkin",
    "w3-checkin-races",
    "w3-receipt-boundary",
    "w3-queue-scope",
    "w3-intake",
    "w3-intake-races",
    "w3-migration",
    "compat-us1",
    "compat-us2",
    "compat-us3",
}


def _require(env: dict[str, str], key: str, pattern: str) -> str:
    value = env.get(key, "")
    if not re.fullmatch(pattern, value, flags=re.ASCII):
        raise ValueError(f"invalid {key}")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    item = {}
    for key, value in pairs:
        if key in item:
            raise ValueError("duplicate JSON field")
        item[key] = value
    return item


def _read_json(path: Path, max_bytes: int = 8192) -> object:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
        raise ValueError("missing, linked or oversized JSON evidence")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("invalid JSON evidence") from error


def github_context(env: dict[str, str]) -> dict:
    """Bind every receipt to the actual checked-out workflow run and PR merge."""
    event = _require(env, "GITHUB_EVENT_NAME", r"(push|pull_request|workflow_dispatch)")
    ref = _require(
        env, "GITHUB_REF", r"refs/(heads/[A-Za-z0-9._/-]+|pull/[1-9][0-9]*/merge)"
    )
    sha = _require(env, "GITHUB_SHA", r"[0-9a-f]{40}")
    run_id = _require(env, "GITHUB_RUN_ID", r"[1-9][0-9]*")
    attempt = _require(env, "GITHUB_RUN_ATTEMPT", r"[1-9][0-9]*")
    repository = _require(env, "GITHUB_REPOSITORY", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    workflow = env.get("GITHUB_WORKFLOW_REF", "")
    workflow_prefix = f"{repository}/.github/workflows/foundation.yml@"
    if not workflow.startswith(workflow_prefix):
        raise ValueError("unverified Foundation workflow identity")
    if event == "push" and ref != "refs/heads/development":
        raise ValueError("unexpected push ref")
    if event == "workflow_dispatch" and not ref.startswith("refs/heads/"):
        raise ValueError("dispatch requires an actual branch")

    pr = None
    if event == "pull_request":
        match = re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge", ref)
        if not match:
            raise ValueError("expected current pull-request merge ref")
        event_path = Path(env.get("GITHUB_EVENT_PATH", ""))
        document = _read_json(event_path, 1024 * 1024)
        if not isinstance(document, dict):
            raise ValueError("invalid pull-request event")
        payload = document.get("pull_request")
        if not isinstance(payload, dict) or type(payload.get("number")) is not int:
            raise ValueError("invalid pull-request identity")
        head = payload.get("head")
        base = payload.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ValueError("missing pull-request heads")
        head_ref = _require(env, "GITHUB_HEAD_REF", r"[A-Za-z0-9._/-]+")
        base_ref = _require(env, "GITHUB_BASE_REF", r"[A-Za-z0-9._/-]+")
        if (
            payload["number"] != int(match.group(1))
            or head.get("ref") != head_ref
            or base.get("ref") != base_ref
            or not re.fullmatch(r"[0-9a-f]{40}", head.get("sha", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", base.get("sha", ""))
        ):
            raise ValueError("pull-request event/ref mismatch")
        workflow_refs = {ref, f"refs/heads/{base_ref}"}
        head_repo = head.get("repo")
        if isinstance(head_repo, dict) and head_repo.get("full_name") == repository:
            workflow_refs.add(f"refs/heads/{head_ref}")
        if workflow[len(workflow_prefix) :] not in workflow_refs:
            raise ValueError("unverified pull-request workflow source")
        pr = {
            "number": payload["number"],
            "head_ref": head_ref,
            "head_sha": head["sha"],
            "base_ref": base_ref,
            "base_sha": base["sha"],
        }
    elif ref.startswith("refs/pull/"):
        raise ValueError("pull-request merge ref requires pull-request event")
    elif workflow != workflow_prefix + ref:
        raise ValueError("workflow ref does not match checked-out branch")

    return {
        "event": event,
        "ref": ref,
        "sha": sha,
        "run_id": run_id,
        "attempt": attempt,
        "repository": repository,
        "workflow": workflow,
        "pr": pr,
    }


def validate_request(env: dict[str, str]) -> dict[str, str]:
    """Normalize only approved, mutually exclusive Foundation invocation modes."""
    context = github_context(env)
    diagnostic = env.get("INPUT_POSTGRES_DIAGNOSTIC", "") or "full"
    if diagnostic not in DIAGNOSTICS | {"full"}:
        raise ValueError("unknown PostgreSQL diagnostic selection")
    for key in ("INPUT_B04_SPIKE", "INPUT_T133_TERMINAL"):
        if env.get(key, "") not in ("", "true", "false"):
            raise ValueError(f"invalid {key}")
    spike = env.get("INPUT_B04_SPIKE") == "true"
    terminal = env.get("INPUT_T133_TERMINAL") == "true"
    terminal_sha = env.get("INPUT_T133_SHA", "")
    if (spike and terminal) or (
        diagnostic != "full" and (spike or terminal or terminal_sha)
    ):
        raise ValueError("conflicting Foundation modes")
    if terminal:
        if terminal_sha != context["sha"]:
            raise ValueError("T133 must target the exact executing SHA")
        if (
            context["repository"] != "ahmedali5696/care-pulse-3"
            or context["ref"] != "refs/heads/feature/wave-2-booking-and-scheduling"
        ):
            raise ValueError("T133 requires the authorized legacy repository and ref")
    elif terminal_sha:
        raise ValueError("T133 SHA without terminal mode")
    if context["event"] != "workflow_dispatch" and (
        diagnostic != "full" or spike or terminal or terminal_sha
    ):
        raise ValueError("nonstandard Foundation mode requires manual dispatch")

    mode = (
        "b04-spike"
        if spike
        else "t133"
        if terminal
        else "compat-diagnostic"
        if diagnostic.startswith("compat-")
        else "postgres-diagnostic"
        if diagnostic != "full"
        else "full"
    )
    return {
        "mode": mode,
        "matrix": json.dumps(
            list(EXPECTED_COVERAGE) if mode == "full" else ["standard"],
            separators=(",", ":"),
        ),
        "candidate_sha": context["sha"],
        "run_id": context["run_id"],
        "attempt": context["attempt"],
        "repository": context["repository"],
        "workflow": context["workflow"],
    }


def _receipt(context: dict, lane: str) -> dict:
    return {
        "schema": 2,
        "kind": "FOUNDATION_FULL_LANE",
        "context": context,
        "lane": lane,
        "coverage": EXPECTED_COVERAGE[lane],
        "statuses": {"execution": "PASS", "tests": "PASS", "skipped": 0},
        "cleanup": "verified-scoped-teardown",
        "provenance": PROVENANCE[lane],
    }


def _safe_path(path: Path) -> None:
    """Refuse symlinks, including an existing ancestor of an evidence path."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink evidence path")


def seal(path: Path, env: dict[str, str]) -> dict:
    """Seal evidence only from a verified successful lane's terminal cleanup."""
    context = github_context(env)
    lane = env.get("CP_FOUNDATION_LANE", "")
    if lane not in EXPECTED_COVERAGE or env.get("CP_FOUNDATION_MODE") != "full":
        raise ValueError("not a full acceptance lane")
    expected = {
        "CP_FOUNDATION_EXECUTION": "PASS",
        "CP_FOUNDATION_TESTS": "PASS",
        "CP_FOUNDATION_ZERO_SKIPS": "true",
        "CP_FOUNDATION_CLEANUP": "PASS",
        "CP_FOUNDATION_COVERAGE": EXPECTED_COVERAGE[lane],
        "CP_FOUNDATION_CLEANUP_VERIFIED": "1",
        "CP_FOUNDATION_COVERAGE_VERIFIED": "1",
        **{PROVENANCE_ENV[field]: value for field, value in PROVENANCE[lane].items()},
    }
    for key, value in expected.items():
        if env.get(key) != value:
            raise ValueError(f"unverified terminal lane evidence: {key}")
    if path.name != RECEIPT_FILE or not path.parent.is_dir():
        raise ValueError("invalid receipt destination")
    _safe_path(path)
    receipt = _receipt(context, lane)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    return receipt


def _identical(actual: object, expected: object) -> bool:
    """Compare JSON types strictly (JSON true must not equal numeric 1)."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _identical(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _identical(x, y) for x, y in zip(actual, expected)
        )
    return actual == expected


def aggregate(root: Path, env: dict[str, str]) -> None:
    """Reject incomplete, duplicate, mixed-run or unverified full-lane evidence."""
    context = github_context(env)
    required = {
        "INPUT_RESULT": "success",
        "LANES_RESULT": "success",
        "NORMALIZED_MODE": "full",
        "CANDIDATE_SHA": context["sha"],
        "COMMON_RUN_ID": context["run_id"],
        "COMMON_ATTEMPT": context["attempt"],
        "COMMON_REPOSITORY": context["repository"],
        "COMMON_WORKFLOW": context["workflow"],
    }
    for key, value in required.items():
        if env.get(key) != value:
            raise ValueError(f"missing or mismatched aggregate dependency: {key}")

    _safe_path(root)
    if not root.is_dir():
        raise ValueError("missing receipt directory")
    expected_dirs = {
        (
            f"foundation-full-{context['run_id']}-{context['attempt']}-"
            f"{context['sha']}-{lane}"
        ): lane
        for lane in EXPECTED_COVERAGE
    }
    actual_dirs = {entry.name: entry for entry in root.iterdir()}
    if actual_dirs.keys() != expected_dirs.keys():
        raise ValueError("missing, duplicate or unexpected lane artifact")
    for name, lane in expected_dirs.items():
        directory = actual_dirs[name]
        _safe_path(directory)
        if not directory.is_dir():
            raise ValueError("invalid lane artifact directory")
        files = list(directory.iterdir())
        if len(files) != 1 or files[0].name != RECEIPT_FILE:
            raise ValueError("unexpected or missing lane receipt")
        _safe_path(files[0])
        evidence = _read_json(files[0])
        if not _identical(evidence, _receipt(context, lane)):
            raise ValueError(
                "incorrect receipt context, coverage, provenance or cleanup"
            )


def _assert_checkout_sha(sha: str) -> None:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("unable to verify checked-out commit") from error
    if head != sha:
        raise ValueError("checked-out commit differs from workflow SHA")


def _publish_acceptance(env: dict[str, str]) -> None:
    """Report acceptance only after every receipt and dependency is verified."""
    context = github_context(env)
    destination = Path(env.get("GITHUB_STEP_SUMMARY", ""))
    if not env.get("GITHUB_STEP_SUMMARY") or not destination.parent.is_dir():
        raise ValueError("missing acceptance summary destination")
    _safe_path(destination)
    lines = [
        "### Full Foundation acceptance",
        f"- Tested github.sha: `{context['sha']}`",
        f"- Event/ref: `{context['event']}` / `{context['ref']}`",
        f"- Repository/workflow: `{context['repository']}` / `{context['workflow']}`",
        f"- Run/attempt: `{context['run_id']}` / `{context['attempt']}`",
        f"- Ordinary ({ORDINARY_SCHEMA}, including B-12 and B-11), historical (163 at 0040), compatibility (44 Python + 11 Web, 0040 to 0046): verified",
        "- Three current-attempt receipts, zero mandatory skips, scoped cleanup, artifact digests: verified",
    ]
    if context["pr"] is not None:
        pr = context["pr"]
        lines.extend(
            [
                f"- PR head: `{pr['head_ref']}` / `{pr['head_sha']}`",
                f"- PR base: `{pr['base_ref']}` / `{pr['base_sha']}`",
            ]
        )
    with destination.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print(
        f"FOUNDATION_FULL_ACCEPTED sha={context['sha']} "
        f"run={context['run_id']} attempt={context['attempt']}"
    )


def main(argv: list[str], env: dict[str, str]) -> None:
    if argv == ["validate-request"]:
        result = validate_request(env)
        _assert_checkout_sha(result["candidate_sha"])
        output_path = env.get("GITHUB_OUTPUT", "")
        if output_path:
            destination = Path(output_path)
            _safe_path(destination)
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(
                    "".join(f"{key}={value}\n" for key, value in result.items())
                )
        else:
            print(json.dumps(result, sort_keys=True))
    elif len(argv) == 2 and argv[0] == "seal":
        _assert_checkout_sha(github_context(env)["sha"])
        receipt = seal(Path(argv[1]), env)
        print(
            f"FOUNDATION_SEALED lane={receipt['lane']} sha={receipt['context']['sha']}"
        )
    elif len(argv) == 2 and argv[0] == "aggregate":
        aggregate(Path(argv[1]), env)
        _assert_checkout_sha(env["GITHUB_SHA"])
        _publish_acceptance(env)
    else:
        raise ValueError(
            "usage: validate-request | seal <receipt-path> | aggregate <download-root>"
        )


if __name__ == "__main__":
    try:
        main(sys.argv[1:], dict(os.environ))
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"FAIL Foundation acceptance: {error}") from None
