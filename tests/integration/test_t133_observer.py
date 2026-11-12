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
CURRENT_HEAD = "0039_t160_capacity_numeric"


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


def _audits(connection, tenant, target):
    return connection.execute(
        """
        SELECT id, actor_staff_id, actor_role, action, reason, correlation_id,
               before_state, after_state, occurred_at
        FROM public.sensitive_audit
        WHERE organization_id = %s::uuid AND action = ANY(%s::text[])
          AND (before_state->>'appointment_id' = %s
               OR after_state->>'appointment_id' = %s)
        ORDER BY occurred_at, id
        """,
        (tenant, list(TERMINAL_AUDIT_ACTIONS), target, target),
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
        audits = _audits(conn, evidence["organization"], case["target"])
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
                   occurred_at, appointment_revision, reason,
                   price_snapshot_appointment_id
            FROM public.appointment_history
            WHERE organization_id=%s::uuid AND appointment_id=%s::uuid
              AND appointment_revision=%s
            """,
            (evidence["organization"], case["target"], case["revision"] + 1),
        ).fetchone()
        assert history is not None
        assert history[1:5] == (actor_id, audit[2], old, new)
        assert history[5] == audit[8]
        assert history[8] == UUID(case["target"])
        if new == "Completed":
            attestation = conn.execute(
                """
                SELECT id, actor_staff_id, actor_role, examination_occurred,
                       completion_history_revision, completed_at
                FROM public.completion_attestations
                WHERE organization_id=%s::uuid AND appointment_id=%s::uuid
                  AND sensitive_audit_id=%s::uuid
                """,
                (evidence["organization"], case["target"], audit[0]),
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
            assert history[7] == audit[4]
            assert audit[5] == str(history[0])
            assert audit[6]["history_id"] == str(history[0])
            assert audit[7]["history_id"] == str(history[0])
            if new == "No-show":
                assert case["claim"] is not None
                assert audit[7]["absence_confirmed"] is True
            for state in (audit[6], audit[7]):
                assert state["claim_id"] == case["claim"]
                assert state["source_visit_id"] == case["source_visit"]
                assert state["source_appointment_id"] == case["source_appointment"]
                if old == "In Examination":
                    assert state["visit_id"] is not None
                else:
                    assert state["visit_id"] is None
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
                SELECT c.state, c.revision, c.terminal_history_revision, c.terminal_at,
                       c.organization_id, c.appointment_id, c.source_visit_id,
                       c.source_appointment_id, c.patient_id, c.doctor_staff_id,
                       p.entitlement_provenance = c.policy_snapshot,
                       a.patient_id = c.patient_id, a.doctor_staff_id = c.doctor_staff_id
                FROM public.followup_claims c
                JOIN public.appointments a ON
                    (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
                JOIN public.appointment_price_snapshots p ON
                    (p.organization_id,p.appointment_id)=(c.organization_id,c.appointment_id)
                WHERE c.organization_id=%s::uuid AND c.id=%s::uuid
                """,
                (evidence["organization"], case["claim"]),
            ).fetchone()
            assert claim is not None
            assert claim[:4] == (
                "CONSUMED" if new == "Completed" else "RELEASED",
                case["claim_revision"] + 1,
                case["revision"] + 1,
                audit[8],
            )
            assert claim[4] == UUID(evidence["organization"])
            assert claim[5] == UUID(case["target"])
            assert claim[6] == UUID(case["source_visit"])
            assert claim[7] == UUID(case["source_appointment"])
            assert claim[10:] == (True, True, True)
            source = conn.execute(
                """
                SELECT v.organization_id, v.appointment_id, v.patient_id,
                       v.doctor_staff_id, ca.examination_occurred,
                       ca.completion_history_revision,
                       h.occurred_at = ca.completed_at
                FROM public.visits v
                JOIN public.completion_attestations ca
                  ON (ca.organization_id,ca.visit_id)=(v.organization_id,v.id)
                JOIN public.appointment_history h
                  ON (h.organization_id,h.appointment_id,h.appointment_revision)=
                     (ca.organization_id,ca.appointment_id,ca.completion_history_revision)
                WHERE v.organization_id=%s::uuid AND v.id=%s::uuid
                """,
                (evidence["organization"], case["source_visit"]),
            ).fetchone()
            assert source == (
                UUID(evidence["organization"]),
                UUID(case["source_appointment"]),
                claim[8],
                claim[9],
                True,
                3,
                True,
            )
            if old == "In Examination" and new == "Cancelled":
                retained = conn.execute(
                    """
                    SELECT v.organization_id, v.appointment_id, v.patient_id,
                           v.doctor_staff_id, v.admission_history_revision,
                           h.old_status, h.new_status, h.occurred_at = v.admitted_at
                    FROM public.visits v
                    JOIN public.appointment_history h ON
                        (h.organization_id,h.appointment_id,h.appointment_revision)=
                        (v.organization_id,v.appointment_id,v.admission_history_revision)
                    WHERE v.organization_id=%s::uuid AND v.id=%s::uuid
                    """,
                    (evidence["organization"], audit[7]["visit_id"]),
                ).fetchone()
                assert retained == (
                    UUID(evidence["organization"]),
                    UUID(case["target"]),
                    claim[8],
                    claim[9],
                    2,
                    "Waiting",
                    "In Examination",
                    True,
                )


def test_new_examination_legacy_completion_keeps_clinical_evidence(evidence):
    case = evidence["cases"]["legacy_new_exam_complete"]
    tenant = evidence["organization"]
    with _migration_login() as conn:
        audits = _audits(conn, tenant, case["target"])
        assert len(audits) == 1
        audit = audits[0]
        assert audit[3] == "VISIT_EXAMINATION_COMPLETED"
        assert audit[1:3] == (UUID(evidence["doctor"]), "DOCTOR_OWNER")
        assert audit[6]["appointment_id"] == case["target"]
        assert audit[7]["status"] == "Completed"
        completion = conn.execute(
            """
            SELECT c.id, c.organization_id, c.appointment_id, c.visit_id,
                   c.completion_history_revision, c.completed_at,
                   c.examination_occurred, h.occurred_at, h.new_status,
                   v.appointment_id, a.service_type
            FROM public.completion_attestations c
            JOIN public.appointment_history h ON
                (h.organization_id,h.appointment_id,h.appointment_revision)=
                (c.organization_id,c.appointment_id,c.completion_history_revision)
            JOIN public.visits v ON
                (v.organization_id,v.id)=(c.organization_id,c.visit_id)
            JOIN public.appointments a ON
                (a.organization_id,a.id)=(c.organization_id,c.appointment_id)
            WHERE c.organization_id=%s::uuid AND c.appointment_id=%s::uuid
              AND c.sensitive_audit_id=%s::uuid
            """,
            (tenant, case["target"], audit[0]),
        ).fetchone()
        assert completion is not None
        assert audit[5] == str(completion[0])
        assert completion[1:3] == (UUID(tenant), UUID(case["target"]))
        assert completion[4:9] == (3, audit[8], True, audit[8], "Completed")
        assert completion[9:] == (UUID(case["target"]), "NEW_EXAMINATION")
        assert (
            conn.execute(
                """
            SELECT count(*) FROM public.followup_claims
            WHERE organization_id=%s::uuid AND appointment_id=%s::uuid
            """,
                (tenant, case["target"]),
            ).fetchone()[0]
            == 0
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
        "deny_non_followup_cancel",
        "deny_non_followup_no_show",
        "deny_non_followup_complete",
    ],
)
def test_denied_terminal_case_has_zero_audit_attestation_and_claim_effects(
    evidence, name
):
    case = evidence["cases"][name]
    with _migration_login() as conn:
        assert _audits(conn, evidence["organization"], case["target"]) == []
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
            == CURRENT_HEAD
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
