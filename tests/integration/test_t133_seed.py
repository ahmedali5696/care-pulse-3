"""Privileged synthetic setup, run before granting the T133 runtime writer.

This controller uses migration credentials only while populating immutable
fixtures. The separate terminal test container receives runtime credentials only.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from carepulse.appointments.persistence import persist_appointment
from carepulse.db import transaction
from carepulse.policies.versions import append_version
from sqlalchemy import text
from test_appointment_persistence import (
    _admit_saved,
    _draft,
    _patient,
    _query,
    _seed_waiting_entry,
)
from test_appointment_persistence import appointment_storage as appointment_storage  # noqa: PLC0414
from test_followup_persistence import _v2_booking, _v2_command, _v2_record, _v2_source
from test_visit_persistence import _receptionist


def _waiting(connection, tenant, doctor, saved):
    target = UUID(saved["id"])
    connection.execute(
        text("""
            UPDATE public.appointments SET status='Waiting', revision=revision+1
            WHERE organization_id=:tenant AND id=:target
        """),
        {"tenant": tenant, "target": target},
    )
    connection.execute(
        text("""
            INSERT INTO public.appointment_history (
                id, organization_id, appointment_id, appointment_revision,
                action, old_status, new_status, old_service_date, new_service_date,
                old_number, new_number, actor_staff_id, actor_role,
                reason, price_snapshot_appointment_id)
            SELECT gen_random_uuid(), organization_id, id, 1, 'STATUS',
                   'Booked', 'Waiting', service_date, service_date,
                   current_number, current_number, :doctor, 'DOCTOR_OWNER',
                   'Verified synthetic arrival', id
            FROM public.appointments WHERE organization_id=:tenant AND id=:target
        """),
        {"tenant": tenant, "target": target, "doctor": doctor},
    )
    _seed_waiting_entry(connection, tenant, doctor, saved, doctor)
    return connection.scalar(
        text("""
            SELECT q.revision FROM public.queue_days q
            JOIN public.queue_entries e ON
                (q.organization_id,q.id)=(e.organization_id,e.queue_day_id)
            WHERE e.organization_id=:tenant AND e.appointment_id=:target
              AND e.disposition='ACTIVE'
        """),
        {"tenant": tenant, "target": target},
    )


def test_seed_terminal_cases_for_runtime_only_worker(appointment_storage):
    runtime, migration, (tenant, foreign_tenant), (doctor, foreign_doctor) = (
        appointment_storage
    )
    receptionist = _receptionist(migration, tenant)
    disabled_receptionist = _receptionist(migration, tenant)
    cases = {}
    definitions = {
        "cancel_booked_doctor": "Booked",
        "cancel_booked_receptionist": "Booked",
        "cancel_waiting_doctor": "Waiting",
        "cancel_waiting_receptionist": "Waiting",
        "cancel_in_exam_doctor": "In Examination",
        "deny_in_exam_receptionist": "In Examination",
        "complete_in_exam_doctor": "In Examination",
        "complete_in_exam_receptionist": "In Examination",
        "deny_stale_revision": "Booked",
        "deny_blank_reason": "Booked",
        "deny_missing_attestation": "In Examination",
        "deny_foreign": "Booked",
        "deny_disabled": "Booked",
        "deny_false_absence": "Booked",
        "deny_stale_claim": "Booked",
        "deny_stale_source": "Booked",
        "deny_stale_authorization": "Booked",
        "deny_stale_queue": "Waiting",
        "race_waiting_attendance_first": "Waiting",
        "race_waiting_terminal_first": "Waiting",
        "deny_extra_field": "Booked",
        "deny_unsupported": "Booked",
        "release_requalify": "Booked",
        "release_after_extension": "Booked",
        "consume_denies_second": "In Examination",
    }
    for name, status in definitions.items():
        with transaction(runtime, tenant) as connection:
            source = _v2_source(connection, tenant, doctor)
            claim = _v2_command(
                connection, "create_followup_claim", source, **_v2_booking(source)
            )
            record = _v2_record(connection, claim)
            queue_revision = None
            if status == "Waiting":
                queue_revision = _waiting(connection, tenant, doctor, record["saved"])
            elif status == "In Examination":
                _admit_saved(connection, tenant, doctor, record["saved"])
            source_revision = connection.scalar(
                text("""
                    SELECT source_revision FROM public.followup_source_state
                    WHERE organization_id=:tenant AND visit_id=:visit
                """),
                {"tenant": tenant, "visit": source["visit"]},
            )
            authorization_revision = connection.scalar(
                text("""
                    SELECT coalesce(max(authorization_revision), 0)
                    FROM public.followup_authorization_events
                    WHERE organization_id=:tenant AND claim_id=:claim
                """),
                {"tenant": tenant, "claim": claim},
            )
            claim_revision = connection.scalar(
                text("""
                    SELECT revision FROM public.followup_claims
                    WHERE organization_id=:tenant AND id=:claim
                """),
                {"tenant": tenant, "claim": claim},
            )
            cases[name] = {
                "target": str(record["id"]),
                "claim": str(claim),
                "source_visit": str(source["visit"]),
                "source_appointment": str(source["appointment"]),
                "source_date": source["day"].isoformat(),
                "status": status,
                "revision": {"Booked": 0, "Waiting": 1, "In Examination": 2}[status],
                "claim_revision": claim_revision,
                "source_revision": source_revision,
                "authorization_revision": authorization_revision,
                "queue_day_revision": queue_revision,
            }
    # Observe both lock-order directions without changing database-owned UUIDs.
    for outcome in ("CANCEL", "COMPLETE"):
        for winner in ("extension", "terminal"):
            for source_first in (True, False):
                for attempt in range(20):
                    with transaction(runtime, tenant) as connection:
                        source = _v2_source(connection, tenant, doctor)
                        claim = _v2_command(
                            connection,
                            "create_followup_claim",
                            source,
                            **_v2_booking(source),
                        )
                        record = _v2_record(connection, claim)
                        oriented = source["appointment"] < record["id"]
                        if oriented != source_first:
                            continue
                        if outcome == "COMPLETE":
                            _admit_saved(connection, tenant, doctor, record["saved"])
                        claim_revision, authorization_revision = connection.execute(
                            text("""
                                SELECT c.revision,
                                       (SELECT coalesce(max(e.authorization_revision),0)
                                        FROM public.followup_authorization_events e
                                        WHERE e.organization_id=c.organization_id
                                          AND e.claim_id=c.id)
                                FROM public.followup_claims c
                                WHERE c.organization_id=:tenant AND c.id=:claim
                            """),
                            {"tenant": tenant, "claim": claim},
                        ).one()
                        source_revision = connection.scalar(
                            text("""
                                SELECT source_revision FROM public.followup_source_state
                                WHERE organization_id=:tenant AND visit_id=:visit
                            """),
                            {"tenant": tenant, "visit": source["visit"]},
                        )
                        name = (
                            f"race_{outcome.lower()}_{winner}_"
                            f"{'source_first' if source_first else 'target_first'}"
                        )
                        cases[name] = {
                            "target": str(record["id"]),
                            "claim": str(claim),
                            "source_visit": str(source["visit"]),
                            "source_appointment": str(source["appointment"]),
                            "source_date": source["day"].isoformat(),
                            "status": "In Examination"
                            if outcome == "COMPLETE"
                            else "Booked",
                            "revision": 2 if outcome == "COMPLETE" else 0,
                            "claim_revision": claim_revision,
                            "source_revision": source_revision,
                            "authorization_revision": authorization_revision,
                            "queue_day_revision": None,
                        }
                        break
                else:
                    raise AssertionError(
                        f"Could not seed {outcome}/{winner}/{source_first} orientation"
                    )
    cairo_today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    for name, offset in (
        ("no_show_old_doctor", -2),
        ("no_show_old_receptionist", -2),
        ("deny_no_show_future", 5),
    ):
        with transaction(runtime, tenant) as connection:
            saved = persist_appointment(
                connection,
                _draft(
                    _patient(connection, tenant),
                    doctor,
                    service_date=cairo_today + timedelta(days=offset),
                ),
                _query(tenant, doctor),
            )
            cases[name] = {
                "target": saved["id"],
                "status": "Booked",
                "revision": 0,
                "claim": None,
            }
    # Prospective clinic price change: previously booked prices remain frozen.
    # A fresh claim after release must use the current price and current window.
    with transaction(runtime, tenant) as connection:
        connection.execute(
            text("SELECT set_config('app.staff_id', :doctor, true)"),
            {"doctor": str(doctor)},
        )
        head = connection.execute(
            text("""
                SELECT h.revision, v.payload FROM public.clinic_policy_heads h
                JOIN public.clinic_policy_versions v
                  ON (v.organization_id,v.kind,v.version)=
                     (h.organization_id,h.kind,h.current_version)
                WHERE h.organization_id=:tenant AND h.kind='PRICE'
            """),
            {"tenant": tenant},
        ).one()
        changed_price = dict(head.payload)
        changed_price["preferential_followup_egp"] = "75.00"
        append_version(
            connection,
            tenant,
            "PRICE",
            changed_price,
            head.revision,
            actor_staff_id=str(doctor),
        )
    with migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.staff_accounts
                SET enabled=false, security_revision=security_revision+1
                WHERE organization_id=:tenant AND id=:staff
            """),
            {"tenant": tenant, "staff": disabled_receptionist},
        )
    with migration.connect() as connection:
        baseline_receipts = connection.scalar(
            text("SELECT count(*) FROM public.idempotency_receipts")
        )
        baseline_outbox = connection.scalar(
            text("SELECT count(*) FROM public.outbox_events")
        )
    # The parent process grants only after this persisted synthetic dataset exists.
    result = {
        "organization": str(tenant),
        "doctor": str(doctor),
        "receptionist": str(receptionist),
        "disabled_receptionist": str(disabled_receptionist),
        "foreign_organization": str(foreign_tenant),
        "foreign_doctor": str(foreign_doctor),
        "cases": cases,
        "baseline_receipts": baseline_receipts,
        "baseline_outbox": baseline_outbox,
    }
    target = Path(os.environ["CP_T133_FIXTURE_PATH"])
    target.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    target.chmod(0o600)
