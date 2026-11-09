"""Privileged read-only acceptance after the temporary T133 grant is revoked.

Runs in a separate, job-owned test controller. The terminal worker has no
migration password and cannot inspect hidden audit evidence itself.
"""

import json
import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

TERMINAL_AUDIT_ACTIONS = (
    "APPOINTMENT_CANCELLED",
    "APPOINTMENT_MARKED_NO_SHOW",
    "VISIT_EXAMINATION_CANCELLED",
    "VISIT_EXAMINATION_COMPLETED",
)


@pytest.fixture(scope="module")
def evidence():
    assert os.environ["CI"] == "true"
    assert os.environ["CP_T133_OBSERVER"] == "true"
    return json.loads(Path(os.environ["CP_T133_FIXTURE_PATH"]).read_text())


def _migration_login():
    return psycopg.connect(
        host="db",
        dbname="carepulse",
        user="carepulse_migration",
        password=Path("/run/secrets/migration_password").read_text().strip(),
        connect_timeout=5,
        options="-c statement_timeout=15000",
    )


def _audits(connection, target):
    return connection.execute(
        """
        SELECT id, actor_staff_id, actor_role, action, reason, correlation_id,
               before_state, after_state, occurred_at
        FROM public.sensitive_audit
        WHERE action = ANY(%s::text[])
          AND (before_state->>'appointment_id' = %s
               OR after_state->>'appointment_id' = %s)
        ORDER BY occurred_at, id
        """,
        (list(TERMINAL_AUDIT_ACTIONS), target, target),
    ).fetchall()


@pytest.mark.parametrize(
    ("name", "actor", "old", "new", "audit_action"),
    [
        (
            "cancel_booked_doctor",
            "doctor",
            "Booked",
            "Cancelled",
            "APPOINTMENT_CANCELLED",
        ),
        (
            "cancel_booked_receptionist",
            "receptionist",
            "Booked",
            "Cancelled",
            "APPOINTMENT_CANCELLED",
        ),
        (
            "cancel_waiting_doctor",
            "doctor",
            "Waiting",
            "Cancelled",
            "APPOINTMENT_CANCELLED",
        ),
        (
            "cancel_waiting_receptionist",
            "receptionist",
            "Waiting",
            "Cancelled",
            "APPOINTMENT_CANCELLED",
        ),
        (
            "cancel_in_exam_doctor",
            "doctor",
            "In Examination",
            "Cancelled",
            "VISIT_EXAMINATION_CANCELLED",
        ),
        (
            "complete_in_exam_doctor",
            "doctor",
            "In Examination",
            "Completed",
            "VISIT_EXAMINATION_COMPLETED",
        ),
        (
            "complete_in_exam_receptionist",
            "receptionist",
            "In Examination",
            "Completed",
            "VISIT_EXAMINATION_COMPLETED",
        ),
        (
            "no_show_old_doctor",
            "doctor",
            "Booked",
            "No-show",
            "APPOINTMENT_MARKED_NO_SHOW",
        ),
        (
            "no_show_old_receptionist",
            "receptionist",
            "Booked",
            "No-show",
            "APPOINTMENT_MARKED_NO_SHOW",
        ),
        ("deny_foreign", "doctor", "Booked", "Cancelled", "APPOINTMENT_CANCELLED"),
        ("release_requalify", "doctor", "Booked", "Cancelled", "APPOINTMENT_CANCELLED"),
        (
            "release_after_extension",
            "doctor",
            "Booked",
            "Cancelled",
            "APPOINTMENT_CANCELLED",
        ),
        (
            "consume_denies_second",
            "doctor",
            "In Examination",
            "Completed",
            "VISIT_EXAMINATION_COMPLETED",
        ),
        (
            "race_exam_cancel_first",
            "doctor",
            "In Examination",
            "Cancelled",
            "VISIT_EXAMINATION_CANCELLED",
        ),
        (
            "race_exam_complete_first",
            "doctor",
            "In Examination",
            "Completed",
            "VISIT_EXAMINATION_COMPLETED",
        ),
    ],
)
def test_committed_terminal_audit_links_exact_history_and_attestation(
    evidence, name, actor, old, new, audit_action
):
    case = evidence["cases"][name]
    with _migration_login() as conn:
        audits = _audits(conn, case["target"])
        assert len(audits) == 1, (name, audits)
        audit = audits[0]
        actor_id = UUID(evidence[actor])
        assert audit[1] == actor_id
        assert audit[2] == ("DOCTOR_OWNER" if actor == "doctor" else "RECEPTIONIST")
        assert audit[3] == audit_action
        assert audit[4] and audit[4].strip()
        assert audit[6]["appointment_id"] == case["target"]
        assert audit[7]["appointment_id"] == case["target"]
        assert audit[6]["status"] == old
        assert audit[7]["status"] == new
        before_revision = audit[6].get("revision", audit[6].get("appointment_revision"))
        after_revision = audit[7].get("revision", audit[7].get("appointment_revision"))
        assert before_revision == case["revision"]
        assert after_revision == case["revision"] + 1
        history = conn.execute(
            """
            SELECT id, actor_staff_id, actor_role, old_status, new_status,
                   occurred_at, appointment_revision
            FROM public.appointment_history
            WHERE appointment_id=%s::uuid AND appointment_revision=%s
            """,
            (case["target"], case["revision"] + 1),
        ).fetchone()
        assert history is not None
        assert history[1:5] == (actor_id, audit[2], old, new)
        assert history[5] == audit[8]
        if new == "Completed":
            attestation = conn.execute(
                """
                SELECT id, actor_staff_id, actor_role, examination_occurred,
                       completion_history_revision, completed_at
                FROM public.completion_attestations
                WHERE appointment_id=%s::uuid AND sensitive_audit_id=%s::uuid
                """,
                (case["target"], audit[0]),
            ).fetchone()
            assert attestation is not None
            assert attestation[1:] == (
                actor_id,
                audit[2],
                True,
                case["revision"] + 1,
                audit[8],
            )
            assert audit[5] == str(attestation[0])
        else:
            assert audit[5] == str(history[0])
            assert audit[6]["history_id"] == str(history[0])
            assert audit[7]["history_id"] == str(history[0])
            if new == "No-show":
                assert audit[7]["absence_confirmed"] is True
            assert (
                conn.execute(
                    """
                SELECT count(*) FROM public.completion_attestations
                WHERE appointment_id=%s::uuid
                """,
                    (case["target"],),
                ).fetchone()[0]
                == 0
            )
        if case["claim"] is not None:
            claim = conn.execute(
                """
                SELECT state, revision, terminal_history_revision, terminal_at
                FROM public.followup_claims WHERE id=%s::uuid
                """,
                (case["claim"],),
            ).fetchone()
            assert claim == (
                "CONSUMED" if new == "Completed" else "RELEASED",
                case["claim_revision"] + 1,
                case["revision"] + 1,
                audit[8],
            )


@pytest.mark.parametrize(
    "name",
    [
        "deny_in_exam_receptionist",
        "deny_stale_revision",
        "deny_blank_reason",
        "deny_missing_attestation",
        "deny_absent_attestation",
        "deny_missing_reason",
        "deny_wrong_tenant",
        "rollback_after_terminal_cancel",
        "rollback_after_terminal_complete",
        "deny_no_show_future",
        "deny_false_absence",
        "deny_stale_claim",
        "deny_stale_source",
        "deny_stale_authorization",
        "deny_stale_queue",
        "deny_extra_field",
        "deny_unsupported",
        "deny_disabled",
    ],
)
def test_denied_terminal_case_has_zero_audit_attestation_and_claim_effects(
    evidence, name
):
    case = evidence["cases"][name]
    with _migration_login() as conn:
        assert _audits(conn, case["target"]) == []
        assert (
            conn.execute(
                """
            SELECT count(*) FROM public.completion_attestations
            WHERE appointment_id=%s::uuid
            """,
                (case["target"],),
            ).fetchone()[0]
            == 0
        )
        actual = conn.execute(
            """
            SELECT a.status, a.revision, c.state, c.revision
            FROM public.appointments a
            LEFT JOIN public.followup_claims c
              ON (c.organization_id,c.appointment_id)=(a.organization_id,a.id)
            WHERE a.id=%s::uuid
            """,
            (case["target"],),
        ).fetchone()
        assert actual == (
            case["status"],
            case["revision"],
            None if case["claim"] is None else "ACTIVE",
            case.get("claim_revision"),
        )
        assert (
            conn.execute(
                """
            SELECT count(*) FROM public.appointment_history
            WHERE appointment_id=%s::uuid
            """,
                (case["target"],),
            ).fetchone()[0]
            == case["revision"] + 1
        )
        assert conn.execute(
            """
            SELECT count(*) FROM public.visits
            WHERE appointment_id=%s::uuid
            """,
            (case["target"],),
        ).fetchone()[0] == (1 if case["status"] == "In Examination" else 0)


def test_post_revoke_sql_acl_and_private_helpers_are_default_denied(evidence):
    with _migration_login() as conn:
        assert conn.execute(
            """
            SELECT NOT has_function_privilege('carepulse_runtime',
                'public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)',
                'EXECUTE'),
                NOT has_function_privilege('carepulse_runtime',
                'public.d07_complete_examined_visit_raw(uuid,uuid,bigint,boolean)',
                'EXECUTE')
            """
        ).fetchone() == (True, True)
        assert (
            conn.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]
            == "0033_d08_terminal_writer"
        )
        assert (
            conn.execute("SELECT count(*) FROM public.idempotency_receipts").fetchone()[
                0
            ]
            == evidence["baseline_receipts"]
        )
        assert (
            conn.execute("SELECT count(*) FROM public.outbox_events").fetchone()[0]
            == evidence["baseline_outbox"]
        )
