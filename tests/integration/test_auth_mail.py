"""F-07 deny-only HTTP and isolated Mailpit evidence with D-02 persistence."""

import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
import pytest
from test_database import REVISION


@pytest.fixture(scope="module", autouse=True)
def disposable_project():
    if os.getenv("CI") != "true":
        pytest.skip("Auth/mail running-service checks are CI-only")
    project = os.environ["CP_CI_PROJECT"]
    assert re.fullmatch(r"carepulse-ci-[0-9]+-[0-9]+", project)
    config = json.loads(Path(os.environ["CP_COMPOSE_CONFIG_PATH"]).read_text())
    assert config["name"] == project
    for service in ("api", "db", "mail"):
        assert not config["services"][service].get("ports")


def _request(url, method="GET", headers=None, body=None):
    try:
        return urlopen(
            Request(url, method=method, headers=headers or {}, data=body), timeout=6
        )
    except HTTPError as error:
        return error


def _denial(response, status, code, method="GET"):
    with response:
        assert response.status == status
        assert response.headers["Cache-Control"] == "no-store"
        body = response.read(2048)
    if method == "HEAD":
        assert body == b""
    else:
        error = json.loads(body)["error"]
        assert set(error) == {"code", "message", "request_id"}
        assert error["code"] == code
        assert (
            error["message"]
            == {
                "UNAUTHENTICATED": "Authentication required",
                "FORBIDDEN": "Operation unavailable",
                "INVALID_INPUT": "Invalid request",
            }[code]
        )
        assert UUID(error["request_id"]).version == 4


@pytest.mark.parametrize(
    "method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
def test_anonymous_bff_never_forwards(method):
    _denial(
        _request(
            "http://web:3000/api/backend/__foundation_bff_must_not_forward__", method
        ),
        401,
        "UNAUTHENTICATED",
        method,
    )


def test_forged_browser_authority_is_not_identity():
    _denial(
        _request(
            "http://web:3000/api/backend/__foundation_bff_must_not_forward__",
            "POST",
            {
                "Cookie": "better-auth.session_token=forged; better-auth.session_data=forged",
                "X-CarePulse-Organization-Id": "forged-tenant",
                "X-CarePulse-Role": "Doctor",
                "X-CarePulse-Service-Key": "forged",
            },
        ),
        401,
        "UNAUTHENTICATED",
    )


@pytest.mark.parametrize("key", [None, "forged", "valid"])
def test_private_service_key_alone_never_authorizes_business_work(key):
    headers = {}
    if key:
        headers["X-CarePulse-Service-Key"] = (
            Path("/run/secrets/service_key").read_text().strip()
            if key == "valid"
            else key
        )
    _denial(
        _request("http://api:8000/v1/__foundation_service_denied__", "POST", headers),
        403 if key == "valid" else 401,
        "FORBIDDEN" if key == "valid" else "UNAUTHENTICATED",
    )


def test_signup_login_and_public_mail_routes_are_absent():
    for path in (
        "/api/auth/sign-up/email",
        "/api/auth/sign-in/email",
        "/api/mail/send",
        "/login",
    ):
        with _request(f"http://web:3000{path}", "POST") as response:
            assert response.status == 404


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Requested-With": "XMLHttpRequest"},
        {"Origin": "http://untrusted.invalid", "X-Requested-With": "XMLHttpRequest"},
        {"Origin": "http://web:3000"},
        {
            "Origin": "http://web:3000",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "cross-site",
        },
    ],
)
def test_owner_invite_rejects_missing_or_foreign_origin_and_csrf(headers):
    payload = json.dumps(
        {
            "organization_id": str(uuid4()),
            "invitation_id": str(uuid4()),
            "proof": "A" * 43,
            "name": "Synthetic forged",
            "password": "synthetic-forged",
        }
    ).encode()
    headers = {"Content-Type": "application/json", **headers}
    response = _request(
        "http://web:3000/api/auth/invite-accept", "POST", headers, payload
    )
    assert response.headers.get("Set-Cookie") is None
    _denial(response, 403, "FORBIDDEN")


def test_owner_invite_unknown_proof_is_denied_without_session_cookie():
    payload = json.dumps(
        {
            "organization_id": str(uuid4()),
            "invitation_id": str(uuid4()),
            "proof": "B" * 43,
            "name": "Synthetic forged",
            "password": "synthetic-forged",
        }
    ).encode()
    response = _request(
        "http://web:3000/api/auth/invite-accept",
        "POST",
        {
            "Content-Type": "application/json",
            "Origin": "http://web:3000",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
        },
        payload,
    )
    assert response.headers.get("Set-Cookie") is None
    _denial(response, 403, "FORBIDDEN")


def test_both_reserved_templates_arrived_without_tokens():
    with urlopen("http://mail:8025/api/v1/messages", timeout=5) as response:
        mailbox = json.load(response)
    assert mailbox["total"] == 2
    for message in mailbox["messages"]:
        with urlopen(
            f"http://mail:8025/api/v1/message/{message['ID']}", timeout=5
        ) as response:
            delivered = json.load(response)
        kind = "invitation" if "invitation" in delivered["Subject"] else "reset"
        assert delivered["Subject"] == f"[SYNTHETIC ONLY] CarePulse {kind}"
        assert [recipient["Address"] for recipient in delivered["To"]] == [
            f"{kind}@example.invalid"
        ]
        for field in ("Text", "HTML"):
            assert (
                f"https://carepulse.invalid/non-operational/{kind}" in delivered[field]
            )
            assert "No account or token is created." in delivered[field]
    assert {message["Subject"] for message in mailbox["messages"]} == {
        "[SYNTHETIC ONLY] CarePulse invitation",
        "[SYNTHETIC ONLY] CarePulse reset",
    }


def test_denied_routes_preserve_current_persistence_boundary():
    with psycopg.connect(
        host="db",
        dbname="carepulse",
        user="carepulse_bootstrap",
        password=Path("/run/secrets/postgres_password").read_text().strip(),
    ) as connection:
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        assert set(tables) == {
            ("alembic_version",),
            ("organizations",),
            ("operator_identities",),
            ("user",),
            ("session",),
            ("account",),
            ("verification",),
            ("staff_accounts",),
            ("invitations",),
            ("recovery_operations",),
            ("auth_recovery_completions",),
            ("recovery_native_effects",),
            ("sensitive_audit",),
            ("clinical_audit",),
            ("outbox_events",),
            ("delivery_jobs",),
            ("protected_delivery_material",),
            ("request_nonces",),
            ("idempotency_receipts",),
            ("auth_login_attempts",),
            ("operator_auth_attempts",),
            ("operator_creation_pending",),
            ("operator_invitation_pending",),
            ("operator_action_pending",),
            ("patients",),
            ("clinic_policy_versions",),
            ("clinic_policy_heads",),
            ("clinic_schedule_intervals",),
            ("clinic_schedule_holidays",),
            ("clinic_policy_previews",),
            ("appointments",),
            ("appointment_price_snapshots",),
            ("appointment_day_counters",),
            ("appointment_number_assignments",),
            ("appointment_history",),
            ("visits",),
            ("clinical_revisions",),
            ("completion_attestations",),
            ("followup_source_state",),
            ("followup_claims",),
            ("followup_extensions",),
            ("queue_days",),
            ("queue_entries",),
            ("doctor_controls",),
            ("doctor_admission_confirmations",),
        }
        assert connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchall() == [(REVISION,)]
