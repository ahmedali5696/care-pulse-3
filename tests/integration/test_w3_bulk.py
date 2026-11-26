"""T059: signed B-19 bulk preview and atomic included-only movement."""

import json
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.policies.versions import append_version
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_pricing import _fresh_receptionist_session, _owner_changes_manager
from test_queue_capacity import _policy_capacity
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_w3_admission import _admit, admit_body
from test_w3_cancel_replace import _appointment, book_case, cancel_body, change_request
from test_w3_checkin import queue_request
from test_w3_intake import (
    _policy_revision,
    _today,
    intake_body,
    intake_request,
    set_intake_schedule,
)
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414
from test_w3_receipt_boundary import _foreign_appointment
from test_w3_reschedule import move_body, move_request, rows
from test_w3_visits import effects, visit_request


def preview_body(ids, original, destination):
    return {
        "appointment_ids": [str(identifier) for identifier in ids],
        "original_service_date": original.isoformat(),
        "destination_date": destination.isoformat(),
        "reason": "Synthetic reviewed bulk movement",
    }


def bulk_request(clinic, actor, kind, body, *, key=None):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/bulk-reschedule/{kind}",
        purpose=f"APPOINTMENT_BULK_{kind.upper()}",
        body=body,
        key=key or uuid4().hex,
    )


def commit_body(preview):
    return {"preview_id": preview["preview_id"], "confirmation": "RESCHEDULE"}


def partition(preview):
    included = {row["appointment_id"] for row in preview["included"]}
    excluded = {row["appointment_id"]: row["reason"] for row in preview["excluded"]}
    assert preview["eligible_count"] == len(preview["included"]) == len(included)
    assert preview["excluded_count"] == len(preview["excluded"]) == len(excluded)
    assert not included.intersection(excluded)
    assert "affected" not in preview and "eligible" not in preview
    return included, excluded


def preview_selection(clinic, actor, body, *, key=None):
    before = effects(clinic)
    status, reply = bulk_request(clinic, actor, "preview", body, key=key)
    assert status == 200, reply
    preview = reply["data"]
    assert UUID(preview["preview_id"])
    included, excluded = partition(preview)
    assert included | excluded.keys() == set(body["appointment_ids"])
    assert preview["original_service_date"] == body["original_service_date"]
    assert preview["destination_date"] == body["destination_date"]
    assert preview["appointment_ids"] == body["appointment_ids"]
    after = effects(clinic)
    assert {k: v for k, v in after.items() if k != "idempotency_receipts"} == {
        k: v for k, v in before.items() if k != "idempotency_receipts"
    }
    (receipt,) = added(before, after, "idempotency_receipts", 1)
    assert receipt["id"] == preview["preview_id"]
    assert receipt["staff_id"] == actor.staff_id
    assert receipt["operation"] == "appointments.bulk_reschedule.preview"
    assert receipt["result_projection"] == preview
    return preview


def added(before, after, table, count):
    assert all(row in after[table] for row in before[table]), table
    delta = [row for row in after[table] if row not in before[table]]
    assert len(delta) == count, table
    return delta


def denied(clinic, actor, kind, body, expected, *, key=None):
    before = effects(clinic)
    status, reply = bulk_request(clinic, actor, kind, body, key=key)
    assert status == expected, reply
    assert set(reply) == {"error"}
    assert effects(clinic) == before


def appointment_row(snapshot, target):
    return next(row for row in snapshot["appointments"] if row["id"] == str(target))


def assert_batch(before, after, actor, preview, projection, key):
    included, excluded = partition(preview)
    assert {row["appointment_id"] for row in projection["moved"]} == included
    assert len(projection["moved"]) == len(included)
    assert projection["moved_count"] == len(included)
    assert projection["preview_id"] == preview["preview_id"]
    assert len(after["appointments"]) == len(before["appointments"])
    for identifier in excluded:
        assert appointment_row(after, identifier) == appointment_row(before, identifier)
        assert rows(after, "queue_entries", identifier) == rows(
            before, "queue_entries", identifier
        )
    for table in (
        "appointment_price_snapshots",
        "followup_claims",
        "followup_source_state",
        "followup_source_decision_events",
        "followup_authorization_events",
        "visits",
        "clinical_audit",
    ):
        assert after[table] == before[table], table
    histories = added(before, after, "appointment_history", len(included))
    assignments = added(before, after, "appointment_number_assignments", len(included))
    audits = added(before, after, "sensitive_audit", len(included))
    events = added(
        before,
        after,
        "outbox_events",
        len(included) * (2 if actor.role == "DOCTOR_OWNER" else 1),
    )
    (receipt,) = added(before, after, "idempotency_receipts", 1)
    assert receipt["idempotency_key"] == key
    assert receipt["operation"] == "appointments.bulk_reschedule.commit"
    assert receipt["result_projection"] == projection
    old_counter = next(
        (
            row["last_number"]
            for row in before["appointment_day_counters"]
            if row["service_date"] == preview["destination_date"]
        ),
        0,
    )
    new_counter = next(
        row["last_number"]
        for row in after["appointment_day_counters"]
        if row["service_date"] == preview["destination_date"]
    )
    assert new_counter == old_counter + len(included)
    assert {row["number"] for row in assignments} == set(
        range(old_counter + 1, new_counter + 1)
    )
    for identifier in included:
        original, saved = (
            appointment_row(before, identifier),
            appointment_row(after, identifier),
        )
        assert saved["service_date"] == preview["destination_date"]
        assert saved["status"] == "Booked"
        assert saved["revision"] == original["revision"] + 1
        for field in (
            "patient_id",
            "doctor_staff_id",
            "classification",
            "lane",
            "service_type",
        ):
            assert saved[field] == original[field]
        (history,) = [row for row in histories if row["appointment_id"] == identifier]
        assert history["action"] == "RESCHEDULE"
        assert history["replacement_appointment_id"] is None
        assert history["reason"] == preview["reason"]
        assert history["actor_staff_id"] == actor.staff_id
        assert history["actor_role"] == actor.role
        assert history["old_service_date"] == original["service_date"]
        assert history["new_service_date"] == saved["service_date"]
        (assignment,) = [
            row for row in assignments if row["appointment_id"] == identifier
        ]
        assert assignment["prior_assignment_id"] == original["current_assignment_id"]
        assert assignment["number"] == saved["current_number"]
        assert assignment["service_date"] == saved["service_date"]
        assert all(
            row["disposition"] != "ACTIVE"
            for row in rows(after, "queue_entries", identifier)
        )
        (audit,) = [row for row in audits if row["correlation_id"] == history["id"]]
        assert audit["action"] == "APPOINTMENT_RESCHEDULED"
        assert audit["actor_staff_id"] == actor.staff_id
        payload = {
            "appointment_id": identifier,
            "status": "Booked",
            "service_date": saved["service_date"],
            "appointment_revision": saved["revision"],
        }
        per_row = [row for row in events if row["aggregate_id"] == identifier]
        assert {row["event_type"] for row in per_row} == (
            {"APPOINTMENT_RESCHEDULED", "DOCTOR_RESCHEDULED_APPOINTMENT"}
            if actor.role == "DOCTOR_OWNER"
            else {"APPOINTMENT_RESCHEDULED"}
        )
        for event in per_row:
            assert event["payload"] == (
                payload
                if event["event_type"] == "DOCTOR_RESCHEDULED_APPOINTMENT"
                else {
                    **payload,
                    "actor_staff_id": actor.staff_id,
                    "actor_role": actor.role,
                }
            )
        bound = next(
            row for row in preview["included"] if row["appointment_id"] == identifier
        )
        for field in ("starts_at", "ends_at"):
            if bound[field] is None:
                assert saved[field] is None
            else:
                assert datetime.fromisoformat(saved[field]) == datetime.fromisoformat(
                    bound[field]
                )


def mixed_cohort(clinic):
    examined = book_case(clinic, waiting=True)
    admitted = _admit(
        clinic,
        clinic.owner,
        admit_body(
            clinic,
            {
                "appointment_id": examined,
                "service_date": _today().isoformat(),
                "lane": "MAIN",
            },
            physical_presence_confirmed=True,
        ),
    )
    status, reply = visit_request(
        clinic,
        clinic.owner,
        UUID(admitted["visit_id"]),
        "clinical",
        {
            "expected_visit_revision": 0,
            "diagnosis": "Synthetic B19 confidential diagnosis",
        },
    )
    assert status == 200, reply
    terminal = book_case(clinic)
    status, reply = change_request(
        clinic, clinic.owner, terminal, "cancel", cancel_body(clinic, terminal)
    )
    assert status == 200, reply
    booked, waiting = book_case(clinic), book_case(clinic, waiting=True)
    status, reply = intake_request(
        clinic,
        clinic.receptionist,
        "urgent",
        intake_body(clinic, new_patient(clinic), "urgent"),
    )
    assert status == 201, reply
    return [booked, waiting], {
        examined: "appointment not eligible",
        terminal: "appointment not eligible",
        reply["data"]["id"]: "service not eligible",
    }


@pytest.mark.parametrize("role", ["owner", "manager", "receptionist"])
def test_mixed_selection_moves_only_exact_included_subset(intake_clinic, role):
    clinic = intake_clinic
    included, excluded = mixed_cohort(clinic)
    if role == "manager":
        _owner_changes_manager(clinic, True, 0)
        actor = _fresh_receptionist_session(clinic, 1)
    else:
        actor = getattr(clinic, role)
    body = preview_body([*included, *excluded], _today(), _today() + timedelta(days=1))
    preview_key, commit_key = uuid4().hex, uuid4().hex
    preview = preview_selection(clinic, actor, body, key=preview_key)
    assert partition(preview) == (set(included), excluded)
    assert "Synthetic B19 confidential diagnosis" not in json.dumps(preview)
    assert '"diagnosis"' not in json.dumps(preview)
    before = effects(clinic)
    assert (
        bulk_request(clinic, actor, "preview", body, key=preview_key)[1]["data"]
        == preview
    )
    assert effects(clinic) == before
    command = commit_body(preview)
    status, reply = bulk_request(clinic, actor, "commit", command, key=commit_key)
    assert status == 200, reply
    after = effects(clinic)
    assert_batch(before, after, actor, preview, reply["data"], commit_key)
    assert bulk_request(clinic, actor, "commit", command, key=commit_key) == (
        200,
        reply,
    )
    assert effects(clinic) == after


def test_strict_selection_zero_included_and_same_day(intake_clinic):
    clinic = intake_clinic
    booked, waiting = book_case(clinic), book_case(clinic, waiting=True)
    actor, day = clinic.owner, _today()
    destination = day + timedelta(days=1)
    for identifiers in ([], [booked, booked]):
        denied(
            clinic, actor, "preview", preview_body(identifiers, day, destination), 422
        )
    for identifiers in ([booked], [booked, waiting]):
        denied(clinic, actor, "preview", preview_body(identifiers, day, day), 409)
    status, reply = move_request(clinic, actor, booked, move_body(clinic, booked, day))
    assert status == 200, reply
    status, reply = change_request(
        clinic, actor, booked, "cancel", cancel_body(clinic, booked)
    )
    assert status == 200, reply
    preview = preview_selection(clinic, actor, preview_body([booked], day, destination))
    assert partition(preview) == (set(), {booked: "appointment not eligible"})
    denied(clinic, actor, "commit", commit_body(preview), 409)


def test_commit_accepts_only_preview_identity_and_confirmation(intake_clinic):
    clinic = intake_clinic
    actor, day = clinic.owner, _today()
    target = book_case(clinic)
    body = preview_body([target], day, day + timedelta(days=1))
    key = uuid4().hex
    preview = preview_selection(clinic, actor, body, key=key)
    command = commit_body(preview)
    for field, replacement in {
        **body,
        "included": [],
        "excluded": [],
        "starts_at": "2027-01-01T09:00:00Z",
        "ends_at": "2027-01-01T09:15:00Z",
        "schedule_revision": 0,
        "capacity_revision": 0,
        "row_evidence": [],
        "expected_appointment_revision": 0,
    }.items():
        denied(clinic, actor, "commit", {**command, field: replacement}, 422)
    denied(clinic, actor, "commit", {**command, "confirmation": "reschedule"}, 422)
    denied(clinic, actor, "commit", {"preview_id": command["preview_id"]}, 422)
    # Test the actual signed SQL boundary against attempted server-side partition
    # replacement, independently of the API's extra-field rejection.
    for changes in (
        {"appointment_ids": []},
        {
            "included": [],
            "eligible_count": 0,
            "excluded": preview["included"],
            "excluded_count": 1,
        },
        {"destination_date": day.isoformat()},
    ):
        before = effects(clinic)
        with (
            pytest.raises(DBAPIError) as error,
            signed_test_transaction(
                clinic,
                actor,
                method="POST",
                path="/v1/appointments/bulk-reschedule/commit",
                purpose="APPOINTMENT_BULK_COMMIT",
                body=command,
                key=uuid4().hex,
            ) as (connection, _),
        ):
            connection.execute(text("SELECT public.f01_b19_prepare()"))
            connection.execute(
                text("SELECT public.f01_b19_apply(CAST(:snapshot AS jsonb))"),
                {"snapshot": json.dumps({**preview, **changes})},
            )
        assert error.value.orig.sqlstate == "40001"
        assert effects(clinic) == before
    other = book_case(clinic)
    denied(clinic, actor, "preview", {**body, "appointment_ids": [other]}, 409, key=key)
    denied(
        clinic,
        actor,
        "preview",
        {**body, "destination_date": (day + timedelta(days=2)).isoformat()},
        409,
        key=key,
    )
    status, reply = move_request(
        clinic, actor, target, move_body(clinic, target, day + timedelta(days=2))
    )
    assert status == 200, reply
    denied(clinic, actor, "commit", command, 409)


def test_preview_and_receipt_are_actor_and_tenant_scoped(intake_clinic):
    clinic = intake_clinic
    day = _today()
    target = book_case(clinic)
    foreign = _foreign_appointment(clinic)
    foreign_before = effects(clinic, tenant=clinic.other)
    body = preview_body([target], day, day + timedelta(days=1))
    denied(
        clinic,
        clinic.owner,
        "preview",
        {**body, "appointment_ids": [target, str(foreign)]},
        404,
    )
    assert effects(clinic, tenant=clinic.other) == foreign_before
    key = uuid4().hex
    preview = preview_selection(clinic, clinic.receptionist, body, key=key)
    command = commit_body(preview)
    denied(clinic, clinic.owner, "commit", command, 404)
    denied(
        clinic,
        clinic.receptionist,
        "commit",
        {**command, "preview_id": str(uuid4())},
        404,
    )
    with clinic.migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.staff_accounts SET enabled=false,security_revision=security_revision+1
            WHERE organization_id=:tenant AND id=:staff
        """),
            {"tenant": clinic.tenant, "staff": UUID(clinic.receptionist.staff_id)},
        )
    before = effects(clinic)
    for kind, payload in (("preview", body), ("commit", command)):
        for actor in (clinic.receptionist, _fresh_receptionist_session(clinic, 1)):
            status, reply = bulk_request(clinic, actor, kind, payload, key=key)
            assert status in (401, 403), reply
            assert set(reply) == {"error"}
    assert effects(clinic) == before


def interval(day, start, end):
    return {
        key: datetime.combine(day, time.fromisoformat(clock), CAIRO)
        .astimezone(UTC)
        .isoformat()
        for key, clock in (("starts_at", start), ("ends_at", end))
    }


def timed_body(clinic, day, start, end, *, overlap):
    body = booking_body(
        new_patient(clinic),
        day=day,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
        expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
        **interval(day, start, end),
    )
    if overlap:
        body["expected_capacity_revision"] = _policy_revision(clinic, "CAPACITY")
    return body


def timed_book(clinic, day, start, end, *, overlap):
    status, reply = booking_request(
        clinic,
        clinic.owner,
        body=timed_body(clinic, day, start, end, overlap=overlap),
        key=uuid4().hex,
    )
    assert status == 201, reply
    return reply["data"]["id"]


def set_capacity(clinic, capacity):
    with transaction(clinic.migration, clinic.tenant) as connection:
        _policy_capacity(connection, clinic.tenant, capacity=capacity)


@pytest.mark.parametrize(
    ("capacity", "n", "occupied", "conflict"),
    [
        (1, 2, False, True),
        (2, 2, False, False),
        (2, 3, False, True),
        (2, 2, True, True),
    ],
)
def test_joint_capacity_is_order_independent(
    intake_clinic, capacity, n, occupied, conflict
):
    clinic = intake_clinic
    original, destination = _today() + timedelta(days=1), _today() + timedelta(days=2)
    set_capacity(clinic, 3 if capacity == 1 or n == 3 else 2)
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=(("09:00", "10:00"),),
        duration=15,
        overlap=True,
    )
    overlapping = [
        timed_book(clinic, original, "09:00", "09:15", overlap=True) for _ in range(n)
    ]
    touching = timed_book(clinic, original, "09:15", "09:30", overlap=True)
    existing = (
        timed_book(clinic, destination, "09:00", "09:15", overlap=True)
        if occupied
        else None
    )
    if capacity == 1:
        set_intake_schedule(
            clinic,
            mode="EXACT_TIME",
            periods=(("09:00", "10:00"),),
            duration=15,
            overlap=False,
        )
    elif n == 3:
        # The live policy publisher rejects a lower capacity for these existing
        # future bookings. Seed the changed policy as maintenance to exercise
        # an inherited source cohort; all preview/commit commands remain signed.
        with transaction(clinic.migration, clinic.tenant) as connection:
            version = append_version(
                connection,
                str(clinic.tenant),
                "CAPACITY",
                {"capacity": 2, "paused": False},
                _policy_revision(clinic, "CAPACITY"),
                actor_staff_id=clinic.owner.staff_id,
            )
            connection.execute(
                text("""
                UPDATE public.doctor_controls SET capacity_policy_version=:version, revision=revision+1
                WHERE organization_id=:tenant
            """),
                {"version": version, "tenant": clinic.tenant},
            )
    selected = [*overlapping, touching]
    expected = (
        ({touching}, dict.fromkeys(overlapping, "destination capacity conflict"))
        if conflict
        else (set(selected), {})
    )
    previews = [
        preview_selection(
            clinic, clinic.owner, preview_body(ids, original, destination)
        )
        for ids in (selected, selected[::-1])
    ]
    for preview in previews:
        assert partition(preview) == expected
        for row in preview["included"]:
            clocks = (
                ("09:15", "09:30")
                if row["appointment_id"] == touching
                else ("09:00", "09:15")
            )
            assert {key: row[key] for key in ("starts_at", "ends_at")} == interval(
                destination, *clocks
            )
        if existing:
            assert existing not in json.dumps(preview)
            assert str(_appointment(clinic, existing)["patient_id"]) not in json.dumps(
                preview
            )
    before = effects(clinic)
    key = uuid4().hex
    status, reply = bulk_request(
        clinic, clinic.owner, "commit", commit_body(previews[0]), key=key
    )
    assert status == 200, reply
    after = effects(clinic)
    assert_batch(before, after, clinic.owner, previews[0], reply["data"], key)
    if existing:
        assert appointment_row(after, existing) == appointment_row(before, existing)


def test_separate_existing_intervals_count_peak_not_pairwise(intake_clinic):
    clinic = intake_clinic
    original, destination = _today() + timedelta(days=1), _today() + timedelta(days=2)
    set_capacity(clinic, 2)
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=(("09:00", "10:00"),),
        duration=15,
        overlap=True,
    )
    existing = [
        timed_book(clinic, destination, start, end, overlap=True)
        for start, end in (("09:00", "09:15"), ("09:15", "09:30"))
    ]
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=(("09:00", "10:00"),),
        duration=30,
        overlap=True,
    )
    target = timed_book(clinic, original, "09:00", "09:30", overlap=True)
    preview = preview_selection(
        clinic, clinic.owner, preview_body([target], original, destination)
    )
    assert partition(preview) == ({target}, {})
    before = effects(clinic)
    key = uuid4().hex
    status, reply = bulk_request(
        clinic, clinic.owner, "commit", commit_body(preview), key=key
    )
    assert status == 200, reply
    after = effects(clinic)
    assert_batch(before, after, clinic.owner, preview, reply["data"], key)
    for identifier in existing:
        assert appointment_row(after, identifier) == appointment_row(before, identifier)


def next_offset_change():
    original = _today() + timedelta(days=1)
    offset = datetime.combine(original, time(9), CAIRO).utcoffset()
    return next(
        original + timedelta(days=i)
        for i in range(1, 370)
        if datetime.combine(original + timedelta(days=i), time(9), CAIRO).utcoffset()
        != offset
    )


def test_cairo_clock_carriage_across_offset_change(intake_clinic):
    clinic = intake_clinic
    destination = next_offset_change()
    original = destination - timedelta(days=1)
    set_intake_schedule(
        clinic, mode="EXACT_TIME", periods=(("09:00", "10:00"),), duration=15
    )
    target = timed_book(clinic, original, "09:00", "09:15", overlap=False)
    preview = preview_selection(
        clinic, clinic.owner, preview_body([target], original, destination)
    )
    assert partition(preview) == ({target}, {})
    (row,) = preview["included"]
    assert row["source_local_start"] == "09:00:00"
    assert row["source_local_end"] == "09:15:00"
    assert {key: row[key] for key in ("starts_at", "ends_at")} == interval(
        destination, "09:00", "09:15"
    )
    assert datetime.fromisoformat(row["starts_at"]) - datetime.fromisoformat(
        interval(original, "09:00", "09:15")["starts_at"]
    ) != timedelta(days=1)
    before = effects(clinic)
    key = uuid4().hex
    status, reply = bulk_request(
        clinic, clinic.owner, "commit", commit_body(preview), key=key
    )
    assert status == 200, reply
    assert_batch(before, effects(clinic), clinic.owner, preview, reply["data"], key)


@pytest.mark.parametrize(
    ("clock", "end", "instants"),
    [
        ("00:15", "00:30", 0),
        ("23:15", "23:30", 2),
    ],
    ids=["spring-gap", "autumn-fold"],
)
def test_unresolved_cairo_wall_clock_is_excluded(intake_clinic, clock, end, instants):
    clinic = intake_clinic
    start = time.fromisoformat(clock)
    first = _today() + timedelta(days=4)

    def count(day):
        local = datetime.combine(day, start, CAIRO)
        candidates = {local.replace(fold=fold).astimezone(UTC) for fold in (0, 1)}
        return sum(
            candidate.astimezone(CAIRO).replace(tzinfo=None)
            == local.replace(tzinfo=None)
            for candidate in candidates
        )

    destination = next(
        first + timedelta(days=i)
        for i in range(370)
        if count(first + timedelta(days=i)) == instants
    )
    original = destination - timedelta(days=3)
    assert count(original) == 1
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=((clock, end), ("09:00", "09:15"))
        if clock < "09:00"
        else (("09:00", "09:15"), (clock, end)),
        duration=15,
    )
    target = timed_book(clinic, original, clock, end, overlap=False)
    independent = timed_book(clinic, original, "09:00", "09:15", overlap=False)
    preview = preview_selection(
        clinic, clinic.owner, preview_body([target, independent], original, destination)
    )
    assert partition(preview) == (
        {independent},
        {target: "destination local time unavailable"},
    )
    (excluded,) = preview["excluded"]
    assert excluded["source_local_start"] == clock + ":00"
    assert excluded["source_local_end"] == end + ":00"
    assert excluded["starts_at"] is None and excluded["ends_at"] is None
    before = effects(clinic)
    key = uuid4().hex
    status, reply = bulk_request(
        clinic, clinic.owner, "commit", commit_body(preview), key=key
    )
    assert status == 200, reply
    assert_batch(before, effects(clinic), clinic.owner, preview, reply["data"], key)


@pytest.mark.parametrize(
    ("guard", "reason"),
    [
        ("holiday", "destination schedule unavailable"),
        ("work", "destination slot unavailable"),
        ("break", "destination slot unavailable"),
        ("duration", "destination slot unavailable"),
        ("existing", "destination capacity conflict"),
    ],
)
def test_individual_destination_guard_excludes_only_invalid_rows(
    intake_clinic, guard, reason
):
    clinic = intake_clinic
    original, destination = _today() + timedelta(days=1), _today() + timedelta(days=2)
    set_intake_schedule(
        clinic, mode="EXACT_TIME", periods=(("09:00", "10:00"),), duration=15
    )
    target = timed_book(clinic, original, "09:00", "09:15", overlap=False)
    independent = timed_book(clinic, original, "09:30", "09:45", overlap=False)
    if guard == "existing":
        existing = timed_book(clinic, destination, "09:00", "09:15", overlap=False)
    else:
        set_intake_schedule(
            clinic,
            mode="EXACT_TIME",
            periods=(("09:30", "10:00"),) if guard == "work" else (("09:00", "10:00"),),
            breaks=(("09:00", "09:15"),) if guard == "break" else (),
            holidays=(destination,) if guard == "holiday" else (),
            duration=30 if guard == "duration" else 15,
        )
    preview = preview_selection(
        clinic, clinic.owner, preview_body([target, independent], original, destination)
    )
    if guard in ("holiday", "duration"):
        assert partition(preview) == (set(), {target: reason, independent: reason})
        denied(clinic, clinic.owner, "commit", commit_body(preview), 409)
    else:
        assert partition(preview) == ({independent}, {target: reason})
        before = effects(clinic)
        key = uuid4().hex
        status, reply = bulk_request(
            clinic, clinic.owner, "commit", commit_body(preview), key=key
        )
        assert status == 200, reply
        after = effects(clinic)
        assert_batch(before, after, clinic.owner, preview, reply["data"], key)
        if guard == "existing":
            assert appointment_row(after, existing) == appointment_row(before, existing)
            assert existing not in json.dumps(preview)


def assert_sql_denied(connection, sql, params, state):
    with pytest.raises(DBAPIError) as error, connection.begin_nested():
        connection.execute(text(sql), params)
    assert error.value.orig.sqlstate == state


def test_signed_destination_projection_and_independent_receipt_scope(intake_clinic):
    clinic = intake_clinic
    original, destination = _today() + timedelta(days=1), _today() + timedelta(days=2)
    set_intake_schedule(
        clinic, mode="EXACT_TIME", periods=(("09:00", "10:00"),), duration=15
    )
    target = timed_book(clinic, original, "09:00", "09:15", overlap=False)
    existing = timed_book(clinic, destination, "09:15", "09:30", overlap=False)
    body = preview_body([target], original, destination)
    actor = clinic.owner
    preview = preview_selection(clinic, actor, body)
    before = effects(clinic)
    for kind, payload in (("preview", body), ("commit", commit_body(preview))):
        with signed_test_transaction(
            clinic,
            actor,
            method="POST",
            path=f"/v1/appointments/bulk-reschedule/{kind}",
            purpose=f"APPOINTMENT_BULK_{kind.upper()}",
            body=payload,
            key=uuid4().hex,
        ) as (connection, _):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            sql = f"SELECT * FROM public.f01_b19_{kind}_intervals(:tenant,:day)"
            params = {"tenant": clinic.tenant, "day": destination}
            ((start, end),) = connection.execute(text(sql), params).all()
            saved = _appointment(clinic, existing)
            assert (start, end) == (saved["starts_at"], saved["ends_at"])
            assert_sql_denied(connection, sql, {**params, "day": original}, "42501")
            assert_sql_denied(
                connection, sql, {**params, "tenant": clinic.other}, "42501"
            )
            opposite = "commit" if kind == "preview" else "preview"
            assert_sql_denied(
                connection,
                f"SELECT * FROM public.f01_b19_{opposite}_intervals(:tenant,:day)",
                params,
                "42501",
            )
            if kind == "preview":
                assert_sql_denied(
                    connection, "SELECT public.f01_b19_read_preview()", {}, "42501"
                )
            else:
                stored = connection.scalar(text("SELECT public.f01_b19_read_preview()"))
                assert stored["preview_id"] == preview["preview_id"]
                assert stored["appointment_ids"] == [target]
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.idempotency_receipts WHERE id<>:id"
                    ),
                    {"id": UUID(preview["preview_id"])},
                )
                == 0
            )
    # The UUID of a real receipt owned by this actor is not a bulk preview.
    unrelated = next(
        row
        for row in before["idempotency_receipts"]
        if row["operation"] == "appointments.book" and row["staff_id"] == actor.staff_id
    )
    for acting, command in (
        (actor, {"preview_id": unrelated["id"], "confirmation": "RESCHEDULE"}),
        (clinic.receptionist, commit_body(preview)),
    ):
        with signed_test_transaction(
            clinic,
            acting,
            method="POST",
            path="/v1/appointments/bulk-reschedule/commit",
            purpose="APPOINTMENT_BULK_COMMIT",
            body=command,
            key=uuid4().hex,
        ) as (connection, _):
            assert_sql_denied(
                connection, "SELECT public.f01_b19_read_preview()", {}, "P0002"
            )
            assert_sql_denied(
                connection,
                "SELECT * FROM public.f01_b19_commit_intervals(:tenant,:day)",
                {"tenant": clinic.tenant, "day": destination},
                "P0002",
            )
    assert effects(clinic) == before
