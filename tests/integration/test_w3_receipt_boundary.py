"""T004: committed target-bound check-in receipts and B-10 compatibility."""

from uuid import UUID, uuid4

from carepulse.queue.checkin import check_in_appointment
from carepulse.queue.contracts import CheckInAppointment
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_decisions import _decision, _signed_visit_request, _source
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_checkin import _book, _new_patient, queue_request


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


def test_target_aware_receipt_conflict_hidden_target_and_lost_response_recovery(
    booking_clinic,
) -> None:
    clinic = booking_clinic
    actor = clinic.receptionist
    first = _book(clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Alpha"))
    second = _book(clinic, clinic.owner, _new_patient(clinic, clinic.owner, "Beta"))
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
    status, hidden = retry(uuid4())
    assert status == 404 and hidden["error"]["code"] == "NOT_FOUND"
    status, changed = retry(
        first["id"], payload={**body, "expected_appointment_revision": 1}
    )
    assert status == 409 and changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # A separate active staff member cannot retrieve the first actor's receipt.
    status, denied = retry(first["id"], caller=clinic.owner)
    assert status == 409 and "data" not in denied
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
