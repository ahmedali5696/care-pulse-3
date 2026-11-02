"""D-05 Appointment and frozen-money persistence on disposable PostgreSQL."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from carepulse.appointments.persistence import AppointmentDraft, persist_appointment
from carepulse.appointments.snapshots import validate_price_snapshot
from carepulse.db import transaction
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.receipts import TenantReceiptQuery
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def _patient(connection, tenant):
    patient_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.patients
                (id, organization_id, name_parts, date_of_birth)
            VALUES (:id, :tenant, ARRAY['Synthetic','Appointment','Patient'],
                    DATE '2001-01-01')
        """),
        {"id": patient_id, "tenant": tenant},
    )
    return patient_id


def _raw_appointment(connection, tenant, patient_id, doctor_id, **changes):
    fields = {
        "id": uuid4(),
        "tenant": tenant,
        "patient": patient_id,
        "doctor": doctor_id,
        "service_date": date(2026, 10, 27),
        "status": "Booked",
        "service_type": "NEW_EXAMINATION",
        "classification": "ROUTINE",
        "lane": "MAIN",
        "booking_mode": "OPEN",
        "starts_at": None,
        "ends_at": None,
        "current_assignment_id": uuid4(),
        "current_number": 1,
    }
    fields.update(changes)
    connection.execute(
        text("""
            INSERT INTO public.appointments
                (id, organization_id, patient_id, doctor_staff_id,
                 service_date, status, service_type, classification,
                 lane, booking_mode, starts_at, ends_at,
                 current_assignment_id, current_number)
            VALUES (:id, :tenant, :patient, :doctor, :service_date,
                    :status, :service_type, :classification, :lane,
                    :booking_mode, :starts_at, :ends_at,
                    :current_assignment_id, :current_number)
        """),
        fields,
    )
    return fields["id"]


def _raw_snapshot(connection, tenant, appointment_id, **changes):
    fields = {
        "tenant": tenant,
        "appointment": appointment_id,
        "service_type": "NEW_EXAMINATION",
        "classification": "ROUTINE",
        "base": Decimal("100.00"),
        "discount_type": "NONE",
        "discount_value": Decimal(0),
        "discount_reason": None,
        "is_free": False,
        "net": Decimal("100.00"),
        "standard": Decimal("100.00"),
        "pricing_version": 1,
        "provenance": None,
    }
    fields.update(changes)
    connection.execute(
        text("""
            INSERT INTO public.appointment_price_snapshots
                (organization_id, appointment_id, service_type, classification,
                 base_egp, discount_type, discount_value, discount_reason,
                 is_free, net_egp, pricing_version, creation_standard_base_egp,
                 entitlement_provenance)
            VALUES (:tenant, :appointment, :service_type, :classification,
                    :base, :discount_type, :discount_value, :discount_reason,
                    :is_free, :net, :pricing_version, :standard,
                    CAST(:provenance AS jsonb))
        """),
        fields,
    )


def _raw_history(connection, tenant, appointment, doctor, number, **changes):
    fields = {
        "id": uuid4(),
        "tenant": tenant,
        "appointment": appointment,
        "actor": doctor,
        "number": number,
        "snapshot": appointment,
        "replacement": None,
    }
    fields.update(changes)
    connection.execute(
        text("""
            INSERT INTO public.appointment_history
                (id, organization_id, appointment_id, appointment_revision,
                 action, old_status, new_status, old_service_date, new_service_date,
                 old_number, new_number, actor_staff_id, actor_role,
                 price_snapshot_appointment_id, replacement_appointment_id)
            VALUES (:id, :tenant, :appointment, 1, 'STATUS', 'Booked', 'Booked',
                    DATE '2026-10-27', DATE '2026-10-27', :number, :number,
                    :actor, 'DOCTOR_OWNER', :snapshot, :replacement)
        """),
        fields,
    )


def _draft(patient, doctor, **changes):
    draft = {
        "patient_id": patient,
        "doctor_staff_id": doctor,
        "service_date": date(2026, 10, 27),
        "status": "Booked",
        "service_type": "NEW_EXAMINATION",
        "classification": "ROUTINE",
        "lane": "MAIN",
        "booking_mode": "OPEN",
    }
    draft.update(changes)
    snapshot = draft.pop("price_snapshot", {})
    draft["price_snapshot"] = {
        "service_type": draft["service_type"],
        "classification": draft["classification"],
        "base_egp": "100.00",
        "discount_type": "NONE",
        "discount_value": "0",
        "discount_reason": None,
        "is_free": False,
        "net_egp": "100.00",
        "pricing_version": 1,
        "creation_standard_base_egp": "100.00",
        "entitlement_provenance": None,
        **snapshot,
    }
    return AppointmentDraft(**draft)


def _query(tenant, doctor):
    actor = AuthorizedStaff(tenant, doctor, "DOCTOR_OWNER", False, 0, 0, 0)
    return TenantReceiptQuery(
        actor,
        "appointments.persist",
        str(uuid4()),
        sha256(str(uuid4()).encode()).hexdigest(),
    )


def _admit_saved(
    connection,
    tenant,
    doctor,
    saved,
    *,
    complete=False,
    actor=None,
    role="DOCTOR_OWNER",
    insert_visit=True,
):
    """Construct only genuine, history-backed synthetic admission evidence."""
    appointment = UUID(saved["id"])
    actor = actor or doctor
    connection.execute(
        text("SELECT set_config('app.staff_id', :actor, true)"),
        {"actor": str(actor)},
    )
    current = "Booked"
    for revision, status in enumerate(
        ("Waiting", "In Examination", "Completed")
        if complete
        else ("Waiting", "In Examination"),
        start=1,
    ):
        connection.execute(
            text("""
                UPDATE public.appointments SET status = :status, revision = :revision
                WHERE organization_id = :tenant AND id = :id
            """),
            {
                "status": status,
                "revision": revision,
                "tenant": tenant,
                "id": appointment,
            },
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history (
                    id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status, old_service_date, new_service_date,
                    old_number, new_number, actor_staff_id, actor_role,
                    reason, price_snapshot_appointment_id
                )
                SELECT :history, a.organization_id, a.id, :revision, 'STATUS',
                       :old, :status, a.service_date, a.service_date,
                       a.current_number, a.current_number, :actor, :role,
                       'Synthetic lifecycle', a.id
                FROM public.appointments a WHERE a.organization_id=:tenant AND a.id=:id
            """),
            {
                "history": uuid4(),
                "revision": revision,
                "old": current,
                "status": status,
                "actor": actor,
                "role": role,
                "tenant": tenant,
                "id": appointment,
            },
        )
        if status == "In Examination" and insert_visit:
            connection.execute(
                text("""
                    INSERT INTO public.visits
                        (id, organization_id, appointment_id, patient_id,
                         doctor_staff_id, service_date, admission_history_revision,
                         admitted_at)
                    SELECT :visit, a.organization_id, a.id, a.patient_id,
                           a.doctor_staff_id, a.service_date, :revision, clock_timestamp()
                    FROM public.appointments a
                    WHERE a.organization_id=:tenant AND a.id=:id
                """),
                {
                    "visit": uuid4(),
                    "revision": revision,
                    "tenant": tenant,
                    "id": appointment,
                },
            )
        current = status


def _persist_with_status(connection, tenant, patient, doctor, status, **changes):
    needs_visit = status in ("In Examination", "Completed")
    saved = persist_appointment(
        connection,
        _draft(patient, doctor, status="Booked" if needs_visit else status, **changes),
        _query(tenant, doctor),
    )
    if needs_visit:
        _admit_saved(connection, tenant, doctor, saved, complete=status == "Completed")
    return {**saved, "status": status}


def _activate_clinic(connection, tenant, operator_id):
    doctor_id = uuid4()
    user_id = f"synthetic-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    invitation_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public."user"
                (id, name, email, "emailVerified", "createdAt", "updatedAt")
            VALUES (:id, 'Synthetic appointment doctor', :email, true,
                    clock_timestamp(), clock_timestamp())
        """),
        {"id": user_id, "email": email},
    )
    connection.execute(
        text("""
            INSERT INTO public.staff_accounts
                (id, organization_id, auth_user_id, verified_email,
                 role, enabled, verified)
            VALUES (:id, :tenant, :user, :email, 'DOCTOR_OWNER', true, true)
        """),
        {"id": doctor_id, "tenant": tenant, "user": user_id, "email": email},
    )
    connection.execute(
        text("""
            INSERT INTO public.invitations
                (id, organization_id, purpose, target_email, proof_digest,
                 issued_by_operator_id, expires_at)
            VALUES (:id, :tenant, 'FIRST_OWNER', :email, :digest,
                    :operator, clock_timestamp() + interval '20 minutes')
        """),
        {
            "id": invitation_id,
            "tenant": tenant,
            "email": email,
            "digest": sha256(str(invitation_id).encode()).hexdigest(),
            "operator": operator_id,
        },
    )
    connection.execute(
        text("""
            UPDATE public.invitations
            SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :id
        """),
        {"user": user_id, "id": invitation_id},
    )
    connection.execute(
        text("UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :id"),
        {"id": tenant},
    )
    return doctor_id


@pytest.fixture
def appointment_storage(role_engine, tenant_factory):
    operator_id, create_tenant = tenant_factory
    tenants = (
        create_tenant("Synthetic D-05 clinic A"),
        create_tenant("Synthetic D-05 clinic B"),
    )
    migration = role_engine("carepulse_migration")
    runtime = role_engine("carepulse_runtime")
    try:
        with migration.begin() as connection:
            doctors = tuple(
                _activate_clinic(connection, tenant, operator_id) for tenant in tenants
            )
        yield runtime, migration, tenants, doctors
    finally:
        migration.dispose()
        runtime.dispose()


@pytest.mark.parametrize(
    "status",
    ["Booked", "Waiting", "In Examination", "Completed", "Cancelled", "No-show"],
)
def test_six_approved_statuses_persist_with_frozen_creation_evidence(
    appointment_storage, status
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        created = _persist_with_status(connection, tenant, patient, doctor, status)
        appointment_id = created["id"]
        assert created["status"] == status
        assert created["number"] == 1
        assert created["net_egp"] == "100.00"
        revision = {"In Examination": 2, "Completed": 3}.get(status, 0)
        assert connection.execute(
            text("""
                SELECT a.status, a.current_number, a.revision, h.appointment_revision,
                       h.action, s.net_egp, s.is_free
                FROM public.appointments a
                JOIN public.appointment_history h
                  ON h.organization_id = a.organization_id
                 AND h.appointment_id = a.id AND h.appointment_revision = a.revision
                JOIN public.appointment_price_snapshots s
                  ON s.organization_id = a.organization_id AND s.appointment_id = a.id
                WHERE a.id = :id
            """),
            {"id": appointment_id},
        ).one() == (
            status,
            1,
            revision,
            revision,
            "STATUS" if revision else "CREATED",
            Decimal("100.00"),
            False,
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointment_history WHERE appointment_id = :id AND action = 'CREATED'"
                ),
                {"id": appointment_id},
            )
            == 1
        )


@pytest.mark.parametrize(
    "counted", ["Booked", "Waiting", "In Examination", "Completed"]
)
def test_counted_statuses_prevent_second_same_patient_day(appointment_storage, counted):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        first = _persist_with_status(connection, tenant, patient, doctor, counted)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            persist_appointment(
                connection, _draft(patient, doctor), _query(tenant, doctor)
            )
        assert denied.value.orig.sqlstate == "23505"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.appointments WHERE patient_id = :patient"
                ),
                {"patient": patient},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("""
                SELECT last_number FROM public.appointment_day_counters
                WHERE service_date = DATE '2026-10-27'
            """)
            )
            == first["number"]
        )


@pytest.mark.parametrize("terminal", ["Cancelled", "No-show"])
def test_terminal_cancel_or_no_show_allows_new_id_and_retains_old_number(
    appointment_storage, terminal
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        first = persist_appointment(
            connection, _draft(patient, doctor), _query(tenant, doctor)
        )
        connection.execute(
            text("""
                UPDATE public.appointments SET status = :status, revision = 1
                WHERE id = :id
            """),
            {"status": terminal, "id": first["id"]},
        )
        second = persist_appointment(
            connection, _draft(patient, doctor), _query(tenant, doctor)
        )
        connection.execute(
            text("""
                INSERT INTO public.appointment_history
                    (id, organization_id, appointment_id, appointment_revision,
                     action, old_status, new_status, old_service_date,
                     new_service_date, old_number, new_number, actor_staff_id,
                     actor_role, reason, price_snapshot_appointment_id,
                     replacement_appointment_id)
                VALUES (:id, :tenant, :original, 1, 'REPLACED', 'Booked', :terminal,
                        DATE '2026-10-27', DATE '2026-10-27', 1, 1,
                        :actor, 'DOCTOR_OWNER', 'Synthetic replacement', :original,
                        :replacement)
            """),
            {
                "id": uuid4(),
                "tenant": tenant,
                "original": first["id"],
                "terminal": terminal,
                "actor": doctor,
                "replacement": second["id"],
            },
        )
    assert first["id"] != second["id"]
    assert (first["number"], second["number"]) == (1, 2)
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("""
                SELECT a.id, a.status, h.replacement_appointment_id
                FROM public.appointments a JOIN public.appointment_history h
                ON (a.organization_id, a.id) = (h.organization_id, h.appointment_id)
                WHERE a.id = :old AND h.appointment_revision = 1
            """),
            {"old": first["id"]},
        ).one() == (UUID(first["id"]), terminal, UUID(second["id"]))
        assert connection.scalars(
            text("""
                SELECT number FROM public.appointment_number_assignments
                WHERE service_date = DATE '2026-10-27' ORDER BY number
            """)
        ).all() == [1, 2]


def test_open_and_exact_time_half_open_adjacency_are_stored(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    start = datetime(2026, 10, 27, 8, tzinfo=UTC)
    with transaction(runtime, tenant) as connection:
        open_appointment = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor),
            _query(tenant, doctor),
        )
        first = persist_appointment(
            connection,
            _draft(
                _patient(connection, tenant),
                doctor,
                booking_mode="EXACT_TIME",
                starts_at=start,
                ends_at=start + timedelta(minutes=20),
            ),
            _query(tenant, doctor),
        )
        second = persist_appointment(
            connection,
            _draft(
                _patient(connection, tenant),
                doctor,
                booking_mode="EXACT_TIME",
                starts_at=start + timedelta(minutes=20),
                ends_at=start + timedelta(minutes=40),
            ),
            _query(tenant, doctor),
        )
        assert connection.execute(
            text("""
                SELECT starts_at, ends_at FROM public.appointments
                WHERE id = :id
            """),
            {"id": open_appointment["id"]},
        ).one() == (None, None)
        assert connection.scalar(
            text("SELECT ends_at FROM public.appointments WHERE id = :id"),
            {"id": first["id"]},
        ) == connection.scalar(
            text("SELECT starts_at FROM public.appointments WHERE id = :id"),
            {"id": second["id"]},
        )


@pytest.mark.parametrize(
    ("classification", "discount_type", "discount", "net"),
    [
        ("ROUTINE", "PERCENT", "0.335", "99.67"),
        ("EMERGENCY", "FIXED", "10.00", "90.00"),
    ],
)
def test_confirmed_decimal_rounding_and_discounted_emergency_persist(
    appointment_storage, classification, discount_type, discount, net
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        draft = _draft(
            _patient(connection, tenant),
            doctor,
            classification=classification,
            lane="EMERGENCY" if classification == "EMERGENCY" else "MAIN",
            price_snapshot={
                "discount_type": discount_type,
                "discount_value": discount,
                "discount_reason": "Synthetic authorized price exception",
                "net_egp": net,
            },
        )
        assert validate_price_snapshot(draft.price_snapshot)["net_egp"] == Decimal(net)
        saved = persist_appointment(connection, draft, _query(tenant, doctor))
        assert saved["net_egp"] == net
        stored = connection.execute(
            text("""
                SELECT base_egp, discount_value, net_egp, creation_standard_base_egp
                FROM public.appointment_price_snapshots
                WHERE appointment_id = :id
            """),
            {"id": saved["id"]},
        ).one()
        assert stored == (Decimal(100), Decimal(discount), Decimal(net), Decimal(100))


def test_appointment_rls_and_operator_cannot_read_clinical_records(
    appointment_storage, role_engine
):
    runtime, _, (tenant, other), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        created = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor),
            _query(tenant, doctor),
        )
    with transaction(runtime, tenant) as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.appointments")) == 1
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.appointment_price_snapshots")
            )
            == 1
        )
    for scope in (other, None):
        with transaction(runtime, scope) as connection:
            assert (
                connection.execute(text("SELECT id FROM public.appointments")).all()
                == []
            )
            assert (
                connection.execute(
                    text("SELECT appointment_id FROM public.appointment_history")
                ).all()
                == []
            )
    operator = role_engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT * FROM public.appointments")).all()
        assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()
    assert created["id"]


def test_bare_status_update_without_matching_history_cannot_commit(
    appointment_storage,
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        created = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor),
            _query(tenant, doctor),
        )
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        connection.execute(
            text("""
                UPDATE public.appointments SET status = 'Waiting', revision = revision + 1
                WHERE id = :id
            """),
            {"id": created["id"]},
        )
    assert denied.value.orig.sqlstate == "23514"
    with transaction(runtime, tenant) as connection:
        assert connection.execute(
            text("SELECT status, revision FROM public.appointments WHERE id = :id"),
            {"id": created["id"]},
        ).one() == ("Booked", 0)


def test_terminal_records_and_frozen_snapshot_number_history_are_immutable(
    appointment_storage,
):
    runtime, migration, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        created = _persist_with_status(
            connection, tenant, _patient(connection, tenant), doctor, "Completed"
        )
        with pytest.raises(DBAPIError) as update_denied, connection.begin_nested():
            connection.execute(
                text("""
                    UPDATE public.appointments SET status = 'Waiting', revision = 1
                    WHERE id = :id
                """),
                {"id": created["id"]},
            )
        assert update_denied.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as delete_denied, connection.begin_nested():
            connection.execute(
                text("DELETE FROM public.appointments WHERE id = :id"),
                {"id": created["id"]},
            )
        assert delete_denied.value.orig.sqlstate == "42501"

    with pytest.raises(DBAPIError) as migration_denied, migration.begin() as connection:
        connection.execute(
            text("DELETE FROM public.appointments WHERE id = :id"),
            {"id": created["id"]},
        )
    assert migration_denied.value.orig.sqlstate == "23514"

    updates = (
        ("appointment_price_snapshots", "net_egp = 1"),
        ("appointment_number_assignments", "number = 99"),
        ("appointment_history", "reason = 'tampered'"),
    )
    for table, assignment in updates:
        for verb in ("UPDATE", "DELETE"):
            statement = (
                f"UPDATE public.{table} SET {assignment} WHERE appointment_id = :id"
                if verb == "UPDATE"
                else f"DELETE FROM public.{table} WHERE appointment_id = :id"
            )
            with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
                connection.execute(text(statement), {"id": created["id"]})
            assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("status", ["Arrived", "In Exam", "NoShow", "Review", "booked"])
def test_appointment_unknown_or_misspelled_status_is_rejected(
    appointment_storage, status
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _raw_appointment(connection, tenant, patient, doctor, status=status)
        assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "changes",
    [
        {"booking_mode": "OPEN", "starts_at": datetime(2026, 10, 27, tzinfo=UTC)},
        {"booking_mode": "EXACT_TIME"},
        {"booking_mode": "EXACT_TIME", "ends_at": datetime(2026, 10, 27, tzinfo=UTC)},
        {
            "booking_mode": "EXACT_TIME",
            "starts_at": datetime(2026, 10, 27, tzinfo=UTC),
            "ends_at": datetime(2026, 10, 27, tzinfo=UTC),
        },
        {
            "booking_mode": "EXACT_TIME",
            "starts_at": datetime(2026, 10, 27, tzinfo=UTC) + timedelta(minutes=20),
            "ends_at": datetime(2026, 10, 27, tzinfo=UTC),
        },
    ],
)
def test_appointment_interval_contract_rejects_incomplete_or_nonpositive_time(
    appointment_storage, changes
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _raw_appointment(connection, tenant, patient, doctor, **changes)
        assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("foreign_relation", ["patient", "doctor"])
def test_appointment_foreign_clinic_relationship_is_rejected(
    appointment_storage, foreign_relation
):
    runtime, migration, (tenant, other), (doctor, other_doctor) = appointment_storage
    with migration.begin() as connection:
        foreign_patient = _patient(connection, other)
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        overrides = {
            foreign_relation: (
                foreign_patient if foreign_relation == "patient" else other_doctor
            )
        }
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _raw_appointment(connection, tenant, patient, doctor, **overrides)
        assert denied.value.orig.sqlstate in ("23503", "23514")


@pytest.mark.parametrize("classification", ["URGENT", "EMERGENCY"])
def test_followup_cannot_take_urgent_or_emergency_classification(
    appointment_storage, classification
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with transaction(runtime, tenant) as connection:
        patient = _patient(connection, tenant)
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            _raw_appointment(
                connection,
                tenant,
                patient,
                doctor,
                service_type="FOLLOW_UP",
                classification=classification,
                lane="EMERGENCY" if classification == "EMERGENCY" else "MAIN",
            )
        assert (
            denied.value.orig.diag.constraint_name == "appointment_followup_class_valid"
        )


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"base": Decimal("NaN")}, "appointment_price_final_valid"),
        ({"base": Decimal("Infinity")}, "appointment_price_final_valid"),
        ({"base": Decimal("100.001")}, "appointment_price_final_valid"),
        ({"discount_value": Decimal(-1)}, "appointment_price_discount_valid"),
        (
            {"discount_type": "FIXED", "discount_value": Decimal(110)},
            "appointment_price_discount_valid",
        ),
        (
            {"discount_type": "PERCENT", "discount_value": Decimal(101)},
            "appointment_price_discount_valid",
        ),
        (
            {"discount_type": "FIXED", "discount_value": Decimal(10)},
            "appointment_price_discount_valid",
        ),
        ({"net": Decimal("99.99")}, "appointment_price_final_valid"),
        ({"is_free": True}, "appointment_price_final_valid"),
        (
            {
                "classification": "URGENT",
                "base": Decimal(110),
                "discount_type": "PERCENT",
                "discount_value": Decimal(10),
                "discount_reason": "Synthetic authorized discount",
                "net": Decimal(99),
            },
            "appointment_price_final_valid",
        ),
        (
            {
                "classification": "URGENT",
                "base": Decimal("100.01"),
                "discount_type": "FIXED",
                "discount_value": Decimal("0.01"),
                "discount_reason": "Synthetic authorized discount",
            },
            "appointment_price_final_valid",
        ),
    ],
)
def test_invalid_discount_money_or_urgent_snapshot_fails_database_validation(
    appointment_storage, changes, constraint
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        patient = _patient(connection, tenant)
        appointment = _raw_appointment(
            connection,
            tenant,
            patient,
            doctor,
            classification=changes.get("classification", "ROUTINE"),
        )
        _raw_snapshot(connection, tenant, appointment, **changes)
    assert denied.value.orig.sqlstate == "23514"
    assert denied.value.orig.diag.constraint_name == constraint


@pytest.mark.parametrize("classification", ["ROUTINE", "EMERGENCY"])
def test_new_exam_nonurgent_base_cannot_differ_from_creation_standard_rate(
    appointment_storage, classification
):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        appointment = _raw_appointment(
            connection,
            tenant,
            _patient(connection, tenant),
            doctor,
            classification=classification,
            lane="EMERGENCY" if classification == "EMERGENCY" else "MAIN",
        )
        _raw_snapshot(
            connection,
            tenant,
            appointment,
            classification=classification,
            base=Decimal(110),
            net=Decimal(110),
            standard=Decimal(100),
        )
    assert denied.value.orig.diag.constraint_name == "appointment_price_final_valid"


def test_followup_price_requires_stored_entitlement_provenance(appointment_storage):
    runtime, _, (tenant, _), (doctor, _) = appointment_storage
    with (
        pytest.raises(DBAPIError) as denied,
        transaction(runtime, tenant) as connection,
    ):
        appointment = _raw_appointment(
            connection,
            tenant,
            _patient(connection, tenant),
            doctor,
            service_type="FOLLOW_UP",
        )
        _raw_snapshot(connection, tenant, appointment, service_type="FOLLOW_UP")
    assert denied.value.orig.diag.constraint_name == "appointment_entitlement_shape"


def test_history_cannot_reference_other_snapshot_or_foreign_replacement(
    appointment_storage,
):
    runtime, _, (tenant, other), (doctor, other_doctor) = appointment_storage
    with transaction(runtime, tenant) as connection:
        first = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor),
            _query(tenant, doctor),
        )
        second = persist_appointment(
            connection,
            _draft(_patient(connection, tenant), doctor),
            _query(tenant, doctor),
        )
        with pytest.raises(DBAPIError) as denied, connection.begin_nested():
            connection.execute(
                text("UPDATE public.appointments SET revision = 1 WHERE id = :id"),
                {"id": first["id"]},
            )
            _raw_history(
                connection, tenant, first["id"], doctor, 1, snapshot=second["id"]
            )
        assert denied.value.orig.sqlstate == "23514"
    with transaction(runtime, other) as connection:
        foreign = persist_appointment(
            connection,
            _draft(_patient(connection, other), other_doctor),
            _query(other, other_doctor),
        )
    with (
        pytest.raises(DBAPIError) as foreign_denied,
        transaction(runtime, tenant) as connection,
    ):
        connection.execute(
            text("UPDATE public.appointments SET revision = 1 WHERE id = :id"),
            {"id": first["id"]},
        )
        _raw_history(
            connection,
            tenant,
            first["id"],
            doctor,
            1,
            replacement=foreign["id"],
        )
    assert foreign_denied.value.orig.sqlstate == "23503"
