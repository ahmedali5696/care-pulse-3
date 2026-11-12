"""B-07 schedule staging and publication on disposable CI PostgreSQL."""

import asyncio
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff, protected_transaction
from carepulse.identity.contracts import ContractError
from carepulse.identity.signing import canonical_payload
from carepulse.main import app
from carepulse.policies.schedule_contracts import ScheduleActivate, SchedulePatch
from carepulse.policies.schedule_service import (
    activate,
    read_schedule,
    stage_or_preview,
)
from sqlalchemy import text
from test_appointment_persistence import (
    _admit_saved,
    _draft,
    _patient,
    _query,
    _seed_waiting_entry,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import (
    _blocked_by,
    _fresh_receptionist_session,
    _manager_change,
    _owner_changes_manager,
)
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414

CAIRO = ZoneInfo("Africa/Cairo")
SERVICE_DAY = datetime.now(CAIRO).date() + timedelta(days=28)
WEEKDAY = SERVICE_DAY.isoweekday()
FOLD_DAY = datetime(2030, 10, 31).date()
FOLD_WEEKDAY = FOLD_DAY.isoweekday()


def _actor(tenant, staff, role="DOCTOR_OWNER"):
    return AuthorizedStaff(tenant, staff, role, False, 0, 0, 0)


def _config(*, mode="EXACT_TIME", **changes):
    fields = {
        "booking_mode": mode,
        "duration_minutes": "30" if mode == "EXACT_TIME" else None,
        "holiday_booking_enabled": False,
        "allow_overlap": False,
        "no_show_cutoff": {"enabled": False, "local_time": None},
        "working_weekdays": [WEEKDAY],
        "weekly_work_periods": [
            {"weekday": WEEKDAY, "starts_at": "09:00", "ends_at": "12:00"}
        ]
        if mode == "EXACT_TIME"
        else [],
        "weekly_breaks": [],
        "weekly_manual_slots": [],
        "open_override_dates": [],
        "override_work_periods": [],
        "manual_slots_by_date": {},
        "holidays": [],
    }
    return {**fields, **changes}


def _patch(config, revision=1, confirmation=None):
    return SchedulePatch.model_validate(
        {
            "expected_schedule_revision": revision,
            "reason": "Synthetic approved calendar review",
            "config": config,
            "overlap_capacity_confirmation": confirmation,
        }
    )


def _stage(
    connection,
    actor,
    config,
    revision=1,
    operation="settings.schedule.preview",
    confirmation=None,
    key=None,
):
    return stage_or_preview(
        connection,
        actor,
        _patch(config, revision, confirmation),
        key or str(uuid4()),
        operation,
    )


def _activate(connection, actor, preview, key=None):
    return activate(
        connection,
        actor,
        ScheduleActivate(
            preview_id=UUID(preview["preview_id"]),
            expected_schedule_revision=preview["revisions"]["schedule"],
            expected_capacity_revision=preview["revisions"]["capacity"],
            reason="Synthetic reviewed schedule publication",
        ),
        key or str(uuid4()),
    )


def _publish(connection, actor, config, revision=1):
    return _activate(connection, actor, _stage(connection, actor, config, revision))


def _booking(
    connection,
    tenant,
    doctor,
    *,
    status="Booked",
    timed=True,
    start=9,
    end=10,
    day=SERVICE_DAY,
):
    bounds = (
        {
            "starts_at": datetime.combine(day, time(start), CAIRO),
            "ends_at": datetime.combine(day, time(end), CAIRO),
        }
        if timed
        else {}
    )
    saved = persist_appointment(
        connection,
        _draft(
            _patient(connection, tenant),
            doctor,
            status="Booked" if status in ("In Examination", "Completed") else status,
            service_date=day,
            booking_mode="EXACT_TIME" if timed else "OPEN",
            **bounds,
        ),
        _query(tenant, doctor),
    )
    if status == "Waiting":
        _seed_waiting_entry(connection, tenant, doctor, saved, doctor)
    if status in ("In Examination", "Completed"):
        _admit_saved(connection, tenant, doctor, saved, complete=status == "Completed")
    return saved


def _timed_booking(connection, tenant, doctor, day, start, end, status="Booked"):
    saved = persist_appointment(
        connection,
        _draft(
            _patient(connection, tenant),
            doctor,
            status=status,
            service_date=day,
            booking_mode="EXACT_TIME",
            starts_at=start,
            ends_at=end,
        ),
        _query(tenant, doctor),
    )
    if status == "Waiting":
        _seed_waiting_entry(connection, tenant, doctor, saved, doctor)
    return saved


def _fold_config(**changes):
    return _config(
        **{
            "working_weekdays": [FOLD_WEEKDAY],
            "weekly_work_periods": [
                {
                    "weekday": FOLD_WEEKDAY,
                    "starts_at": "23:00",
                    "ends_at": "23:59",
                }
            ],
            **changes,
        }
    )


def _frozen(connection, appointment_id):
    return connection.execute(
        text("""
        SELECT a.status, a.booking_mode, a.service_date, a.starts_at, a.ends_at,
               a.revision, row_to_json(s)::text AS snapshot,
               (SELECT count(*) FROM public.appointment_history h
                WHERE h.appointment_id = a.id) AS history_count
        FROM public.appointments a
        JOIN public.appointment_price_snapshots s ON s.appointment_id = a.id
        WHERE a.id = :id
    """),
        {"id": appointment_id},
    ).one()


def _schedule_request(
    clinic,
    actor,
    *,
    method="GET",
    suffix="",
    body=None,
    key="",
    raw_response=False,
    raw_body=None,
):
    path = "/v1/settings/schedule" + suffix
    encoded = (
        raw_body
        if raw_body is not None
        else (
            json.dumps(body, separators=(",", ":")).encode()
            if body is not None
            else b""
        )
    )
    envelope = replace(
        actor,
        method=method,
        path=path,
        query="",
        body_sha256=sha256(encoded).hexdigest(),
        purpose="SETTINGS_SCHEDULE",
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    signed_fields = (
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
            str(getattr(envelope, field)).encode(),
        )
        for field in signed_fields
    ] + [
        (b"content-type", b"application/json"),
        (b"x-carepulse-service-key", clinic.config.service_key.encode()),
        (
            b"x-carepulse-signature",
            new(clinic.config.service_key.encode(), canonical_payload(envelope), sha256)
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
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("synthetic", 1),
        "server": ("api", 8000),
    }
    outgoing = []

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        outgoing.append(message)

    asyncio.run(app(scope, receive, send))
    payload = b"".join(message.get("body", b"") for message in outgoing[1:])
    return outgoing[0]["status"], payload if raw_response else json.loads(payload)


@pytest.mark.parametrize("operation", ["stage", "preview", "activate"])
@pytest.mark.parametrize("raw_body", [b"\xff", b'{"reason":' + b"9" * 4301 + b"}"])
def test_signed_schedule_malformed_json_denied_without_effects(
    patient_backend, operation, raw_body
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    suffix, method = {
        "stage": ("", "PATCH"),
        "preview": ("/preview", "POST"),
        "activate": ("/activate", "POST"),
    }[operation]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        before = connection.execute(
            text("""
            SELECT (SELECT revision FROM public.clinic_policy_heads
                    WHERE organization_id=:tenant AND kind='SCHEDULE'),
                   (SELECT count(*) FROM public.clinic_policy_previews
                    WHERE organization_id=:tenant),
                   (SELECT count(*) FROM public.sensitive_audit
                    WHERE organization_id=:tenant),
                   (SELECT count(*) FROM public.idempotency_receipts
                    WHERE organization_id=:tenant)
        """),
            {"tenant": clinic.tenant},
        ).one()
    status, denied = _schedule_request(
        clinic,
        clinic.owner,
        method=method,
        suffix=suffix,
        raw_body=raw_body,
        key=str(uuid4()),
    )
    assert status == 422 and denied["error"]["code"] == "INVALID_INPUT"
    with transaction(clinic.runtime, clinic.tenant) as connection:
        after = connection.execute(
            text("""
            SELECT (SELECT revision FROM public.clinic_policy_heads
                    WHERE organization_id=:tenant AND kind='SCHEDULE'),
                   (SELECT count(*) FROM public.clinic_policy_previews
                    WHERE organization_id=:tenant),
                   (SELECT count(*) FROM public.sensitive_audit
                    WHERE organization_id=:tenant),
                   (SELECT count(*) FROM public.idempotency_receipts
                    WHERE organization_id=:tenant)
        """),
            {"tenant": clinic.tenant},
        ).one()
    assert after == before


@pytest.mark.parametrize("operation", ["stage", "preview", "activate"])
def test_signed_schedule_surrogate_reason_denies_without_receipt(
    patient_backend, operation
):
    clinic = patient_backend
    if operation == "activate":
        body = {
            "preview_id": str(uuid4()),
            "expected_schedule_revision": 1,
            "expected_capacity_revision": 1,
            "reason": "Approved activation",
        }
        suffix, method = "/activate", "POST"
    else:
        body = _patch(_config(mode="OPEN")).model_dump(mode="json")
        suffix, method = ("", "PATCH") if operation == "stage" else ("/preview", "POST")

    with transaction(clinic.runtime, clinic.tenant) as connection:
        before = connection.execute(
            text("""
            SELECT (SELECT revision FROM public.clinic_policy_heads
                    WHERE organization_id=:tenant AND kind='SCHEDULE'),
                   (SELECT count(*) FROM public.clinic_policy_previews
                    WHERE organization_id=:tenant),
                   (SELECT count(*) FROM public.sensitive_audit
                    WHERE organization_id=:tenant),
                   (SELECT count(*) FROM public.idempotency_receipts
                    WHERE organization_id=:tenant)
        """),
            {"tenant": clinic.tenant},
        ).one()
    key = str(uuid4())
    for reason in ("\ud800", "\udfff"):
        status, denied = _schedule_request(
            clinic,
            clinic.owner,
            method=method,
            suffix=suffix,
            body={**body, "reason": reason},
            key=key,
        )
        assert status == 422 and denied["error"]["code"] == "INVALID_INPUT"
        with transaction(clinic.runtime, clinic.tenant) as connection:
            assert (
                connection.execute(
                    text("""
                SELECT (SELECT revision FROM public.clinic_policy_heads
                        WHERE organization_id=:tenant AND kind='SCHEDULE'),
                       (SELECT count(*) FROM public.clinic_policy_previews
                        WHERE organization_id=:tenant),
                       (SELECT count(*) FROM public.sensitive_audit
                        WHERE organization_id=:tenant),
                       (SELECT count(*) FROM public.idempotency_receipts
                        WHERE organization_id=:tenant)
            """),
                    {"tenant": clinic.tenant},
                ).one()
                == before
            )

    if operation != "activate":
        accepted = {**body, "reason": "مراجعة الجدول"}
        status, result = _schedule_request(
            clinic,
            clinic.owner,
            method=method,
            suffix=suffix,
            body=accepted,
            key=key,
        )
        assert status == 200, result
        assert _schedule_request(
            clinic,
            clinic.owner,
            method=method,
            suffix=suffix,
            body=accepted,
            key=key,
        ) == (status, result)


def test_owner_patch_and_preview_stage_without_publication_then_activate(
    patient_backend,
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    status, initial = _schedule_request(clinic, clinic.owner)
    assert status == 200 and initial["data"]["schedule_revision"] == 1
    proposed = _config(
        weekly_manual_slots=[
            {"weekday": WEEKDAY, "starts_at": "09:00", "ends_at": "09:30"}
        ],
        manual_slots_by_date={SERVICE_DAY.isoformat(): []},
        no_show_cutoff={"enabled": True, "local_time": "17:00"},
    )
    body = _patch(proposed).model_dump(mode="json")
    key = str(uuid4())
    status, staged = _schedule_request(
        clinic, clinic.owner, method="PATCH", body=body, key=key
    )
    assert status == 200 and staged["data"]["revisions"]["schedule"] == 1
    assert _schedule_request(
        clinic, clinic.owner, method="PATCH", body=body, key=key
    ) == (status, staged)
    changed = {**body, "reason": "Different content with same key"}
    conflict_status, conflict = _schedule_request(
        clinic, clinic.owner, method="PATCH", body=changed, key=key
    )
    assert (
        conflict_status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert _schedule_request(clinic, clinic.owner)[1]["data"]["schedule_revision"] == 1
    status, preview = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200 and preview["data"]["affected"] == []
    assert preview["data"]["preview_id"] != staged["data"]["preview_id"]
    activation = {
        "preview_id": preview["data"]["preview_id"],
        "expected_schedule_revision": 1,
        "expected_capacity_revision": 1,
        "reason": "Synthetic authorized activation",
    }
    status, published = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/activate",
        body=activation,
        key=str(uuid4()),
    )
    assert status == 200 and published["data"]["schedule_revision"] == 2
    assert published["data"]["config"] == proposed
    assert published["data"]["routine_walk_in_enabled"] is False
    assert _schedule_request(clinic, clinic.owner)[1]["data"]["config"] == proposed
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_schedule_intervals
            WHERE interval_kind = 'MANUAL_SLOT' AND weekday = :weekday
        """),
                {"weekday": WEEKDAY},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_schedule_intervals
            WHERE interval_kind = 'MANUAL_SLOT' AND service_date = :day
        """),
                {"day": SERVICE_DAY},
            )
            == 0
        )
        assert read_schedule(connection, _actor(clinic.tenant, clinic.staff))["config"][
            "manual_slots_by_date"
        ] == {
            SERVICE_DAY.isoformat(): [],
        }
    with clinic.migration.begin() as connection:
        connection.execute(
            text('DELETE FROM public."session" WHERE id = :session'),
            {"session": clinic.owner.session_id},
        )
    assert _schedule_request(clinic, clinic.owner, method="PATCH", body=body, key=key)[
        0
    ] in (401, 403)
    assert _schedule_request(clinic, clinic.owner)[0] in (401, 403)


@pytest.mark.parametrize(
    "duration",
    ["9007199254740993", "9" * 4301],
    ids=["js_precision", "over_4300_digits"],
)
def test_duration_string_survives_signed_http_activation_and_preserves_history(
    patient_backend, duration
):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    status, initial = _schedule_request(clinic, clinic.owner)
    assert status == 200
    assert initial["data"]["config"]["duration_minutes"] is None

    candidate = _config(duration_minutes=duration)
    body = {
        "expected_schedule_revision": initial["data"]["schedule_revision"],
        "reason": "Synthetic duration precision regression",
        "config": candidate,
        "overlap_capacity_confirmation": None,
    }
    status, staged = _schedule_request(
        clinic, clinic.owner, method="PATCH", body=body, key=str(uuid4())
    )
    assert status == 200 and staged["data"]["revisions"]["schedule"] == 1
    assert (
        _schedule_request(clinic, clinic.owner)[1]["data"]["config"]["duration_minutes"]
        is None
    )

    status, preview = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200 and preview["data"]["affected"] == []
    status, published = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/activate",
        body={
            "preview_id": preview["data"]["preview_id"],
            "expected_schedule_revision": 1,
            "expected_capacity_revision": 1,
            "reason": "Synthetic duration activation",
        },
        key=str(uuid4()),
    )
    assert status == 200 and published["data"]["schedule_revision"] == 2
    assert published["data"]["config"]["duration_minutes"] == duration

    status, raw = _schedule_request(clinic, clinic.owner, raw_response=True)
    assert status == 200
    assert json.loads(raw)["data"]["config"]["duration_minutes"] == duration
    node = shutil.which("node")
    assert node is not None, "Pinned Node runtime required for JSON round-trip"
    javascript = subprocess.run(
        [
            node,
            "-e",
            (
                "const x=JSON.parse(require('node:fs').readFileSync(0,'utf8')); "
                "process.stdout.write(JSON.stringify(x.data.config))"
            ),
        ],
        input=raw,
        capture_output=True,
        check=True,
    )
    javascript_config = json.loads(javascript.stdout)
    assert javascript_config["duration_minutes"] == duration

    # A later unrelated edit must survive the browser's JSON parse/stringify path.
    later_config = {
        **javascript_config,
        "no_show_cutoff": {"enabled": True, "local_time": "17:00"},
    }
    later_body = {
        **body,
        "expected_schedule_revision": 2,
        "reason": "Synthetic later cutoff edit",
        "config": later_config,
    }
    status, stage_again = _schedule_request(
        clinic, clinic.owner, method="PATCH", body=later_body, key=str(uuid4())
    )
    assert status == 200 and stage_again["data"]["revisions"]["schedule"] == 2
    status, preview_again = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=later_body,
        key=str(uuid4()),
    )
    assert status == 200 and preview_again["data"]["affected"] == []
    status, published_again = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/activate",
        body={
            "preview_id": preview_again["data"]["preview_id"],
            "expected_schedule_revision": 2,
            "expected_capacity_revision": 1,
            "reason": "Synthetic later cutoff activation",
        },
        key=str(uuid4()),
    )
    assert status == 200 and published_again["data"]["schedule_revision"] == 3
    assert published_again["data"]["config"] == later_config
    assert _schedule_request(clinic, clinic.owner)[1]["data"]["config"] == later_config

    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.execute(
            text("""
            SELECT version, jsonb_typeof(payload->'duration_minutes'),
                   payload->>'duration_minutes'
            FROM public.clinic_policy_versions
            WHERE kind='SCHEDULE' ORDER BY version
        """)
        ).all() == [
            (1, "null", None),
            (2, "number", duration),
            (3, "number", duration),
        ]


@pytest.mark.parametrize("numeric", [30, 9007199254740993, 30.0])
def test_schedule_patch_rejects_json_numeric_duration(patient_backend, numeric):
    clinic = patient_backend
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    body = {
        "expected_schedule_revision": 1,
        "reason": "Synthetic numeric duration rejection",
        "config": _config(duration_minutes=numeric),
        "overlap_capacity_confirmation": None,
    }
    status, _ = _schedule_request(
        clinic, clinic.owner, method="PATCH", body=body, key=str(uuid4())
    )
    assert status == 422
    assert _schedule_request(clinic, clinic.owner)[1]["data"]["schedule_revision"] == 1


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", ""),
        ("PATCH", ""),
        ("POST", "/preview"),
        ("POST", "/activate"),
    ],
)
@pytest.mark.parametrize("actor_name", ["receptionist", "operator"])
def test_schedule_routes_deny_ungranted_roles(
    patient_backend, method, suffix, actor_name
):
    clinic = patient_backend
    actor = (
        clinic.receptionist
        if actor_name == "receptionist"
        else replace(clinic.owner, role="OPERATOR")
    )
    body = (
        _patch(_config()).model_dump(mode="json")
        if suffix != "/activate"
        else {
            "preview_id": str(uuid4()),
            "expected_schedule_revision": 1,
            "expected_capacity_revision": 1,
            "reason": "Synthetic denial",
        }
    )
    status, _ = _schedule_request(
        clinic,
        actor,
        method=method,
        suffix=suffix,
        body=None if method == "GET" else body,
        key=str(uuid4()),
    )
    assert status in (401, 403)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_policy_previews WHERE policy_kind='SCHEDULE'
        """)
            )
            == 0
        )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"working_weekdays": [], "weekly_work_periods": []}, "DATE_CLOSED"),
        (
            {
                "weekly_work_periods": [
                    {"weekday": WEEKDAY, "starts_at": "11:00", "ends_at": "15:00"}
                ]
            },
            "OUTSIDE_PERIOD",
        ),
        (
            {
                "weekly_breaks": [
                    {"weekday": WEEKDAY, "starts_at": "09:30", "ends_at": "10:30"}
                ]
            },
            "IN_BREAK",
        ),
    ],
)
def test_waiting_only_date_or_work_period_or_break_cannot_activate(
    appointment_storage, change, code
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config())
        waiting = _booking(connection, tenant, doctor, status="Waiting")
        frozen = _frozen(connection, waiting["id"])
        candidate = _config(**change)
        preview = _stage(connection, owner, candidate, revision=2)
        assert {entry["appointment_id"] for entry in preview["affected"]} == {
            waiting["id"]
        }
        assert preview["affected"][0]["blocking"] is True
        assert code in preview["affected"][0]["effects"]
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            _activate(connection, owner, preview)
        assert denied.value.status_code == 409
        assert read_schedule(connection, owner)["schedule_revision"] == 2
        assert _frozen(connection, waiting["id"]) == frozen


@pytest.mark.parametrize("status", ["Booked", "Waiting"])
@pytest.mark.parametrize(
    ("change", "conflict"),
    [
        (
            {
                "weekly_breaks": [
                    {
                        "weekday": FOLD_WEEKDAY,
                        "starts_at": "23:20",
                        "ends_at": "23:40",
                    }
                ]
            },
            "IN_BREAK",
        ),
        (
            {
                "weekly_work_periods": [
                    {
                        "weekday": FOLD_WEEKDAY,
                        "starts_at": "23:45",
                        "ends_at": "23:59",
                    }
                ]
            },
            "OUTSIDE_PERIOD",
        ),
    ],
)
def test_fall_fold_full_interval_conflicts_block_confirmed_booking(
    appointment_storage, status, change, conflict
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    start = datetime.combine(FOLD_DAY, time(23, 45), CAIRO)
    end = datetime.combine(FOLD_DAY, time(23, 55), CAIRO).replace(fold=1)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=70)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _fold_config())
        booked = _timed_booking(
            connection, tenant, doctor, FOLD_DAY, start, end, status
        )
        frozen = _frozen(connection, booked["id"])
        preview = _stage(connection, owner, _fold_config(**change), revision=2)
        assert preview["affected"] == [
            {
                "appointment_id": booked["id"],
                "service_date": FOLD_DAY.isoformat(),
                "effects": sorted({conflict, "OUTSIDE_PERIOD", "SLOTS_CHANGED"}),
                "blocking": True,
            }
        ]
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            _activate(connection, owner, preview)
        assert denied.value.status_code == 409
        assert read_schedule(connection, owner)["schedule_revision"] == 2
        assert _frozen(connection, booked["id"]) == frozen


def test_fall_fold_preserves_half_open_endpoint(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _fold_config())
        safe = _timed_booking(
            connection,
            tenant,
            doctor,
            FOLD_DAY,
            datetime.combine(FOLD_DAY, time(23), CAIRO),
            datetime.combine(FOLD_DAY, time(23, 20), CAIRO),
        )
        before = _frozen(connection, safe["id"])
        break_config = _fold_config(
            weekly_breaks=[
                {"weekday": FOLD_WEEKDAY, "starts_at": "23:20", "ends_at": "23:40"}
            ]
        )
        preview = _stage(connection, owner, break_config, revision=2)
        assert preview["affected"] == [
            {
                "appointment_id": safe["id"],
                "service_date": FOLD_DAY.isoformat(),
                "effects": ["SLOTS_CHANGED"],
                "blocking": False,
            }
        ]
        assert _activate(connection, owner, preview)["schedule_revision"] == 3
        assert _frozen(connection, safe["id"]) == before


def test_fall_fold_grid_change_does_not_require_new_candidate_membership(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _fold_config())
        crossing = _timed_booking(
            connection,
            tenant,
            doctor,
            FOLD_DAY,
            datetime.combine(FOLD_DAY, time(23, 35), CAIRO).replace(fold=1),
            datetime.combine(FOLD_DAY, time(23, 45), CAIRO).replace(fold=1),
        )
        crossing_before = _frozen(connection, crossing["id"])
        grid_preview = _stage(
            connection, owner, _fold_config(duration_minutes="15"), revision=2
        )
        assert grid_preview["affected"] == [
            {
                "appointment_id": crossing["id"],
                "service_date": FOLD_DAY.isoformat(),
                "effects": ["SLOTS_CHANGED"],
                "blocking": False,
            },
        ]
        assert _activate(connection, owner, grid_preview)["schedule_revision"] == 3
        assert _frozen(connection, crossing["id"]) == crossing_before


def test_fall_fold_new_conflict_invalidates_pending_activation(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _fold_config())
        safe = _timed_booking(
            connection,
            tenant,
            doctor,
            FOLD_DAY,
            datetime.combine(FOLD_DAY, time(23), CAIRO),
            datetime.combine(FOLD_DAY, time(23, 20), CAIRO),
        )
        preview = _stage(
            connection,
            owner,
            _fold_config(
                weekly_breaks=[
                    {"weekday": FOLD_WEEKDAY, "starts_at": "23:20", "ends_at": "23:40"}
                ]
            ),
            revision=2,
        )
        assert preview["affected"][0]["blocking"] is False
        crossed = _timed_booking(
            connection,
            tenant,
            doctor,
            FOLD_DAY,
            datetime.combine(FOLD_DAY, time(23, 45), CAIRO),
            datetime.combine(FOLD_DAY, time(23, 55), CAIRO).replace(fold=1),
            status="Waiting",
        )
        frozen = [_frozen(connection, row["id"]) for row in (safe, crossed)]
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            _activate(connection, owner, preview)
        assert denied.value.status_code == 409
        assert denied.value.code == "STALE_REVISION"
        assert read_schedule(connection, owner)["schedule_revision"] == 2
        assert [_frozen(connection, row["id"]) for row in (safe, crossed)] == frozen


def test_counted_waiting_examination_completed_peak_blocks_unchanged_overlap_limit(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config())
        appointments = [
            _booking(connection, tenant, doctor, status=status)
            for status in ("Waiting", "In Examination", "Completed")
        ]
        frozen = [_frozen(connection, row["id"]) for row in appointments]
        preview = _stage(connection, owner, _config(duration_minutes="15"), revision=2)
        affected = {row["appointment_id"]: row for row in preview["affected"]}
        assert set(affected) == {row["id"] for row in appointments}
        assert all(
            "OVERLAP_LIMIT_EXCEEDED" in row["effects"] and row["blocking"]
            for row in affected.values()
        )
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            _activate(connection, owner, preview)
        assert denied.value.status_code == 409
        assert read_schedule(connection, owner)["schedule_revision"] == 2
        assert [_frozen(connection, row["id"]) for row in appointments] == frozen


def test_disjoint_over_limit_peaks_capture_every_contributor_and_block_activation(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config())
        bookings = [
            _booking(connection, tenant, doctor, status=status, start=start, end=end)
            for status, start, end in (
                ("Booked", 9, 10),
                ("Waiting", 9, 10),
                ("In Examination", 9, 10),
                ("Completed", 11, 12),
                ("Waiting", 11, 12),
            )
        ]
        expected_revisions = {
            str(row.id): row.revision
            for row in connection.execute(
                text("SELECT id, revision FROM public.appointments")
            )
        }
        preview = _stage(connection, owner, _config(duration_minutes="15"), revision=2)
        affected = {entry["appointment_id"]: entry for entry in preview["affected"]}
        assert set(affected) == {booking["id"] for booking in bookings}
        assert all(
            entry["blocking"] and "OVERLAP_LIMIT_EXCEEDED" in entry["effects"]
            for entry in affected.values()
        )
        stored = connection.execute(
            text("""
                SELECT affected_refs, record_revisions
                FROM public.clinic_policy_previews WHERE id = :preview
            """),
            {"preview": preview["preview_id"]},
        ).one()
        assert stored.record_revisions == expected_revisions
        assert {entry["appointment_id"] for entry in stored.affected_refs} == set(
            affected
        )
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            _activate(connection, owner, preview)
        assert denied.value.status_code == 409
        assert read_schedule(connection, owner)["schedule_revision"] == 2


def test_disabling_overlap_blocks_existing_overlapping_booked_rows(appointment_storage):
    from carepulse.policies.versions import append_version

    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        append_version(
            connection, tenant, "CAPACITY", {"capacity": 2, "paused": False}, 1
        )
        overlap_preview = _stage(
            connection,
            owner,
            _config(allow_overlap=True),
            confirmation={"capacity": 2, "capacity_revision": 2},
        )
        assert _activate(connection, owner, overlap_preview)["schedule_revision"] == 2
        booked = [_booking(connection, tenant, doctor) for _ in range(2)]
        before = [_frozen(connection, row["id"]) for row in booked]
        preview = _stage(connection, owner, _config(), revision=2)
        assert {entry["appointment_id"] for entry in preview["affected"]} == {
            row["id"] for row in booked
        }
        assert all(
            "OVERLAP_LIMIT_EXCEEDED" in entry["effects"]
            for entry in preview["affected"]
        )
        with pytest.raises(ContractError) as denied, connection.begin_nested():
            _activate(connection, owner, preview)
        assert denied.value.status_code == 409
        assert read_schedule(connection, owner)["config"]["allow_overlap"] is True
        assert [_frozen(connection, row["id"]) for row in booked] == before


def test_integral_json_capacity_decimal_keeps_saved_policy_and_overlap(
    appointment_storage,
):
    from carepulse.policies.versions import append_version

    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        append_version(
            connection, tenant, "CAPACITY", {"capacity": 2.0, "paused": False}, 1
        )
        raw_before = connection.scalar(
            text("""
            SELECT payload::text FROM public.clinic_policy_versions
            WHERE organization_id=:tenant AND kind='CAPACITY' AND version=2
        """),
            {"tenant": tenant},
        )
        preview = _stage(
            connection,
            owner,
            _config(allow_overlap=True),
            confirmation={"capacity": 2, "capacity_revision": 2},
        )
        assert _activate(connection, owner, preview)["schedule_revision"] == 2
        assert read_schedule(connection, owner)["capacity"] == 2
        assert (
            connection.scalar(
                text("""
            SELECT payload::text FROM public.clinic_policy_versions
            WHERE organization_id=:tenant AND kind='CAPACITY' AND version=2
        """),
                {"tenant": tenant},
            )
            == raw_before
        )


def test_mode_duration_and_manual_slot_changes_are_review_only_for_confirmed_time(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config())
        booked = _booking(connection, tenant, doctor)
        before = _frozen(connection, booked["id"])
        candidate = _config(
            mode="OPEN",
            duration_minutes=None,
            weekly_work_periods=[
                {"weekday": WEEKDAY, "starts_at": "09:00", "ends_at": "12:00"}
            ],
            weekly_manual_slots=[
                {"weekday": WEEKDAY, "starts_at": "10:30", "ends_at": "11:00"}
            ],
            no_show_cutoff={"enabled": True, "local_time": "17:30"},
        )
        preview = _stage(connection, owner, candidate, revision=2)
        assert preview["affected"] == [
            {
                "appointment_id": booked["id"],
                "service_date": SERVICE_DAY.isoformat(),
                "effects": ["CUTOFF_EXPOSURE", "MODE_CHANGED", "SLOTS_CHANGED"],
                "blocking": False,
            }
        ]
        assert _activate(connection, owner, preview)["schedule_revision"] == 3
        assert _frozen(connection, booked["id"]) == before
        assert read_schedule(connection, owner)["config"] == candidate


@pytest.mark.parametrize(
    ("active", "candidate", "timed", "status", "effects"),
    [
        (_config(), _config(duration_minutes="240"), True, "Booked", ["SLOTS_CHANGED"]),
        (
            _config(
                duration_minutes=None,
                weekly_manual_slots=[
                    {"weekday": WEEKDAY, "starts_at": "09:00", "ends_at": "10:00"}
                ],
            ),
            _config(duration_minutes=None),
            True,
            "Waiting",
            ["SLOTS_CHANGED"],
        ),
        (
            _config(mode="OPEN"),
            _config(duration_minutes="240"),
            False,
            "Waiting",
            ["MODE_CHANGED"],
        ),
    ],
)
def test_existing_confirmed_bookings_survive_disappearing_new_slot_candidates(
    appointment_storage, active, candidate, timed, status, effects
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, active)
        booked = _booking(connection, tenant, doctor, timed=timed, status=status)
        frozen = _frozen(connection, booked["id"])
        preview = _stage(connection, owner, candidate, revision=2)
        assert preview["affected"] == [
            {
                "appointment_id": booked["id"],
                "service_date": SERVICE_DAY.isoformat(),
                "effects": effects,
                "blocking": False,
            }
        ]
        assert _activate(connection, owner, preview)["schedule_revision"] == 3
        assert _frozen(connection, booked["id"]) == frozen


def test_open_booking_survives_exact_time_holiday_without_new_timed_hours(
    appointment_storage,
):
    from carepulse.policies.schedule import date_rules
    from carepulse.policies.schedule_service import load_current_schedule

    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config(mode="OPEN"))
        booked = _booking(connection, tenant, doctor, timed=False)
        frozen = _frozen(connection, booked["id"])
        candidate = _config(
            holiday_booking_enabled=True, holidays=[SERVICE_DAY.isoformat()]
        )
        preview = _stage(connection, owner, candidate, revision=2)
        assert preview["affected"] == [
            {
                "appointment_id": booked["id"],
                "service_date": SERVICE_DAY.isoformat(),
                "effects": ["MODE_CHANGED"],
                "blocking": False,
            }
        ]
        assert _activate(connection, owner, preview)["schedule_revision"] == 3
        assert not date_rules(
            load_current_schedule(connection, tenant), SERVICE_DAY
        ).eligible
        assert _frozen(connection, booked["id"]) == frozen


def test_weekly_manual_slots_inherit_except_explicit_date_replacements(
    appointment_storage,
):
    from carepulse.policies.schedule import effective_manual_slots
    from carepulse.policies.schedule_service import load_current_schedule

    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    empty_date = SERVICE_DAY.isoformat()
    replaced_date = (SERVICE_DAY + timedelta(days=7)).isoformat()
    inherited_day = SERVICE_DAY + timedelta(days=14)
    weekly = {"weekday": WEEKDAY, "starts_at": "09:00", "ends_at": "09:30"}
    replacement = {"starts_at": "10:00", "ends_at": "10:30"}
    candidate = _config(
        weekly_manual_slots=[weekly],
        manual_slots_by_date={empty_date: [], replaced_date: [replacement]},
    )
    with transaction(runtime, tenant) as connection:
        assert _publish(connection, owner, candidate)["schedule_revision"] == 2
        assert read_schedule(connection, owner)["config"] == candidate
        current = load_current_schedule(connection, tenant)
        assert effective_manual_slots(current, SERVICE_DAY) == ()
        assert [
            (slot.starts_at, slot.ends_at)
            for slot in effective_manual_slots(current, inherited_day)
        ] == [(time(9), time(9, 30))]
        assert [
            (slot.starts_at, slot.ends_at)
            for slot in effective_manual_slots(current, SERVICE_DAY + timedelta(days=7))
        ] == [
            (time(10), time(10, 30)),
        ]
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_schedule_intervals
            WHERE policy_version=2 AND interval_kind='MANUAL_SLOT' AND weekday = :weekday
        """),
                {"weekday": WEEKDAY},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.clinic_schedule_intervals
            WHERE policy_version=2 AND interval_kind='MANUAL_SLOT' AND service_date IS NOT NULL
        """)
            )
            == 1
        )
        payload = connection.scalar(
            text("""
            SELECT payload FROM public.clinic_policy_versions
            WHERE kind='SCHEDULE' AND version=2
        """)
        )
        assert payload["manual_slot_dates"] == [empty_date, replaced_date]


@pytest.mark.parametrize(
    "change",
    [
        {"working_weekdays": [], "weekly_work_periods": []},
        {
            "weekly_work_periods": [
                {"weekday": WEEKDAY, "starts_at": "11:00", "ends_at": "15:00"}
            ]
        },
        {
            "weekly_breaks": [
                {"weekday": WEEKDAY, "starts_at": "09:30", "ends_at": "10:30"}
            ]
        },
    ],
)
def test_examination_and_completed_only_are_preserved_when_peak_is_valid(
    appointment_storage, change
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config())
        first = _booking(connection, tenant, doctor, status="In Examination")
        second = _booking(
            connection, tenant, doctor, status="Completed", start=10, end=11
        )
        frozen = {row["id"]: _frozen(connection, row["id"]) for row in (first, second)}
        candidate = _config(**change)
        preview = _stage(connection, owner, candidate, revision=2)
        assert not any(row["blocking"] for row in preview["affected"])
        assert _activate(connection, owner, preview)["schedule_revision"] == 3
        assert read_schedule(connection, owner)["config"] == candidate
        assert {
            row["id"]: _frozen(connection, row["id"]) for row in (first, second)
        } == frozen


def test_cutoff_exposure_is_booked_only_and_does_not_mutate_frozen_open_or_timed(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        _publish(connection, owner, _config(mode="OPEN"))
        booked = _booking(connection, tenant, doctor, timed=False)
        waiting = _booking(connection, tenant, doctor, timed=False, status="Waiting")
        frozen = {
            row["id"]: _frozen(connection, row["id"]) for row in (booked, waiting)
        }
        preview = _stage(
            connection,
            owner,
            _config(
                mode="OPEN", no_show_cutoff={"enabled": True, "local_time": "16:30"}
            ),
            revision=2,
        )
        affected = {row["appointment_id"]: row for row in preview["affected"]}
        assert set(affected) == {booked["id"], waiting["id"]}
        assert affected[booked["id"]]["effects"] == ["CUTOFF_EXPOSURE"]
        assert affected[waiting["id"]]["effects"] == []
        assert all(not row["blocking"] for row in affected.values())
        assert _activate(connection, owner, preview)["schedule_revision"] == 3
        assert {
            row["id"]: _frozen(connection, row["id"]) for row in (booked, waiting)
        } == frozen


@pytest.mark.parametrize(
    ("mode", "enabled", "overrides", "eligible"),
    [
        ("OPEN", False, [], False),
        ("OPEN", True, [], True),
        ("EXACT_TIME", False, [], False),
        ("EXACT_TIME", True, [], False),
        (
            "EXACT_TIME",
            True,
            [
                {
                    "service_date": SERVICE_DAY.isoformat(),
                    "starts_at": "09:00",
                    "ends_at": "12:00",
                }
            ],
            True,
        ),
    ],
)
def test_holiday_toggle_respects_open_and_timed_override_rules(
    appointment_storage, mode, enabled, overrides, eligible
):
    from carepulse.policies.schedule import date_rules
    from carepulse.policies.schedule_service import load_current_schedule

    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    owner = _actor(tenant, doctor)
    with transaction(runtime, tenant) as connection:
        candidate = _config(
            mode=mode,
            holiday_booking_enabled=enabled,
            holidays=[SERVICE_DAY.isoformat()],
            override_work_periods=overrides,
        )
        _publish(connection, owner, candidate)
        active = load_current_schedule(connection, tenant)
        assert date_rules(active, SERVICE_DAY).eligible is eligible


def test_preview_scoped_to_original_tenant_and_stale_after_schedule_change(
    appointment_storage,
):
    runtime, _, (tenant_a, tenant_b), (doctor_a, doctor_b) = appointment_storage
    owner_a, owner_b = _actor(tenant_a, doctor_a), _actor(tenant_b, doctor_b)
    with transaction(runtime, tenant_a) as connection:
        preview = _stage(connection, owner_a, _config())
        _publish(connection, owner_a, _config(mode="OPEN"))
        with pytest.raises(ContractError) as stale, connection.begin_nested():
            _activate(connection, owner_a, preview)
        assert stale.value.status_code == 409
    with transaction(runtime, tenant_b) as connection:
        with pytest.raises(ContractError) as missing, connection.begin_nested():
            _activate(connection, owner_b, preview)
        assert missing.value.status_code == 404
        assert read_schedule(connection, owner_b)["schedule_revision"] == 1


def test_owner_preview_captures_revision_and_can_activate(
    patient_backend,
):
    clinic = patient_backend
    body = _patch(_config(mode="OPEN")).model_dump(mode="json")
    status, prepared = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200, prepared
    preview_id = UUID(prepared["data"]["preview_id"])
    with transaction(clinic.runtime, clinic.tenant) as connection:
        captured = connection.execute(
            text("""
                SELECT actor_staff_id, actor_security_revision
                FROM public.clinic_policy_previews
                WHERE organization_id = :tenant AND id = :preview
            """),
            {"tenant": clinic.tenant, "preview": preview_id},
        ).one()
        assert captured == (UUID(clinic.owner.staff_id), 0)

    activation = {
        "preview_id": str(preview_id),
        "expected_schedule_revision": 1,
        "expected_capacity_revision": 1,
        "reason": "Synthetic captured-revision preview activation",
    }
    status, published = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/activate",
        body=activation,
        key=str(uuid4()),
    )
    assert status == 200 and published["data"]["schedule_revision"] == 2


def test_signed_manager_schedule_publication_audits_actual_actor(patient_backend):
    clinic = patient_backend
    body = _patch(_config(mode="OPEN")).model_dump(mode="json")
    status, owner_preview = _schedule_request(
        clinic,
        clinic.owner,
        method="POST",
        suffix="/preview",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200, owner_preview
    owner_activation = {
        "preview_id": owner_preview["data"]["preview_id"],
        "expected_schedule_revision": 1,
        "expected_capacity_revision": 1,
        "reason": "Synthetic owner preview",
    }
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    status, initial = _schedule_request(clinic, manager)
    assert status == 200 and initial["data"]["schedule_revision"] == 1
    assert initial["data"]["capacity_revision"] == 1
    assert _schedule_request(clinic, replace(manager, role="DOCTOR_OWNER"))[0] == 403
    assert (
        _schedule_request(clinic, replace(manager, organization_id=str(uuid4())))[0]
        == 403
    )
    assert (
        _schedule_request(
            clinic,
            manager,
            method="POST",
            suffix="/activate",
            body=owner_activation,
            key=str(uuid4()),
        )[0]
        == 404
    )

    stage_key = str(uuid4())
    status, staged = _schedule_request(
        clinic, manager, method="PATCH", body=body, key=stage_key
    )
    assert status == 200 and staged["data"]["revisions"]["schedule"] == 1
    assert _schedule_request(
        clinic, manager, method="PATCH", body=body, key=stage_key
    ) == (status, staged)
    preview_key = str(uuid4())
    status, preview = _schedule_request(
        clinic,
        manager,
        method="POST",
        suffix="/preview",
        body=body,
        key=preview_key,
    )
    assert (
        status == 200 and preview["data"]["preview_id"] != staged["data"]["preview_id"]
    )
    assert _schedule_request(clinic, manager)[1]["data"]["schedule_revision"] == 1
    activation = {
        "preview_id": preview["data"]["preview_id"],
        "expected_schedule_revision": 1,
        "expected_capacity_revision": 1,
        "reason": "Synthetic Manager schedule publication",
    }
    assert (
        _schedule_request(
            clinic,
            clinic.owner,
            method="POST",
            suffix="/activate",
            body=activation,
            key=str(uuid4()),
        )[0]
        == 404
    )
    activation_key = str(uuid4())
    status, published = _schedule_request(
        clinic,
        manager,
        method="POST",
        suffix="/activate",
        body=activation,
        key=activation_key,
    )
    assert status == 200 and published["data"]["schedule_revision"] == 2
    assert _schedule_request(
        clinic,
        manager,
        method="POST",
        suffix="/activate",
        body=activation,
        key=activation_key,
    ) == (status, published)
    with clinic.migration.connect() as connection:
        rows = connection.execute(
            text("""
                SELECT actor_staff_id, actor_role FROM public.sensitive_audit
                WHERE organization_id=:tenant AND action='settings.schedule.activate'
            """),
            {"tenant": clinic.tenant},
        ).all()
        assert rows == [(UUID(manager.staff_id), "RECEPTIONIST")]
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.idempotency_receipts
                WHERE organization_id=:tenant AND actor_id=:staff
                  AND operation LIKE 'settings.schedule.%'
            """),
                {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
            )
            == 3
        )
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.clinic_policy_previews
                WHERE organization_id=:tenant AND actor_staff_id=:staff
            """),
                {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
            )
            == 2
        )
        assert (
            connection.scalar(
                text("""
                SELECT revision FROM public.clinic_policy_heads
                WHERE organization_id=:tenant AND kind='SCHEDULE'
            """),
                {"tenant": clinic.tenant},
            )
            == 2
        )


def test_revoking_manager_invalidates_pending_preview_but_preserves_receipt(
    patient_backend,
):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = _patch(_config(mode="OPEN")).model_dump(mode="json")
    preview_key = str(uuid4())
    status, pending = _schedule_request(
        clinic, manager, method="POST", suffix="/preview", body=body, key=preview_key
    )
    assert status == 200, pending
    activation = {
        "preview_id": pending["data"]["preview_id"],
        "expected_schedule_revision": 1,
        "expected_capacity_revision": 1,
        "reason": "Synthetic preview before revocation",
    }

    _owner_changes_manager(clinic, False, 1)
    assert _schedule_request(clinic, manager)[0] == 401
    assert (
        _schedule_request(
            clinic,
            manager,
            method="POST",
            suffix="/preview",
            body=body,
            key=preview_key,
        )[0]
        == 401
    )
    assert (
        _schedule_request(
            clinic,
            manager,
            method="POST",
            suffix="/activate",
            body=activation,
            key=str(uuid4()),
        )[0]
        == 401
    )
    ordinary = _fresh_receptionist_session(clinic, 2)
    assert _schedule_request(clinic, ordinary)[0] == 403
    assert (
        _schedule_request(
            clinic,
            ordinary,
            method="POST",
            suffix="/activate",
            body=activation,
            key=str(uuid4()),
        )[0]
        == 403
    )

    _owner_changes_manager(clinic, True, 2)
    regranted = _fresh_receptionist_session(clinic, 3)
    assert _schedule_request(clinic, regranted)[0] == 200
    assert _schedule_request(
        clinic,
        regranted,
        method="POST",
        suffix="/preview",
        body=body,
        key=preview_key,
    ) == (200, pending)
    stale_status, stale = _schedule_request(
        clinic,
        regranted,
        method="POST",
        suffix="/activate",
        body=activation,
        key=str(uuid4()),
    )
    assert stale_status == 409 and stale["error"]["code"] == "STALE_REVISION"
    with clinic.migration.begin() as connection:
        assert (
            connection.scalar(
                text("""
                SELECT count(*) FROM public.idempotency_receipts
                WHERE organization_id=:tenant AND actor_id=:staff
                  AND operation LIKE 'settings.schedule.%'
            """),
                {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
                SELECT revision FROM public.clinic_policy_heads
                WHERE organization_id=:tenant AND kind='SCHEDULE'
            """),
                {"tenant": clinic.tenant},
            )
            == 1
        )
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                SET enabled=false, security_revision=security_revision+1
                WHERE organization_id=:tenant AND id=:staff
            """),
            {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
        )
    assert _schedule_request(clinic, _fresh_receptionist_session(clinic, 4))[0] == 403


@pytest.mark.parametrize("winner", ["revoke", "authorize"])
@pytest.mark.parametrize("operation", ["preview", "activate"])
def test_manager_revoke_and_schedule_operations_serialize(
    patient_backend, winner, operation
):
    clinic = patient_backend
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    body = _patch(_config(mode="OPEN")).model_dump(mode="json")
    prepared = None
    if operation == "activate":
        with protected_transaction(
            clinic.runtime, manager, clinic.config.policy.session_idle_minutes
        ) as (connection, actor):
            prepared = _stage(connection, actor, _config(mode="OPEN"))
        body = {
            "preview_id": prepared["preview_id"],
            "expected_schedule_revision": 1,
            "expected_capacity_revision": 1,
            "reason": "Synthetic reviewed schedule publication",
        }
    suffix = "/preview" if operation == "preview" else "/activate"
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        if winner == "revoke":
            with protected_transaction(
                clinic.runtime, clinic.owner, clinic.config.policy.session_idle_minutes
            ) as (connection, _):
                holder = connection.scalar(text("SELECT pg_backend_pid()"))
                _manager_change(connection, clinic, False, 1)
                operation_future = pool.submit(
                    _schedule_request,
                    clinic,
                    manager,
                    method="POST",
                    suffix=suffix,
                    body=body,
                    key=key,
                )
                _blocked_by(clinic, holder)
            status, denied = operation_future.result(timeout=30)
            assert status == 401, denied
        else:
            with protected_transaction(
                clinic.runtime, manager, clinic.config.policy.session_idle_minutes
            ) as (connection, actor):
                holder = connection.scalar(text("SELECT pg_backend_pid()"))
                revoke_future = pool.submit(_owner_changes_manager, clinic, False, 1)
                _blocked_by(clinic, holder)
                if operation == "preview":
                    assert (
                        _stage(connection, actor, _config(mode="OPEN"), key=key)[
                            "revisions"
                        ]["schedule"]
                        == 1
                    )
                else:
                    assert prepared is not None
                    assert (
                        _activate(connection, actor, prepared, key=key)[
                            "schedule_revision"
                        ]
                        == 2
                    )
            revoke_future.result(timeout=30)

    assert (
        _schedule_request(
            clinic, manager, method="POST", suffix=suffix, body=body, key=key
        )[0]
        == 401
    )
    refreshed = _fresh_receptionist_session(clinic, 2)
    assert (
        _schedule_request(
            clinic, refreshed, method="POST", suffix=suffix, body=body, key=key
        )[0]
        == 403
    )
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.scalar(
            text("""
                SELECT count(*) FROM public.idempotency_receipts
                 WHERE organization_id=:tenant AND actor_id=:staff
                   AND operation LIKE 'settings.schedule.%'
            """),
            {"tenant": clinic.tenant, "staff": UUID(manager.staff_id)},
        ) == int(operation == "activate") + int(winner == "authorize")
        assert read_schedule(
            connection, _actor(clinic.tenant, UUID(clinic.owner.staff_id))
        )["schedule_revision"] == 1 + int(
            operation == "activate" and winner == "authorize"
        )
