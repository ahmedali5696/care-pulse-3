"""B-12: signed queue resource scope and cross-partition denial regression."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_database import admin as admin  # noqa: PLC0414
from test_database import disposable_project as disposable_project  # noqa: PLC0414
from test_database import empty_database as empty_database  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_checkin import (
    PRIOR_DAY,
    SERVICE_DAY,
    _new_patient,
    _seed_booked,
    queue_request,
)


def _check_in(clinic, appointment):
    status, response = queue_request(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/appointments/{appointment['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=f"scope-{uuid4().hex}",
    )
    assert status == 200, response
    return response["data"]


def _visible_ids(connection, table):
    return set(connection.scalars(text(f"SELECT id::text FROM public.{table}")))


def test_signed_checkin_can_shift_peer_rank_but_cannot_touch_other_queue(
    booking_clinic,
):
    clinic = booking_clinic
    target_patient = UUID(_new_patient(clinic, clinic.owner, "Target"))
    target = _seed_booked(clinic, target_patient, SERVICE_DAY)
    peer = _seed_booked(
        clinic, UUID(_new_patient(clinic, clinic.owner, "Peer")), SERVICE_DAY
    )
    unrelated = _seed_booked(
        clinic, UUID(_new_patient(clinic, clinic.owner, "Unrelated")), PRIOR_DAY
    )
    emergency = _seed_booked(
        clinic,
        UUID(_new_patient(clinic, clinic.owner, "Emergency")),
        SERVICE_DAY,
        classification="EMERGENCY",
        lane="EMERGENCY",
    )
    peer_entry = _check_in(clinic, peer)
    unrelated_entry = _check_in(clinic, unrelated)
    emergency_entry = _check_in(clinic, emergency)

    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path=f"/v1/appointments/{target['id']}/check-in",
        purpose="QUEUE_CHECK_IN",
        body={"physical_presence_confirmed": True, "expected_appointment_revision": 0},
        key=f"scope-denial-{uuid4().hex}",
    ) as (connection, _actor):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        assert _visible_ids(connection, "appointments") == {target["id"], peer["id"]}
        assert _visible_ids(connection, "queue_days") == {peer_entry["queue_day_id"]}
        assert _visible_ids(connection, "queue_entries") == {
            peer_entry["queue_entry_id"]
        }
        assert _visible_ids(connection, "patients") == {str(target_patient)}
        assert set(
            connection.scalars(
                text("SELECT kind FROM public.clinic_policy_heads FOR SHARE")
            )
        ) == {"SCHEDULE", "QUEUE"}
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    INSERT INTO public.queue_days
                        (id, organization_id, doctor_staff_id, service_date, lane,
                         revision, queue_policy_version, arrival_counter)
                    VALUES (:id, :tenant, :doctor, :day, 'MAIN', 1, :version, 1)
                """),
                {
                    "id": uuid4(),
                    "tenant": clinic.tenant,
                    "doctor": UUID(clinic.owner.staff_id),
                    "day": SERVICE_DAY + timedelta(days=2),
                    "version": peer_entry["queue_policy_version"],
                },
            )
        assert denied.value.orig.sqlstate == "42501"
        for other in (unrelated_entry, emergency_entry):
            assert (
                connection.execute(
                    text(
                        "UPDATE public.queue_days SET revision=revision+1 WHERE id=:id RETURNING id"
                    ),
                    {"id": UUID(other["queue_day_id"])},
                ).one_or_none()
                is None
            )
            assert (
                connection.execute(
                    text(
                        "UPDATE public.queue_entries SET routine_rank=routine_rank+1, revision=revision+1 WHERE id=:id RETURNING id"
                    ),
                    {"id": UUID(other["queue_entry_id"])},
                ).one_or_none()
                is None
            )
        # A valid partition alone does not authorize a standalone rank shift.
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("UPDATE public.queue_days SET revision=revision+1 WHERE id=:id"),
                {"id": UUID(peer_entry["queue_day_id"])},
            )
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text(
                    "UPDATE public.queue_days SET revision=revision+1, arrival_counter=arrival_counter+1 WHERE id=:id"
                ),
                {"id": UUID(peer_entry["queue_day_id"])},
            )
            connection.execute(
                text(
                    "UPDATE public.queue_entries SET routine_rank=routine_rank+1, revision=revision+1 WHERE id=:id"
                ),
                {"id": UUID(peer_entry["queue_entry_id"])},
            )
            connection.execute(
                text("SET CONSTRAINTS f01_queue_rank_shift_final IMMEDIATE")
            )
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.scalar(
                text("SELECT public.f01_queue_active_exam_count(:tenant)"),
                {"tenant": clinic.tenant},
            )
        assert denied.value.orig.sqlstate == "42501"

    target_entry = _check_in(clinic, target)
    assert target_entry["routine_rank"] == 1
    assert target_entry["position"] == 1
    with clinic.migration.connect() as connection:
        for projection, expected_rank, expected_revision in (
            (peer_entry, 2, 2),
            (unrelated_entry, 1, 1),
            (emergency_entry, None, 1),
        ):
            assert connection.execute(
                text(
                    "SELECT routine_rank, revision FROM public.queue_entries WHERE id=:id"
                ),
                {"id": UUID(projection["queue_entry_id"])},
            ).one() == (expected_rank, expected_revision)

    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="GET",
        path="/v1/queue",
        purpose="QUEUE_READ",
        query=f"date={SERVICE_DAY.isoformat()}&lane=MAIN&limit=1&offset=0",
    ) as (connection, _actor):
        assert _visible_ids(connection, "appointments") == {target["id"], peer["id"]}
        assert _visible_ids(connection, "queue_days") == {peer_entry["queue_day_id"]}
        assert _visible_ids(connection, "queue_entries") == {
            target_entry["queue_entry_id"],
            peer_entry["queue_entry_id"],
        }
        assert _visible_ids(connection, "patients") == {
            target_entry["patient_id"],
            peer_entry["patient_id"],
        }
        assert (
            connection.scalar(
                text("SELECT public.f01_queue_active_exam_count(:tenant)"),
                {"tenant": clinic.tenant},
            )
            == 0
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.scalar(
                text("SELECT public.f01_queue_active_exam_count(:tenant)"),
                {"tenant": uuid4()},
            )
        assert denied.value.orig.sqlstate == "42501"

    with clinic.runtime.connect() as connection:
        for table in ("appointments", "queue_days", "queue_entries", "patients"):
            assert not _visible_ids(connection, table)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.scalar(
                text("SELECT public.f01_queue_active_exam_count(:tenant)"),
                {"tenant": clinic.tenant},
            )
        assert denied.value.orig.sqlstate == "42501"
