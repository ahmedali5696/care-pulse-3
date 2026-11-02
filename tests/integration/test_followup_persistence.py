"""D-08 durable preferential claims, lifecycle, tenant isolation and grants."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import (
    _admit_saved,
    _draft,
    _patient,
    _query,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414


def _source(connection, tenant, doctor, *, classification="ROUTINE", complete=True):
    """Use the D-07 admission history and a real Doctor decision as source evidence."""
    patient = _patient(connection, tenant)
    saved = persist_appointment(
        connection,
        _draft(
            patient,
            doctor,
            classification=classification,
            lane="EMERGENCY" if classification == "EMERGENCY" else "MAIN",
            price_snapshot=(
                {"base_egp": "120.00", "net_egp": "120.00"}
                if classification == "URGENT"
                else {}
            ),
        ),
        _query(tenant, doctor),
    )
    _admit_saved(connection, tenant, doctor, saved, complete=complete)
    appointment = UUID(saved["id"])
    visit = connection.scalar(
        text("SELECT id FROM public.visits WHERE appointment_id=:id"),
        {"id": appointment},
    )
    connection.execute(
        text("SELECT set_config('app.clinical_change_kind', 'DECISION_DEFAULT', true)")
    )
    connection.execute(text("SELECT set_config('app.clinical_reason', '', true)"))
    connection.execute(
        text("""
            UPDATE public.visits SET followup_decision=CAST(:decision AS jsonb)
            WHERE id=:visit
        """),
        {"decision": '{"kind":"DEFAULT","eligible":true}', "visit": visit},
    )
    completion_day = connection.scalar(
        text("""
            SELECT (h.occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history h
            WHERE h.appointment_id=:id AND h.new_status='Completed'
        """),
        {"id": appointment},
    )
    if completion_day is None:
        completion_day = connection.scalar(
            text("SELECT (clock_timestamp() AT TIME ZONE 'Africa/Cairo')::date")
        )
    return {
        "patient": patient,
        "doctor": doctor,
        "appointment": appointment,
        "visit": visit,
        "day": completion_day,
    }


def _provenance(source, *, version=1):
    return {
        "source_visit_id": str(source["visit"]),
        "source_revision": 1,
        "doctor_staff_id": str(source["doctor"]),
        "actor_staff_id": str(source["doctor"]),
        "authorized": True,
        "policy_revision": version,
    }


def _insert_claim(
    connection,
    tenant,
    source,
    appointment,
    provenance,
    *,
    patient=None,
    doctor=None,
    source_visit=None,
    source_appointment=None,
    state="ACTIVE",
    completion_day=None,
    deadline=None,
):
    claim_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.followup_claims
                (id, organization_id, appointment_id, patient_id, doctor_staff_id,
                 source_visit_id, source_appointment_id, policy_snapshot,
                 completion_local_date, approved_deadline, state)
            VALUES (:id, :tenant, :appointment, :patient, :doctor, :visit,
                    :source_appointment, CAST(:policy AS jsonb), :completed,
                    :deadline, :state)
        """),
        {
            "id": claim_id,
            "tenant": tenant,
            "appointment": appointment,
            "patient": patient or source["patient"],
            "doctor": doctor or source["doctor"],
            "visit": source_visit or source["visit"],
            "source_appointment": source_appointment or source["appointment"],
            "policy": json.dumps(provenance),
            "completed": completion_day or source["day"],
            "deadline": deadline or source["day"] + timedelta(days=7),
            "state": state,
        },
    )
    return claim_id


def _claim(
    connection,
    tenant,
    source,
    *,
    day_offset=1,
    patient=None,
    doctor=None,
    provenance=None,
    price="100.00",
    source_visit=None,
    source_appointment=None,
    completion_day=None,
    deadline=None,
):
    """Confirm an actual D-05 priced Appointment and its D-08 claim atomically."""
    patient = patient or source["patient"]
    doctor = doctor or source["doctor"]
    provenance = provenance or _provenance(source)
    saved = persist_appointment(
        connection,
        _draft(
            patient,
            doctor,
            service_date=source["day"] + timedelta(days=day_offset),
            service_type="FOLLOW_UP",
            price_snapshot={
                "base_egp": price,
                "net_egp": price,
                "entitlement_provenance": provenance,
            },
        ),
        _query(tenant, doctor),
    )
    claim_id = _insert_claim(
        connection,
        tenant,
        source,
        UUID(saved["id"]),
        provenance,
        patient=patient,
        doctor=doctor,
        source_visit=source_visit,
        source_appointment=source_appointment,
        completion_day=completion_day,
        deadline=deadline,
    )
    return {"id": UUID(saved["id"]), "saved": saved, "claim": claim_id}


def _outcome(
    connection,
    tenant,
    doctor,
    record,
    *,
    status,
    after_admission=False,
    update_claim=True,
):
    """Write a real terminal history; claim transition shares the transaction."""
    if after_admission or status == "Completed":
        _admit_saved(
            connection,
            tenant,
            doctor,
            record["saved"],
            complete=status == "Completed",
        )
    previous = (
        "In Examination" if (after_admission or status == "Completed") else "Booked"
    )
    if status == "Completed":
        revision = 3
    else:
        revision = 3 if after_admission else 1
        connection.execute(
            text("""
                UPDATE public.appointments
                SET status=:status, revision=:revision
                WHERE organization_id=:tenant AND id=:id
            """),
            {
                "status": status,
                "revision": revision,
                "tenant": tenant,
                "id": record["id"],
            },
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision,
                     action, old_status, new_status, old_service_date, new_service_date,
                     old_number, new_number, actor_staff_id, actor_role,
                     reason, price_snapshot_appointment_id)
                SELECT :history, organization_id, id, :revision, :action,
                       :previous, :status, service_date, service_date,
                       current_number, current_number, :doctor, 'DOCTOR_OWNER',
                       'Synthetic D-08 outcome', id
                FROM public.appointments WHERE organization_id=:tenant AND id=:id
            """),
            {
                "history": uuid4(),
                "revision": revision,
                "action": "DOCTOR_EXCEPTION" if after_admission else "STATUS",
                "previous": previous,
                "status": status,
                "doctor": doctor,
                "tenant": tenant,
                "id": record["id"],
            },
        )
    terminal_at = connection.scalar(
        text("""
            SELECT occurred_at FROM public.appointment_history
            WHERE organization_id=:tenant AND appointment_id=:id
              AND appointment_revision=:revision
        """),
        {"tenant": tenant, "id": record["id"], "revision": revision},
    )
    if update_claim:
        connection.execute(
            text("""
                UPDATE public.followup_claims
                SET state=:state, revision=revision+1,
                    terminal_history_revision=:revision, terminal_at=:at
                WHERE organization_id=:tenant AND id=:claim
            """),
            {
                "state": "CONSUMED" if status == "Completed" else "RELEASED",
                "revision": revision,
                "at": terminal_at,
                "tenant": tenant,
                "claim": record["claim"],
            },
        )
    return terminal_at, revision


def _doctor(migration, tenant):
    doctor, auth_user, email = (
        uuid4(),
        f"synthetic-followup-{uuid4()}",
        f"{uuid4()}@example.invalid",
    )
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public."user"
                    (id, name, email, "emailVerified", "createdAt", "updatedAt")
                VALUES (:user, 'Synthetic second Doctor', :email, true, now(), now())
            """),
            {"user": auth_user, "email": email},
        )
        connection.execute(
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
                VALUES (:id, :tenant, :user, :email, 'DOCTOR_OWNER', true, true)
            """),
            {"id": doctor, "tenant": tenant, "user": auth_user, "email": email},
        )
    return doctor


@pytest.mark.parametrize("classification", ["ROUTINE", "URGENT", "EMERGENCY"])
def test_completed_new_examination_sources_create_active_claim(
    appointment_storage, classification
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor, classification=classification)
        record = _claim(connection, tenant, source)
        stored = connection.execute(
            text("""
                SELECT c.state, c.revision, c.patient_id, c.doctor_staff_id,
                       c.source_visit_id, c.source_appointment_id,
                       c.policy_snapshot, c.completion_local_date, c.approved_deadline,
                       a.service_type, a.classification, p.entitlement_provenance
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id, a.id) =
                     (c.organization_id, c.appointment_id)
                JOIN public.appointment_price_snapshots p
                  ON (p.organization_id, p.appointment_id) =
                     (c.organization_id, c.appointment_id)
                WHERE c.id=:claim
            """),
            {"claim": record["claim"]},
        ).one()
        assert stored[:6] == (
            "ACTIVE",
            1,
            source["patient"],
            doctor,
            source["visit"],
            source["appointment"],
        )
        assert stored[6] == _provenance(source)
        assert stored[7:11] == (
            source["day"],
            source["day"] + timedelta(days=7),
            "FOLLOW_UP",
            "ROUTINE",
        )
        assert stored[11] == stored[6]
    with transaction(runtime, tenant) as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.followup_claims WHERE id=:id"),
                {"id": record["claim"]},
            )
            == 1
        )


@pytest.mark.parametrize(
    "bad_source", ["uncompleted", "exceptional_cancel", "followup"]
)
def test_nonqualifying_source_cannot_claim(appointment_storage, bad_source):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        if bad_source == "followup":
            original = _source(connection, tenant, doctor)
            prior = _claim(connection, tenant, original)
            _outcome(connection, tenant, doctor, prior, status="Completed")
            visit_id = connection.scalar(
                text("SELECT id FROM public.visits WHERE appointment_id=:id"),
                {"id": prior["id"]},
            )
            source = {
                "patient": original["patient"],
                "doctor": doctor,
                "appointment": prior["id"],
                "visit": visit_id,
                "day": connection.scalar(
                    text("""
                        SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
                        FROM public.appointment_history
                        WHERE appointment_id=:id AND new_status='Completed'
                    """),
                    {"id": prior["id"]},
                ),
            }
        else:
            source = _source(connection, tenant, doctor, complete=False)
            if bad_source == "exceptional_cancel":
                connection.execute(
                    text("""
                        UPDATE public.appointments
                        SET status='Cancelled', revision=3 WHERE id=:id
                    """),
                    {"id": source["appointment"]},
                )
                connection.execute(
                    text("""
                        INSERT INTO public.appointment_history
                            (id, organization_id, appointment_id, appointment_revision,
                             action, old_status, new_status, old_service_date,
                             new_service_date, old_number, new_number, actor_staff_id,
                             actor_role, reason, price_snapshot_appointment_id)
                        SELECT :history, organization_id, id, 3, 'DOCTOR_EXCEPTION',
                               'In Examination', 'Cancelled', service_date, service_date,
                               current_number, current_number, :doctor, 'DOCTOR_OWNER',
                               'Synthetic exceptional cancellation', id
                        FROM public.appointments WHERE id=:id
                    """),
                    {"history": uuid4(), "doctor": doctor, "id": source["appointment"]},
                )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(
                connection,
                tenant,
                source,
                day_offset=2 if bad_source == "followup" else 1,
            )
        assert denied.value.orig.sqlstate in ("23514", "23503")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize("wrong_relation", ["patient", "doctor", "clinic"])
def test_followup_target_must_match_source_tenant_patient_doctor(
    appointment_storage, wrong_relation
):
    runtime, migration, (tenant, other), (doctor, foreign_doctor) = appointment_storage
    second_doctor = _doctor(migration, tenant) if wrong_relation == "doctor" else None
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
    if wrong_relation == "clinic":
        with transaction(runtime, other) as connection:
            foreign_patient = _patient(connection, other)
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _claim(
                    connection,
                    other,
                    source,
                    patient=foreign_patient,
                    doctor=foreign_doctor,
                )
    else:
        with transaction(runtime, tenant) as connection:
            alternate_patient = (
                _patient(connection, tenant) if wrong_relation == "patient" else None
            )
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                _claim(
                    connection,
                    tenant,
                    source,
                    patient=alternate_patient,
                    doctor=second_doctor,
                )
    assert denied.value.orig.sqlstate in ("23514", "23503", "42501")


def test_one_active_source_across_different_service_dates(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        first = _claim(connection, tenant, source)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, day_offset=2)
        assert denied.value.orig.sqlstate == "23505"
        assert connection.execute(
            text("""
                SELECT id, state FROM public.followup_claims
                WHERE source_visit_id=:visit
            """),
            {"visit": source["visit"]},
        ).all() == [(first["claim"], "ACTIVE")]


@pytest.mark.parametrize("tamper", ["actor", "approval", "policy_revision"])
def test_claim_cannot_forge_frozen_authorization_evidence(appointment_storage, tamper):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        provenance = _provenance(source)
        if tamper == "actor":
            provenance["actor_staff_id"] = str(uuid4())
        elif tamper == "approval":
            provenance["authorized"] = False
        else:
            provenance["policy_revision"] = "unapproved"
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, provenance=provenance)
        assert denied.value.orig.sqlstate == "23514"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:id"
                ),
                {"id": source["visit"]},
            )
            == 0
        )


def test_consumed_source_cannot_regain_active_or_second_consumed_claim(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        first = _claim(connection, tenant, source)
        _outcome(connection, tenant, doctor, first, status="Completed")
    with transaction(runtime, tenant) as connection:
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, day_offset=2)
        assert denied.value.orig.sqlstate == "23505"
        assert connection.execute(
            text("""
                SELECT state, count(*) FROM public.followup_claims
                WHERE source_visit_id=:visit GROUP BY state
            """),
            {"visit": source["visit"]},
        ).all() == [("CONSUMED", 1)]


def test_released_prior_claim_cannot_become_second_consumption(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        first = _claim(connection, tenant, source)
        _outcome(connection, tenant, doctor, first, status="Cancelled")
        second = _claim(connection, tenant, source, day_offset=2)
        completed_at, completed_revision = _outcome(
            connection, tenant, doctor, second, status="Completed"
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.followup_claims
                    SET state='CONSUMED', revision=3,
                        terminal_history_revision=:revision, terminal_at=:at
                    WHERE id=:id
                """),
                {
                    "revision": completed_revision,
                    "at": completed_at,
                    "id": first["claim"],
                },
            )
        assert denied.value.orig.sqlstate == "23514"
        assert connection.execute(
            text("""
                SELECT state, count(*) FROM public.followup_claims
                WHERE source_visit_id=:visit GROUP BY state ORDER BY state
            """),
            {"visit": source["visit"]},
        ).all() == [("CONSUMED", 1), ("RELEASED", 1)]


@pytest.mark.parametrize("failure", ["missing_decision", "stale_source_revision"])
def test_source_authorization_and_revision_required(appointment_storage, failure):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        provenance = _provenance(source)
        if failure == "missing_decision":
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_change_kind', 'CORRECTION', true)"
                )
            )
            connection.execute(
                text(
                    "SELECT set_config('app.clinical_reason', 'Revoked approval', true)"
                )
            )
            connection.execute(
                text("UPDATE public.visits SET followup_decision=NULL WHERE id=:visit"),
                {"visit": source["visit"]},
            )
            provenance["source_revision"] = 2
        else:
            provenance["source_revision"] = 0
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _claim(connection, tenant, source, provenance=provenance)
        assert denied.value.orig.sqlstate == "23514"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


def test_released_history_allows_fresh_eligibility_and_price_snapshot(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        released = _claim(connection, tenant, source, price="100.00")
        _outcome(connection, tenant, doctor, released, status="Cancelled")
        connection.execute(
            text(
                "SELECT set_config('app.clinical_change_kind', 'DECISION_EXCEPTION', true)"
            )
        )
        connection.execute(
            text(
                "SELECT set_config('app.clinical_reason', 'Synthetic reviewed exception', true)"
            )
        )
        connection.execute(
            text("""
                UPDATE public.visits
                SET followup_decision=CAST(:decision AS jsonb)
                WHERE id=:visit
            """),
            {
                "decision": json.dumps(
                    {
                        "kind": "DEADLINE",
                        "deadline_date": (
                            source["day"] + timedelta(days=7)
                        ).isoformat(),
                        "reason": "Synthetic reviewed exception",
                    }
                ),
                "visit": source["visit"],
            },
        )
        fresh_provenance = {**_provenance(source, version=2), "source_revision": 2}
        fresh = _claim(
            connection,
            tenant,
            source,
            day_offset=2,
            provenance=fresh_provenance,
            price="125.00",
        )
        rows = connection.execute(
            text("""
                SELECT c.id, c.state, c.policy_snapshot, s.net_egp
                FROM public.followup_claims c
                JOIN public.appointment_price_snapshots s
                  ON (s.organization_id, s.appointment_id) =
                     (c.organization_id, c.appointment_id)
                WHERE c.source_visit_id=:visit ORDER BY c.revision DESC, c.id
            """),
            {"visit": source["visit"]},
        ).all()
        assert len(rows) == 2
        assert {row.id: row.state for row in rows} == {
            released["claim"]: "RELEASED",
            fresh["claim"]: "ACTIVE",
        }
        assert next(row.net_egp for row in rows if row.id == released["claim"]) == 100
        assert next(row.net_egp for row in rows if row.id == fresh["claim"]) == 125
        assert (
            next(row.policy_snapshot for row in rows if row.id == fresh["claim"])
            == fresh_provenance
        )


def test_claim_and_preferential_appointment_must_both_exist_at_commit(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
    with (
        pytest.raises(DBAPIError) as missing_claim,
        transaction(runtime, tenant) as connection,
    ):
        persist_appointment(
            connection,
            _draft(
                source["patient"],
                doctor,
                service_type="FOLLOW_UP",
                service_date=source["day"] + timedelta(days=1),
                price_snapshot={"entitlement_provenance": _provenance(source)},
            ),
            _query(tenant, doctor),
        )
    assert missing_claim.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        with pytest.raises(DBAPIError) as missing_target, connection.begin_nested():
            _insert_claim(connection, tenant, source, uuid4(), _provenance(source))
        assert missing_target.value.orig.sqlstate in ("23503", "23514")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE source_visit_id=:visit"
                ),
                {"visit": source["visit"]},
            )
            == 0
        )


@pytest.mark.parametrize(
    ("terminal", "admit"),
    [("Cancelled", False), ("No-show", False), ("Cancelled", True)],
)
def test_cancel_no_show_and_exceptional_admission_release_in_same_commit(
    appointment_storage, terminal, admit
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
        history_at, revision = _outcome(
            connection,
            tenant,
            doctor,
            record,
            status=terminal,
            after_admission=admit,
        )
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT a.status, a.revision, c.state, c.revision,
                       c.terminal_history_revision, c.terminal_at, h.occurred_at
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id, a.id) =
                     (c.organization_id, c.appointment_id)
                JOIN public.appointment_history h ON (h.organization_id,
                     h.appointment_id, h.appointment_revision) =
                     (c.organization_id, c.appointment_id, c.terminal_history_revision)
                WHERE c.id=:claim
            """),
            {"claim": record["claim"]},
        ).one() == (terminal, revision, "RELEASED", 2, revision, history_at, history_at)
        visit_count = connection.scalar(
            text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
            {"id": record["id"]},
        )
        assert visit_count == int(admit)


def test_completed_followup_consumes_once_and_retains_immutable_history(
    appointment_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
        history_at, revision = _outcome(
            connection, tenant, doctor, record, status="Completed"
        )
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT c.state, c.revision, c.terminal_history_revision,
                       c.terminal_at, a.status, h.action, h.new_status
                FROM public.followup_claims c
                JOIN public.appointments a ON (a.organization_id, a.id) =
                     (c.organization_id, c.appointment_id)
                JOIN public.appointment_history h ON (h.organization_id,
                     h.appointment_id, h.appointment_revision) =
                     (c.organization_id, c.appointment_id, c.terminal_history_revision)
                WHERE c.id=:claim
            """),
            {"claim": record["claim"]},
        ).one() == (
            "CONSUMED",
            2,
            revision,
            history_at,
            "Completed",
            "STATUS",
            "Completed",
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.visits WHERE appointment_id=:id"),
                {"id": record["id"]},
            )
            == 1
        )
        for mutation in (
            "UPDATE public.followup_claims SET state='RELEASED' WHERE id=:id",
            "UPDATE public.followup_claims SET policy_snapshot='{}'::jsonb WHERE id=:id",
            "DELETE FROM public.followup_claims WHERE id=:id",
        ):
            with pytest.raises(DBAPIError) as denied, connection.begin_nested():
                connection.execute(text(mutation), {"id": record["claim"]})
            assert denied.value.orig.sqlstate in ("42501", "23514", "23505")
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.appointment_history
                    SET reason='Changed completion evidence'
                    WHERE appointment_id=:id AND appointment_revision=:revision
                """),
                {"id": record["id"], "revision": revision},
            )
        assert denied.value.orig.sqlstate == "23514"
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_claims WHERE id=:id AND state='CONSUMED'"
                ),
                {"id": record["claim"]},
            )
            == 1
        )


def test_injected_failure_after_appointment_write_rolls_back_both_sides(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
    with transaction(runtime, tenant) as connection:
        with (
            pytest.raises(RuntimeError, match="synthetic write failure"),
            connection.begin_nested(),
        ):
            _outcome(
                connection,
                tenant,
                doctor,
                record,
                status="Cancelled",
                update_claim=False,
            )
            raise RuntimeError("synthetic write failure")
        assert connection.execute(
            text("""
                SELECT a.status, a.revision, c.state, c.revision
                FROM public.appointments a JOIN public.followup_claims c
                  ON (a.organization_id, a.id) = (c.organization_id, c.appointment_id)
                WHERE a.id=:id
            """),
            {"id": record["id"]},
        ).one() == ("Booked", 0, "ACTIVE", 1)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE appointment_id=:id"
                ),
                {"id": record["id"]},
            )
            == 1
        )


def test_unpaired_terminal_transition_cannot_commit(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        _outcome(
            connection,
            tenant,
            doctor,
            record,
            status="No-show",
            update_claim=False,
        )
    assert denied.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT a.status, c.state FROM public.appointments a
                JOIN public.followup_claims c ON (c.organization_id, c.appointment_id) =
                     (a.organization_id, a.id)
                WHERE a.id=:id
            """),
            {"id": record["id"]},
        ).one() == ("Booked", "ACTIVE")


def test_tenant_rls_no_context_operator_denied_and_narrow_runtime_grants(
    appointment_storage, role_engine
):
    runtime, migration, (tenant, other), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        record = _claim(connection, tenant, source)
        _outcome(connection, tenant, doctor, record, status="Completed")
    for scope in (other, None):
        with transaction(runtime, scope) as connection:
            for relation in ("followup_claims", "followup_extensions"):
                assert (
                    connection.scalar(text(f"SELECT count(*) FROM public.{relation}"))
                    == 0
                )
            assert (
                connection.execute(
                    text("""
                    UPDATE public.followup_claims SET state='RELEASED',
                           revision=revision+1
                    WHERE id=:claim RETURNING id
                """),
                    {"claim": record["claim"]},
                ).all()
                == []
            )
    operator = role_engine("carepulse_operator")
    try:
        for relation in ("followup_claims", "followup_extensions"):
            with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
                connection.execute(text(f"SELECT * FROM public.{relation}")).all()
            assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()
    with migration.connect() as connection:
        for relation in ("followup_claims", "followup_extensions"):
            relation_name = f"public.{relation}"
            assert connection.execute(
                text("""
                    SELECT relrowsecurity, relforcerowsecurity
                    FROM pg_class WHERE oid=CAST(:relation AS regclass)
                """),
                {"relation": relation_name},
            ).one() == (True, True)
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert (
                    connection.scalar(
                        text("""
                        SELECT has_table_privilege('carepulse_operator', :relation, :privilege)
                    """),
                        {"relation": relation_name, "privilege": privilege},
                    )
                    is False
                )
                if privilege != "DELETE":
                    assert (
                        connection.scalar(
                            text("""
                            SELECT has_any_column_privilege(
                                'carepulse_operator', :relation, :privilege
                            )
                        """),
                            {"relation": relation_name, "privilege": privilege},
                        )
                        is False
                    )
            assert (
                connection.scalar(
                    text("""
                    SELECT has_table_privilege('carepulse_runtime', :relation, 'DELETE')
                """),
                    {"relation": relation_name},
                )
                is False
            )
        for col in ("state", "revision", "terminal_at", "terminal_history_revision"):
            assert (
                connection.scalar(
                    text("""
                    SELECT has_column_privilege('carepulse_runtime',
                        'public.followup_claims', :column, 'UPDATE')
                """),
                    {"column": col},
                )
                is True
            )
        for col in (
            "policy_snapshot",
            "patient_id",
            "approved_deadline",
            "source_visit_id",
        ):
            assert (
                connection.scalar(
                    text("""
                    SELECT has_column_privilege('carepulse_runtime',
                        'public.followup_claims', :column, 'UPDATE')
                """),
                    {"column": col},
                )
                is False
            )
        assert (
            connection.scalar(
                text("""
                SELECT has_any_column_privilege(
                    'carepulse_runtime', 'public.followup_extensions', 'UPDATE'
                )
            """)
            )
            is False
        )


def test_d08_adds_no_lifecycle_or_fixture_api_routes():
    from carepulse.main import app

    route_set = {
        (method, route.path)
        for included in app.routes
        for route in getattr(
            getattr(included, "original_router", None), "routes", (included,)
        )
        if hasattr(route, "path")
        for method in getattr(route, "methods", set())
    }
    assert ("POST", "/v1/appointments") in route_set
    assert not any(
        path.startswith(("/v1/followups", "/v1/followup-claims", "/v1/fixtures"))
        or (
            path.startswith("/v1/appointments/")
            and method in ("PATCH", "POST", "DELETE")
        )
        for method, path in route_set
    )
