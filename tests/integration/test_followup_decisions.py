"""B-10 Doctor decisions and exact confirmed-claim extensions on real PostgreSQL."""

from datetime import timedelta
from uuid import uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.identity.receipts import ReceiptConflict
from carepulse.visits.eligibility import extend_followup, record_followup_decision
from carepulse.visits.eligibility_contracts import ExtendFollowup, FollowupDecisionPatch
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_followup_persistence import _claim, _outcome, _source
from test_visit_persistence import _receptionist, _seed, _visit


def _actor(tenant, staff, role="DOCTOR_OWNER"):
    return AuthorizedStaff(tenant, staff, role, False, 0, 0, 0)


def _doctor_context(connection, staff):
    connection.execute(
        text("SELECT set_config('app.staff_id', :staff, true)"),
        {"staff": str(staff)},
    )


def _decision(kind, revision, *, deadline=None, reason=None):
    details = {"kind": kind}
    if deadline is not None:
        details["deadline_date"] = deadline.isoformat()
    if reason is not None:
        details["reason"] = reason
    return FollowupDecisionPatch.model_validate(
        {"followup_decision": details, "expected_visit_revision": revision}
    )


def _extension(source, target, *, revision=1, claim_revision=1, deadline=None):
    return ExtendFollowup.model_validate(
        {
            "appointment_id": str(target),
            "extended_deadline": (
                deadline or source["day"] + timedelta(days=9)
            ).isoformat(),
            "reason": "Synthetic Doctor extension",
            "expected_visit_revision": revision,
            "expected_claim_revision": claim_revision,
        }
    )


@pytest.mark.parametrize("kind", ["DEFAULT", "OPTED_OUT", "DEADLINE"])
def test_doctor_records_only_decision_slice_with_clinical_evidence(
    appointment_storage, kind
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        completed = connection.scalar(
            text("""
            SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history
            WHERE appointment_id=:id AND new_status='Completed'
        """),
            {"id": appointment},
        )
    command = _decision(
        kind,
        0,
        deadline=completed + timedelta(days=14) if kind == "DEADLINE" else None,
        reason="Synthetic clinical judgment" if kind != "DEFAULT" else None,
    )
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        result = record_followup_decision(
            connection, _actor(tenant, doctor), visit.id, command, str(uuid4())
        )
        assert result["revision"] == 1
        record = connection.execute(
            text("""
            SELECT r.change_kind, r.old_values, r.new_values, r.reason,
                   a.doctor_staff_id, a.clinical_revision, a.correlation_id
            FROM public.clinical_revisions r
            JOIN public.clinical_audit a ON a.organization_id=r.organization_id
                 AND a.correlation_id=r.id::text
            WHERE r.visit_id=:visit
        """),
            {"visit": visit.id},
        ).one()
        assert record.change_kind == (
            "DECISION_DEFAULT" if kind == "DEFAULT" else "DECISION_EXCEPTION"
        )
        assert record.old_values["followup_decision"] is None
        assert record.new_values["followup_decision"] == result["followup_decision"]
        assert record.doctor_staff_id == doctor and record.clinical_revision == 1
        assert record.reason == command.followup_decision.reason
        assert (
            connection.execute(
                text("""
            SELECT examination, findings, diagnosis, medical_notes, treatment, tests
            FROM public.visits WHERE id=:visit
        """),
                {"visit": visit.id},
            ).one()
            == (None,) * 6
        )


@pytest.mark.parametrize(
    "case", ["before_completion", "stale", "receptionist", "operator", "disabled"]
)
def test_invalid_or_unauthorized_decision_rolls_back_all_evidence(
    appointment_storage, case
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        appointment = _seed(connection, tenant, doctor, complete=True)
        visit = _visit(connection, appointment)
        completed = connection.scalar(
            text("""
            SELECT (occurred_at AT TIME ZONE 'Africa/Cairo')::date
            FROM public.appointment_history
            WHERE appointment_id=:id AND new_status='Completed'
        """),
            {"id": appointment},
        )
    receptionist = _receptionist(migration, tenant) if case == "receptionist" else None
    acting_staff = receptionist or doctor
    role = (
        "RECEPTIONIST"
        if receptionist
        else "OPERATOR"
        if case == "operator"
        else "DOCTOR_OWNER"
    )
    command = _decision(
        "DEADLINE",
        int(case == "stale"),
        deadline=completed + timedelta(days=-1 if case == "before_completion" else 8),
        reason="Synthetic exception",
    )
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, acting_staff)
        if case == "disabled":
            connection.execute(
                text("UPDATE public.staff_accounts SET enabled=false WHERE id=:id"),
                {"id": doctor},
            )
        failure_type = DBAPIError if case == "disabled" else ContractError
        with pytest.raises(failure_type) as denied, connection.begin_nested():
            record_followup_decision(
                connection,
                _actor(tenant, acting_staff, role),
                visit.id,
                command,
                str(uuid4()),
            )
        if isinstance(denied.value, ContractError):
            assert denied.value.status_code == (
                403
                if case in ("receptionist", "operator")
                else 409
                if case == "stale"
                else 422
            )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:id"),
                {"id": visit.id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.clinical_revisions WHERE visit_id=:id"
                ),
                {"id": visit.id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.idempotency_receipts WHERE operation='visits.followup_decision' AND organization_id=:tenant"
                ),
                {"tenant": tenant},
            )
            == 0
        )


def test_strict_decision_input_excludes_clinical_edit_and_missing_reason():
    with pytest.raises(ValidationError):
        FollowupDecisionPatch.model_validate(
            {
                "followup_decision": {"kind": "OPTED_OUT", "reason": " "},
                "expected_visit_revision": 0,
            }
        )
    with pytest.raises(ValidationError):
        FollowupDecisionPatch.model_validate(
            {
                "followup_decision": {"kind": "DEFAULT"},
                "expected_visit_revision": 0,
                "diagnosis": "override",
            }
        )
    with pytest.raises(ValidationError):
        ExtendFollowup.model_validate(
            {
                "appointment_id": str(uuid4()),
                "extended_deadline": "2026-11-10",
                "reason": "",
                "expected_visit_revision": 1,
                "expected_claim_revision": 1,
            }
        )


def test_source_decision_edit_preserves_confirmed_claim_and_replays_receipt(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        confirmed = _claim(connection, tenant, source)
    command = _decision("OPTED_OUT", 1, reason="Synthetic later opt-out")
    key = str(uuid4())
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        original = connection.execute(
            text("""
            SELECT c.approved_deadline, c.policy_snapshot, s.entitlement_provenance,
                   s.base_egp, s.net_egp, a.service_date
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        result = record_followup_decision(
            connection, _actor(tenant, doctor), source["visit"], command, key
        )
        assert result["revision"] == 2
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        assert (
            record_followup_decision(
                connection, _actor(tenant, doctor), source["visit"], command, key
            )
            == result
        )
        with pytest.raises(ReceiptConflict):
            record_followup_decision(
                connection,
                _actor(tenant, doctor),
                source["visit"],
                _decision("DEFAULT", 2),
                key,
            )
        frozen = connection.execute(
            text("""
            SELECT c.approved_deadline, c.policy_snapshot, s.entitlement_provenance,
                   s.base_egp, s.net_egp, a.service_date
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        assert frozen == original


def test_extension_preserves_confirmed_target_and_records_linked_evidence(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        confirmed = _claim(connection, tenant, source)
    command = _extension(source, confirmed["id"])
    key = str(uuid4())
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        frozen = connection.execute(
            text("""
            SELECT c.state, c.revision, c.approved_deadline, c.policy_snapshot,
                   s.entitlement_provenance, s.base_egp, s.net_egp,
                   a.service_date, a.status
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        result = extend_followup(
            connection, _actor(tenant, doctor), source["visit"], command, key
        )
        assert result["revision"] == 2
        record = connection.execute(
            text("""
            SELECT e.prior_deadline, e.extended_deadline,
                   e.reason AS extension_reason,
                   e.expected_source_revision, e.expected_claim_revision,
                   r.change_kind, r.reason AS revision_reason,
                   r.revision, a.correlation_id
            FROM public.followup_extensions e
            JOIN public.clinical_revisions r ON r.id=e.clinical_revision_id
            JOIN public.clinical_audit a ON a.id=e.clinical_audit_id
            WHERE e.claim_id=:claim
        """),
            {"claim": confirmed["claim"]},
        ).one()
        assert record.prior_deadline == source["day"] + timedelta(days=7)
        assert record.extended_deadline == source["day"] + timedelta(days=9)
        assert record.extension_reason == record.revision_reason == command.reason
        assert record.change_kind == "EXTENSION" and record.revision == 2
        assert (
            record.expected_source_revision == 1 and record.expected_claim_revision == 1
        )
        assert (
            connection.execute(
                text("""
            SELECT c.state, c.revision, c.approved_deadline, c.policy_snapshot,
                   s.entitlement_provenance, s.base_egp, s.net_egp,
                   a.service_date, a.status
            FROM public.followup_claims c
            JOIN public.appointment_price_snapshots s ON s.appointment_id=c.appointment_id
            JOIN public.appointments a ON a.id=c.appointment_id
            WHERE c.id=:claim
        """),
                {"claim": confirmed["claim"]},
            ).one()
            == frozen
        )
        assert frozen.state == "ACTIVE"
        assert frozen.approved_deadline == source["day"] + timedelta(days=7)
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        assert (
            extend_followup(
                connection, _actor(tenant, doctor), source["visit"], command, key
            )
            == result
        )
        repeated_deadline = _extension(
            source,
            confirmed["id"],
            revision=2,
            deadline=source["day"] + timedelta(days=9),
        )
        renewed = extend_followup(
            connection,
            _actor(tenant, doctor),
            source["visit"],
            repeated_deadline,
            str(uuid4()),
        )
        assert renewed["revision"] == 3
        deadlines = connection.execute(
            text("""
            SELECT prior_deadline, extended_deadline
            FROM public.followup_extensions WHERE claim_id=:claim
            ORDER BY expected_source_revision
        """),
            {"claim": confirmed["claim"]},
        ).all()
        assert len(deadlines) == 2
        assert deadlines[1] == (command.extended_deadline, command.extended_deadline)


@pytest.mark.parametrize(
    "case",
    ["wrong_target", "stale_source", "stale_claim", "shorten", "released", "consumed"],
)
def test_extension_denials_leave_no_new_evidence(appointment_storage, case):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        source = _source(connection, tenant, doctor)
        confirmed = _claim(connection, tenant, source)
        if case in ("released", "consumed"):
            _outcome(
                connection,
                tenant,
                doctor,
                confirmed,
                status="Cancelled" if case == "released" else "Completed",
            )
    command = _extension(
        source,
        uuid4() if case == "wrong_target" else confirmed["id"],
        revision=0 if case == "stale_source" else 1,
        claim_revision=0 if case == "stale_claim" else 1,
        deadline=source["day"] + timedelta(days=6) if case == "shorten" else None,
    )
    with transaction(runtime, tenant) as connection:
        _doctor_context(connection, doctor)
        with pytest.raises(ContractError) as denied:
            extend_followup(
                connection,
                _actor(tenant, doctor),
                source["visit"],
                command,
                str(uuid4()),
            )
        assert denied.value.status_code == (422 if case == "shorten" else 409)
        if case in ("stale_source", "stale_claim"):
            assert denied.value.code == "STALE_REVISION"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.followup_extensions WHERE claim_id=:claim"
                ),
                {"claim": confirmed["claim"]},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT clinical_revision FROM public.visits WHERE id=:visit"),
                {"visit": source["visit"]},
            )
            == 1
        )
