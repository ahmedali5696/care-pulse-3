"""T071: current-head signed transport, rollback and pooled authority boundaries."""

import json
from dataclasses import replace
from uuid import UUID, uuid4

from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import event, text
from test_appointment_persistence import _activate_clinic, _patient
from test_booking import booking_clinic as booking_clinic  # noqa: PLC0414
from test_followup_decisions import _signed_visit_request
from test_patients import patient_backend as patient_backend  # noqa: PLC0414
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414
from test_w3_admission import (
    admit_body,
    admit_request,
    assert_admitted,
    today,
    waiting_case,
)
from test_w3_checkin import queue_request
from test_w3_day_review import _reviews
from test_w3_queue_scope import _visible_ids
from test_w3_visits import effects


def _snapshot(clinic):
    return {**effects(clinic), **_reviews(clinic)}


def _assert_error(response, status, code, message):
    actual, body = response
    assert actual == status, body
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["message"] == message
    assert UUID(body["error"]["request_id"])


def test_http_database_failure_rolls_back_and_is_sanitized(booking_clinic):
    clinic = booking_clinic
    waiting = waiting_case(clinic)
    body = admit_body(clinic, waiting, physical_presence_confirmed=True)
    before = _snapshot(clinic)
    key = uuid4().hex
    marker = "synthetic_t071_" + uuid4().hex
    # The admission writer inserts its receipt after Visit, queue, history and evidence writes.
    with clinic.migration.begin() as connection:
        connection.execute(
            text(f"""
            CREATE FUNCTION public.{marker}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.organization_id='{clinic.tenant}'::uuid
                    AND NEW.operation='queue.admit' AND NEW.idempotency_key='{key}'
                    AND NEW.result_projection->>'appointment_id'='{waiting["appointment_id"]}'
                THEN
                    RAISE EXCEPTION 'synthetic private database detail' USING ERRCODE='P0199';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER {marker} AFTER INSERT ON public.idempotency_receipts
                FOR EACH ROW EXECUTE FUNCTION public.{marker}()
        """)
        )
    failures = []

    def observe_failure(context):
        failures.append(getattr(context.original_exception, "sqlstate", None))

    event.listen(clinic.runtime, "handle_error", observe_failure)
    try:
        _assert_error(
            admit_request(clinic, clinic.owner, body, key),
            503,
            "UNAVAILABLE",
            "Service unavailable",
        )
        assert failures == ["P0199"]
        assert _snapshot(clinic) == before
    finally:
        event.remove(clinic.runtime, "handle_error", observe_failure)
        with clinic.migration.begin() as connection:
            connection.execute(
                text(f"DROP TRIGGER {marker} ON public.idempotency_receipts")
            )
            connection.execute(text(f"DROP FUNCTION public.{marker}()"))

    status, accepted = admit_request(clinic, clinic.owner, body, key)
    assert status == 200, accepted
    assert_admitted(clinic, clinic.owner, body, key, accepted["data"])


def _foreign_actor_and_patient(clinic):
    session = f"synthetic-t071-{uuid4()}"
    with clinic.migration.begin() as connection:
        operator = connection.scalar(
            text(
                "SELECT created_by_operator_id FROM public.organizations WHERE id=:tenant"
            ),
            {"tenant": clinic.other},
        )
        doctor = _activate_clinic(connection, clinic.other, operator)
        patient = _patient(connection, clinic.other)
        connection.execute(
            text("""
            INSERT INTO public."session" (id,"userId",token,"expiresAt","createdAt","updatedAt",
                "securityRevision","absoluteDeadline","lastActivity")
            SELECT :session,auth_user_id,:token,now()+interval '1 hour',now(),now(),
                security_revision,now()+interval '1 hour',now()
            FROM public.staff_accounts WHERE organization_id=:tenant AND id=:doctor
        """),
            {
                "session": session,
                "token": str(uuid4()),
                "tenant": clinic.other,
                "doctor": doctor,
            },
        )
    return replace(
        clinic.owner,
        organization_id=str(clinic.other),
        staff_id=str(doctor),
        session_id=session,
        security_revision="0",
    ), str(patient)


def _assert_unbound(connection, pid):
    assert connection.execute(
        text("""
        SELECT pg_backend_pid(), session_user, public.f01_org(), public.f01_staff()
    """)
    ).one() == (pid, "carepulse_api", None, None)
    assert connection.scalar(text("SELECT count(*) FROM public.f01_context()")) == 0
    for table in ("patients", "appointments", "queue_days", "queue_entries"):
        assert not _visible_ids(connection, table)


def test_pool_reuse_clears_tenant_and_actor_authority(booking_clinic):
    clinic = booking_clinic
    waiting = waiting_case(clinic)
    local_patient = waiting["patient_id"]
    foreign_actor, foreign_patient = _foreign_actor_and_patient(clinic)
    with clinic.runtime.connect() as connection:
        pid = connection.scalar(text("SELECT pg_backend_pid()"))
        _assert_unbound(connection, pid)
    for actor, expected_patient in (
        (clinic.owner, local_patient),
        (clinic.receptionist, local_patient),
        (foreign_actor, foreign_patient),
        (clinic.owner, local_patient),
    ):
        with signed_test_transaction(
            clinic,
            actor,
            method="GET",
            path="/v1/patients",
            purpose="PATIENT_REGISTRY",
        ) as (connection, authorized):
            assert connection.execute(
                text("""
                SELECT pg_backend_pid(), session_user, public.f01_org(), public.f01_staff()
            """)
            ).one() == (pid, "carepulse_api", actor.organization_id, actor.staff_id)
            assert connection.execute(
                text("""
                SELECT organization_id, staff_id, role, purpose, session_id FROM public.f01_context()
            """)
            ).one() == (
                actor.organization_id,
                actor.staff_id,
                actor.role,
                "PATIENT_REGISTRY",
                actor.session_id,
            )
            assert str(authorized.organization_id) == actor.organization_id
            assert str(authorized.staff_id) == actor.staff_id
            assert _visible_ids(connection, "patients") == {expected_patient}
        with clinic.runtime.connect() as connection:
            _assert_unbound(connection, pid)
        with signed_test_transaction(
            clinic,
            actor,
            method="GET",
            path="/v1/queue",
            purpose="QUEUE_READ",
            query=f"date={waiting['service_date']}&lane=MAIN",
        ) as (connection, _):
            assert connection.execute(
                text("""
                SELECT pg_backend_pid(), organization_id, staff_id, purpose FROM public.f01_context()
            """)
            ).one() == (pid, actor.organization_id, actor.staff_id, "QUEUE_READ")
            expected_entries = (
                {waiting["queue_entry_id"]}
                if actor.organization_id == str(clinic.tenant)
                else set()
            )
            assert _visible_ids(connection, "queue_entries") == expected_entries
        with clinic.runtime.connect() as connection:
            _assert_unbound(connection, pid)


def test_signed_boundary_rejects_excessive_and_malformed_requests(booking_clinic):
    clinic = booking_clinic
    waiting = waiting_case(clinic)
    body = admit_body(clinic, waiting, physical_presence_confirmed=True)
    before = _snapshot(clinic)
    raw = json.dumps(body, separators=(",", ":"))
    at_limit = raw.ljust(1_048_576)
    # The exact boundary reaches purpose verification; one extra byte is rejected first.
    for payload, status, code, message in (
        (at_limit, 403, "FORBIDDEN", "Operation unavailable"),
        (at_limit + " ", 422, "INVALID_INPUT", "Invalid request"),
    ):
        _assert_error(
            _signed_visit_request(
                clinic,
                clinic.owner,
                path="/v1/queue/admit",
                method="POST",
                body=payload,
                key=uuid4().hex,
                purpose="QUEUE_READ",
            ),
            status,
            code,
            message,
        )
    # A correctly signed malformed or excessive request cannot reach a writer.
    for payload in (
        at_limit + " ",
        "{",
        raw[:-1] + ',"physical_presence_confirmed":true}',
        json.dumps({**body, "physical_presence_confirmed": "true"}),
        json.dumps({**body, "organization_id": str(clinic.other)}),
    ):
        _assert_error(
            _signed_visit_request(
                clinic,
                clinic.owner,
                path="/v1/queue/admit",
                method="POST",
                body=payload,
                key=uuid4().hex,
                purpose="QUEUE_ADMIT",
            ),
            422,
            "INVALID_INPUT",
            "Invalid request",
        )
    date = waiting["service_date"]
    for path, purpose, query in (
        ("/v1/queue", "QUEUE_READ", {"date": date, "lane": "MAIN"}),
        ("/v1/reconciliation", "RECONCILIATION_READ", {"date": date}),
    ):
        for invalid in (
            {"date": f"{date}T00:00:00"},
            {"offset": 9223372036854775808},
            {"limit": 101},
            {"limit": 0},
            {"offset": -1},
            {"limit": ["1", "2"]},
            {"unknown": "x"},
        ):
            _assert_error(
                queue_request(
                    clinic,
                    clinic.owner,
                    method="GET",
                    path=path,
                    purpose=purpose,
                    query={**query, **invalid},
                ),
                422,
                "INVALID_INPUT",
                "Invalid request",
            )
        status, accepted = queue_request(
            clinic,
            clinic.owner,
            method="GET",
            path=path,
            purpose=purpose,
            query={**query, "limit": 100, "offset": 0},
        )
        assert status == 200, accepted
    assert _snapshot(clinic) == before
    key = uuid4().hex
    status, accepted = _signed_visit_request(
        clinic,
        clinic.owner,
        path="/v1/queue/admit",
        method="POST",
        body=at_limit,
        key=key,
        purpose="QUEUE_ADMIT",
    )
    assert status == 200, accepted
    assert_admitted(clinic, clinic.owner, body, key, accepted["data"])


def test_wave3_routes_reject_wrong_signed_purpose(booking_clinic):
    clinic = booking_clinic
    target = str(uuid4())
    appointment = f"/v1/appointments/{target}"
    visit = f"/v1/visits/{target}"
    day = today().isoformat()
    # Independent route inventory: static and UUID/date patterns, including both PATCH families.
    routes = (
        ("GET", "/v1/queue", "QUEUE_READ"),
        ("POST", f"{appointment}/check-in", "QUEUE_CHECK_IN"),
        ("POST", "/v1/intake/routine", "INTAKE_ROUTINE"),
        ("POST", "/v1/intake/emergency", "INTAKE_EMERGENCY"),
        ("POST", "/v1/intake/urgent", "INTAKE_URGENT"),
        ("GET", "/v1/settings/queue", "SETTINGS_QUEUE_READ"),
        ("PATCH", "/v1/settings/queue", "SETTINGS_QUEUE_UPDATE"),
        ("POST", "/v1/queue/placement", "QUEUE_PLACEMENT"),
        ("GET", "/v1/settings/admission", "ADMISSION_SETTINGS_READ"),
        ("PATCH", "/v1/settings/admission", "ADMISSION_MODE_UPDATE"),
        ("PATCH", "/v1/queue/capacity", "QUEUE_CAPACITY_UPDATE"),
        ("POST", "/v1/queue/pause", "QUEUE_PAUSE"),
        ("POST", "/v1/queue/admission-confirmations", "QUEUE_CONFIRMATION_ISSUE"),
        (
            "GET",
            "/v1/queue/admission-confirmations/usable",
            "QUEUE_CONFIRMATION_USABLE",
        ),
        (
            "POST",
            f"/v1/queue/admission-confirmations/{target}/revoke",
            "QUEUE_CONFIRMATION_REVOKE",
        ),
        ("POST", "/v1/queue/admit", "QUEUE_ADMIT"),
        ("GET", visit, "VISIT_READ"),
        ("POST", f"{visit}/complete", "VISIT_COMPLETE"),
        ("POST", f"{visit}/cancel-exceptional", "VISIT_CANCEL_EXCEPTIONAL"),
        ("POST", f"{visit}/extend-followup", "VISIT_FOLLOWUP"),
        ("POST", f"{appointment}/cancel", "APPOINTMENT_CANCEL"),
        ("POST", f"{appointment}/replace-urgent", "APPOINTMENT_REPLACE_URGENT"),
        ("POST", f"{appointment}/reschedule", "APPOINTMENT_RESCHEDULE"),
        (
            "POST",
            "/v1/appointments/bulk-reschedule/preview",
            "APPOINTMENT_BULK_PREVIEW",
        ),
        ("POST", "/v1/appointments/bulk-reschedule/commit", "APPOINTMENT_BULK_COMMIT"),
        ("GET", "/v1/reconciliation", "RECONCILIATION_READ"),
        ("POST", f"{appointment}/manual-no-show", "APPOINTMENT_MANUAL_NO_SHOW"),
        ("POST", f"/v1/days/{day}/review", "DAY_REVIEW"),
        ("POST", f"/v1/days/{day}/close", "DAY_CLOSE"),
    )
    before = _snapshot(clinic)
    for method, path, correct in routes:
        wrong = "INTAKE_EMERGENCY" if correct == "QUEUE_READ" else "QUEUE_READ"
        _assert_error(
            queue_request(
                clinic,
                clinic.owner,
                method=method,
                path=path,
                purpose=wrong,
                body=None if method == "GET" else {},
                key="" if method == "GET" else uuid4().hex,
            ),
            403,
            "FORBIDDEN",
            "Operation unavailable",
        )
    for body, wrong in (
        ({"expected_visit_revision": 0, "diagnosis": "Synthetic"}, "VISIT_FOLLOWUP"),
        (
            {"expected_visit_revision": 0, "followup_decision": {"kind": "DEFAULT"}},
            "VISIT_CLINICAL",
        ),
    ):
        _assert_error(
            queue_request(
                clinic,
                clinic.owner,
                method="PATCH",
                path=visit,
                purpose=wrong,
                body=body,
                key=uuid4().hex,
            ),
            403,
            "FORBIDDEN",
            "Operation unavailable",
        )
    assert _snapshot(clinic) == before
