"""Ordinary Foundation supervisor event admission; diagnostics remain manual-only."""

from __future__ import annotations

import json

import pytest

from ops.compat.ci_host import trusted_foundation_event


@pytest.fixture
def trusted_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "GITHUB_REPOSITORY": "ahmedali5696/care-pulse-3",
        "GITHUB_SERVER_URL": "https://github.com",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    ("event", "ref", "accepted"),
    [
        ("workflow_dispatch", "refs/heads/feature/pre-wave-3-runtime-identity", True),
        ("push", "refs/heads/development", True),
        ("push", "refs/heads/unreviewed", False),
        ("schedule", "refs/heads/development", False),
        ("workflow_dispatch", "refs/tags/unsafe", False),
    ],
)
def test_foundation_trusted_events(trusted_runner, monkeypatch, event, ref, accepted):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    assert trusted_foundation_event() is accepted


def test_first_party_pull_request_and_fork_denial(
    trusted_runner, monkeypatch, tmp_path
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/123/merge")
    path = tmp_path / "event.json"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    payload = {
        "repository": {"full_name": "ahmedali5696/care-pulse-3"},
        "pull_request": {
            "head": {"repo": {"full_name": "ahmedali5696/care-pulse-3"}},
            "base": {"repo": {"full_name": "ahmedali5696/care-pulse-3"}},
        },
    }
    path.write_text(json.dumps(payload))
    assert trusted_foundation_event()
    payload["pull_request"]["head"]["repo"]["full_name"] = "untrusted/fork"
    path.write_text(json.dumps(payload))
    assert not trusted_foundation_event()
    path.write_text("not-json")
    assert not trusted_foundation_event()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CI", "false"),
        ("GITHUB_ACTIONS", "false"),
        ("RUNNER_OS", "Windows"),
        ("GITHUB_REPOSITORY", "another/repository"),
        ("GITHUB_SERVER_URL", "https://example.invalid"),
    ],
)
def test_untrusted_runners_rejected(trusted_runner, monkeypatch, key, value):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/development")
    monkeypatch.setenv(key, value)
    assert not trusted_foundation_event()
