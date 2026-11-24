"""Negative evidence cases for the permanent Foundation full-lane gate."""

import json

import pytest

from ops import foundation_acceptance as acceptance


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
        "matrix": '["ordinary","historical","compat"]',
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
    "changes",
    [
        {"INPUT_B04_SPIKE": "TRUE"},
        {"INPUT_T133_TERMINAL": "yes"},
        {"INPUT_POSTGRES_DIAGNOSTIC": "compat-full"},
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
        ("ordinary", "0046_f01_audit_integrity"),
        ("compat", "0047_b12_queue_checkin_grants"),
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
        ("schema", True),
        ("kind", "PROFILING_ONLY_NOT_ACCEPTANCE"),
        ("coverage", "incomplete"),
        ("statuses", {"execution": "PASS", "tests": "PASS", "skipped": 1}),
        ("cleanup", "cleanup attempted"),
        ("provenance", {"source_sha": "b" * 40}),
        ("context", {"sha": "b" * 40}),
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
    ],
)
def test_seal_requires_positive_terminal_evidence(tmp_path, env, key, value):
    path = tmp_path / acceptance.RECEIPT_FILE
    with pytest.raises(ValueError):
        acceptance.seal(path, {**seal_env(env, "compat"), key: value})
    assert not path.exists()


def test_seal_cannot_overwrite_existing_proof(tmp_path, env):
    path = tmp_path / acceptance.RECEIPT_FILE
    acceptance.seal(path, seal_env(env, "ordinary"))
    first = path.read_bytes()
    with pytest.raises(FileExistsError):
        acceptance.seal(path, seal_env(env, "ordinary"))
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
    assert 'matrix=["ordinary","historical","compat"]\n' in output.read_text(
        encoding="utf-8"
    )


def test_cli_rejects_checkout_mismatch_before_sealing(tmp_path, env, monkeypatch):
    monkeypatch.setattr(
        acceptance,
        "_assert_checkout_sha",
        lambda _: (_ for _ in ()).throw(ValueError("wrong SHA")),
    )
    path = tmp_path / acceptance.RECEIPT_FILE
    with pytest.raises(ValueError):
        acceptance.main(["seal", str(path)], seal_env(env, "ordinary"))
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
    assert "44 Python + 11 Web" in summary.read_text()
    summary.unlink()
    receipt_file(receipts, env).unlink()
    with pytest.raises(ValueError):
        acceptance.main(["aggregate", str(receipts)], context)
    assert not summary.exists()
