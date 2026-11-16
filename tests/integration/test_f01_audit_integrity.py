"""Request-bound audit integrity, operation read isolation, and native identity isolation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.book import book_appointment
from carepulse.appointments.booking_contracts import BookAppointment
from carepulse.identity import staff
from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError
from carepulse.identity.staff_contracts import (
    CreateReceptionist,
    SetReceptionistGrant,
    StaffCommand,
)
from carepulse.notifications.evidence import append_sensitive_audit
from carepulse.patients.contracts import CreatePatient
from carepulse.patients.service import register_patient
from postgres_locks import wait_for_postgres_lock
from runtime_identity_fixture import registered_test_request, signed_test_transaction
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_appointment_persistence import _activate_clinic
from test_booking import booking_body, booking_request, new_patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followups import completed_source, followup_body
from test_followups import followup_clinic as followup_clinic  # noqa: PLC0414
from test_patients import PATIENT
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_provisioning import operator_config as operator_config  # noqa: PLC0414
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_schedule import SERVICE_DAY, _config, _patch


def _denied(connection, operation, *args, **kwargs):
    with pytest.raises(DBAPIError) as denied, connection.begin_nested():
        operation(*args, **kwargs)
    assert denied.value.orig.sqlstate == "42501"


def _history(
    connection,
    tenant,
    appointment,
    status_value,
    service_date,
    number,
    actor,
    role,
    **changes,
):
    fields = {
        "id": uuid4(),
        "tenant": tenant,
        "appointment": appointment,
        "status": status_value,
        "service_date": service_date,
        "number": number,
        "actor": actor,
        "role": role,
        "action": "CREATED",
        "reason": None,
        "replacement": None,
        **changes,
    }
    connection.execute(
        text("""
            INSERT INTO public.appointment_history
                (id, organization_id, appointment_id, appointment_revision, action,
                 new_status, new_service_date, new_number, actor_staff_id, actor_role,
                 reason, price_snapshot_appointment_id, replacement_appointment_id)
            VALUES (:id, :tenant, :appointment, 0, :action, :status, :service_date,
                :number, :actor, :role, :reason, :appointment, :replacement)
        """),
        fields,
    )


def test_booking_history_binds_new_appointment_actor_role_and_creation_event(
    booking_clinic,
):
    clinic = booking_clinic
    patient = new_patient(clinic)
    status, old = booking_request(
        clinic, clinic.owner, body=booking_body(new_patient(clinic)), key=str(uuid4())
    )
    assert status == 201, old
    previous = UUID(old["data"]["id"])
    body, key = booking_body(patient), str(uuid4())
    checked = []
    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path="/v1/appointments",
        purpose="APPOINTMENT_BOOKING",
        body=body,
        key=key,
    ) as (connection, actor):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"

        def probe(conn, clause, multiparams, params, execution_options):
            if (
                getattr(getattr(clause, "table", None), "name", None)
                != "appointment_history"
            ):
                return
            current, cur_status, cur_date, cur_number = conn.execute(
                text(
                    "SELECT id, status, service_date, current_number "
                    "FROM public.appointments WHERE patient_id=:patient"
                ),
                {"patient": patient},
            ).one()
            old_row = (
                previous,
                old["data"]["status"],
                date.fromisoformat(old["data"]["service_date"]),
                old["data"]["number"],
            )
            cur_row = (current, cur_status, cur_date, cur_number)
            for (appt_id, st, sdate, num), staff_id, role, changes in (
                (old_row, actor.staff_id, actor.role, {}),
                (cur_row, clinic.staff, "DOCTOR_OWNER", {}),
                (cur_row, actor.staff_id, "DOCTOR_OWNER", {}),
                (cur_row, actor.staff_id, actor.role, {"action": "STATUS"}),
                (cur_row, actor.staff_id, actor.role, {"reason": "fabricated"}),
                (cur_row, actor.staff_id, actor.role, {"replacement": previous}),
            ):
                _denied(
                    conn,
                    _history,
                    conn,
                    clinic.tenant,
                    appt_id,
                    st,
                    sdate,
                    num,
                    staff_id,
                    role,
                    **changes,
                )
                checked.append(appt_id)

        event.listen(connection, "before_execute", probe)
        try:
            saved = book_appointment(
                connection, actor, BookAppointment.model_validate(body), key
            )
        finally:
            event.remove(connection, "before_execute", probe)
    assert len(checked) == 6
    with clinic.migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT actor_staff_id, actor_role, action, appointment_revision, reason,
                    replacement_appointment_id FROM public.appointment_history
                WHERE appointment_id=:id
            """),
            {"id": UUID(saved["id"])},
        ).one() == (
            UUID(clinic.receptionist.staff_id),
            "RECEPTIONIST",
            "CREATED",
            0,
            None,
            None,
        )


def _audit_fields(clinic, action, before, after, correlation=None):
    return {
        "organization_id": clinic.tenant,
        "actor_staff_id": clinic.staff,
        "actor_role": "DOCTOR_OWNER",
        "action": action,
        "reason": "Synthetic audit integrity",
        "before_state": before,
        "after_state": after,
        "correlation_id": str(correlation or uuid4()),
    }


def _staff_request(clinic, target, action, command, *, method="POST"):
    return signed_test_transaction(
        clinic,
        clinic.owner,
        method=method,
        path=f"/v1/staff/receptionists/{target}/{action}",
        purpose="STAFF_MANAGEMENT",
        body=command.model_dump(),
    )


def _command(revision, security):
    return StaffCommand(
        reason="Synthetic audit integrity",
        expected_revision=revision,
        expected_security_revision=security,
    )


def test_owner_audit_requires_actual_matching_mutation_and_is_single_use(
    patient_backend, monkeypatch
):
    clinic = patient_backend
    target = UUID(clinic.receptionist.staff_id)
    before = {"id": str(target), "enabled": True}
    after = {"id": str(target), "enabled": False}
    fields = _audit_fields(clinic, "RECEPTIONIST_DISABLE", before, after)
    append = staff.append_sensitive_audit

    def probe(connection, values):
        for change in (
            {"action": "RECEPTIONIST_ENABLE"},
            {"actor_staff_id": target, "actor_role": "RECEPTIONIST"},
            {"before_state": {**before, "enabled": False}},
            {"after_state": {**after, "id": str(uuid4())}},
        ):
            _denied(connection, append, connection, {**values, **change})
        audit = append(connection, values)
        _denied(connection, lambda: append(connection, {**values, "id": uuid4()}))
        return audit

    monkeypatch.setattr(staff, "append_sensitive_audit", probe)
    command = _command(0, 0)
    with _staff_request(clinic, target, "disable", command) as (connection, actor):
        assert connection.scalar(text("SELECT session_user")) == "carepulse_api"
        _denied(connection, lambda: append_sensitive_audit(connection, fields))
        # A mutation rolled back to a savepoint cannot authorize later audit insertion.
        savepoint = connection.begin_nested()
        staff.change_receptionist(connection, actor, target, command, "disable")
        savepoint.rollback()
        _denied(connection, lambda: append_sensitive_audit(connection, fields))
        staff.change_receptionist(connection, actor, target, command, "disable")
    with clinic.migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT actor_staff_id, actor_role, before_state, after_state
                FROM public.sensitive_audit WHERE organization_id=:tenant
                    AND action='RECEPTIONIST_DISABLE'
            """),
            {"tenant": clinic.tenant},
        ).one() == (clinic.staff, "DOCTOR_OWNER", before, after)
    monkeypatch.setattr(staff, "append_sensitive_audit", append)
    with _staff_request(clinic, target, "enable", _command(1, 1)) as (
        connection,
        actor,
    ):
        staff.change_receptionist(connection, actor, target, _command(1, 1), "enable")
    for enabled, revision in ((True, 2), (False, 3)):
        grant = SetReceptionistGrant(
            **_command(revision, revision).model_dump(), operational_manager=enabled
        )
        with _staff_request(clinic, target, "permissions", grant, method="PATCH") as (
            connection,
            actor,
        ):
            _denied(
                connection,
                append_sensitive_audit,
                connection,
                _audit_fields(
                    clinic,
                    "RECEPTIONIST_PERMISSIONS",
                    {
                        "id": str(target),
                        "operational_manager": not enabled,
                        "revision": revision,
                        "security_revision": revision,
                    },
                    {
                        "id": str(target),
                        "operational_manager": enabled,
                        "revision": revision + 1,
                        "security_revision": revision + 1,
                    },
                ),
            )
            staff.change_receptionist_manager(connection, actor, target, grant)
    with clinic.migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT before_state->>'operational_manager', after_state->>'operational_manager'
                FROM public.sensitive_audit WHERE organization_id=:tenant AND action='RECEPTIONIST_PERMISSIONS'
                ORDER BY (after_state->>'revision')::bigint
            """),
            {"tenant": clinic.tenant},
        ).all() == [("false", "true"), ("true", "false")]


def test_owner_create_invite_replace_and_revoke_audit_requires_matching_evidence(
    patient_backend,
    operator_config,
    monkeypatch,
):
    clinic = patient_backend
    append = staff.append_sensitive_audit

    def probe(connection, values):
        _denied(
            connection,
            append,
            connection,
            {
                **values,
                "after_state": {
                    **values["after_state"],
                    "id": str(uuid4()),
                },
            },
        )
        if values["action"] in ("RECEPTIONIST_CREATED", "RECEPTIONIST_INVITE_ISSUED"):
            _denied(
                connection,
                append,
                connection,
                {**values, "correlation_id": str(uuid4())},
            )
        audit = append(connection, values)
        _denied(connection, append, connection, {**values, "id": uuid4()})
        return audit

    monkeypatch.setattr(staff, "append_sensitive_audit", probe)
    native = f"synthetic-audit-{uuid4()}"
    email = f"{native}@example.invalid"
    with clinic.migration.begin() as connection:
        connection.execute(
            text("""INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt")
                    VALUES (:id, 'Synthetic Receptionist', :email, false, now(), now())"""),
            {"id": native, "email": email},
        )
    command = CreateReceptionist(
        email=email,
        name="Synthetic Receptionist",
        prepared_user_id=native,
        reason="Synthetic audit integrity",
    )
    key = str(uuid4())
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/staff/receptionists",
        purpose="STAFF_MANAGEMENT",
        body=command.model_dump(),
        key=key,
    ) as (connection, actor):
        fake = uuid4()
        _denied(
            connection,
            lambda: append_sensitive_audit(
                connection,
                _audit_fields(
                    clinic,
                    "RECEPTIONIST_CREATED",
                    None,
                    {"id": str(fake), "enabled": True, "verified": False},
                    fake,
                ),
            ),
        )
        projection, _ = staff.create_receptionist(connection, actor, command, key)
    target = UUID(projection["id"])
    prior_state = None
    for revision in (0, 1):
        invite = _command(revision, 0)
        identity, body, _ = _signed(
            clinic,
            path=f"/v1/staff/receptionists/{target}/invite",
            purpose="STAFF_MANAGEMENT",
            command=invite.model_dump(),
        )
        prepared = staff.prepare_staff_invitation(operator_config, clinic.tenant, email)
        forged = _audit_fields(
            clinic,
            "RECEPTIONIST_INVITE_ISSUED",
            {"id": str(target), "invitation_state": prior_state},
            {"id": str(target), "invitation_state": "ISSUED"},
            prepared.id,
        )
        with registered_test_request(clinic, identity, body):
            with protected_transaction(
                clinic.runtime,
                identity,
                clinic.config.policy.session_idle_minutes,
                stage="INVITATION_PREPARE",
            ) as (connection, actor):
                _denied(connection, append_sensitive_audit, connection, forged)
            with protected_transaction(
                clinic.runtime,
                identity,
                clinic.config.policy.session_idle_minutes,
                stage="INVITATION_APPLY",
            ) as (connection, actor):
                _denied(connection, append_sensitive_audit, connection, forged)
                projection, _ = staff.change_staff_invitation(
                    connection, actor, target, invite, "invite", prepared
                )
                assert projection["invitation_state"] == "ISSUED"
        prior_state = "ISSUED"
    revoke = _command(2, 0)
    with _staff_request(clinic, target, "revoke-invite", revoke) as (connection, actor):
        _denied(
            connection,
            lambda: append_sensitive_audit(
                connection,
                _audit_fields(
                    clinic,
                    "RECEPTIONIST_INVITE_REVOKED",
                    {"id": str(target), "invitation_state": "ISSUED"},
                    {"id": str(target), "invitation_state": "REVOKED"},
                ),
            ),
        )
        staff.change_staff_invitation(
            connection, actor, target, revoke, "revoke-invite"
        )
    with clinic.migration.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT action, before_state->>'invitation_state', after_state->>'invitation_state'
            FROM public.sensitive_audit WHERE organization_id=:tenant AND after_state->>'id'=:id
        """),
            {"tenant": clinic.tenant, "id": str(target)},
        ).all()
        assert set(rows) == {
            ("RECEPTIONIST_CREATED", None, None),
            ("RECEPTIONIST_INVITE_ISSUED", None, "ISSUED"),
            ("RECEPTIONIST_INVITE_ISSUED", "ISSUED", "ISSUED"),
            ("RECEPTIONIST_INVITE_REVOKED", "ISSUED", "REVOKED"),
        }
        assert len(rows) == 4


def _count_clinical_tables(connection):
    return {
        table: connection.scalar(text(f"SELECT count(*) FROM public.{table}"))
        for table in (
            "patients",
            "appointments",
            "appointment_price_snapshots",
            "appointment_history",
            "appointment_number_assignments",
            "appointment_day_counters",
            "visits",
            "completion_attestations",
            "followup_source_state",
            "followup_claims",
            "followup_source_decision_events",
            "followup_authorization_events",
            "followup_extensions",
        )
    }


def test_operation_resource_read_isolation_across_signed_purposes(followup_clinic):
    clinic = followup_clinic
    patient_a = new_patient(clinic)
    patient_b = new_patient(clinic)
    archived_patient = new_patient(clinic)
    with clinic.migration.begin() as connection:
        connection.execute(
            text(
                "UPDATE public.patients SET archived = true, archived_at = now(), "
                "lifecycle_revision = 1 WHERE organization_id = :tenant AND id = :id"
            ),
            {"tenant": clinic.tenant, "id": archived_patient},
        )
    source_a = completed_source(clinic)
    source_b = completed_source(clinic)
    status_fa, created_fa = booking_request(
        clinic, clinic.owner, body=followup_body(source_a), key=str(uuid4())
    )
    assert status_fa == 201, created_fa
    followup_a = UUID(created_fa["data"]["id"])
    status_fb, created_fb = booking_request(
        clinic, clinic.owner, body=followup_body(source_b), key=str(uuid4())
    )
    assert status_fb == 201, created_fb
    followup_b = UUID(created_fb["data"]["id"])

    # 1. SLOTS & SETTINGS_PRICING & SETTINGS_SCHEDULE cannot read clinical/patient tables directly.
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/slots",
        purpose="SLOTS",
        query=f"service_date={SERVICE_DAY.isoformat()}",
    ) as (connection, _):
        assert all(count == 0 for count in _count_clinical_tables(connection).values())
        assert isinstance(
            connection.execute(
                text(
                    "SELECT * FROM public.f01_counted_appointment_intervals(:tenant, :day)"
                ),
                {"tenant": clinic.tenant, "day": SERVICE_DAY},
            ).all(),
            list,
        )
        _denied(
            connection,
            connection.execute,
            text(
                "SELECT * FROM public.f01_counted_appointment_intervals(:tenant, :day)"
            ),
            {"tenant": clinic.tenant, "day": SERVICE_DAY + timedelta(days=1)},
        )
        _denied(
            connection,
            connection.execute,
            text(
                "SELECT * FROM public.f01_schedule_review_appointments(:tenant, :day)"
            ),
            {"tenant": clinic.tenant, "day": date.today()},
        )

    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/settings/pricing",
        purpose="SETTINGS_PRICING",
    ) as (connection, _):
        assert all(count == 0 for count in _count_clinical_tables(connection).values())
        _denied(
            connection,
            connection.execute,
            text(
                "SELECT * FROM public.f01_counted_appointment_intervals(:tenant, :day)"
            ),
            {"tenant": clinic.tenant, "day": SERVICE_DAY},
        )
        _denied(
            connection,
            connection.execute,
            text(
                "SELECT * FROM public.f01_schedule_review_appointments(:tenant, :day)"
            ),
            {"tenant": clinic.tenant, "day": date.today()},
        )

    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/settings/schedule",
        purpose="SETTINGS_SCHEDULE",
    ) as (connection, _):
        assert all(count == 0 for count in _count_clinical_tables(connection).values())
        _denied(
            connection,
            connection.execute,
            text(
                "SELECT * FROM public.f01_schedule_review_appointments(:tenant, :day)"
            ),
            {"tenant": clinic.tenant, "day": SERVICE_DAY},
        )

    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/settings/schedule/preview",
        purpose="SETTINGS_SCHEDULE",
        body=_patch(_config(mode="OPEN"), revision=3).model_dump(mode="json"),
        key=str(uuid4()),
    ) as (connection, _):
        assert all(count == 0 for count in _count_clinical_tables(connection).values())
        reviewed = (
            connection.execute(
                text(
                    "SELECT id FROM public.f01_schedule_review_appointments(:tenant, :day)"
                ),
                {"tenant": clinic.tenant, "day": min(source_a["day"], SERVICE_DAY)},
            )
            .scalars()
            .all()
        )
        assert {followup_a, followup_b} <= set(reviewed)

    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="POST",
        path="/v1/settings/schedule/preview",
        purpose="SETTINGS_SCHEDULE",
        body=_patch(_config(mode="OPEN"), revision=3).model_dump(mode="json"),
        key=str(uuid4()),
    ) as (connection, _):
        _denied(
            connection,
            connection.execute,
            text(
                "SELECT * FROM public.f01_schedule_review_appointments(:tenant, :day)"
            ),
            {"tenant": clinic.tenant, "day": SERVICE_DAY},
        )

    # 2. PATIENT_REGISTRY: POST cannot scan existing patients; GET {id} is single-target & lock-free;
    #    RECEPTIONIST cannot read archived patients even via raw SQL.
    patient_create_key = str(uuid4())
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/patients",
        purpose="PATIENT_REGISTRY",
        body=PATIENT,
        key=patient_create_key,
    ) as (connection, actor):
        assert connection.scalar(text("SELECT count(*) FROM public.patients")) == 0
        created = register_patient(
            connection, actor, CreatePatient.model_validate(PATIENT), patient_create_key
        )
        visible_ids = connection.scalars(text("SELECT id FROM public.patients")).all()
        assert visible_ids == [UUID(created["id"])]

    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="GET",
        path=f"/v1/patients/{patient_a}",
        purpose="PATIENT_REGISTRY",
    ) as (connection, _):
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == [
            patient_a
        ]
        assert (
            connection.scalar(
                text("SELECT id FROM public.patients WHERE id = :id"),
                {"id": patient_b},
            )
            is None
        )
        assert (
            connection.scalar(
                text("SELECT id FROM public.patients WHERE id = :id FOR UPDATE"),
                {"id": patient_a},
            )
            is None
        )

    with signed_test_transaction(
        clinic,
        clinic.receptionist,
        method="GET",
        path=f"/v1/patients/{archived_patient}",
        purpose="PATIENT_REGISTRY",
    ) as (connection, _):
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == []

    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path=f"/v1/patients/{archived_patient}",
        purpose="PATIENT_REGISTRY",
    ) as (connection, _):
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == [
            archived_patient
        ]

    # 3. APPOINTMENT_BOOKING: list/detail/POST isolate unrelated clinical & provenance tables.
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path="/v1/appointments",
        purpose="APPOINTMENT_BOOKING",
    ) as (connection, _):
        counts = _count_clinical_tables(connection)
        assert counts["appointments"] >= 4
        assert counts["appointment_price_snapshots"] == counts["appointments"]
        for restricted in (
            "patients",
            "appointment_history",
            "appointment_number_assignments",
            "appointment_day_counters",
            "visits",
            "completion_attestations",
            "followup_source_state",
            "followup_claims",
            "followup_source_decision_events",
            "followup_authorization_events",
            "followup_extensions",
        ):
            assert counts[restricted] == 0, restricted

    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="GET",
        path=f"/v1/appointments/{followup_a}",
        purpose="APPOINTMENT_BOOKING",
    ) as (connection, _):
        assert set(
            connection.scalars(text("SELECT id FROM public.appointments")).all()
        ) == {followup_a, source_a["appointment"]}
        assert connection.scalars(
            text("SELECT appointment_id FROM public.appointment_price_snapshots")
        ).all() == [followup_a]
        assert set(
            connection.scalars(
                text("SELECT appointment_id FROM public.appointment_history")
            ).all()
        ) == {followup_a}
        assert connection.scalars(text("SELECT id FROM public.visits")).all() == [
            source_a["visit"]
        ]
        assert connection.scalars(
            text("SELECT appointment_id FROM public.followup_claims")
        ).all() == [followup_a]
        assert connection.scalar(text("SELECT count(*) FROM public.patients")) == 0
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointment_number_assignments")
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointment_day_counters")
            )
            == 0
        )

    new_exam_body = booking_body(
        patient_a,
        expected_price_revision=3,
        expected_schedule_revision=3,
    )
    new_exam_key = str(uuid4())
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/appointments",
        purpose="APPOINTMENT_BOOKING",
        body=new_exam_body,
        key=new_exam_key,
    ) as (connection, actor):
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == [
            patient_a
        ]
        assert (
            connection.scalar(
                text("SELECT id FROM public.patients WHERE id = :id"),
                {"id": patient_b},
            )
            is None
        )
        assert connection.scalar(text("SELECT count(*) FROM public.appointments")) == 0
        assert connection.scalar(text("SELECT count(*) FROM public.visits")) == 0
        _denied(
            connection,
            connection.execute,
            text("SELECT public.b10_operational_source_proof(:tenant, :visit)"),
            {"tenant": clinic.tenant, "visit": source_b["visit"]},
        )
        booked = book_appointment(
            connection,
            actor,
            BookAppointment.model_validate(new_exam_body),
            new_exam_key,
        )
        assert connection.scalars(
            text("SELECT appointment_id FROM public.appointment_number_assignments")
        ).all() == [UUID(booked["id"])]
        assert connection.scalars(text("SELECT id FROM public.appointments")).all() == [
            UUID(booked["id"])
        ]
        assert connection.scalar(
            text("SELECT appointment_id FROM public.f01_audit_targets()")
        ) == UUID(booked["id"])
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_price_snapshots WHERE appointment_id = :id"
                ),
                {"id": UUID(booked["id"])},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE appointment_id = :id AND action = 'CREATED' AND appointment_revision = 0"
                ),
                {"id": UUID(booked["id"])},
            )
            == 1
        )

    # 4. VISIT_FOLLOWUP: PATCH and extend-followup isolate only the target visit/patient/claim.
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="PATCH",
        path=f"/v1/visits/{source_a['visit']}",
        purpose="VISIT_FOLLOWUP",
        body={"followup_decision": {"kind": "DEFAULT"}},
        key=str(uuid4()),
    ) as (connection, _):
        assert connection.scalars(text("SELECT id FROM public.visits")).all() == [
            source_a["visit"]
        ]
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == [
            source_a["patient"]
        ]
        assert connection.scalars(text("SELECT id FROM public.appointments")).all() == [
            source_a["appointment"]
        ]
        assert (
            connection.scalar(text("SELECT count(*) FROM public.followup_claims")) == 0
        )

    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path=f"/v1/visits/{source_a['visit']}/extend-followup",
        purpose="VISIT_FOLLOWUP",
        body={"appointment_id": str(followup_a)},
        key=str(uuid4()),
    ) as (connection, _):
        assert connection.scalars(text("SELECT id FROM public.visits")).all() == [
            source_a["visit"]
        ]
        assert connection.scalars(text("SELECT id FROM public.patients")).all() == [
            source_a["patient"]
        ]
        assert set(
            connection.scalars(text("SELECT id FROM public.appointments")).all()
        ) == {source_a["appointment"], followup_a}
        assert connection.scalars(
            text("SELECT appointment_id FROM public.followup_claims")
        ).all() == [followup_a]


def test_native_identity_isolation_blocks_bound_accounts_and_preserves_unbound_create(
    patient_backend,
):
    clinic = patient_backend
    foreign_user = f"synthetic-foreign-{uuid4()}"
    verified_only_user = f"synthetic-verified-{uuid4()}"
    credential_user = f"synthetic-cred-{uuid4()}"
    session_user_id = f"synthetic-sess-{uuid4()}"
    unbound_user = f"synthetic-unbound-{uuid4()}"
    unbound_race_user = f"synthetic-race-unbound-{uuid4()}"
    emails = {
        foreign_user: f"{foreign_user}@example.invalid",
        verified_only_user: f"{verified_only_user}@example.invalid",
        credential_user: f"{credential_user}@example.invalid",
        session_user_id: f"{session_user_id}@example.invalid",
        unbound_user: f"{unbound_user}@example.invalid",
        unbound_race_user: f"{unbound_race_user}@example.invalid",
    }
    with clinic.migration.begin() as connection:
        other_operator = connection.scalar(
            text(
                "SELECT created_by_operator_id FROM public.organizations WHERE id = :id"
            ),
            {"id": clinic.other},
        )
        other_owner_id = _activate_clinic(connection, clinic.other, other_operator)
        other_owner_user, other_owner_email = connection.execute(
            text(
                "SELECT auth_user_id, verified_email FROM public.staff_accounts WHERE id = :id"
            ),
            {"id": other_owner_id},
        ).one()
        other_session = f"synthetic-other-sess-{uuid4()}"
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token, now() + interval '1 hour',
                        now(), now(), 0, now() + interval '1 hour', now())
            """),
            {"id": other_session, "user": other_owner_user, "token": f"tok-{uuid4()}"},
        )
        for uid, email in emails.items():
            connection.execute(
                text("""
                    INSERT INTO public."user"
                        (id, name, email, "emailVerified", "createdAt", "updatedAt")
                    VALUES (:id, 'Synthetic Native Candidate', :email, :verified,
                            now(), now())
                """),
                {"id": uid, "email": email, "verified": uid == verified_only_user},
            )
        connection.execute(
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
                VALUES (:id, :tenant, :user, :email, 'RECEPTIONIST', true, false)
            """),
            {
                "id": uuid4(),
                "tenant": clinic.other,
                "user": foreign_user,
                "email": emails[foreign_user],
            },
        )
        connection.execute(
            text("""
                INSERT INTO public."account"
                    (id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt")
                VALUES (:id, :user, 'credential', :user, 'hash', now(), now())
            """),
            {"id": f"acc-{uuid4()}", "user": credential_user},
        )
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token, now() + interval '1 hour',
                        now(), now(), 0, now() + interval '1 hour', now())
            """),
            {
                "id": f"sess-{uuid4()}",
                "user": session_user_id,
                "token": f"tok-{uuid4()}",
            },
        )
        local_user, local_email = connection.execute(
            text(
                "SELECT auth_user_id, verified_email FROM public.staff_accounts WHERE id = :id"
            ),
            {"id": UUID(clinic.receptionist.staff_id)},
        ).one()
        staff_count_before = connection.scalar(
            text("SELECT count(*) FROM public.staff_accounts")
        )

    other_actor = replace(
        clinic.owner,
        organization_id=str(clinic.other),
        staff_id=str(other_owner_id),
        session_id=other_session,
    )

    ineligible = [
        (foreign_user, emails[foreign_user]),
        (other_owner_user, other_owner_email),
        (local_user, local_email),
        (verified_only_user, emails[verified_only_user]),
        (credential_user, emails[credential_user]),
        (session_user_id, emails[session_user_id]),
    ]
    for candidate_id, candidate_email in ineligible:
        command = CreateReceptionist(
            email=candidate_email,
            name="Synthetic Native Candidate",
            prepared_user_id=candidate_id,
            reason="Forbidden native binding probe",
        )
        key = str(uuid4())
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path="/v1/staff/receptionists",
            purpose="STAFF_MANAGEMENT",
            body=command.model_dump(),
            key=key,
        ) as (connection, actor):
            assert (
                connection.execute(
                    text('SELECT id, name, email FROM public."user" WHERE id = :id'),
                    {"id": candidate_id},
                ).one_or_none()
                is None
            )
            assert not connection.scalar(
                text(
                    "SELECT public.native_account_unbound_for_receptionist(:tenant, :user)"
                ),
                {"tenant": clinic.tenant, "user": candidate_id},
            )
            _denied(
                connection,
                connection.execute,
                text("""
                    INSERT INTO public.staff_accounts
                        (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
                    VALUES (:id, :tenant, :user, :email, 'RECEPTIONIST', true, false)
                """),
                {
                    "id": uuid4(),
                    "tenant": clinic.tenant,
                    "user": candidate_id,
                    "email": candidate_email,
                },
            )
            with pytest.raises(ContractError) as conflict, connection.begin_nested():
                staff.create_receptionist(connection, actor, command, key)
            assert conflict.value.status_code == 409

    with clinic.migration.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM public.staff_accounts"))
            == staff_count_before
        )

    # Positive control: legitimate unbound prepared native account succeeds once,
    # then cannot be read or re-bound in this or another clinic.
    valid_command = CreateReceptionist(
        email=emails[unbound_user],
        name="Synthetic Native Candidate",
        prepared_user_id=unbound_user,
        reason="Legitimate unbound staff creation",
    )
    valid_key = str(uuid4())
    with signed_test_transaction(
        clinic,
        clinic.owner,
        method="POST",
        path="/v1/staff/receptionists",
        purpose="STAFF_MANAGEMENT",
        body=valid_command.model_dump(),
        key=valid_key,
    ) as (connection, actor):
        assert connection.execute(
            text('SELECT id, email FROM public."user" WHERE id = :id'),
            {"id": unbound_user},
        ).one() == (unbound_user, emails[unbound_user])
        assert (
            connection.execute(
                text('SELECT id, email FROM public."user" WHERE id = :id'),
                {"id": foreign_user},
            ).one_or_none()
            is None
        )
        _denied(
            connection,
            connection.execute,
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
                VALUES (:id, :tenant, :user, 'wrong@example.invalid', 'RECEPTIONIST', true, false)
            """),
            {"id": uuid4(), "tenant": clinic.tenant, "user": unbound_user},
        )
        created_staff, _ = staff.create_receptionist(
            connection, actor, valid_command, valid_key
        )
        assert created_staff["email"] == emails[unbound_user]

    with signed_test_transaction(
        clinic,
        other_actor,
        method="POST",
        path="/v1/staff/receptionists",
        purpose="STAFF_MANAGEMENT",
        body={
            "email": emails[unbound_user],
            "name": "Synthetic Native Candidate",
            "prepared_user_id": unbound_user,
            "reason": "Cross-clinic post-bind read probe",
        },
        key=str(uuid4()),
    ) as (connection, _):
        assert (
            connection.scalar(
                text('SELECT email FROM public."user" WHERE id = :id'),
                {"id": unbound_user},
            )
            is None
        )
        _denied(
            connection,
            connection.execute,
            text("""
                INSERT INTO public.staff_accounts
                    (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
                VALUES (:id, :tenant, :user, :email, 'RECEPTIONIST', true, false)
            """),
            {
                "id": uuid4(),
                "tenant": clinic.other,
                "user": unbound_user,
                "email": emails[unbound_user],
            },
        )

    # Concurrency safety: two clinics racing on one unbound account resolve 1 winner + 1 clean 409.
    race_cmd_a = CreateReceptionist(
        email=emails[unbound_race_user],
        name="Synthetic Native Candidate",
        prepared_user_id=unbound_race_user,
        reason="Race winner",
    )
    race_cmd_b = CreateReceptionist(
        email=emails[unbound_race_user],
        name="Synthetic Native Candidate",
        prepared_user_id=unbound_race_user,
        reason="Race contender",
    )
    inserted, release_winner = Event(), Event()
    loser_ready = Event()
    holder_pid, waiter_pid = [], []

    def hold_insert(connection, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO public.staff_accounts" in statement and not inserted.is_set():
            holder_pid.append(connection.connection.driver_connection.info.backend_pid)
            inserted.set()
            assert release_winner.wait(timeout=30)

    def watch_contender(connection, _cursor, statement, _parameters, _context, _many):
        if inserted.is_set() and "INSERT INTO public.staff_accounts" in statement:
            pid = connection.connection.driver_connection.info.backend_pid
            if pid != holder_pid[0]:
                waiter_pid.append(pid)
                loser_ready.set()

    def create_in_a():
        key = str(uuid4())
        with signed_test_transaction(
            clinic,
            clinic.owner,
            method="POST",
            path="/v1/staff/receptionists",
            purpose="STAFF_MANAGEMENT",
            body=race_cmd_a.model_dump(),
            key=key,
        ) as (connection, actor):
            return staff.create_receptionist(connection, actor, race_cmd_a, key)

    def create_in_b():
        key = str(uuid4())
        with signed_test_transaction(
            clinic,
            other_actor,
            method="POST",
            path="/v1/staff/receptionists",
            purpose="STAFF_MANAGEMENT",
            body=race_cmd_b.model_dump(),
            key=key,
        ) as (connection, actor):
            try:
                return staff.create_receptionist(connection, actor, race_cmd_b, key)
            except ContractError as conflict:
                return ("conflict", conflict.status_code)

    event.listen(clinic.runtime, "after_cursor_execute", hold_insert)
    event.listen(clinic.runtime, "before_cursor_execute", watch_contender)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner_future = pool.submit(create_in_a)
            try:
                assert inserted.wait(timeout=15)
                contender_future = pool.submit(create_in_b)
                assert loser_ready.wait(timeout=15)
                with clinic.migration.connect() as observer:
                    wait_for_postgres_lock(observer, waiter_pid[0], holder_pid[0])
            finally:
                release_winner.set()
            winner_result = winner_future.result(timeout=30)
            contender_result = contender_future.result(timeout=30)
    finally:
        release_winner.set()
        event.remove(clinic.runtime, "before_cursor_execute", watch_contender)
        event.remove(clinic.runtime, "after_cursor_execute", hold_insert)

    assert winner_result[0]["email"] == emails[unbound_race_user]
    assert contender_result == ("conflict", 409)
