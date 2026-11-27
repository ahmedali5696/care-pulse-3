"""B-12: signed queue resource scope and cross-partition denial regression."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, normalized_command_digest
from carepulse.identity.receipts import Receipt, insert_tenant_receipt
from carepulse.notifications.evidence import append_outbox, append_sensitive_audit
from carepulse.queue.checkin import check_in_appointment, recover_committed_checkin
from carepulse.queue.contracts import CheckInAppointment
from runtime_identity_fixture import registered_test_request, signed_test_transaction
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


def test_checkin_signed_authority_and_transition_evidence(booking_clinic):
    clinic = booking_clinic
    target = _seed_booked(
        clinic, UUID(_new_patient(clinic, clinic.owner, "Signed target")), SERVICE_DAY
    )
    peer = _seed_booked(
        clinic, UUID(_new_patient(clinic, clinic.owner, "Signed peer")), SERVICE_DAY
    )
    day = _check_in(clinic, peer)
    target_id = UUID(target["id"])
    path = f"/v1/appointments/{target_id}/check-in"
    body = {
        "physical_presence_confirmed": True,
        "expected_appointment_revision": 0,
        "expected_queue_day_revision": 1,
    }
    params = {"tenant": clinic.tenant, "appointment": target_id}
    writer = text("SELECT public.f01_check_in_appointment(:tenant, :appointment, 0)")
    advance = text(
        "UPDATE public.queue_days SET revision=revision+1, arrival_counter=arrival_counter+1 WHERE id=:id"
    )
    for invalid in (
        {**body, "physical_presence_confirmed": False},
        {**body, "expected_appointment_revision": 1},
    ):
        with signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="POST",
            path=path,
            purpose="QUEUE_CHECK_IN",
            body=invalid,
            key=str(uuid4()),
        ) as (connection, _actor):
            assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                if invalid["physical_presence_confirmed"]:
                    # Valid queue revision; the supplied appointment revision is the denial.
                    connection.execute(advance, {"id": UUID(day["queue_day_id"])})
                connection.scalar(writer, params)
            assert denied.value.orig.sqlstate == "42501"
            if not invalid["physical_presence_confirmed"]:
                with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                    connection.execute(advance, {"id": UUID(day["queue_day_id"])})
                assert denied.value.orig.sqlstate == "42501"
    with (
        pytest.raises(ContractError) as denied,
        signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="POST",
            path=path,
            purpose="QUEUE_CHECK_IN",
            body={**body, "physical_presence_confirmed": "true"},
            key=str(uuid4()),
        ),
    ):
        pytest.fail("Malformed signed check-in activated")
    assert denied.value.status_code == 403
    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body={**body, "expected_queue_day_revision": 0},
        key=str(uuid4()),
    ) as (connection, _actor):
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(advance, {"id": UUID(day["queue_day_id"])})
        assert denied.value.orig.sqlstate == "42501"

    key = str(uuid4())
    command = CheckInAppointment.model_validate(body)
    digest = normalized_command_digest(
        "appointments.check_in", command, target={"appointment_id": target_id}
    )
    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=key,
    ) as (connection, actor):

        def receipt(projection):
            return Receipt(
                actor_id=actor.staff_id,
                staff_id=actor.staff_id,
                organization_id=clinic.tenant,
                operation="appointments.check_in",
                idempotency_key=key,
                request_digest=digest,
                result_projection=projection,
                result_revision=1,
            )

        def audit(appointment_id, projection):
            return {
                "organization_id": clinic.tenant,
                "actor_staff_id": actor.staff_id,
                "actor_role": actor.role,
                "action": "appointments.check_in",
                "correlation_id": str(appointment_id),
                "before_state": {
                    "appointment_id": str(appointment_id),
                    "status": "Booked",
                    "revision": 0,
                },
                "after_state": {
                    "appointment_id": str(appointment_id),
                    "queue_entry_id": projection["queue_entry_id"],
                    "status": "Waiting",
                    "revision": 1,
                    "service_date": projection["service_date"],
                    "lane": projection["lane"],
                    "arrival_sequence": projection["arrival_sequence"],
                },
            }

        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            insert_tenant_receipt(
                connection, receipt({**day, "appointment_id": str(target_id)})
            )
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            append_sensitive_audit(connection, audit(target_id, day))
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            append_outbox(
                connection,
                {
                    "organization_id": clinic.tenant,
                    "event_type": "PATIENT_CHECKED_IN",
                    "aggregate_type": "APPOINTMENT",
                    "aggregate_id": str(target_id),
                    "payload": {},
                },
            )
        assert denied.value.orig.sqlstate == "42501"

        projection, revision = check_in_appointment(
            connection, actor, target_id, command, key
        )
        assert revision == 1
        assert projection["queue_day_revision"] == 2
        # Deferred D-05 integrity must validate evidence without making it API-readable.
        for table in (
            "appointment_price_snapshots",
            "appointment_number_assignments",
            "appointment_history",
        ):
            assert connection.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
        connection.execute(
            text("SET CONSTRAINTS appointment_evidence_committed IMMEDIATE")
        )
        for forged in (
            {**projection, "appointment_id": peer["id"]},
            {**projection, "queue_entry_id": day["queue_entry_id"]},
            {**projection, "position": 999},
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                insert_tenant_receipt(connection, receipt(forged))
            assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            append_sensitive_audit(connection, audit(peer["id"], projection))
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            append_sensitive_audit(connection, audit(target_id, projection))
        assert denied.value.orig.sqlstate == "42501"

    raw_body = json.dumps(body, separators=(",", ":")).encode()
    identity = replace(
        clinic.receptionist,
        method="POST",
        path=path,
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
            pytest.raises(ContractError) as denied,
            protected_transaction(
                clinic.runtime, identity, idle, stage="RECEIPT_RECOVERY"
            ),
        ):
            pytest.fail("Recovery cannot be the first stage")
        assert denied.value.status_code == 403
        with (
            pytest.raises(RuntimeError, match="synthetic rollback"),
            protected_transaction(clinic.runtime, identity, idle),
        ):
            raise RuntimeError("synthetic rollback")
        with protected_transaction(
            clinic.runtime, identity, idle, stage="RECEIPT_RECOVERY"
        ) as (connection, actor):
            assert recover_committed_checkin(
                connection, actor, target_id, command, key
            ) == (projection, revision)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                insert_tenant_receipt(connection, receipt(projection))
            assert denied.value.orig.sqlstate == "42501"
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.scalar(writer, params)
            assert denied.value.orig.sqlstate == "42501"


def test_checkin_requires_all_evidence_at_commit_and_rolls_back(
    booking_clinic, monkeypatch
):
    clinic = booking_clinic
    target = _seed_booked(
        clinic,
        UUID(_new_patient(clinic, clinic.owner, "Incomplete target")),
        SERVICE_DAY,
    )
    peer = _seed_booked(
        clinic, UUID(_new_patient(clinic, clinic.owner, "Incomplete peer")), SERVICE_DAY
    )
    day = _check_in(clinic, peer)
    target_id = UUID(target["id"])
    path = f"/v1/appointments/{target_id}/check-in"
    body = {
        "physical_presence_confirmed": True,
        "expected_appointment_revision": 0,
        "expected_queue_day_revision": day["queue_day_revision"],
    }
    command = CheckInAppointment.model_validate(body)
    key = str(uuid4())

    def snapshot():
        with clinic.migration.connect() as connection:
            return tuple(
                connection.scalar(
                    text(f"""
                        SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t))
                        FROM public.{table} t WHERE organization_id = :tenant
                    """),
                    {"tenant": clinic.tenant},
                )
                for table in (
                    "appointments",
                    "appointment_day_counters",
                    "appointment_number_assignments",
                    "appointment_price_snapshots",
                    "appointment_history",
                    "queue_days",
                    "queue_entries",
                    "visits",
                    "sensitive_audit",
                    "outbox_events",
                    "idempotency_receipts",
                )
            )

    before = snapshot()
    for missing in ("append_sensitive_audit", "append_outbox", "insert_tenant_receipt"):
        with monkeypatch.context() as patch:
            patch.setattr(f"carepulse.queue.checkin.{missing}", lambda *_: None)
            with (
                pytest.raises(
                    DBAPIError, match="Check-in commit evidence incomplete"
                ) as denied,
                signed_test_transaction(
                    clinic,
                    clinic.receptionist,
                    method="POST",
                    path=path,
                    purpose="QUEUE_CHECK_IN",
                    body=body,
                    key=key,
                ) as (connection, actor),
            ):
                projection, revision = check_in_appointment(
                    connection, actor, target_id, command, key
                )
                assert revision == 1
                assert projection["queue_day_revision"] == day["queue_day_revision"] + 1
                assert projection["routine_rank"] == 1
            assert denied.value.orig.sqlstate == "42501", missing
        assert snapshot() == before, missing

    # Even a fully evidenced transition must disappear if its transaction aborts.
    with (
        pytest.raises(RuntimeError, match="synthetic check-in rollback"),
        signed_test_transaction(
            clinic,
            clinic.receptionist,
            method="POST",
            path=path,
            purpose="QUEUE_CHECK_IN",
            body=body,
            key=key,
        ) as (connection, actor),
    ):
        check_in_appointment(connection, actor, target_id, command, key)
        raise RuntimeError("synthetic check-in rollback")
    assert snapshot() == before

    status, response = queue_request(
        clinic,
        clinic.receptionist,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=key,
    )
    assert status == 200, response
    assert response["data"]["appointment_id"] == str(target_id)
    assert response["data"]["appointment_revision"] == 1
    assert response["data"]["queue_day_revision"] == day["queue_day_revision"] + 1
    committed = snapshot()
    assert committed != before
    for old, new in zip(before[-3:], committed[-3:], strict=True):
        assert len(new) == len(old) + 1
    assert queue_request(
        clinic,
        clinic.receptionist,
        method="POST",
        path=path,
        purpose="QUEUE_CHECK_IN",
        body=body,
        key=key,
    ) == (status, response)
    assert snapshot() == committed
