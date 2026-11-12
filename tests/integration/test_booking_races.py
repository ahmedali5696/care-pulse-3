"""B-09 transaction ordering with independent PostgreSQL connections."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.main import app
from carepulse.policies.schedule_service import load_current_schedule
from carepulse.policies.slots import candidates_for_date
from carepulse.policies.versions import append_version
from postgres_locks import wait_for_postgres_lock
from sqlalchemy import event, text
from test_booking import booking_body, booking_effects, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _activate, _actor, _config, _publish, _stage


def _attempt(clinic, patient, key=None, **changes):
    command = BookAppointment.model_validate(booking_body(patient, **changes))
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        return book_appointment(connection, actor, command, key or str(uuid4()))


def _concurrent(*actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=15)
        try:
            return action()
        except ContractError as denied:
            return denied

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [future.result(timeout=35) for future in futures]


def test_same_patient_and_same_key_races_have_one_committed_appointment(booking_clinic):
    clinic = booking_clinic
    patient = new_patient(clinic)
    key = str(uuid4())
    outcomes = _concurrent(
        lambda: _attempt(clinic, patient, key),
        lambda: _attempt(clinic, patient, key),
    )
    assert all(isinstance(outcome, dict) for outcome in outcomes), outcomes
    assert outcomes[0] == outcomes[1]
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_signed_same_key_distinct_patient_insert_collision_rolls_back_loser(
    booking_clinic,
):
    clinic = booking_clinic
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    first, second = new_patient(clinic), new_patient(clinic)
    key = str(uuid4())
    results = _concurrent(
        lambda: booking_request(
            clinic, clinic.owner, body=booking_body(first), key=key
        ),
        lambda: booking_request(
            clinic, clinic.owner, body=booking_body(second), key=key
        ),
    )
    assert sorted(status for status, _ in results) == [201, 409], results
    assert next(data for status, data in results if status == 409)["error"]["code"] == (
        "IDEMPOTENCY_CONFLICT"
    )
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)
    winner = next(data for status, data in results if status == 201)
    assert booking_request(
        clinic,
        clinic.owner,
        body=booking_body(UUID(winner["data"]["patient_id"])),
        key=key,
    ) == (201, winner)


def test_signed_booked_receipt_survives_later_archive(booking_clinic):
    clinic = booking_clinic
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    patient = new_patient(clinic)
    body, key = booking_body(patient), str(uuid4())
    original = booking_request(clinic, clinic.owner, body=body, key=key)
    assert original[0] == 201
    with transaction(clinic.runtime, clinic.tenant) as connection:
        connection.execute(
            text(
                "UPDATE public.patients SET archived=true, "
                "lifecycle_revision=lifecycle_revision+1 WHERE id=:patient"
            ),
            {"patient": patient},
        )
    assert booking_request(clinic, clinic.owner, body=body, key=key) == original
    changed = {**body, "expected_price_revision": 999}
    denied = booking_request(clinic, clinic.owner, body=changed, key=key)
    assert denied[0] == 409
    assert denied[1]["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert booking_request(clinic, clinic.owner, body=body, key=str(uuid4()))[0] == 409
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_direct_retry_waits_behind_archive_then_replays_committed_booking(
    booking_clinic,
):
    """Exercise the patient-lock retry directly; verify signed replay after archive."""
    clinic = booking_clinic
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    patient, key = new_patient(clinic), str(uuid4())
    body = booking_body(patient)
    archive_started = Event()
    archive_updated = Event()
    release_archive = Event()
    archive_pid = []
    receipt_read = Event()
    resume_retry = Event()
    retry_thread = []
    retry_pid = []

    def hold_first_receipt_read(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            "FROM public.idempotency_receipts AS r" in statement
            and get_ident() in retry_thread
            and not receipt_read.is_set()
        ):
            receipt_read.set()
            assert resume_retry.wait(timeout=30)

    def archive():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            archive_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            archive_started.set()
            connection.execute(
                text(
                    "UPDATE public.patients SET archived=true, "
                    "lifecycle_revision=lifecycle_revision+1 WHERE id=:patient"
                ),
                {"patient": patient},
            )
            archive_updated.set()
            assert release_archive.wait(timeout=30)

    def retry_after_first_receipt_miss():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            retry_thread.append(get_ident())
            retry_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            return book_appointment(
                connection,
                _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                BookAppointment.model_validate(body),
                key,
            )

    event.listen(clinic.runtime, "after_cursor_execute", hold_first_receipt_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with transaction(clinic.runtime, clinic.tenant) as holder:
                saved = book_appointment(
                    holder,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(body),
                    key,
                )
                holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
                archive_future = pool.submit(archive)
                assert archive_started.wait(timeout=15)
                with transaction(clinic.runtime, clinic.tenant) as observer:
                    wait_for_postgres_lock(observer, archive_pid[0], holder_pid)
                replay_future = pool.submit(retry_after_first_receipt_miss)
                assert receipt_read.wait(timeout=15)
            assert archive_updated.wait(timeout=15)
            resume_retry.set()
            with transaction(clinic.runtime, clinic.tenant) as observer:
                wait_for_postgres_lock(observer, retry_pid[0], archive_pid[0])
            release_archive.set()
            archive_future.result(timeout=30)
            assert replay_future.result(timeout=30) == saved
    finally:
        resume_retry.set()
        release_archive.set()
        event.remove(clinic.runtime, "after_cursor_execute", hold_first_receipt_read)
    status, replay = booking_request(clinic, clinic.owner, body=body, key=key)
    assert status == 201 and replay["data"] == saved
    assert replay["revision"] == saved["revision"]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.scalar(
            text("SELECT archived FROM public.patients WHERE id=:patient"),
            {"patient": patient},
        )
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_signed_retry_waits_behind_archive_then_replays_committed_booking(
    booking_clinic,
):
    """Signed retry queues at the organization guard before replaying after archive."""
    clinic = booking_clinic
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    patient, key = new_patient(clinic), str(uuid4())
    body = booking_body(patient)
    archive_started = Event()
    signed_started = Event()
    receipt_reached = Event()
    release_receipt = Event()
    archive_pid = []
    signed_pid = []

    def observe_signed_sql(
        connection, _cursor, statement, _parameters, _context, _many
    ):
        pid = connection.connection.driver_connection.info.backend_pid
        if "public.lock_active_organization" in statement:
            signed_pid.append(pid)
            signed_started.set()
        if (
            signed_pid
            and pid == signed_pid[0]
            and "FROM public.idempotency_receipts AS r" in statement
        ):
            receipt_reached.set()
            assert release_receipt.wait(timeout=30)

    def archive():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            archive_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            archive_started.set()
            connection.execute(
                text(
                    "UPDATE public.patients SET archived=true, "
                    "lifecycle_revision=lifecycle_revision+1 WHERE id=:patient"
                ),
                {"patient": patient},
            )

    def signed_retry():
        return booking_request(clinic, clinic.owner, body=body, key=key)

    event.listen(clinic.runtime, "before_cursor_execute", observe_signed_sql)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with transaction(clinic.runtime, clinic.tenant) as holder:
                saved = book_appointment(
                    holder,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(body),
                    key,
                )
                holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
                archive_future = pool.submit(archive)
                assert archive_started.wait(timeout=15)
                with transaction(clinic.runtime, clinic.tenant) as observer:
                    wait_for_postgres_lock(observer, archive_pid[0], holder_pid)
                retry_future = pool.submit(signed_retry)
                assert signed_started.wait(timeout=15)
                with transaction(clinic.runtime, clinic.tenant) as observer:
                    wait_for_postgres_lock(observer, signed_pid[0], holder_pid)
                assert not receipt_reached.is_set()
            archive_future.result(timeout=30)
            assert receipt_reached.wait(timeout=15)
            release_receipt.set()
            status, replay = retry_future.result(timeout=30)
    finally:
        release_receipt.set()
        event.remove(clinic.runtime, "before_cursor_execute", observe_signed_sql)
    assert status == 201 and replay["data"] == saved
    assert replay["revision"] == saved["revision"]
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert connection.scalar(
            text("SELECT archived FROM public.patients WHERE id=:patient"),
            {"patient": patient},
        )
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_distinct_keys_same_patient_day_have_one_winner_with_no_number_gap(
    booking_clinic,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    outcomes = _concurrent(
        lambda: _attempt(clinic, patient), lambda: _attempt(clinic, patient)
    )
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    denied = [outcome for outcome in outcomes if isinstance(outcome, ContractError)]
    assert len(winners) == len(denied) == 1
    assert denied[0].status_code == 409
    assert winners[0]["number"] == 1
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


def test_distinct_patients_race_for_unique_sequential_day_numbers(booking_clinic):
    clinic = booking_clinic
    patients = [new_patient(clinic) for _ in range(2)]
    outcomes = _concurrent(
        *[lambda patient=patient: _attempt(clinic, patient) for patient in patients]
    )
    assert all(isinstance(outcome, dict) for outcome in outcomes), outcomes
    assert {outcome["number"] for outcome in outcomes} == {1, 2}
    assert {outcome["patient_id"] for outcome in outcomes} == set(map(str, patients))
    assert booking_effects(clinic) == (2, 1, 2, 2, 2, 2, 2, 2)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT last_number FROM public.appointment_day_counters")
            )
            == 2
        )
        assert set(
            connection.scalars(
                text("SELECT number FROM public.appointment_number_assignments")
            )
        ) == {1, 2}


@pytest.mark.parametrize("archive_first", [True, False])
def test_archive_and_booking_serialize_for_both_commit_orders(
    booking_clinic,
    archive_first,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    ready = Event()
    waiter = []

    def compete():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiter.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            if archive_first:
                return book_appointment(
                    connection,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            connection.execute(
                text("""
                UPDATE public.patients SET archived=true,
                    lifecycle_revision=lifecycle_revision+1 WHERE id=:id
            """),
                {"id": patient},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if archive_first:
                holder.execute(
                    text("""
                    UPDATE public.patients SET archived=true,
                        lifecycle_revision=lifecycle_revision+1 WHERE id=:id
                """),
                    {"id": patient},
                )
            else:
                saved = book_appointment(
                    holder,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            future = pool.submit(compete)
            assert ready.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                wait_for_postgres_lock(observer, waiter[0], holder_pid)
        if archive_first:
            with pytest.raises(ContractError) as denied:
                future.result(timeout=30)
            assert denied.value.status_code == 409
            assert booking_effects(clinic) == (0, 0, 0, 0, 0, 0, 0, 0)
        else:
            future.result(timeout=30)
            assert saved["number"] == 1
            assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT archived FROM public.patients WHERE id=:id"),
                {"id": patient},
            )
            is True
        )


def _reprice(connection, clinic):
    raw = connection.scalar(
        text("""
        SELECT v.payload::text FROM public.clinic_policy_heads h
        JOIN public.clinic_policy_versions v
          ON (h.organization_id,h.kind,h.current_version)
           = (v.organization_id,v.kind,v.version)
        WHERE h.kind='PRICE'
    """)
    )
    payload = json.loads(raw)
    payload["standard_new_exam_egp"] = "150.00"
    append_version(connection, clinic.tenant, "PRICE", payload, 2)


@pytest.mark.parametrize("price_first", [True, False])
def test_price_policy_and_booking_freeze_only_confirmed_revision(
    booking_clinic,
    price_first,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    ready = Event()
    waiter = []

    def compete():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiter.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            if price_first:
                return book_appointment(
                    connection,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            _reprice(connection, clinic)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if price_first:
                _reprice(holder, clinic)
            else:
                booked = book_appointment(
                    holder,
                    _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            future = pool.submit(compete)
            assert ready.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                wait_for_postgres_lock(observer, waiter[0], holder_pid)
        if price_first:
            with pytest.raises(ContractError) as denied:
                future.result(timeout=30)
            assert denied.value.status_code == 409
            assert booking_effects(clinic) == (0, 0, 0, 0, 0, 0, 0, 0)
        else:
            future.result(timeout=30)
            assert booked["net_egp"] == "100.00"
            assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("activation_first", [True, False])
def test_schedule_activation_and_booking_serialize_on_first_date(
    booking_clinic,
    activation_first,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    actor = _actor(clinic.tenant, UUID(clinic.owner.staff_id))
    with transaction(clinic.runtime, clinic.tenant) as connection:
        preview = _stage(
            connection, actor, _config(mode="OPEN", working_weekdays=[]), revision=2
        )
    ready = Event()
    waiter = []

    def compete():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            waiter.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            if activation_first:
                return book_appointment(
                    connection,
                    actor,
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            return _activate(connection, actor, preview)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as holder:
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            if activation_first:
                _activate(holder, actor, preview)
            else:
                booked = book_appointment(
                    holder,
                    actor,
                    BookAppointment.model_validate(booking_body(patient)),
                    str(uuid4()),
                )
            future = pool.submit(compete)
            assert ready.wait(timeout=15)
            with transaction(clinic.runtime, clinic.tenant) as observer:
                wait_for_postgres_lock(observer, waiter[0], holder_pid)
        with pytest.raises(ContractError) as denied:
            future.result(timeout=30)
        assert denied.value.status_code == 409
    assert booking_effects(clinic) == (
        (0, 0, 0, 0, 0, 0, 0, 0) if activation_first else (1, 1, 1, 1, 1, 1, 1, 1)
    )
    if not activation_first:
        assert booked["number"] == 1


def test_two_timed_confirmations_for_same_candidate_cannot_overbook(booking_clinic):
    clinic = booking_clinic
    with transaction(clinic.runtime, clinic.tenant) as connection:
        _publish(
            connection,
            _actor(clinic.tenant, UUID(clinic.owner.staff_id)),
            _config(mode="EXACT_TIME"),
            revision=2,
        )
        slot = candidates_for_date(
            load_current_schedule(connection, clinic.tenant), SERVICE_DAY
        )[0]
    patients = [new_patient(clinic) for _ in range(2)]
    endpoints = {
        "expected_schedule_revision": 3,
        "starts_at": slot.starts_at.isoformat(),
        "ends_at": slot.ends_at.isoformat(),
    }
    outcomes = _concurrent(
        *[
            lambda patient=patient: _attempt(clinic, patient, **endpoints)
            for patient in patients
        ]
    )
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    denied = [outcome for outcome in outcomes if isinstance(outcome, ContractError)]
    assert len(winners) == len(denied) == 1
    assert denied[0].status_code == 409
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 1)
