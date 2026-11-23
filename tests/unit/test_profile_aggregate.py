"""Independent negative proof for profiling-only aggregation semantics."""

import copy
import json

import pytest

from ops.profile_aggregate import EXPECTED, validate


@pytest.fixture
def context(tmp_path):
    env = {
        "INPUT_RESULT": "success",
        "LANES_RESULT": "success",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/feature/foundation-parallel-profile-20261123",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    for lane, coverage in EXPECTED.items():
        directory = tmp_path / f"foundation-profile-{lane}-{env['GITHUB_SHA']}"
        directory.mkdir()
        (directory / "profile-proof.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "PROFILING_ONLY_NOT_ACCEPTANCE",
                    "lane": lane,
                    "sha": env["GITHUB_SHA"],
                    "run_id": env["GITHUB_RUN_ID"],
                    "attempt": env["GITHUB_RUN_ATTEMPT"],
                    "status": "PASS",
                    "coverage": coverage,
                    "cleanup": "completed-by-checked-shell-trap",
                }
            )
        )
    return tmp_path, env


def test_full_evidence_passes(context):
    validate(*context)


@pytest.mark.parametrize("phase", ["INPUT_RESULT", "LANES_RESULT"])
@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped", ""])
def test_non_successful_dependency_fails_closed(context, phase, status):
    root, env = context
    changed = copy.copy(env)
    changed[phase] = status
    with pytest.raises(ValueError):
        validate(root, changed)


@pytest.mark.parametrize("lane", list(EXPECTED))
def test_missing_mandatory_lane_fails_closed(context, lane):
    root, env = context
    (
        root / f"foundation-profile-{lane}-{env['GITHUB_SHA']}" / "profile-proof.json"
    ).unlink()
    with pytest.raises(ValueError):
        validate(root, env)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha", "b" * 40),
        ("run_id", "654321"),
        ("attempt", "2"),
        ("status", "SKIPPED"),
        ("coverage", "incomplete"),
        ("cleanup", "not-performed"),
        ("kind", "FOUNDATION_ACCEPTANCE"),
    ],
)
def test_changed_evidence_fails_closed(context, field, value):
    root, env = context
    path = (
        root / f"foundation-profile-compat-{env['GITHUB_SHA']}" / "profile-proof.json"
    )
    item = json.loads(path.read_text())
    item[field] = value
    path.write_text(json.dumps(item))
    with pytest.raises(ValueError):
        validate(root, env)


def test_duplicate_lane_fails_closed(context):
    root, env = context
    duplicate = root / "duplicate"
    duplicate.mkdir()
    original = (
        root / f"foundation-profile-ordinary-{env['GITHUB_SHA']}" / "profile-proof.json"
    )
    (duplicate / "profile-proof.json").write_bytes(original.read_bytes())
    with pytest.raises(ValueError):
        validate(root, env)


def test_wrong_branch_fails_closed(context):
    root, env = context
    changed = {**env, "GITHUB_REF": "refs/heads/development"}
    with pytest.raises(ValueError):
        validate(root, changed)
