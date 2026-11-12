"""T168 direct PostgreSQL receipt-insert contention (not a signed insert race)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.db import transaction
from carepulse.identity.contracts import normalized_request_digest
from carepulse.identity.receipts import Receipt, ReceiptConflict, insert_tenant_receipt
from carepulse.main import app
from postgres_locks import wait_for_postgres_lock
from sqlalchemy import event, text
from test_booking import booking_body, booking_effects, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


def test_direct_receipt_unique_insert_wait_rollback_and_fresh_signed_recovery(
    booking_clinic,
):
    clinic = booking_clinic
    app.state.service_config, app.state.db_engine = clinic.config, clinic.runtime
    patient = new_patient(clinic)
    body = booking_body(patient)
    original = booking_request(clinic, clinic.owner, body=body, key=str(uuid4()))
    assert original[0] == 201
    key = str(uuid4())
    receipt = Receipt(
        actor_id=UUID(clinic.owner.staff_id),
        operation="appointments.book",
        idempotency_key=key,
        request_digest=normalized_request_digest(BookAppointment.model_validate(body)),
        result_projection=original[1]["data"],
        result_revision=original[1]["revision"],
        organization_id=clinic.tenant,
        staff_id=UUID(clinic.owner.staff_id),
    )
    inserted = Event()
    loser_started = Event()
    release_winner = Event()
    winner_pid = []
    loser_pid = []

    def observe_loser_insert(
        connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            "INSERT INTO public.idempotency_receipts" in statement
            and loser_pid
            and connection.connection.driver_connection.info.backend_pid == loser_pid[0]
        ):
            loser_started.set()

    def winner():
        with transaction(clinic.runtime, clinic.tenant) as connection:
            winner_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            insert_tenant_receipt(connection, receipt)
            inserted.set()
            assert release_winner.wait(timeout=30)

    def loser():
        assert inserted.wait(timeout=15)
        try:
            with transaction(clinic.runtime, clinic.tenant) as connection:
                loser_pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
                # Earlier same-transaction effects must roll back after losing the insert.
                updated = connection.execute(
                    text(
                        "UPDATE public.patients SET lifecycle_revision=lifecycle_revision+1 "
                        "WHERE id=:patient"
                    ),
                    {"patient": patient},
                )
                assert updated.rowcount == 1
                insert_tenant_receipt(connection, receipt)
        except ReceiptConflict:
            return "rolled_back"
        return "unexpected_insert"

    with transaction(clinic.runtime, clinic.tenant) as connection:
        revision_before = connection.scalar(
            text("SELECT lifecycle_revision FROM public.patients WHERE id=:patient"),
            {"patient": patient},
        )
    event.listen(clinic.runtime, "before_cursor_execute", observe_loser_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner_future = pool.submit(winner)
            try:
                assert inserted.wait(timeout=15)
                loser_future = pool.submit(loser)
                assert loser_started.wait(timeout=15)
                with transaction(clinic.runtime, clinic.tenant) as observer:
                    wait_for_postgres_lock(observer, loser_pid[0], winner_pid[0])
            finally:
                release_winner.set()
            winner_future.result(timeout=30)
            assert loser_future.result(timeout=30) == "rolled_back"
    finally:
        release_winner.set()
        event.remove(clinic.runtime, "before_cursor_execute", observe_loser_insert)

    # New signed request enforces current authorization and resource visibility.
    assert booking_request(clinic, clinic.owner, body=body, key=key) == original
    changed = {**body, "expected_price_revision": 999}
    status, conflict = booking_request(clinic, clinic.owner, body=changed, key=key)
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with transaction(clinic.runtime, clinic.tenant) as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT lifecycle_revision FROM public.patients WHERE id=:patient"
                ),
                {"patient": patient},
            )
            == revision_before
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts "
                    "WHERE operation='appointments.book' AND idempotency_key=:key"
                ),
                {"key": key},
            )
            == 1
        )
    # One legitimate booking plus its original receipt and one direct alias receipt.
    assert booking_effects(clinic) == (1, 1, 1, 1, 1, 1, 1, 2)
