"""T060: real signed B-19 contention, lost replies and transactional rollback."""

from contextlib import contextmanager
from datetime import date, timedelta
from uuid import uuid4

import pytest
from carepulse.appointments.bulk_preview import (
    BulkCommitCommand,
    BulkPreviewCommand,
    preview_bulk_reschedule,
)
from carepulse.appointments.bulk_reschedule import commit_bulk_reschedule
from carepulse.visits.eligibility import extend_followup
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_decisions import _decision, _extension
from test_followups import claim_record
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import admit_body
from test_w3_admission_controls import _capacity_body
from test_w3_admission_races import _admission, _policy
from test_w3_bulk import (
    added,
    appointment_row,
    assert_batch,
    bulk_request,
    commit_body,
    denied,
    interval,
    partition,
    preview_body,
    preview_selection,
    set_capacity,
    timed_body,
    timed_book,
)
from test_w3_cancel_replace import _appointment, book_case, cancel_body, change_request
from test_w3_cancel_replace_races import _checkpoint, _observe_waiter
from test_w3_checkin_races import _archive, _ordered_race
from test_w3_intake import _policy_revision, _today, set_intake_schedule
from test_w3_intake import intake_clinic as intake_clinic  # noqa: PLC0414
from test_w3_reschedule import move_body
from test_w3_reschedule_races import (
    _confirmed_claim,
    _extended_claim,
    booking_transaction,
    move_transaction,
)
from test_w3_visit_races import _visit_command
from test_w3_visits import effects


@contextmanager
def bulk_transaction(clinic, actor, kind, body, key):
    schema, service = (
        (BulkPreviewCommand, preview_bulk_reschedule)
        if kind == "preview"
        else (BulkCommitCommand, commit_bulk_reschedule)
    )
    with signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path=f"/v1/appointments/bulk-reschedule/{kind}",
        purpose=f"APPOINTMENT_BULK_{kind.upper()}",
        body=body,
        key=key,
    ) as (connection, authorized):
        yield (
            connection,
            lambda: service(connection, authorized, schema.model_validate(body), key),
        )


def batch_race(clinic, bulk, competitor, bulk_first, *, losing=True):
    before = effects(clinic)
    observed = []
    winner, waiter = (bulk, competitor) if bulk_first else (competitor, bulk)
    first, second = _ordered_race(
        clinic,
        winner,
        lambda: _observe_waiter(clinic, waiter, observed),
        denial="CONFLICT" if losing else None,
        checkpoint=_checkpoint(clinic, before),
    )
    after = effects(clinic)
    assert len(observed) == 1
    if losing:
        assert observed == [after]
    return (first, second, before, after, observed[0])


def open_book_on(clinic, day):
    status, reply = booking_request(
        clinic,
        clinic.owner,
        body=booking_body(
            new_patient(clinic),
            day=day,
            expected_price_revision=_policy_revision(clinic, "PRICE"),
            expected_schedule_revision=_policy_revision(clinic, "SCHEDULE"),
        ),
        key=uuid4().hex,
    )
    assert status == 201, reply
    return reply["data"]["id"]


def stable_exclusion(clinic, day):
    target = open_book_on(clinic, day)
    status, reply = change_request(
        clinic, clinic.owner, target, "cancel", cancel_body(clinic, target)
    )
    assert status == 200, reply
    return target


@pytest.mark.parametrize("bulk_first", [True, False])
@pytest.mark.parametrize("competitor", ["admit", "reschedule", "archive"])
def test_bulk_commit_serializes_with_included_row_changes(
    intake_clinic, bulk_first, competitor
):
    clinic = intake_clinic
    day, destination = _today(), _today() + timedelta(days=1)
    target = book_case(clinic, waiting=competitor == "admit")
    independent = book_case(clinic)
    excluded = stable_exclusion(clinic, day)
    actor = clinic.owner
    preview = preview_selection(
        clinic, actor, preview_body([target, independent, excluded], day, destination)
    )
    assert partition(preview) == (
        {target, independent},
        {excluded: "appointment not eligible"},
    )
    key, other_key = uuid4().hex, uuid4().hex

    def bulk():
        return bulk_transaction(clinic, actor, "commit", commit_body(preview), key)

    if competitor == "admit":
        command = admit_body(
            clinic,
            {"appointment_id": target, "service_date": day.isoformat(), "lane": "MAIN"},
            physical_presence_confirmed=True,
        )

        def compete():
            return _admission(clinic, actor, command, other_key)
    elif competitor == "reschedule":
        command = move_body(clinic, target, day + timedelta(days=2))

        def compete():
            return move_transaction(clinic, actor, target, command, other_key)
    else:
        patient = _appointment(clinic, target)["patient_id"]

        def compete():
            return _archive(clinic, patient)

    both_succeed = bulk_first and competitor == "archive"
    first, _, before, after, winner = batch_race(
        clinic, bulk, compete, bulk_first, losing=not both_succeed
    )
    assert appointment_row(after, excluded) == appointment_row(before, excluded)
    if bulk_first:
        assert_batch(before, winner, actor, preview, first[0], key)
        assert (
            appointment_row(after, independent)["service_date"]
            == destination.isoformat()
        )
    else:
        assert appointment_row(after, independent) == appointment_row(
            before, independent
        )
        assert not any(
            row["idempotency_key"] == key for row in after["idempotency_receipts"]
        )
        assert not any(
            row["appointment_id"] == independent
            and row not in before["appointment_number_assignments"]
            for row in after["appointment_number_assignments"]
        )
    if competitor == "archive":
        assert (
            next(row for row in after["patients"] if row["id"] == str(patient))[
                "archived"
            ]
            is True
        )


@pytest.mark.parametrize("bulk_first", [True, False])
@pytest.mark.parametrize("competitor", ["restriction", "extension"])
def test_bulk_claim_authority_race(intake_clinic, bulk_first, competitor):
    clinic = intake_clinic
    source, target, destination, _ = _extended_claim(clinic)
    original = _appointment(clinic, target)["service_date"]
    independent = open_book_on(clinic, original)
    excluded = stable_exclusion(clinic, original)
    actor = clinic.receptionist
    preview = preview_selection(
        clinic,
        actor,
        preview_body([target, independent, excluded], original, destination),
    )
    assert partition(preview) == (
        {target, independent},
        {excluded: "appointment not eligible"},
    )
    assert claim_record(clinic, target)["state"] == "ACTIVE"
    key, other_key = uuid4().hex, uuid4().hex

    def bulk():
        return bulk_transaction(clinic, actor, "commit", commit_body(preview), key)

    if competitor == "restriction":
        command = _decision(
            "OPTED_OUT",
            reason="Synthetic later Doctor restriction",
            followup_revision=_policy_revision(clinic, "FOLLOWUP"),
        )

        def compete():
            return _visit_command(
                clinic,
                clinic.owner,
                source["visit"],
                "source",
                command.model_dump(mode="json", exclude_unset=True),
                other_key,
            )
    else:
        command = _extension(
            source,
            target,
            deadline=destination + timedelta(days=1),
            followup_revision=_policy_revision(clinic, "FOLLOWUP"),
            authorization_revision=move_body(clinic, target, destination)[
                "expected_authorization_revision"
            ],
        )

        @contextmanager
        def compete():
            with signed_test_transaction(
                clinic,
                clinic.owner,
                method="POST",
                path=f"/v1/visits/{source['visit']}/extend-followup",
                purpose="VISIT_FOLLOWUP",
                body=command.model_dump(mode="json", exclude_unset=True),
                key=other_key,
            ) as (connection, authorized):
                yield (
                    connection,
                    lambda: extend_followup(
                        connection, authorized, source["visit"], command, other_key
                    ),
                )

    both_succeed = bulk_first and competitor == "restriction"
    first, _, before, after, winner = batch_race(
        clinic, bulk, compete, bulk_first, losing=not both_succeed
    )
    assert after["appointment_price_snapshots"] == before["appointment_price_snapshots"]
    assert after["followup_claims"] == before["followup_claims"]
    assert appointment_row(after, excluded) == appointment_row(before, excluded)
    if bulk_first:
        assert_batch(before, winner, actor, preview, first[0], key)
    else:
        assert appointment_row(after, target) == appointment_row(before, target)
        assert appointment_row(after, independent) == appointment_row(
            before, independent
        )
        assert (
            after["appointment_number_assignments"]
            == before["appointment_number_assignments"]
        )
        assert not any(
            row["idempotency_key"] == key for row in after["idempotency_receipts"]
        )


def timed_cohort(clinic, capacity, occupied=False):
    original, destination = _today() + timedelta(days=1), _today() + timedelta(days=2)
    excluded = stable_exclusion(clinic, original)
    set_capacity(clinic, capacity)
    set_intake_schedule(
        clinic,
        mode="EXACT_TIME",
        periods=(("09:00", "10:00"),),
        duration=15,
        overlap=True,
    )
    candidates = [
        timed_book(clinic, original, "09:00", "09:15", overlap=True) for _ in range(2)
    ]
    touching = timed_book(clinic, original, "09:15", "09:30", overlap=True)
    if occupied:
        timed_book(clinic, destination, "09:00", "09:15", overlap=True)
    return (
        preview_body([*candidates, touching, excluded], original, destination),
        candidates,
        touching,
        excluded,
    )


def assert_timed_partition(preview, candidates, touching, excluded, *, conflicted):
    expected = {excluded: "appointment not eligible"}
    included = {touching}
    if conflicted:
        expected.update(dict.fromkeys(candidates, "destination capacity conflict"))
    else:
        included.update(candidates)
    assert partition(preview) == (included, expected)
    for row in preview["included"]:
        clocks = (
            ("09:15", "09:30")
            if row["appointment_id"] == touching
            else ("09:00", "09:15")
        )
        assert row["source_local_start"] == clocks[0] + ":00"
        assert row["source_local_end"] == clocks[1] + ":00"
        assert {key: row[key] for key in ("starts_at", "ends_at")} == interval(
            date.fromisoformat(preview["destination_date"]), *clocks
        )


@pytest.mark.parametrize("bulk_first", [True, False])
@pytest.mark.parametrize("stage", ["preview", "commit"])
def test_destination_booking_serializes_with_preview_and_commit(
    intake_clinic, bulk_first, stage
):
    clinic = intake_clinic
    body, candidates, touching, excluded = timed_cohort(clinic, 2)
    actor = clinic.owner
    destination = date.fromisoformat(body["destination_date"])
    booking = timed_body(clinic, destination, "09:00", "09:15", overlap=True)
    bulk_key, book_key = uuid4().hex, uuid4().hex
    if stage == "commit":
        preview = preview_selection(clinic, actor, body)
        assert_timed_partition(
            preview, candidates, touching, excluded, conflicted=False
        )
        command = commit_body(preview)
    else:
        command = body

    def bulk():
        return bulk_transaction(clinic, actor, stage, command, bulk_key)

    def book():
        return booking_transaction(clinic, booking, book_key)

    first, second, before, after, winner = batch_race(
        clinic, bulk, book, bulk_first, losing=stage == "commit"
    )
    assert appointment_row(after, excluded) == appointment_row(before, excluded)
    if stage == "preview":
        stored = (first if bulk_first else second)[0]
        assert_timed_partition(
            stored, candidates, touching, excluded, conflicted=not bulk_first
        )
        if bulk_first:
            denied(clinic, actor, "commit", commit_body(stored), 409)
            stored = preview_selection(clinic, actor, body)
            assert_timed_partition(
                stored, candidates, touching, excluded, conflicted=True
            )
        before_commit = effects(clinic)
        key = uuid4().hex
        status, reply = bulk_request(
            clinic, actor, "commit", commit_body(stored), key=key
        )
        assert status == 200, reply
        committed = effects(clinic)
        assert_batch(before_commit, committed, actor, stored, reply["data"], key)
        assert bulk_request(clinic, actor, "commit", commit_body(stored), key=key) == (
            200,
            reply,
        )
        assert effects(clinic) == committed
    elif bulk_first:
        assert_batch(before, winner, actor, preview, first[0], bulk_key)
        assert not any(
            row["idempotency_key"] == book_key for row in after["idempotency_receipts"]
        )
    else:
        for identifier in [*candidates, touching]:
            assert appointment_row(after, identifier) == appointment_row(
                before, identifier
            )
        assert not any(
            row["idempotency_key"] == bulk_key for row in after["idempotency_receipts"]
        )
        fresh = preview_selection(clinic, actor, body)
        assert_timed_partition(fresh, candidates, touching, excluded, conflicted=True)
        key = uuid4().hex
        before_commit = effects(clinic)
        status, reply = bulk_request(
            clinic, actor, "commit", commit_body(fresh), key=key
        )
        assert status == 200, reply
        assert_batch(before_commit, effects(clinic), actor, fresh, reply["data"], key)


@pytest.mark.parametrize("bulk_first", [True, False])
@pytest.mark.parametrize("occupied", [True, False])
def test_bulk_commit_and_capacity_publication_both_orders(
    intake_clinic, bulk_first, occupied
):
    clinic = intake_clinic
    body, candidates, touching, excluded = timed_cohort(clinic, 3, occupied=occupied)
    actor = clinic.owner
    preview = preview_selection(clinic, actor, body)
    assert_timed_partition(preview, candidates, touching, excluded, conflicted=False)
    key, policy_key = uuid4().hex, uuid4().hex
    capacity = _capacity_body(clinic, 2)

    def bulk():
        return bulk_transaction(clinic, actor, "commit", commit_body(preview), key)

    def publish():
        return _policy(clinic, actor, capacity, policy_key)

    first, _, before, after, winner = batch_race(
        clinic, bulk, publish, bulk_first, losing=not bulk_first or occupied
    )
    assert appointment_row(after, excluded) == appointment_row(before, excluded)
    if bulk_first:
        assert_batch(before, winner, actor, preview, first[0], key)
    else:
        for identifier in [*candidates, touching]:
            assert appointment_row(after, identifier) == appointment_row(
                before, identifier
            )
        assert (
            after["appointment_number_assignments"]
            == before["appointment_number_assignments"]
        )
        assert not any(
            row["idempotency_key"] == key for row in after["idempotency_receipts"]
        )
        fresh = preview_selection(clinic, actor, body)
        assert_timed_partition(
            fresh, candidates, touching, excluded, conflicted=occupied
        )
        retry_key = uuid4().hex
        before_commit = effects(clinic)
        status, reply = bulk_request(
            clinic, actor, "commit", commit_body(fresh), key=retry_key
        )
        assert status == 200, reply
        assert_batch(
            before_commit, effects(clinic), actor, fresh, reply["data"], retry_key
        )
    assert _policy_revision(clinic, "CAPACITY") == capacity[
        "expected_capacity_revision"
    ] + (not bulk_first or not occupied)


@pytest.mark.parametrize("kind", ["preview", "commit"])
def test_duplicate_signed_bulk_commands_recover_exact_original_receipt(
    intake_clinic, kind
):
    clinic = intake_clinic
    actor, day = clinic.owner, _today()
    target, other = book_case(clinic), book_case(clinic, waiting=True)
    body = preview_body([target, other], day, day + timedelta(days=1))
    if kind == "commit":
        preview = preview_selection(clinic, actor, body)
        body = commit_body(preview)
    key = uuid4().hex

    def command():
        return bulk_transaction(clinic, actor, kind, body, key)

    first, second, before, after, winner = batch_race(
        clinic, command, command, True, losing=False
    )
    assert first == second
    assert winner == after
    assert len(added(before, after, "idempotency_receipts", 1)) == 1
    if kind == "commit":
        assert_batch(before, after, actor, preview, first[0], key)
    else:
        assert partition(first[0]) == ({target, other}, {})
        assert all(
            before[table] == after[table]
            for table in before
            if table != "idempotency_receipts"
        )
    assert bulk_request(clinic, actor, kind, body, key=key) == (
        200,
        {"data": first[0], "revision": first[1]},
    )
    assert effects(clinic) == after


def test_bulk_failure_after_first_tentative_move_rolls_back_all_effects(intake_clinic):
    clinic = intake_clinic
    _, target = _confirmed_claim(clinic)
    original = _appointment(clinic, target)["service_date"]
    other = open_book_on(clinic, original)
    destination = original + timedelta(days=1)
    actor = clinic.owner
    preview = preview_selection(
        clinic, actor, preview_body([target, other], original, destination)
    )
    assert partition(preview) == ({target, other}, {})
    assert claim_record(clinic, target)["state"] == "ACTIVE"
    before = effects(clinic)
    marker = "synthetic_b19_" + uuid4().hex
    # Fail on the second history insertion only after the first included move is visible inside the writer.
    with clinic.migration.begin() as connection:
        connection.execute(
            text(f"""
            CREATE FUNCTION public.{marker}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.organization_id='{clinic.tenant}'::uuid AND NEW.action='RESCHEDULE'
                    AND NEW.appointment_id IN ('{target}'::uuid,'{other}'::uuid)
                    AND EXISTS (
                        SELECT 1 FROM public.appointment_history h JOIN public.appointments a
                            ON a.organization_id=h.organization_id AND a.id=h.appointment_id
                        WHERE h.organization_id=NEW.organization_id AND h.appointment_id<>NEW.appointment_id
                            AND h.appointment_id IN ('{target}'::uuid,'{other}'::uuid)
                            AND h.action='RESCHEDULE' AND h.new_service_date='{destination}'::date
                            AND a.service_date=h.new_service_date
                    ) THEN
                    RAISE EXCEPTION 'synthetic failure after first included move' USING ERRCODE='P0199';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER {marker} BEFORE INSERT ON public.appointment_history
                FOR EACH ROW EXECUTE FUNCTION public.{marker}()
        """)
        )
    key = uuid4().hex
    try:
        with (
            pytest.raises(DBAPIError) as failure,
            bulk_transaction(clinic, actor, "commit", commit_body(preview), key) as (
                _,
                execute,
            ),
        ):
            execute()
        assert failure.value.orig.sqlstate == "P0199"
        assert effects(clinic) == before
    finally:
        with clinic.migration.begin() as connection:
            connection.execute(
                text(f"DROP TRIGGER {marker} ON public.appointment_history")
            )
            connection.execute(text(f"DROP FUNCTION public.{marker}()"))
    status, reply = bulk_request(clinic, actor, "commit", commit_body(preview), key=key)
    assert status == 200, reply
    after = effects(clinic)
    assert_batch(before, after, actor, preview, reply["data"], key)
    assert bulk_request(clinic, actor, "commit", commit_body(preview), key=key) == (
        200,
        reply,
    )
    assert effects(clinic) == after
