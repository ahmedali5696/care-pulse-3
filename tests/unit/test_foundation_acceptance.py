"""Negative evidence cases for the permanent Foundation full-lane gate."""

import json

import pytest

from ops import foundation_acceptance as acceptance
from ops import foundation_inventory as inventory


@pytest.fixture
def env():
    repository = "ahmedali5696/care-pulse-3"
    ref = "refs/heads/feature/foundation-permanent-acceptance-20261123"
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": ref,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REPOSITORY": repository,
        "GITHUB_WORKFLOW_REF": f"{repository}/.github/workflows/foundation.yml@{ref}",
        "INPUT_POSTGRES_DIAGNOSTIC": "full",
        "INPUT_B04_SPIKE": "false",
        "INPUT_T133_TERMINAL": "false",
        "INPUT_T133_SHA": "",
    }


def seal_env(env, lane):
    reports = inventory.expected_evidence(lane)
    reports["report_identity_sha256"] = {
        name: inventory.EXPECTED_IDENTITY_DIGESTS[name]
        for name in reports["report_counts"]
    }
    return {
        **env,
        "CP_FOUNDATION_MODE": "full",
        "CP_FOUNDATION_LANE": lane,
        "CP_FOUNDATION_EXECUTION": "PASS",
        "CP_FOUNDATION_TESTS": "PASS",
        "CP_FOUNDATION_ZERO_SKIPS": "true",
        "CP_FOUNDATION_CLEANUP": "PASS",
        "CP_FOUNDATION_COVERAGE": acceptance.EXPECTED_COVERAGE[lane],
        "CP_FOUNDATION_COVERAGE_VERIFIED": "1",
        "CP_FOUNDATION_CLEANUP_VERIFIED": "1",
        "CP_FOUNDATION_REPORT_EVIDENCE": json.dumps(reports),
        **{
            acceptance.PROVENANCE_ENV[key]: value
            for key, value in acceptance.PROVENANCE[lane].items()
        },
    }


@pytest.fixture
def receipts(tmp_path, env):
    for lane in acceptance.EXPECTED_COVERAGE:
        name = f"foundation-full-123456-2-{'a' * 40}-{lane}"
        directory = tmp_path / name
        directory.mkdir()
        acceptance.seal(directory / acceptance.RECEIPT_FILE, seal_env(env, lane))
    return tmp_path


def aggregate_env(env):
    return {
        **env,
        "INPUT_RESULT": "success",
        "LANES_RESULT": "success",
        "NORMALIZED_MODE": "full",
        "CANDIDATE_SHA": env["GITHUB_SHA"],
        "COMMON_RUN_ID": env["GITHUB_RUN_ID"],
        "COMMON_ATTEMPT": env["GITHUB_RUN_ATTEMPT"],
        "COMMON_REPOSITORY": env["GITHUB_REPOSITORY"],
        "COMMON_WORKFLOW": env["GITHUB_WORKFLOW_REF"],
    }


def receipt_file(receipts, env, lane="compat"):
    directory = (
        f"foundation-full-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}-"
        f"{env['GITHUB_SHA']}-{lane}"
    )
    return receipts / directory / acceptance.RECEIPT_FILE


def test_request_classifies_full_and_exclusive_modes(env):
    normalized = acceptance.validate_request(env)
    assert normalized == {
        "mode": "full",
        "matrix": '["ordinary-db","ordinary-stack","historical","compat"]',
        "candidate_sha": "a" * 40,
        "run_id": "123456",
        "attempt": "2",
        "repository": env["GITHUB_REPOSITORY"],
        "workflow": env["GITHUB_WORKFLOW_REF"],
    }
    for name, changes in (
        ("postgres-diagnostic", {"INPUT_POSTGRES_DIAGNOSTIC": "booking-races"}),
        ("compat-diagnostic", {"INPUT_POSTGRES_DIAGNOSTIC": "compat-us3"}),
        ("b04-spike", {"INPUT_B04_SPIKE": "true"}),
        ("t133", {"INPUT_T133_TERMINAL": "true", "INPUT_T133_SHA": "a" * 40}),
    ):
        if name == "t133":
            legacy_ref = "refs/heads/feature/wave-2-booking-and-scheduling"
            changes.update(
                GITHUB_REF=legacy_ref,
                GITHUB_WORKFLOW_REF=(
                    f"{env['GITHUB_REPOSITORY']}/.github/workflows/foundation.yml@{legacy_ref}"
                ),
            )
        result = acceptance.validate_request({**env, **changes})
        assert result["mode"] == name
        assert json.loads(result["matrix"]) == ["standard"]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "w3-checkin",
        "w3-checkin-races",
        "w3-receipt-boundary",
        "w3-queue-scope",
        "w3-intake",
        "w3-intake-races",
        "w3-admission-controls",
        "w3-admission-confirmations",
        "w3-admission",
        "w3-admission-races",
        "w3-migration",
    ],
)
def test_wave3_diagnostic_remains_isolated_from_full_acceptance(env, diagnostic):
    normalized = acceptance.validate_request(
        {**env, "INPUT_POSTGRES_DIAGNOSTIC": diagnostic}
    )
    assert normalized["mode"] == "postgres-diagnostic"
    assert json.loads(normalized["matrix"]) == ["standard"]


@pytest.mark.parametrize(
    "changes",
    [
        {"INPUT_B04_SPIKE": "TRUE"},
        {"INPUT_T133_TERMINAL": "yes"},
        {"INPUT_POSTGRES_DIAGNOSTIC": "compat-full"},
        {"INPUT_ORDINARY_PROFILE_SHA": "a" * 40},
        {"INPUT_ORDINARY_PARTITION_SHA": "a" * 40},
        {
            "INPUT_B04_SPIKE": "true",
            "INPUT_T133_TERMINAL": "true",
            "INPUT_T133_SHA": "a" * 40,
        },
        {"INPUT_B04_SPIKE": "true", "INPUT_POSTGRES_DIAGNOSTIC": "booking-races"},
        {"INPUT_T133_SHA": "a" * 40},
        {"INPUT_T133_SHA": "b" * 40},
        {"INPUT_T133_TERMINAL": "true", "INPUT_T133_SHA": "b" * 40},
        {"INPUT_T133_TERMINAL": "true", "INPUT_T133_SHA": "A" * 40},
        {"GITHUB_SHA": "b" * 39},
        {"GITHUB_RUN_ATTEMPT": "0"},
        {"GITHUB_REPOSITORY": "../unsafe"},
        {"GITHUB_WORKFLOW_REF": "another.yml@refs/heads/main"},
        {"GITHUB_REF": "refs/pull/4/merge"},
    ],
)
def test_invalid_normalization_fails_closed(env, changes):
    with pytest.raises(ValueError):
        acceptance.validate_request({**env, **changes})


@pytest.mark.parametrize(
    ("repository", "ref"),
    [
        (
            "ahmedali5696/care-pulse-3",
            "refs/heads/feature/foundation-permanent-acceptance-20261123",
        ),
        ("other/care-pulse-3", "refs/heads/feature/wave-2-booking-and-scheduling"),
    ],
)
def test_t133_rejects_unauthorized_context_even_with_exact_sha(env, repository, ref):
    with pytest.raises(ValueError, match="authorized legacy repository and ref"):
        acceptance.validate_request(
            {
                **env,
                "GITHUB_REPOSITORY": repository,
                "GITHUB_REF": ref,
                "GITHUB_WORKFLOW_REF": (
                    f"{repository}/.github/workflows/foundation.yml@{ref}"
                ),
                "INPUT_T133_TERMINAL": "true",
                "INPUT_T133_SHA": env["GITHUB_SHA"],
            }
        )


def test_unapproved_modes_on_push_fail(env):
    push = {
        **env,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/development",
        "GITHUB_WORKFLOW_REF": (
            f"{env['GITHUB_REPOSITORY']}/.github/workflows/"
            "foundation.yml@refs/heads/development"
        ),
    }
    assert acceptance.validate_request(push)["mode"] == "full"
    with pytest.raises(ValueError):
        acceptance.validate_request(
            {**push, "INPUT_POSTGRES_DIAGNOSTIC": "booking-races"}
        )


def test_pr_merge_context_is_included_and_checked(tmp_path, env):
    number = 27
    ref = f"refs/pull/{number}/merge"
    event_file = tmp_path / "event.json"
    event_file.write_text(
        json.dumps(
            {
                "noncritical_event_payload": "x" * 12000,
                "pull_request": {
                    "number": number,
                    "head": {"ref": "feature/next", "sha": "b" * 40},
                    "base": {"ref": "development", "sha": "c" * 40},
                },
            }
        ),
        encoding="utf-8",
    )
    pr_env = {
        **env,
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REF": ref,
        "GITHUB_WORKFLOW_REF": (
            f"{env['GITHUB_REPOSITORY']}/.github/workflows/"
            "foundation.yml@refs/heads/development"
        ),
        "GITHUB_HEAD_REF": "feature/next",
        "GITHUB_BASE_REF": "development",
        "GITHUB_EVENT_PATH": str(event_file),
    }
    ctx = acceptance.github_context(pr_env)
    assert ctx["sha"] == "a" * 40  # The checked-out merge SHA, not the PR head.
    assert ctx["pr"] == {
        "number": number,
        "head_ref": "feature/next",
        "head_sha": "b" * 40,
        "base_ref": "development",
        "base_sha": "c" * 40,
    }
    for changed in (
        {"GITHUB_HEAD_REF": "wrong"},
        {"GITHUB_REF": "refs/pull/28/merge"},
    ):
        with pytest.raises(ValueError):
            acceptance.github_context(
                {
                    **pr_env,
                    **changed,
                    "GITHUB_WORKFLOW_REF": (
                        f"{env['GITHUB_REPOSITORY']}/.github/workflows/"
                        f"foundation.yml@{changed['GITHUB_REF']}"
                        if "GITHUB_REF" in changed
                        else pr_env["GITHUB_WORKFLOW_REF"]
                    ),
                }
            )
    with pytest.raises(ValueError):
        acceptance.github_context(
            {
                **pr_env,
                "GITHUB_WORKFLOW_REF": (
                    f"{env['GITHUB_REPOSITORY']}/.github/workflows/"
                    "foundation.yml@refs/heads/unrelated"
                ),
            }
        )


def test_full_aggregate_matches_all_exact_receipts(receipts, env):
    acceptance.aggregate(receipts, aggregate_env(env))


@pytest.mark.parametrize(
    "lane,wrong_head",
    [
        ("ordinary-db", "0046_f01_audit_integrity"),
        ("ordinary-stack", "0046_f01_audit_integrity"),
        ("compat", "0047_b12_queue_checkin_grants"),
        ("compat", "0058_b15_admission_guards"),
    ],
)
def test_lane_schema_provenance_cannot_be_interchanged(tmp_path, env, lane, wrong_head):
    path = tmp_path / acceptance.RECEIPT_FILE
    with pytest.raises(ValueError, match="CP_FOUNDATION_CANDIDATE_SCHEMA"):
        acceptance.seal(
            path, {**seal_env(env, lane), "CP_FOUNDATION_CANDIDATE_SCHEMA": wrong_head}
        )
    assert not path.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("INPUT_RESULT", "failure"),
        ("INPUT_RESULT", "skipped"),
        ("LANES_RESULT", "cancelled"),
        ("NORMALIZED_MODE", "compat-diagnostic"),
        ("CANDIDATE_SHA", "b" * 40),
        ("COMMON_RUN_ID", "2"),
        ("COMMON_ATTEMPT", "3"),
        ("COMMON_REPOSITORY", "different/repo"),
        ("COMMON_WORKFLOW", "different"),
    ],
)
def test_dependency_or_attempt_mismatch_fails(receipts, env, key, value):
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, {**aggregate_env(env), key: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", 1),
        ("schema", 2),
        ("schema", True),
        ("kind", "PROFILING_ONLY_NOT_ACCEPTANCE"),
        ("coverage", "incomplete"),
        ("statuses", {"execution": "PASS", "tests": "PASS", "skipped": 1}),
        ("cleanup", "cleanup attempted"),
        ("provenance", {"source_sha": "b" * 40}),
        ("context", {"sha": "b" * 40}),
        ("reports", {}),
        ("lane", "ordinary"),
    ],
)
def test_changed_receipt_fails(receipts, env, field, value):
    target = receipt_file(receipts, env)
    item = json.loads(target.read_text(encoding="utf-8"))
    item[field] = value
    target.write_text(json.dumps(item), encoding="utf-8")
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, aggregate_env(env))


def test_duplicate_json_key_fails(receipts, env):
    target = receipt_file(receipts, env)
    content = target.read_text(encoding="utf-8")
    target.write_text(
        content.replace('{"cleanup":', '{"schema":2,"cleanup":', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, aggregate_env(env))


def test_missing_extra_and_wrong_named_artifacts_fail(receipts, env):
    target = receipt_file(receipts, env)
    target.unlink()
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, aggregate_env(env))
    target.write_text("{}", encoding="utf-8")
    extra = receipts / "foundation-full-older-attempt-ordinary"
    extra.mkdir()
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, aggregate_env(env))


def test_extra_file_in_artifact_fails(receipts, env):
    (receipt_file(receipts, env).parent / "extra.txt").write_text(
        "unexpected", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, aggregate_env(env))


def test_linked_receipt_fails(receipts, env):
    target = receipt_file(receipts, env)
    original = target.read_text(encoding="utf-8")
    target.unlink()
    external = receipts.parent / "linked-receipt-target.json"
    external.write_text(original, encoding="utf-8")
    try:
        target.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks not permitted by this local test host")
    with pytest.raises(ValueError):
        acceptance.aggregate(receipts, aggregate_env(env))


@pytest.mark.parametrize(
    "key,value",
    [
        ("CP_FOUNDATION_COVERAGE_VERIFIED", "0"),
        ("CP_FOUNDATION_CLEANUP_VERIFIED", "0"),
        ("CP_FOUNDATION_EXECUTION", "FAIL"),
        ("CP_FOUNDATION_TESTS", "SKIPPED"),
        ("CP_FOUNDATION_ZERO_SKIPS", "false"),
        ("CP_FOUNDATION_CLEANUP", "FAIL"),
        ("CP_FOUNDATION_COVERAGE", "incomplete"),
        ("CP_FOUNDATION_SOURCE_SHA", "b" * 40),
        ("CP_FOUNDATION_SOURCE_SCHEMA", "0041"),
        ("CP_FOUNDATION_CANDIDATE_SCHEMA", "0045"),
        ("CP_FOUNDATION_MODE", "postgres-diagnostic"),
        ("CP_FOUNDATION_REPORT_EVIDENCE", ""),
        ("CP_FOUNDATION_REPORT_EVIDENCE", "{}"),
        ("INPUT_POSTGRES_DIAGNOSTIC", "compat-us3"),
    ],
)
def test_seal_requires_positive_terminal_evidence(tmp_path, env, key, value):
    path = tmp_path / acceptance.RECEIPT_FILE
    with pytest.raises(ValueError):
        acceptance.seal(path, {**seal_env(env, "compat"), key: value})
    assert not path.exists()


def test_seal_cannot_overwrite_existing_proof(tmp_path, env):
    path = tmp_path / acceptance.RECEIPT_FILE
    acceptance.seal(path, seal_env(env, "ordinary-db"))
    first = path.read_bytes()
    with pytest.raises(FileExistsError):
        acceptance.seal(path, seal_env(env, "ordinary-db"))
    assert path.read_bytes() == first


def test_validation_does_not_publish_partial_output(tmp_path, env, monkeypatch):
    output = tmp_path / "github-output"
    output.write_text("existing=retained\n", encoding="utf-8")
    monkeypatch.setattr(acceptance, "_assert_checkout_sha", lambda _: None)
    with pytest.raises(ValueError):
        acceptance.main(
            ["validate-request"],
            {**env, "GITHUB_OUTPUT": str(output), "INPUT_B04_SPIKE": "garbage"},
        )
    assert output.read_text(encoding="utf-8") == "existing=retained\n"
    acceptance.main(["validate-request"], {**env, "GITHUB_OUTPUT": str(output)})
    assert (
        'matrix=["ordinary-db","ordinary-stack","historical","compat"]\n'
        in output.read_text(encoding="utf-8")
    )


def test_cli_rejects_checkout_mismatch_before_sealing(tmp_path, env, monkeypatch):
    monkeypatch.setattr(
        acceptance,
        "_assert_checkout_sha",
        lambda _: (_ for _ in ()).throw(ValueError("wrong SHA")),
    )
    path = tmp_path / acceptance.RECEIPT_FILE
    with pytest.raises(ValueError):
        acceptance.main(["seal", str(path)], seal_env(env, "ordinary-db"))
    assert not path.exists()


def test_cli_emits_final_acceptance_only_after_complete_evidence(
    receipts, tmp_path, env, monkeypatch, capsys
):
    monkeypatch.setattr(acceptance, "_assert_checkout_sha", lambda _: None)
    summary = tmp_path / "summary.md"
    context = {
        **aggregate_env(env),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    acceptance.main(["aggregate", str(receipts)], context)
    output = capsys.readouterr().out
    assert "FOUNDATION_FULL_ACCEPTED" in output
    assert f"sha={env['GITHUB_SHA']}" in output
    assert f"Tested github.sha: `{env['GITHUB_SHA']}`" in summary.read_text()
    assert (
        f"Ordinary ({acceptance.ORDINARY_SCHEMA}, including B-12, B-11 and B-14)"
        in summary.read_text()
    )
    assert "44 Python + 11 Web" in summary.read_text()
    assert "Four current-attempt receipts" in summary.read_text()
    summary.unlink()
    receipt_file(receipts, env).unlink()
    with pytest.raises(ValueError):
        acceptance.main(["aggregate", str(receipts)], context)
    assert not summary.exists()


def test_current_inventory_has_exact_disjoint_ordinary_reports():
    db = inventory.REPORT_COUNTS["ordinary-db"]
    stack = inventory.REPORT_COUNTS["ordinary-stack"]
    assert (sum(db.values()), sum(stack.values())) == (485, 605)
    assert not db.keys() & stack.keys()
    assert not (
        {inventory.EXPECTED_IDENTITY_DIGESTS[name] for name in db}
        & {inventory.EXPECTED_IDENTITY_DIGESTS[name] for name in stack}
    )
    assert set(inventory.EXPECTED_IDENTITY_DIGESTS) == {
        name for counts in inventory.REPORT_COUNTS.values() for name in counts
    }


@pytest.mark.parametrize(
    "field",
    [
        "manifest_sha256",
        "total",
        "report_counts",
        "report_identity_sha256",
    ],
)
def test_seal_rejects_changed_exact_report_inventory(tmp_path, env, field):
    lane_env = seal_env(env, "ordinary-db")
    evidence = json.loads(lane_env["CP_FOUNDATION_REPORT_EVIDENCE"])
    if field == "manifest_sha256":
        evidence[field] = "c" * 64
    elif field == "total":
        evidence[field] += 1
    elif field == "report_counts":
        evidence[field]["f01"] -= 1
        evidence[field]["d08"] += 1
    else:
        evidence[field]["f01"] = "c" * 64
    lane_env["CP_FOUNDATION_REPORT_EVIDENCE"] = json.dumps(evidence)
    path = tmp_path / acceptance.RECEIPT_FILE
    with pytest.raises(ValueError):
        acceptance.seal(path, lane_env)
    assert not path.exists()


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "wrong-name",
        "wrong-case",
        "skip",
        "duplicate",
        "wrong-tap-case",
        "tap-skipped",
        "tap-duplicate-summary",
        None,
    ],
)
def test_exact_report_collection_rejects_incomplete_or_substituted_cases(
    tmp_path, monkeypatch, change
):
    # Use two actual baseline report identities in a small manifest.
    monkeypatch.setitem(
        inventory.REPORT_COUNTS,
        "ordinary-stack",
        {
            "health-2": 1,
            "web-ingress": 2,
        },
    )
    junit = (
        '<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0">'
        '<testcase classname="tests.integration.test_health" name="test_running_service_state"/>'
        "</testsuite></testsuites>"
    )
    tap = (
        "TAP version 13\n"
        "ok 1 - real TCP peers have independent login limits despite spoofed forwarding headers\n"
        "ok 2 - identity limit holds across distinct TCP peers\n"
        "1..2\n# tests 2\n# pass 2\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n"
    )
    if change == "wrong-case":
        junit = junit.replace("test_running_service_state", "test_other")
    elif change == "skip":
        junit = junit.replace("/>", "><skipped/></testcase>")
    elif change == "duplicate":
        junit = junit.replace(
            "</testsuite>",
            '<testcase classname="tests.integration.test_health" name="test_running_service_state"/></testsuite>',
        )
    elif change == "wrong-tap-case":
        tap = tap.replace("identity limit holds", "identity limit fails")
    elif change == "tap-skipped":
        tap = tap.replace("# skipped 0", "# skipped 1")
    elif change == "tap-duplicate-summary":
        tap += "# tests 2\n"
    if change != "missing":
        filename = "unexpected.xml" if change == "wrong-name" else "health-2.xml"
        (tmp_path / filename).write_text(junit, encoding="utf-8")
    if change == "extra":
        (tmp_path / "extra.xml").write_text(junit, encoding="utf-8")
    (tmp_path / "web-ingress.tap").write_text(tap, encoding="utf-8")
    if change is None:
        assert inventory.collect("ordinary-stack", tmp_path)["total"] == 3
    else:
        with pytest.raises(ValueError):
            inventory.collect("ordinary-stack", tmp_path)
