"""T015: protected immediate intake commands and immutable committed effects."""

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.intake_contracts import (
    EmergencyIntake,
    RoutineIntake,
    UrgentIntake,
)
from carepulse.db import transaction
from carepulse.identity.contracts import normalized_request_digest
from carepulse.policies.versions import append_version
from sqlalchemy import text
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followups import claim_record, completed_source, followup_body
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_patients import private_request
from test_pricing import (
    _fresh_receptionist_session,
    _owner_changes_manager,
    _patch,
    pricing_request,
)
from test_queue_capacity import _policy_capacity
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import CAIRO
from test_slots import _slots_request
from test_w3_checkin import queue_request

_SCHEMAS = {
    "routine": RoutineIntake,
    "emergency": EmergencyIntake,
    "urgent": UrgentIntake,
}
_EFFECT_TABLES = (
    "appointments",
    "appointment_day_counters",
    "appointment_number_assignments",
    "appointment_price_snapshots",
    "appointment_history",
    "queue_days",
    "queue_entries",
    "visits",
    "completion_attestations",
    "followup_claims",
    "followup_authorization_events",
    "sensitive_audit",
    "outbox_events",
    "idempotency_receipts",
)


def _today():
    return datetime.now(CAIRO).date()


def _policy_revision(clinic, kind):
    with clinic.migration.connect() as conn:
        return conn.scalar(
            text("""
            SELECT revision FROM public.clinic_policy_heads
            WHERE organization_id=:tenant AND kind=:kind
        """),
            {"tenant": clinic.tenant, "kind": kind},
        )


def set_intake_schedule(
    clinic,
    *,
    routine_enabled=True,
    mode="OPEN",
    periods=(("00:00", "23:59"),),
    breaks=(),
    holidays=(),
    overlap=False,
    manual=(),
    duration=5,
    holiday_booking_enabled=False,
):
    """Synthetic immutable policy publication: the upstream writer cannot toggle walk-in."""
    with transaction(clinic.migration, clinic.tenant) as conn:
        revision = conn.scalar(
            text("""
            SELECT revision FROM public.clinic_policy_heads
            WHERE organization_id=:tenant AND kind='SCHEDULE'
        """),
            {"tenant": clinic.tenant},
        )
        version = append_version(
            conn,
            str(clinic.tenant),
            "SCHEDULE",
            {
                "booking_mode": mode,
                "duration_minutes": duration if mode == "EXACT_TIME" else None,
                "holiday_booking_enabled": holiday_booking_enabled,
                "routine_walk_in_enabled": routine_enabled,
                "allow_overlap": overlap,
                "no_show_cutoff": {"enabled": False, "local_time": None},
                "working_weekdays": list(range(1, 8)),
                "open_override_dates": [],
                "manual_slot_dates": [],
            },
            revision,
            actor_staff_id=clinic.owner.staff_id,
            intervals=[
                {
                    "interval_kind": kind,
                    "weekday": weekday,
                    "service_date": None,
                    "starts_at": time.fromisoformat(start),
                    "ends_at": time.fromisoformat(end),
                }
                for weekday in range(1, 8)
                for kind, bounds in (
                    ("WORK", periods),
                    ("BREAK", breaks),
                    ("MANUAL_SLOT", manual),
                )
                for start, end in bounds
            ],
            holidays=list(holidays),
        )
        assert version == revision + 1
    return revision + 1


@pytest.fixture
def intake_clinic(booking_clinic):
    clinic = booking_clinic
    set_intake_schedule(clinic)
    status, result = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(
            expected_price_revision=2,
            preferential_followup_egp="30.00",
            urgent_base_egp="150.00",
            urgent_enabled=True,
        ),
        key=uuid4().hex,
    )
    assert status == 200, result
    return clinic


def intake_body(clinic, patient, kind, **changes):
    base = "150.00" if kind == "urgent" else "100.00"
    body = {
        "patient_id": str(patient),
        "physical_presence_confirmed": True,
        "discount": {
            "discount_type": "NONE",
            "discount_value": "0",
            "discount_reason": None,
        },
        "expected_base_egp": base,
        "expected_net_egp": base,
        "expected_price_revision": _policy_revision(clinic, "PRICE"),
    }
    if kind != "emergency":
        body["expected_schedule_revision"] = _policy_revision(clinic, "SCHEDULE")
    if kind == "routine":
        body.update(
            service_date=_today().isoformat(), requested_service_type="NEW_EXAMINATION"
        )
    return {**body, **changes}


def intake_request(clinic, actor, kind, body, key=None):
    return queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/intake/{kind}",
        purpose=f"INTAKE_{kind.upper()}",
        body=body,
        key=key or uuid4().hex,
    )


def intake_effects(clinic):
    """Compare every committed domain row, including mutable counters and rank/cursor."""
    with clinic.migration.connect() as conn:
        return {
            table: tuple(
                conn.scalars(
                    text(
                        f"SELECT row_to_json(t)::text FROM public.{table} t "
                        "WHERE organization_id=:tenant ORDER BY row_to_json(t)::text"
                    ),
                    {"tenant": clinic.tenant},
                )
            )
            for table in _EFFECT_TABLES
        }


def _deny(clinic, actor, kind, body, expected, key=None):
    before = intake_effects(clinic)
    status, result = intake_request(clinic, actor, kind, body, key)
    assert status == expected, result
    assert set(result) == {"error"}
    assert intake_effects(clinic) == before


def _created(clinic, actor, kind, body, *, replay=True):
    before = intake_effects(clinic)
    key = uuid4().hex
    status, result = intake_request(clinic, actor, kind, body, key)
    assert status == 201, result
    assert result["revision"] == 0
    saved = result["data"]
    assert saved["status"] == "Waiting" and saved["revision"] == 0
    assert saved["classification"] == kind.upper()
    assert saved["patient_id"] == body["patient_id"]
    assert saved["doctor_staff_id"] == clinic.owner.staff_id
    assert saved["lane"] == ("EMERGENCY" if kind == "emergency" else "MAIN")
    assert saved["attested_by_staff_id"] == actor.staff_id
    assert saved["base_egp"] == body["expected_base_egp"]
    assert saved["net_egp"] == body["expected_net_egp"]
    assert saved["is_free"] is (Decimal(body["expected_net_egp"]) == 0)
    assert saved["history"] == [
        {
            "action": "CREATED",
            "old_status": None,
            "new_status": "Waiting",
            "old_service_date": None,
            "new_service_date": saved["service_date"],
            "old_number": None,
            "new_number": saved["number"],
            "appointment_revision": 0,
            "reason": None,
        }
    ]
    after = intake_effects(clinic)
    for table in (
        "appointments",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "appointment_history",
        "queue_entries",
        "sensitive_audit",
        "outbox_events",
        "idempotency_receipts",
    ):
        assert len(after[table]) == len(before[table]) + 1, table
        assert set(before[table]) <= set(after[table]), table
    for table in ("visits", "completion_attestations"):
        assert after[table] == before[table], table
    for table, matching, counter in (
        (
            "appointment_day_counters",
            {"service_date": saved["service_date"]},
            "last_number",
        ),
        (
            "queue_days",
            {"service_date": saved["service_date"], "lane": saved["lane"]},
            "arrival_counter",
        ),
    ):
        old_rows, new_rows = (
            [json.loads(row) for row in snapshot[table]] for snapshot in (before, after)
        )

        def matches(row, matching=matching):
            return all(row[k] == v for k, v in matching.items())

        previous = next((row for row in old_rows if matches(row)), None)
        current = next(row for row in new_rows if matches(row))
        assert current[counter] == (previous[counter] if previous else 0) + 1
        assert [r for r in old_rows if not matches(r)] == [
            r for r in new_rows if not matches(r)
        ]
        if table == "queue_days":
            assert current["revision"] == (previous["revision"] if previous else 0) + 1
            assert current["routine_cursor"] == (
                previous["routine_cursor"] if previous else 0
            )
    operation = f"appointments.intake_{kind}"
    appointment_id = UUID(saved["id"])
    with clinic.migration.connect() as conn:
        params = {"tenant": clinic.tenant, "appointment": appointment_id, "key": key}
        row = conn.execute(
            text("""
            SELECT a.current_number, n.number, n.service_date, c.last_number,
                   s.base_egp, s.net_egp, s.is_free, s.pricing_version,
                   s.creation_standard_base_egp, s.discount_type, s.discount_reason,
                   s.discount_value, s.discount_percent_text,
                   h.action, h.new_status, h.old_status, h.appointment_revision,
                   h.actor_staff_id, h.actor_role,
                   e.id, e.disposition, e.arrival_sequence, e.routine_rank,
                   e.attested_by_staff_id, e.attested_at, e.revision,
                   q.id, q.arrival_counter, q.revision, q.routine_cursor, q.queue_policy_version
            FROM public.appointments a
            JOIN public.appointment_number_assignments n
              ON (n.organization_id,n.id)=(a.organization_id,a.current_assignment_id)
            JOIN public.appointment_day_counters c
              ON (c.organization_id,c.service_date)=(a.organization_id,a.service_date)
            JOIN public.appointment_price_snapshots s
              ON (s.organization_id,s.appointment_id)=(a.organization_id,a.id)
            JOIN public.appointment_history h
              ON (h.organization_id,h.appointment_id)=(a.organization_id,a.id)
            JOIN public.queue_entries e
              ON (e.organization_id,e.appointment_id)=(a.organization_id,a.id)
            JOIN public.queue_days q
              ON (q.organization_id,q.id)=(e.organization_id,e.queue_day_id)
            WHERE a.organization_id=:tenant AND a.id=:appointment
        """),
            params,
        ).one()
        assert tuple(row) == (
            saved["number"],
            saved["number"],
            datetime.fromisoformat(saved["service_date"]).date(),
            saved["number"],
            Decimal(saved["base_egp"]),
            Decimal(saved["net_egp"]),
            saved["is_free"],
            saved["pricing_version"],
            Decimal("100.00"),
            body["discount"]["discount_type"],
            body["discount"].get("discount_reason"),
            None
            if body["discount"]["discount_type"] == "PERCENT"
            else Decimal(body["discount"]["discount_value"]),
            body["discount"]["discount_value"]
            if body["discount"]["discount_type"] == "PERCENT"
            else None,
            "CREATED",
            "Waiting",
            None,
            0,
            UUID(actor.staff_id),
            actor.role,
            UUID(saved["queue_entry_id"]),
            "ACTIVE",
            saved["arrival_sequence"],
            saved["routine_rank"],
            UUID(actor.staff_id),
            datetime.fromisoformat(saved["attested_at"]),
            saved["queue_entry_revision"],
            UUID(saved["queue_day_id"]),
            saved["arrival_sequence"],
            saved["queue_day_revision"],
            saved["routine_cursor"],
            saved["queue_policy_version"],
        )
        evidence = {
            "appointment_id": saved["id"],
            "queue_entry_id": saved["queue_entry_id"],
            "doctor_staff_id": saved["doctor_staff_id"],
            "service_date": saved["service_date"],
            "lane": saved["lane"],
            "classification": saved["classification"],
            "arrival_sequence": saved["arrival_sequence"],
        }
        assert conn.execute(
            text("""
            SELECT actor_staff_id, actor_role, action, before_state, after_state
            FROM public.sensitive_audit
            WHERE organization_id=:tenant AND correlation_id=CAST(:appointment AS text)
        """),
            params,
        ).one() == (
            UUID(actor.staff_id),
            actor.role,
            operation,
            None,
            {**evidence, "status": "Waiting", "revision": 0},
        )
        assert conn.execute(
            text("""
            SELECT event_type, aggregate_type, payload FROM public.outbox_events
            WHERE organization_id=:tenant AND aggregate_id=CAST(:appointment AS text)
        """),
            params,
        ).one() == ("APPOINTMENT_INTAKE", "APPOINTMENT", evidence)
        assert conn.execute(
            text("""
            SELECT staff_id, actor_id, operation, request_digest, result_projection, result_revision
            FROM public.idempotency_receipts WHERE organization_id=:tenant AND idempotency_key=:key
        """),
            params,
        ).one() == (
            UUID(actor.staff_id),
            UUID(actor.staff_id),
            operation,
            normalized_request_digest(_SCHEMAS[kind].model_validate(body)),
            saved,
            0,
        )
    if replay:
        assert intake_request(clinic, actor, kind, body, key) == (200, result)
        assert intake_effects(clinic) == after
    return saved


def _queue(clinic, lane):
    status, result = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": _today().isoformat(), "lane": lane},
    )
    assert status == 200, result
    return result["data"]


def test_routine_toggle_and_real_prebooking(intake_clinic):
    clinic = intake_clinic
    set_intake_schedule(clinic, routine_enabled=False)
    patient = new_patient(clinic)
    body = intake_body(clinic, patient, "routine")
    _deny(clinic, clinic.receptionist, "routine", body, 409)
    # A combined request is rejected even while normal Booked -> check-in remains lawful.
    before = intake_effects(clinic)
    booked_body = booking_body(
        patient,
        day=_today(),
        expected_price_revision=body["expected_price_revision"],
        expected_schedule_revision=body["expected_schedule_revision"],
    )
    status, denied = booking_request(
        clinic,
        clinic.receptionist,
        body={**booked_body, "physical_presence_confirmed": True},
        key=uuid4().hex,
    )
    assert status == 422, denied
    assert intake_effects(clinic) == before
    status, booked = booking_request(
        clinic, clinic.receptionist, body=booked_body, key=uuid4().hex
    )
    assert status == 201 and booked["data"]["status"] == "Booked"
    assert (
        queue_request(
            clinic,
            clinic.receptionist,
            method="POST",
            path=f"/v1/appointments/{booked['data']['id']}/check-in",
            purpose="QUEUE_CHECK_IN",
            body={
                "physical_presence_confirmed": True,
                "expected_appointment_revision": 0,
            },
            key=uuid4().hex,
        )[0]
        == 200
    )
    set_intake_schedule(clinic)
    saved = _created(
        clinic,
        clinic.owner,
        "routine",
        intake_body(clinic, new_patient(clinic), "routine"),
    )
    assert saved["routine_rank"] == 2 and saved["position"] == 2
    assert saved["routine_cursor"] == 0


def test_routine_followup_uses_existing_claim_and_frozen_entitlement(intake_clinic):
    clinic = intake_clinic
    source = completed_source(clinic)
    body = followup_body(
        source,
        offset=1,
        expected_price_revision=_policy_revision(clinic, "PRICE"),
        expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
        physical_presence_confirmed=True,
    )
    saved = _created(clinic, clinic.receptionist, "routine", body)
    assert saved["service_type"] == "FOLLOW_UP"
    claim = claim_record(clinic, saved["id"])
    assert claim["state"] == "ACTIVE"
    assert claim["source_visit_id"] == source["visit"]
    assert claim["source_appointment_id"] == source["appointment"]
    assert claim["policy_snapshot"] == claim["entitlement_provenance"]
    assert claim["base_egp"] == claim["net_egp"] == Decimal("30.00")


def test_emergency_independence_roles_discounts_and_main_order(intake_clinic):
    clinic = intake_clinic
    routine = _created(
        clinic,
        clinic.receptionist,
        "routine",
        intake_body(clinic, new_patient(clinic), "routine"),
    )
    urgent = _created(
        clinic,
        clinic.receptionist,
        "urgent",
        intake_body(clinic, new_patient(clinic), "urgent"),
    )
    main = _queue(clinic, "MAIN")
    assert [e["appointment_id"] for e in main["entries"]] == [
        urgent["id"],
        routine["id"],
    ]
    # Disable both toggles and declare today a holiday without any hours.
    set_intake_schedule(clinic, routine_enabled=False, periods=(), holidays=(_today(),))
    status, reply = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            urgent_enabled=False,
        ),
        key=uuid4().hex,
    )
    assert status == 200, reply
    first = _created(
        clinic,
        clinic.owner,
        "emergency",
        intake_body(clinic, new_patient(clinic), "emergency"),
    )
    second = _created(
        clinic,
        clinic.receptionist,
        "emergency",
        intake_body(
            clinic,
            new_patient(clinic),
            "emergency",
            expected_net_egp="0.00",
            discount={
                "discount_type": "PERCENT",
                "discount_value": "100",
                "discount_reason": "Synthetic approved free emergency",
            },
        ),
    )
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    third = _created(
        clinic,
        manager,
        "emergency",
        intake_body(
            clinic,
            new_patient(clinic),
            "emergency",
            expected_net_egp="75.00",
            discount={
                "discount_type": "FIXED",
                "discount_value": "25",
                "discount_reason": "Synthetic approved emergency discount",
            },
        ),
    )
    assert [e["appointment_id"] for e in _queue(clinic, "EMERGENCY")["entries"]] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    current_main = _queue(clinic, "MAIN")
    for field in ("entries", "queue_day_revision", "routine_cursor"):
        assert current_main[field] == main[field]
    assert [e["arrival_sequence"] for e in (first, second, third)] == [1, 2, 3]
    assert all(e["routine_rank"] is None for e in (first, second, third))


def test_urgent_open_role_presence_premium_and_price_revision(intake_clinic):
    clinic = intake_clinic
    patient = new_patient(clinic)
    body = intake_body(clinic, patient, "urgent")
    _deny(clinic, clinic.owner, "urgent", body, 403)
    for changes in (
        {"physical_presence_confirmed": False},
        {"physical_presence_confirmed": 1},
        {"service_date": (_today() + timedelta(days=1)).isoformat()},
        {"starts_at": None, "ends_at": None},
        {
            "starts_at": datetime.now(UTC).isoformat(),
            "ends_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
        {"discount": {"discount_type": "FIXED", "discount_value": "5"}},
    ):
        _deny(clinic, clinic.receptionist, "urgent", {**body, **changes}, 422)
    for discount, net in (("50", "100.00"), ("150", "0.00"), ("50.01", "99.99")):
        _deny(
            clinic,
            clinic.receptionist,
            "urgent",
            {
                **body,
                "discount": {
                    "discount_type": "FIXED",
                    "discount_value": discount,
                    "discount_reason": "Synthetic premium threshold",
                },
                "expected_net_egp": net,
            },
            422,
        )
    _deny(
        clinic,
        clinic.receptionist,
        "urgent",
        {**body, "expected_price_revision": body["expected_price_revision"] - 1},
        409,
    )
    _deny(
        clinic,
        clinic.receptionist,
        "urgent",
        {**body, "expected_net_egp": "149.00"},
        409,
    )
    saved = _created(
        clinic,
        clinic.receptionist,
        "urgent",
        {
            **body,
            "discount": {
                "discount_type": "FIXED",
                "discount_value": "49.99",
                "discount_reason": "Synthetic strict premium",
            },
            "expected_net_egp": "100.01",
        },
    )
    assert (
        saved["booking_mode"] == "OPEN"
        and saved["starts_at"] is saved["ends_at"] is None
    )
    assert saved["service_date"] == _today().isoformat()
    _owner_changes_manager(clinic, True, 0)
    manager = _fresh_receptionist_session(clinic, 1)
    _created(
        clinic, manager, "urgent", intake_body(clinic, new_patient(clinic), "urgent")
    )


def test_urgent_calendar_guards_and_disabled_toggle(intake_clinic):
    clinic = intake_clinic
    patient = new_patient(clinic)
    outside = (
        (("13:00", "14:00"),)
        if datetime.now(CAIRO).hour < 12
        else (("00:00", "01:00"),)
    )
    for config in (
        {"periods": ()},
        {"periods": outside},
        {"holidays": (_today(),), "holiday_booking_enabled": True},
        {"breaks": (("00:00", "23:59"),)},
    ):
        set_intake_schedule(clinic, **config)
        _deny(
            clinic,
            clinic.receptionist,
            "urgent",
            intake_body(clinic, patient, "urgent"),
            409,
        )
    set_intake_schedule(clinic)
    # Invalid premium configuration cannot be enabled even by the Doctor.
    before = intake_effects(clinic)
    revision = _policy_revision(clinic, "PRICE")
    status, reply = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(expected_price_revision=revision, urgent_base_egp="100.00"),
        key=uuid4().hex,
    )
    assert status == 422, reply
    assert _policy_revision(clinic, "PRICE") == revision
    assert intake_effects(clinic) == before
    status, reply = pricing_request(
        clinic,
        clinic.owner,
        method="PATCH",
        body=_patch(
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            urgent_enabled=False,
        ),
        key=uuid4().hex,
    )
    assert status == 200, reply
    _deny(
        clinic,
        clinic.receptionist,
        "urgent",
        intake_body(clinic, patient, "urgent"),
        409,
    )


def test_active_patient_counted_conflict_and_strict_operation_payload(intake_clinic):
    clinic = intake_clinic
    archived = new_patient(clinic)
    status, result = private_request(
        clinic,
        clinic.owner,
        f"/v1/patients/{archived}/archive",
        method="POST",
        body={
            "reason": "Synthetic archived new intake",
            "expected_lifecycle_revision": 0,
        },
    )
    assert status == 200, result
    counted = new_patient(clinic)
    _created(
        clinic, clinic.owner, "emergency", intake_body(clinic, counted, "emergency")
    )
    for kind in _SCHEMAS:
        actor = clinic.receptionist
        _deny(clinic, actor, kind, intake_body(clinic, archived, kind), 409)
        _deny(clinic, actor, kind, intake_body(clinic, counted, kind), 409)
        body = intake_body(clinic, new_patient(clinic), kind)
        _deny(clinic, actor, kind, {**body, "classification": "EMERGENCY"}, 422)
        _deny(clinic, actor, kind, {**body, "physical_presence_confirmed": "true"}, 422)
        _deny(
            clinic,
            actor,
            kind,
            {k: v for k, v in body.items() if k != "physical_presence_confirmed"},
            422,
        )


@pytest.mark.parametrize("overlap", [False, True])
def test_urgent_exact_time_selected_slot_and_counted_capacity(intake_clinic, overlap):
    clinic = intake_clinic
    if overlap:
        with transaction(clinic.migration, clinic.tenant) as conn:
            _policy_capacity(conn, clinic.tenant, capacity=2)
    # Real Cairo clock and candidate membership; overlap mode also tests manual precedence.
    future = datetime.now(CAIRO).replace(second=0, microsecond=0) + timedelta(minutes=8)
    manual_end = future + timedelta(minutes=2)
    manual = ()
    if overlap:
        assert manual_end.date() == _today(), (
            "Real Cairo day has no remaining test interval"
        )
        manual = ((future.strftime("%H:%M"), manual_end.strftime("%H:%M")),)
    set_intake_schedule(
        clinic, mode="EXACT_TIME", overlap=overlap, duration=1, manual=manual
    )
    status, result = _slots_request(clinic, clinic.receptionist, _today())
    assert status == 200, result
    slots = result["data"]
    # Leave time for the focused assertions and avoid selecting a slot about to start.
    earliest = datetime.now(UTC) + timedelta(minutes=3)
    offered = next(
        (
            s
            for s in slots["candidates"]
            if s["available"]
            and s["origin"] == ("MANUAL" if overlap else "GENERATED")
            and datetime.fromisoformat(s["starts_at"]) > earliest
        ),
        None,
    )
    assert offered is not None, (
        "Real Cairo operating day has no future selected candidate"
    )
    interval = {k: offered[k] for k in ("starts_at", "ends_at")}
    if overlap:
        interval["expected_capacity_revision"] = slots["capacity_revision"]
    patient = new_patient(clinic)
    body = intake_body(clinic, patient, "urgent", **interval)
    _deny(
        clinic,
        clinic.receptionist,
        "urgent",
        intake_body(clinic, patient, "urgent"),
        422,
    )
    _deny(clinic, clinic.receptionist, "urgent", {**body, "starts_at": "invalid"}, 422)
    _deny(
        clinic,
        clinic.receptionist,
        "urgent",
        {**body, "ends_at": body["starts_at"]},
        409,
    )
    shifted = (
        datetime.fromisoformat(body["starts_at"]) + timedelta(seconds=15)
    ).isoformat()
    _deny(clinic, clinic.receptionist, "urgent", {**body, "starts_at": shifted}, 409)
    _deny(
        clinic,
        clinic.receptionist,
        "urgent",
        {**body, "expected_schedule_revision": slots["schedule_revision"] - 1},
        409,
    )
    if overlap:
        # The one-minute generated subinterval overlaps the selected two-minute manual slot.
        suppressed_end = (
            datetime.fromisoformat(body["starts_at"]) + timedelta(minutes=1)
        ).isoformat()
        assert not any(
            s["starts_at"] == body["starts_at"] and s["ends_at"] == suppressed_end
            for s in slots["candidates"]
        )
        _deny(
            clinic,
            clinic.receptionist,
            "urgent",
            {**body, "ends_at": suppressed_end},
            409,
        )
        _deny(
            clinic,
            clinic.receptionist,
            "urgent",
            {**body, "expected_capacity_revision": slots["capacity_revision"] - 1},
            409,
        )
        _deny(
            clinic,
            clinic.receptionist,
            "urgent",
            {k: v for k, v in body.items() if k != "expected_capacity_revision"},
            422,
        )
    saved = _created(clinic, clinic.receptionist, "urgent", body)
    assert saved["booking_mode"] == "EXACT_TIME"
    assert datetime.fromisoformat(saved["starts_at"]) == datetime.fromisoformat(
        offered["starts_at"]
    )
    assert datetime.fromisoformat(saved["ends_at"]) == datetime.fromisoformat(
        offered["ends_at"]
    )
    contender = intake_body(clinic, new_patient(clinic), "urgent", **interval)
    if overlap:
        _created(clinic, clinic.receptionist, "urgent", contender)
        contender = intake_body(clinic, new_patient(clinic), "urgent", **interval)
    _deny(clinic, clinic.receptionist, "urgent", contender, 409)
