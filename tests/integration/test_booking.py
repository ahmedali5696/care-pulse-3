"""B-09 signed booking acceptance against disposable PostgreSQL."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.db import transaction
from carepulse.identity.signing import canonical_payload, canonical_query
from carepulse.main import app
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.slots import candidates_for_date
from carepulse.policies.versions import append_version
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _patient, _persist_with_status
from test_patients import create_patient
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _patch, pricing_request
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _actor, _config, _publish


def booking_request(
    clinic,
    actor,
    *,
    method="POST",
    path="/v1/appointments",
    body=None,
    key="",
    query=None,
    raw_body=None,
):
    encoded = (
        raw_body
        if raw_body is not None
        else json.dumps(body, separators=(",", ":")).encode()
        if body is not None
        else b""
    )
    signed = replace(
        actor,
        method=method,
        path=path,
        query=canonical_query(urlencode(query or {}, doseq=True)),
        body_sha256=sha256(encoded).hexdigest(),
        purpose="APPOINTMENT_BOOKING",
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    fields = (
        "protocol_version",
        "key_id",
        "service",
        "organization_id",
        "staff_id",
        "session_id",
        "role",
        "security_revision",
        "timestamp",
        "nonce",
        "purpose",
        "idempotency_key",
    )
    headers = [
        (
            f"x-carepulse-{field.replace('_', '-')}".encode(),
            str(getattr(signed, field)).encode(),
        )
        for field in fields
    ] + [
        (b"content-type", b"application/json"),
        (b"x-carepulse-service-key", clinic.config.service_key.encode()),
        (
            b"x-carepulse-signature",
            new(clinic.config.service_key.encode(), canonical_payload(signed), sha256)
            .hexdigest()
            .encode(),
        ),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": signed.query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("synthetic", 1),
        "server": ("api", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages[0]["status"], json.loads(
        b"".join(message.get("body", b"") for message in messages[1:])
    )


@pytest.fixture
def booking_clinic(patient_backend):
    clinic = patient_backend
    status, response = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(standard_new_exam_egp="100.00"),
        key=str(uuid4()),
    )
    assert status == 200, response
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            _config(mode="OPEN"),
        )
    return clinic


def booking_body(patient, *, day=SERVICE_DAY, **changes):
    return {
        "patient_id": str(patient),
        "service_date": day.isoformat(),
        "requested_service_type": "NEW_EXAMINATION",
        "discount": {
            "discount_type": "NONE",
            "discount_value": "0",
            "discount_reason": None,
        },
        "expected_base_egp": "100.00",
        "expected_net_egp": "100.00",
        "expected_price_revision": 2,
        "expected_followup_revision": 1,
        "expected_schedule_revision": 2,
        **changes,
    }


def booking_effects(clinic):
    tables = {
        "appointments": None,
        "appointment_day_counters": None,
        "appointment_number_assignments": None,
        "appointment_price_snapshots": None,
        "appointment_history": None,
        "sensitive_audit": "action = 'appointments.book'",
        "outbox_events": "event_type = 'APPOINTMENT_BOOKED'",
        "idempotency_receipts": "operation = 'appointments.book'",
    }
    # Sensitive audits are intentionally hidden from unbound runtime SELECT by RLS.
    with clinic.migration.connect() as connection:
        return tuple(
            connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} WHERE organization_id = :tenant"
                    + (f" AND {condition}" if condition else "")
                ),
                {"tenant": clinic.tenant},
            )
            for table, condition in tables.items()
        )


def new_patient(clinic):
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return _patient(connection, clinic.tenant)


def test_signed_booking_rejects_unrepresentable_reason_then_replays_valid_receipt(
    booking_clinic,
):
    clinic = booking_clinic
    body = booking_body(
        new_patient(clinic),
        discount={
            "discount_type": "FIXED",
            "discount_value": "5.00",
            "discount_reason": None,
        },
        expected_net_egp="95.00",
    )
    key = str(uuid4())
    before = booking_effects(clinic)
    for invalid in ("Approved\x00reduction", "Approved\ud800reduction"):
        body["discount"]["discount_reason"] = invalid
        status, response = booking_request(clinic, clinic.owner, body=body, key=key)
        assert status == 422, response
        assert (response["error"]["code"], response["error"]["message"]) == (
            "INVALID_INPUT",
            "Invalid request",
        )
        assert booking_effects(clinic) == before

    reason = "  approved hardship  "
    body["discount"]["discount_reason"] = reason
    status, response = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201, response
    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
                SELECT discount_reason FROM public.appointment_price_snapshots
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
                {"tenant": clinic.tenant, "appointment": UUID(response["data"]["id"])},
            )
            == reason
        )
    after = booking_effects(clinic)
    assert booking_request(clinic, clinic.owner, body=body, key=key) == (201, response)
    assert booking_effects(clinic) == after


@pytest.mark.parametrize("role", ["owner", "receptionist"])
def test_signed_open_booking_commits_frozen_ledger_and_readback(booking_clinic, role):
    clinic = booking_clinic
    patient = new_patient(clinic)
    actor = getattr(clinic, role)
    body = booking_body(
        patient,
        discount={
            "discount_type": "PERCENT",
            "discount_value": "12.345",
            "discount_reason": "Synthetic approved reduction",
        },
        expected_net_egp="87.66",
    )
    status, response = booking_request(clinic, actor, body=body, key=str(uuid4()))
    assert status == 201, response
    saved = response["data"]
    assert (
        saved["status"],
        saved["service_type"],
        saved["classification"],
        saved["lane"],
        saved["booking_mode"],
    ) == ("Booked", "NEW_EXAMINATION", "ROUTINE", "MAIN", "OPEN")
    assert (saved["patient_id"], saved["doctor_staff_id"]) == (
        str(patient),
        clinic.owner.staff_id,
    )
    assert (
        saved["number"],
        saved["revision"],
        saved["base_egp"],
        saved["net_egp"],
        saved["discount_value"],
    ) == (1, 0, "100.00", "87.66", "12.345")
    assert saved["starts_at"] is saved["ends_at"] is None
    assert len(saved["history"]) == 1 and saved["history"][0]["action"] == "CREATED"
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.execute(
            text("""
            SELECT a.id, a.current_number, n.number, s.net_egp, s.discount_value,
                   s.discount_percent_text, h.appointment_revision
            FROM public.appointments a
            JOIN public.appointment_number_assignments n
              ON n.id = a.current_assignment_id
            JOIN public.appointment_price_snapshots s ON s.appointment_id = a.id
            JOIN public.appointment_history h ON h.appointment_id = a.id
            WHERE a.id = :id
        """),
            {"id": UUID(saved["id"])},
        ).one() == (UUID(saved["id"]), 1, 1, Decimal("87.66"), None, "12.345", 0)
        # Neither an absent future table nor an existing one may contain admission.
        for table in ("visits", "queue_entries"):
            if (
                connection.scalar(
                    text("SELECT to_regclass(:table)"), {"table": f"public.{table}"}
                )
                is not None
            ):
                assert (
                    connection.scalar(
                        text(
                            f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant"
                        ),
                        {"tenant": clinic.tenant},
                    )
                    == 0
                )
    assert (
        booking_request(
            clinic, actor, method="GET", path=f"/v1/appointments/{saved['id']}"
        )[1]["data"]
        == saved
    )
    status, listed = booking_request(
        clinic, actor, method="GET", query={"patient_id": str(patient)}
    )
    assert status == 200 and len(listed["data"]["items"]) == 1
    assert listed["data"]["items"][0]["id"] == saved["id"]
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_signed_booking_roundtrips_16384_digit_percentage_and_receipt(booking_clinic):
    clinic = booking_clinic
    patient = new_patient(clinic)
    percentage = "0.005" + "0" * 16380 + "1"
    body = booking_body(
        patient,
        discount={
            "discount_type": "PERCENT",
            "discount_value": percentage,
            "discount_reason": "Synthetic exact percentage",
        },
        expected_net_egp="99.99",
    )
    key = str(uuid4())
    status, first = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201, first
    assert first["data"]["discount_value"] == percentage
    assert first["data"]["net_egp"] == "99.99"
    before = booking_effects(clinic)
    assert booking_request(clinic, clinic.owner, body=body, key=key) == (201, first)
    assert booking_effects(clinic) == before
    with transaction(clinic.runtime, clinic.tenant) as connection:
        numeric_value, text_value, net = connection.execute(
            text("""
                SELECT discount_value, discount_percent_text, net_egp
                FROM public.appointment_price_snapshots
                WHERE organization_id=:tenant AND appointment_id=:appointment
            """),
            {
                "tenant": clinic.tenant,
                "appointment": UUID(first["data"]["id"]),
            },
        ).one()
        assert numeric_value is None
        assert text_value == percentage
        assert net == Decimal("99.99")


def test_signed_appointment_list_keyset_cursor_and_strict_query_boundaries(
    booking_clinic,
):
    clinic = booking_clinic
    confirmed = set()
    for _ in range(2):
        status, response = booking_request(
            clinic,
            clinic.receptionist,
            body=booking_body(new_patient(clinic)),
            key=str(uuid4()),
        )
        assert status == 201, response
        confirmed.add(response["data"]["id"])
    before = booking_effects(clinic)
    status, first = booking_request(
        clinic, clinic.receptionist, method="GET", query={"limit": "1"}
    )
    assert status == 200, first
    assert len(first["data"]["items"]) == 1
    assert first["data"]["next_cursor"]
    status, second = booking_request(
        clinic,
        clinic.receptionist,
        method="GET",
        query={"limit": "1", "cursor": first["data"]["next_cursor"]},
    )
    assert status == 200, second
    assert second["data"]["next_cursor"] is None
    assert {
        first["data"]["items"][0]["id"],
        second["data"]["items"][0]["id"],
    } == confirmed
    for invalid in ("0", "101", "abc", ["1", "2"]):
        status, _ = booking_request(
            clinic, clinic.receptionist, method="GET", query={"limit": invalid}
        )
        assert status == 422
    assert booking_effects(clinic) == before


@pytest.mark.parametrize(
    "existing_status,allowed",
    [
        ("Booked", False),
        ("Waiting", False),
        ("In Examination", False),
        ("Completed", False),
        ("Cancelled", True),
        ("No-show", True),
    ],
)
def test_counted_patient_day_guard_preserves_terminal_old_number(
    booking_clinic,
    existing_status,
    allowed,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        previous = _persist_with_status(
            connection,
            clinic.tenant,
            patient,
            UUID(clinic.owner.staff_id),
            existing_status,
            service_date=SERVICE_DAY,
        )
    before = booking_effects(clinic)
    status, result = booking_request(
        clinic, clinic.owner, body=booking_body(patient), key=str(uuid4())
    )
    assert status == (201 if allowed else 409), result
    if allowed:
        assert result["data"]["id"] != previous["id"]
        assert result["data"]["number"] == previous["number"] + 1
        with transaction(clinic.runtime, clinic.tenant) as connection:
            assert (
                connection.scalar(
                    text("SELECT current_number FROM public.appointments WHERE id=:id"),
                    {"id": UUID(previous["id"])},
                )
                == previous["number"]
            )
    else:
        assert booking_effects(clinic) == before


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"expected_price_revision": 1}, 409),
        ({"expected_schedule_revision": 1}, 409),
        ({"expected_net_egp": "99.99"}, 409),
        ({"status": "Completed"}, 422),
        ({"current_number": 999}, 422),
        ({"price_snapshot": {"net_egp": "0.00"}}, 422),
        (
            {
                "starts_at": "2030-01-01T09:00:00+02:00",
                "ends_at": "2030-01-01T09:30:00+02:00",
            },
            422,
        ),
        ({"service_date": (SERVICE_DAY - timedelta(days=100)).isoformat()}, 409),
    ],
)
def test_invalid_booking_denies_without_partial_ledger(
    booking_clinic, change, expected
):
    clinic = booking_clinic
    before = booking_effects(clinic)
    status, result = booking_request(
        clinic,
        clinic.receptionist,
        body=booking_body(new_patient(clinic), **change),
        key=str(uuid4()),
    )
    assert status == expected, result
    assert booking_effects(clinic) == before


def test_archived_patient_and_foreign_patient_cannot_create(booking_clinic):
    clinic = booking_clinic
    archived = new_patient(clinic)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text("""
            UPDATE public.patients SET archived = true,
                lifecycle_revision = lifecycle_revision + 1 WHERE id = :id
        """),
            {"id": archived},
        )
    with clinic.migration.begin() as connection:
        foreign = _patient(connection, clinic.other)
    before = booking_effects(clinic)
    for patient, expected in ((archived, 409), (foreign, 404)):
        status, result = booking_request(
            clinic, clinic.owner, body=booking_body(patient), key=str(uuid4())
        )
        assert status == expected, result
        assert booking_effects(clinic) == before


def test_same_phone_is_not_patient_identity_for_day_guard(booking_clinic):
    clinic = booking_clinic
    patients = [
        create_patient(clinic, phone_raw="+201001234567")["id"] for _ in range(2)
    ]
    booked = []
    for patient in patients:
        status, response = booking_request(
            clinic, clinic.owner, body=booking_body(patient), key=str(uuid4())
        )
        assert status == 201, response
        booked.append(response["data"])
    assert len({item["id"] for item in booked}) == 2
    assert {item["number"] for item in booked} == {1, 2}
    assert booking_effects(clinic) == (2, 1, 2, 2, 2, 2, 2, 2)


@pytest.mark.parametrize(
    "mode,days_ahead",
    [
        ("OPEN", 366),
        ("OPEN", 3650),
        ("EXACT_TIME", 366),
        ("EXACT_TIME", 3650),
    ],
)
def test_no_booking_horizon_but_current_rules_still_apply(
    booking_clinic,
    mode,
    days_ahead,
):
    clinic = booking_clinic
    day = datetime.now(ZoneInfo("Africa/Cairo")).date() + timedelta(days=days_ahead)
    proposal = _config(mode=mode, working_weekdays=list(range(1, 8)))
    if mode == "EXACT_TIME":
        proposal["weekly_work_periods"] = [
            {"weekday": weekday, "starts_at": "09:00", "ends_at": "12:00"}
            for weekday in range(1, 8)
        ]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            proposal,
            revision=2,
        )
        interval = (
            candidates_for_date(load_current_schedule(connection, clinic.tenant), day)[
                0
            ]
            if mode == "EXACT_TIME"
            else None
        )
    fields = {"expected_schedule_revision": 3}
    if interval:
        fields.update(
            starts_at=interval.starts_at.isoformat(),
            ends_at=interval.ends_at.isoformat(),
        )
    patient = new_patient(clinic)
    before = booking_effects(clinic)
    assert (
        booking_request(
            clinic,
            clinic.owner,
            body=booking_body(patient, day=day, expected_price_revision=1, **fields),
            key=str(uuid4()),
        )[0]
        == 409
    )
    assert booking_effects(clinic) == before
    status, result = booking_request(
        clinic,
        clinic.owner,
        body=booking_body(patient, day=day, **fields),
        key=str(uuid4()),
    )
    assert status == 201, result
    assert result["data"]["service_date"] == day.isoformat()
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_signed_booking_rejects_normalized_offset_and_truncated_instant(
    booking_clinic,
):
    clinic = booking_clinic
    day = date(2030, 7, 12)
    weekday = day.isoweekday()
    config = _config(
        working_weekdays=[weekday],
        weekly_work_periods=[
            {"weekday": weekday, "starts_at": "09:00", "ends_at": "12:00"}
        ],
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            config,
            revision=2,
        )
        slot = candidates_for_date(
            load_current_schedule(connection, clinic.tenant), day
        )[0]

    patient = new_patient(clinic)
    end = slot.ends_at.isoformat().replace("+00:00", "Z")
    before = booking_effects(clinic)
    for start in (
        f"{day}T09:00:00+02:60",
        f"{day}T09:00:00.0000009+03:00",
    ):
        status, denied = booking_request(
            clinic,
            clinic.owner,
            body=booking_body(
                patient,
                day=day,
                expected_schedule_revision=3,
                starts_at=start,
                ends_at=end,
            ),
            key=str(uuid4()),
        )
        assert status == 422, denied
        assert booking_effects(clinic) == before

    status, confirmed = booking_request(
        clinic,
        clinic.owner,
        body=booking_body(
            patient,
            day=day,
            expected_schedule_revision=3,
            starts_at=slot.starts_at.astimezone(ZoneInfo("Africa/Cairo")).isoformat(),
            ends_at=end,
        ),
        key=str(uuid4()),
    )
    assert status == 201, confirmed
    assert (
        datetime.fromisoformat(confirmed["data"]["starts_at"]).astimezone(UTC)
        == slot.starts_at
    )


@pytest.mark.parametrize("authority", ["disabled_staff", "suspended_clinic"])
def test_withdrawn_live_authority_denies_even_valid_booking(
    booking_clinic,
    authority,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    actor = clinic.receptionist if authority == "disabled_staff" else clinic.owner
    with clinic.migration.begin() as connection:
        if authority == "disabled_staff":
            connection.execute(
                text(
                    "UPDATE public.staff_accounts SET enabled=false, security_revision=security_revision+1 WHERE id=:id"
                ),
                {"id": UUID(clinic.receptionist.staff_id)},
            )
        else:
            connection.execute(
                text("UPDATE public.organizations SET state='SUSPENDED' WHERE id=:id"),
                {"id": clinic.tenant},
            )
    before = booking_effects(clinic)
    status, response = booking_request(
        clinic, actor, body=booking_body(patient), key=str(uuid4())
    )
    assert status in (401, 403), response
    assert booking_effects(clinic) == before


def test_receipt_survives_repricing_and_archive_but_not_changed_body_or_revocation(
    booking_clinic,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    body, key = booking_body(patient), str(uuid4())
    status, original = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201, original
    assert (
        pricing_request(
            clinic,
            clinic.owner,
            method="PATCH",
            key=str(uuid4()),
            body=_patch(expected_price_revision=2, standard_new_exam_egp="150.00"),
        )[0]
        == 200
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text(
                "UPDATE public.patients SET archived = true, lifecycle_revision = 1 WHERE id=:id"
            ),
            {"id": patient},
        )
    before = booking_effects(clinic)
    assert booking_request(clinic, clinic.owner, body=body, key=key) == (201, original)
    conflict, payload = booking_request(
        clinic, clinic.owner, body={**body, "expected_net_egp": "99.00"}, key=key
    )
    assert conflict == 409 and payload["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert booking_effects(clinic) == before
    with clinic.migration.begin() as connection:
        connection.execute(
            text('DELETE FROM public."session" WHERE id = :id'),
            {"id": clinic.owner.session_id},
        )
    assert booking_request(clinic, clinic.owner, body=body, key=key)[0] in (401, 403)
    assert booking_effects(clinic) == before


def test_lost_response_after_commit_reconciles_by_original_key(booking_clinic):
    clinic = booking_clinic
    patient = new_patient(clinic)
    body, key = booking_body(patient), str(uuid4())
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        book_appointment(connection, actor, BookAppointment.model_validate(body), key)
    before = booking_effects(clinic)
    status, resolved = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201 and resolved["data"]["number"] == 1
    assert booking_effects(clinic) == before
    assert booking_request(clinic, clinic.owner, body=body, key=str(uuid4()))[0] == 409
    assert booking_effects(clinic) == before


def test_paused_doctor_and_disabled_walkin_do_not_block_ordinary_booking(
    booking_clinic,
):
    clinic = booking_clinic
    with transaction(clinic.runtime, clinic.tenant) as connection:
        append_version(
            connection,
            clinic.tenant,
            "CAPACITY",
            {"capacity": 1, "paused": True},
            1,
        )
    patient = new_patient(clinic)
    status, response = booking_request(
        clinic, clinic.receptionist, body=booking_body(patient), key=str(uuid4())
    )
    assert status == 201, response
    assert response["data"]["status"] == "Booked"
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


BOOKING_WRITE_STAGES = (
    "appointment_day_counters",
    "appointments",
    "appointment_number_assignments",
    "appointment_price_snapshots",
    "appointment_history",
    "sensitive_audit",
    "outbox_events",
    "idempotency_receipts",
)


def assert_tenant_context_cleared(connection, backend_pid, *, terminated=False):
    current_pid, organization_id = connection.execute(
        text("SELECT pg_backend_pid(), current_setting('app.organization_id', true)")
    ).one()
    if terminated:
        assert current_pid != backend_pid
    else:
        assert current_pid == backend_pid
    assert organization_id in ("", None)


@pytest.mark.parametrize("blocked_table", BOOKING_WRITE_STAGES)
def test_postgres_lock_timeout_rolls_back_all_booking_writes_and_reuses_unassigned_number(
    booking_clinic,
    blocked_table,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    command = BookAppointment.model_validate(booking_body(patient))
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    key = str(uuid4())
    before = booking_effects(clinic)
    with clinic.migration.begin() as holder:
        holder.execute(text(f"LOCK TABLE public.{blocked_table} IN SHARE MODE"))
        with clinic.runtime.connect() as connection:
            with pytest.raises(DBAPIError) as blocked, connection.begin():
                connection.execute(
                    text("SELECT set_config('app.organization_id', :tenant, true)"),
                    {"tenant": str(clinic.tenant)},
                )
                backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
                connection.execute(text("SET LOCAL lock_timeout = '300ms'"))
                book_appointment(connection, actor, command, key)
            assert blocked.value.orig.sqlstate == "55P03"
            assert_tenant_context_cleared(connection, backend_pid)
    assert booking_effects(clinic) == before
    status, saved = booking_request(
        clinic, clinic.owner, body=booking_body(patient), key=key
    )
    assert status == 201, saved
    assert saved["data"]["number"] == 1
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("failed_table", BOOKING_WRITE_STAGES)
def test_real_sql_error_at_each_write_stage_rolls_back_every_booking_effect(
    booking_clinic, failed_table
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    command = BookAppointment.model_validate(booking_body(patient))
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    key = str(uuid4())
    expected_sql = f"INSERT INTO public.{failed_table} "
    reached = []
    before = booking_effects(clinic)

    with clinic.runtime.connect() as connection:
        with pytest.raises(DBAPIError) as failed, connection.begin():
            connection.execute(
                text("SELECT set_config('app.organization_id', :tenant, true)"),
                {"tenant": str(clinic.tenant)},
            )
            backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))

            @event.listens_for(connection, "before_cursor_execute", retval=True)
            def fail_sql_write(
                _connection, _cursor, statement, params, _context, _many
            ):
                if expected_sql in " ".join(statement.split()):
                    reached.append(failed_table)
                    return "SELECT 1 / 0", ()
                return statement, params

            book_appointment(connection, actor, command, key)

        assert reached == [failed_table]
        assert failed.value.orig.sqlstate == "22012"
        assert_tenant_context_cleared(connection, backend_pid)
    assert booking_effects(clinic) == before
    status, recovered = booking_request(
        clinic, clinic.owner, body=booking_body(patient), key=key
    )
    assert status == 201, recovered
    assert recovered["data"]["number"] == 1
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("failed_table", BOOKING_WRITE_STAGES)
def test_real_postgres_connection_loss_at_each_write_stage_rolls_back_receipt(
    booking_clinic, failed_table
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    command = BookAppointment.model_validate(booking_body(patient))
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    key = str(uuid4())
    terminated = []
    before = booking_effects(clinic)
    expected_sql = f"INSERT INTO public.{failed_table} "

    with (
        pytest.raises(DBAPIError) as failed,
        transaction(clinic.runtime, clinic.tenant) as connection,
    ):
        backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))

        @event.listens_for(connection, "before_cursor_execute")
        def terminate_before_write(
            _connection, _cursor, statement, _params, _context, _many
        ):
            if expected_sql in " ".join(statement.split()):
                with clinic.runtime.connect() as killer:
                    terminated.append(
                        killer.scalar(
                            text("SELECT pg_terminate_backend(:pid, 5000)"),
                            {"pid": backend_pid},
                        )
                    )

        book_appointment(connection, actor, command, key)

    assert terminated == [True]
    assert failed.value.connection_invalidated
    assert booking_effects(clinic) == before
    with clinic.runtime.connect() as new_connection:
        assert_tenant_context_cleared(new_connection, backend_pid, terminated=True)
    status, recovered = booking_request(
        clinic, clinic.owner, body=booking_body(patient), key=key
    )
    assert status == 201, recovered
    assert recovered["data"]["number"] == 1
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)
