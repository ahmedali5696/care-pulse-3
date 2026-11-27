"""T004: committed target-bound check-in receipts and B-10 compatibility."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, normalized_command_digest
from carepulse.identity.receipts import Receipt, insert_tenant_receipt
from carepulse.queue.checkin import check_in_appointment, recover_committed_checkin
from carepulse.queue.contracts import CheckInAppointment
from runtime_identity_fixture import registered_test_request, signed_test_transaction
from sqlalchemy import text
from test_appointment_persistence import _activate_clinic
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_decisions import (
    _decision,
    _signed_visit_request,
    _source,
    _staff_context,
)
from test_followup_persistence import _v2_source
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_checkin import (
    SERVICE_DAY,
    _book,
    _new_patient,
    _seed_booked,
    queue_request,
)


def _effects(clinic):
    with clinic.migration.connect() as connection:
        return tuple(
            connection.scalar(
                text(
                    f"SELECT count(*) FROM public.{table} WHERE organization_id=:tenant"
                ),
                {"tenant": clinic.tenant},
            )
            for table in (
                "appointments",
                "appointment_history",
                "appointment_number_assignments",
                "appointment_price_snapshots",
                "queue_days",
                "queue_entries",
                "visits",
                "sensitive_audit",
                "outbox_events",
                "idempotency_receipts",
            )
        )


def _foreign_appointment(clinic):
    with clinic.migration.begin() as connection:
        operator = connection.scalar(
            text(
                "SELECT created_by_operator_id FROM public.organizations WHERE id=:tenant"
            ),
            {"tenant": clinic.other},
        )
        doctor = _activate_clinic(connection, clinic.other, operator)
    with transaction(clinic.migration, clinic.other) as connection:
        return _v2_source(connection, clinic.other, doctor)["appointment"]


def _receipt_rows(connection, key):
    return connection.execute(
        text("""
            SELECT result_projection, result_revision FROM public.idempotency_receipts
            WHERE operation='appointments.check_in' AND idempotency_key=:key
        """),
        {"key": key},
    ).all()


def _assert_signed_receipt_scope(clinic, target, key, expected, *, denial=None):
    actor = clinic.receptionist
    body = {"physical_presence_confirmed": True, "expected_appointment_revision": 0}
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    identity = replace(
        actor,
        method="POST",
        path=f"/v1/appointments/{target}/check-in",
        purpose="QUEUE_CHECK_IN",
        query="",
        body_sha256=sha256(raw_body).hexdigest(),
        timestamp=str(int(datetime.now(UTC).timestamp() * 1000)),
        nonce=f"synthetic_{uuid4().hex}",
        idempotency_key=key,
    )
    idle = clinic.config.policy.session_idle_minutes
    with registered_test_request(clinic, identity, raw_body):
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            protected_transaction(clinic.runtime, identity, idle) as (connection, _),
        ):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            if expected is not None:
                assert _receipt_rows(connection, key) == expected
            raise RuntimeError("synthetic rollback")
        with protected_transaction(
            clinic.runtime, identity, idle, stage="RECEIPT_RECOVERY"
        ) as (connection, authorized):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            if expected is not None:
                assert _receipt_rows(connection, key) == expected
            if denial is None:
                assert (
                    recover_committed_checkin(
                        connection,
                        authorized,
                        UUID(str(target)),
                        CheckInAppointment.model_validate(body),
                        key,
                    )
                    == expected[0]
                )
            else:
                with pytest.raises(ContractError) as denied:
                    recover_committed_checkin(
                        connection,
                        authorized,
                        UUID(str(target)),
                        CheckInAppointment.model_validate(body),
                        key,
                    )
                assert (denied.value.status_code, denied.value.code) == denial


def test_target_aware_receipt_conflict_hidden_target_and_lost_response_recovery(
    booking_clinic,
) -> None:
    clinic = booking_clinic
    actor = clinic.receptionist
    first = _book(clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Alpha"))
    second = _book(clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Beta"))
    foreign = _foreign_appointment(clinic)
    key = f"t004-{uuid4().hex}"
    body = {"physical_presence_confirmed": True, "expected_appointment_revision": 0}
    path = f"/v1/appointments/{first['id']}/check-in"
    before = _effects(clinic)

    # Commit the actual command and deliberately lose the result before any HTTP reply.
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=key,
    ) as (connection, authorized):
        original, revision = check_in_appointment(
            connection,
            authorized,
            UUID(first["id"]),
            CheckInAppointment.model_validate(body),
            key,
        )
    after = _effects(clinic)
    assert after == tuple(
        a + b for a, b in zip(before, (0, 1, 0, 0, 1, 1, 0, 1, 1, 1), strict=True)
    )
    assert revision == 1

    def retry(target, *, caller=actor, payload=body):
        return queue_request(
            clinic,
            caller,
            method="POST",
            path=f"/v1/appointments/{target}/check-in",
            purpose="QUEUE_CHECK_IN",
            body=payload,
            key=key,
        )

    status, replay = retry(first["id"])
    assert (
        status == 200 and replay["data"] == original and replay["revision"] == revision
    )
    status, conflict = retry(second["id"])
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "data" not in conflict
    for target in (foreign, uuid4()):
        status, hidden = retry(target)
        assert status == 404 and hidden["error"]["code"] == "NOT_FOUND"
        assert "data" not in hidden
    status, changed = retry(
        first["id"], payload={**body, "expected_appointment_revision": 1}
    )
    assert status == 409 and changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "data" not in changed
    # A separate active staff member cannot retrieve the first actor's receipt.
    status, denied = retry(first["id"], caller=clinic.owner)
    assert status == 409 and "data" not in denied
    with clinic.runtime.connect() as connection:
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        assert _receipt_rows(connection, key) == []
    _assert_signed_receipt_scope(clinic, first["id"], key, [(original, revision)])
    _assert_signed_receipt_scope(
        clinic, second["id"], key, [], denial=(409, "IDEMPOTENCY_CONFLICT")
    )
    _assert_signed_receipt_scope(clinic, foreign, key, [], denial=(404, None))
    assert _effects(clinic) == after

    with clinic.migration.begin() as connection:
        receipt = connection.execute(
            text("""
                SELECT actor_id, staff_id, operation, result_projection, result_revision
                FROM public.idempotency_receipts
                WHERE organization_id=:tenant AND operation='appointments.check_in'
                  AND idempotency_key=:key
            """),
            {"tenant": clinic.tenant, "key": key},
        ).one()
        assert receipt == (
            UUID(actor.staff_id),
            UUID(actor.staff_id),
            "appointments.check_in",
            original,
            revision,
        )
        assert connection.execute(
            text("SELECT status, revision FROM public.appointments WHERE id=:id"),
            {"id": UUID(second["id"])},
        ).one() == ("Booked", 0)
        connection.execute(
            text('DELETE FROM public."session" WHERE id=:id'),
            {"id": actor.session_id},
        )
    status, revoked = retry(first["id"])
    assert status in (401, 403) and "data" not in revoked
    assert _effects(clinic) == after


def test_checkin_receipt_replays_original_after_later_urgent_arrival(booking_clinic):
    clinic = booking_clinic
    appointment = _book(
        clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Routine")
    )
    urgent = _seed_booked(
        clinic,
        UUID(_new_patient(clinic, clinic.owner, "Urgent")),
        SERVICE_DAY,
        classification="URGENT",
    )
    body = {"physical_presence_confirmed": True, "expected_appointment_revision": 0}
    key = str(uuid4())
    path = f"/v1/appointments/{appointment['id']}/check-in"
    status, original = queue_request(
        clinic,
        clinic.receptionist,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=key,
    )
    assert status == 200, original
    status, arrival = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{urgent['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200, arrival
    assert (
        arrival["data"]["queue_day_revision"]
        == original["data"]["queue_day_revision"] + 1
    )
    status, queue = queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": SERVICE_DAY.isoformat(), "lane": "MAIN"},
    )
    assert status == 200, queue
    assert [entry["appointment_id"] for entry in queue["data"]["entries"]] == [
        urgent["id"],
        appointment["id"],
    ]
    assert original["data"]["position"] == 1
    assert queue["data"]["entries"][1]["position"] == 2
    before = _effects(clinic)
    status, replay = queue_request(
        clinic,
        clinic.receptionist,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=key,
    )
    assert status == 200 and replay == original
    _assert_signed_receipt_scope(
        clinic, appointment["id"], key, [(original["data"], original["revision"])]
    )
    assert _effects(clinic) == before
    assert queue_request(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query={"date": SERVICE_DAY.isoformat(), "lane": "MAIN"},
    ) == (200, queue)


def test_malformed_or_mismatched_saved_checkin_receipts_deny_without_effects(
    booking_clinic,
):
    clinic = booking_clinic
    actor = clinic.receptionist
    first = _book(
        clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Receipt target")
    )
    second = _book(
        clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Receipt peer")
    )
    body = {"physical_presence_confirmed": True, "expected_appointment_revision": 0}
    path = f"/v1/appointments/{first['id']}/check-in"
    status, accepted = queue_request(
        clinic,
        actor,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200, accepted
    status, peer = queue_request(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/{second['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=str(uuid4()),
    )
    assert status == 200, peer
    original = accepted["data"]
    digest = normalized_command_digest(
        "appointments.check_in",
        CheckInAppointment.model_validate(body),
        target={"appointment_id": UUID(first["id"])},
    )
    corruptions = {
        "nested_position": {"position": {"private": "not a number"}},
        "boolean_revision": {"queue_entry_revision": True},
        "negative_cursor": {"routine_cursor": -1},
        "unexpected_field": {"clinical_notes": "private"},
        "wrong_patient": {"patient_id": peer["data"]["patient_id"]},
        "wrong_entry": {"queue_entry_id": peer["data"]["queue_entry_id"]},
        "wrong_day": {"queue_day_id": str(uuid4())},
        "wrong_actor": {"attested_by_staff_id": clinic.owner.staff_id},
        "wrong_date": {"service_date": (SERVICE_DAY + timedelta(days=1)).isoformat()},
        "wrong_number": {"booking_number": second["number"]},
        "wrong_arrival": {"arrival_sequence": peer["data"]["arrival_sequence"]},
        "wrong_time": {
            "attested_at": (
                datetime.fromisoformat(original["attested_at"]) + timedelta(seconds=1)
            ).isoformat()
        },
        "cross_target": peer["data"],
    }
    for label, changes in corruptions.items():
        key = str(uuid4())
        # Migration-role fixture deliberately imports historical corrupt data; API writes cannot do this.
        with transaction(clinic.migration, clinic.tenant) as connection:
            _staff_context(connection, UUID(actor.staff_id))
            insert_tenant_receipt(
                connection,
                Receipt(
                    actor_id=UUID(actor.staff_id),
                    staff_id=UUID(actor.staff_id),
                    organization_id=clinic.tenant,
                    operation="appointments.check_in",
                    idempotency_key=key,
                    request_digest=digest,
                    result_projection={**original, **changes},
                    result_revision=accepted["revision"],
                ),
            )
        before = _effects(clinic)
        status, denied = queue_request(
            clinic,
            actor,
            method="POST",
            path=path,
            purpose="QUEUE_CHECK_IN",
            body=body,
            key=key,
        )
        assert status == 409 and "data" not in denied, (label, status, denied)
        if label in ("nested_position", "wrong_patient"):
            _assert_signed_receipt_scope(
                clinic,
                first["id"],
                key,
                None,
                denial=(409, None),
            )
        elif label == "cross_target":
            _assert_signed_receipt_scope(
                clinic,
                first["id"],
                key,
                [],
                denial=(409, "IDEMPOTENCY_CONFLICT"),
            )
        assert _effects(clinic) == before, label


def test_b10_existing_receipt_compatibility_preserved(followup_clinic) -> None:
    source = _source(followup_clinic)
    key = f"t004-b10-{uuid4().hex}"
    payload = _decision("OPTED_OUT", reason="Not indicated").model_dump(
        mode="json", exclude_unset=True
    )
    first_status, first_body = _signed_visit_request(
        followup_clinic,
        followup_clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{source['visit']}",
        body=payload,
        key=key,
    )
    assert first_status == 200, first_body

    replay_status, replay_body = _signed_visit_request(
        followup_clinic,
        followup_clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{source['visit']}",
        body=payload,
        key=key,
    )
    assert replay_status == 200, replay_body
    assert replay_body == first_body

    second_source = _source(followup_clinic)
    conflict_status, conflict_body = _signed_visit_request(
        followup_clinic,
        followup_clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{second_source['visit']}",
        body=payload,
        key=key,
    )
    assert conflict_status == 403, conflict_body
    assert conflict_body["error"]["code"] == "FORBIDDEN"
