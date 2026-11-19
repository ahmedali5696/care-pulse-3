"""Populated, referentially valid 0040 cutover fixture and retained-row proof (T029)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass, replace

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

TENANT_TABLES = (
    "staff_accounts",
    "invitations",
    "patients",
    "clinic_policy_versions",
    "clinic_policy_heads",
    "appointment_day_counters",
    "appointments",
    "appointment_number_assignments",
    "appointment_price_snapshots",
    "appointment_history",
    "queue_days",
    "queue_entries",
    "doctor_controls",
    "visits",
    "clinical_revisions",
    "clinical_audit",
    "idempotency_receipts",
    "sensitive_audit",
    "outbox_events",
    "delivery_jobs",
)


@dataclass(frozen=True)
class Populated0040Inventory:
    """Source identities and original field values retained across each successor."""

    org_a_id: str
    org_b_id: str
    patient_a_id: str
    patient_b_id: str
    appointment_a_id: str
    visit_a_id: str
    delivery_job_a_id: str
    operator_id: str
    owner_user_ids: tuple[str, str]
    receptionist_user_id: str
    nonce_reference: tuple[str, str]
    expected_rows: dict[str, tuple[dict, ...]]


def _retained_rows(
    conn: psycopg.Connection, inventory: Populated0040Inventory
) -> dict[str, tuple[dict, ...]]:
    """Capture complete original row values; future additive columns are allowed."""
    tenants = (inventory.org_a_id, inventory.org_b_id)
    queries = {
        "organizations": ("id IN (%s, %s)", tenants),
        "operator_identities": ("id = %s", (inventory.operator_id,)),
        '"user"': (
            "id IN (%s, %s, %s)",
            (*inventory.owner_user_ids, inventory.receptionist_user_id),
        ),
        "request_nonces": (
            "key_id = %s AND nonce = %s",
            inventory.nonce_reference,
        ),
        "protected_delivery_material": (
            "job_id IN (SELECT id FROM public.delivery_jobs WHERE organization_id IN (%s, %s))",
            tenants,
        ),
    }
    for table in TENANT_TABLES:
        queries[table] = ("organization_id IN (%s, %s)", tenants)

    results = {}
    for table, (predicate, params) in queries.items():
        statement = sql.SQL("SELECT to_jsonb(t) AS row FROM public.{} t WHERE ").format(
            sql.Identifier(table.strip('"'))
        ) + sql.SQL(predicate)
        results[table] = tuple(row["row"] for row in conn.execute(statement, params))
    return results


def seed_populated_0040_fixture(conn: psycopg.Connection) -> Populated0040Inventory:
    """Create two real 0040 tenants with verified Owner and relational evidence."""

    def new_id() -> str:
        return str(uuid.uuid4())

    operator = new_id()
    orgs = (new_id(), new_id())
    patients = (new_id(), new_id())
    appointments = (new_id(), new_id())
    owner_users = (f"compat-owner-{new_id()}", f"compat-owner-{new_id()}")
    receptionist_user = f"compat-receptionist-{new_id()}"
    nonce_reference = (f"synthetic-0040-{new_id()}", f"nonce-{new_id()}")
    visit = new_id()
    delivery_job = new_id()

    conn.execute(
        """INSERT INTO public.operator_identities
               (id, credential_digest, issued_by_reference)
               VALUES (%s, %s, 'synthetic-0040-cutover')""",
        (operator, f"synthetic:{operator}"),
    )
    conn.execute(
        """INSERT INTO public.request_nonces (key_id, nonce, signed_at)
           VALUES (%s, %s, clock_timestamp())""",
        nonce_reference,
    )

    for index, (org, patient, appointment, owner_user) in enumerate(
        zip(orgs, patients, appointments, owner_users, strict=True)
    ):
        doctor, invitation, assignment = new_id(), new_id(), new_id()
        email = f"compat-owner-{index}-{new_id()}@example.invalid"
        date = "2026-10-27"
        conn.execute(
            """INSERT INTO public.organizations (id, branch, created_by_operator_id)
               VALUES (%s, %s, %s)""",
            (
                org,
                f"Synthetic 0040 Clinic {'Alpha' if index == 0 else 'Beta'}",
                operator,
            ),
        )
        conn.execute(
            """INSERT INTO public."user"
               (id, name, email, "emailVerified", "createdAt", "updatedAt")
               VALUES (%s, 'Synthetic 0040 Doctor', %s, true,
                       clock_timestamp(), clock_timestamp())""",
            (owner_user, email),
        )
        conn.execute(
            """INSERT INTO public.staff_accounts
               (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
               VALUES (%s, %s, %s, %s, 'DOCTOR_OWNER', true, true)""",
            (doctor, org, owner_user, email),
        )
        actor, actor_role = doctor, "DOCTOR_OWNER"
        conn.execute(
            """INSERT INTO public.invitations
               (id, organization_id, purpose, target_email, proof_digest,
                issued_by_operator_id, expires_at)
               VALUES (%s, %s, 'FIRST_OWNER', %s, %s, %s,
                       clock_timestamp() + interval '20 minutes')""",
            (
                invitation,
                org,
                email,
                hashlib.sha256(invitation.encode()).hexdigest(),
                operator,
            ),
        )

        if index == 0:
            # Genuine first-Owner delivery evidence; ciphertext contains no real proof.
            conn.execute(
                """INSERT INTO public.delivery_jobs
                   (id, organization_id, invitation_id, proof_reference, purpose,
                    recipient, template, logical_key, proof_expires_at)
                   SELECT %s, organization_id, id, id::text, 'FIRST_OWNER',
                          target_email, 'first-owner', %s, expires_at
                   FROM public.invitations WHERE id = %s""",
                (delivery_job, f"synthetic:{invitation}", invitation),
            )
            conn.execute(
                """INSERT INTO public.protected_delivery_material
                   (job_id, delivery_key_id, ciphertext_base64url)
                   VALUES (%s, 'synthetic-0040-key', %s)""",
                (
                    delivery_job,
                    base64.urlsafe_b64encode(b"x" * 384).decode().rstrip("="),
                ),
            )
        else:
            # Exercise a real CLAIMED lease transition, then settle it before
            # drain. No SMTP was attempted and no SENT outcome is asserted.
            past_claim_job, claim_owner = new_id(), new_id()
            conn.execute(
                """INSERT INTO public.delivery_jobs
                   (id, organization_id, invitation_id, proof_reference, purpose,
                    recipient, template, logical_key, proof_expires_at)
                   SELECT %s, organization_id, id, id::text, 'FIRST_OWNER',
                          target_email, 'first-owner', %s, expires_at
                   FROM public.invitations WHERE id = %s""",
                (past_claim_job, f"synthetic:{invitation}", invitation),
            )
            conn.execute(
                """INSERT INTO public.protected_delivery_material
                   (job_id, delivery_key_id, ciphertext_base64url)
                   VALUES (%s, 'synthetic-0040-key', %s)""",
                (
                    past_claim_job,
                    base64.urlsafe_b64encode(b"y" * 384).decode().rstrip("="),
                ),
            )
            conn.execute(
                """UPDATE public.delivery_jobs
                   SET state = 'CLAIMED', attempts = 1, claim_owner = %s,
                       claim_generation = 1, claimed_at = clock_timestamp(),
                       lease_expires_at = clock_timestamp() + interval '5 minutes'
                   WHERE id = %s""",
                (claim_owner, past_claim_job),
            )
            conn.execute(
                """SELECT set_config('app.delivery_claim_owner', %s, true),
                          set_config('app.delivery_claim_generation', '1', true)""",
                (claim_owner,),
            )
            conn.execute(
                """UPDATE public.delivery_jobs
                   SET state = 'FAILED', claim_owner = NULL, claimed_at = NULL,
                       lease_expires_at = NULL,
                       last_outcome = 'synthetic:no-transport-attempted'
                   WHERE id = %s""",
                (past_claim_job,),
            )
            conn.execute(
                "DELETE FROM public.protected_delivery_material WHERE job_id = %s",
                (past_claim_job,),
            )

        conn.execute(
            """UPDATE public.invitations SET state = 'ACCEPTED',
               accepted_by_user_id = %s,
               acceptance_reference = 'SIGNED_OWNER_ACTIVATION' WHERE id = %s""",
            (owner_user, invitation),
        )
        conn.execute(
            "UPDATE public.organizations SET state = 'ACTIVE' WHERE id = %s", (org,)
        )
        conn.execute(
            """INSERT INTO public.sensitive_audit
               (id, organization_id, actor_role, action, correlation_id)
               VALUES (%s, %s, 'SYSTEM', 'FIRST_OWNER_ACTIVATED', %s)""",
            (new_id(), org, invitation),
        )
        if index == 1:
            # Only an activated clinic can issue a genuine Receptionist invitation.
            actor, actor_role = new_id(), "RECEPTIONIST"
            receptionist_email = f"compat-receptionist-{new_id()}@example.invalid"
            conn.execute(
                """INSERT INTO public."user"
                   (id, name, email, "emailVerified", "createdAt", "updatedAt")
                   VALUES (%s, 'Synthetic Receptionist', %s, true,
                           clock_timestamp(), clock_timestamp())""",
                (receptionist_user, receptionist_email),
            )
            conn.execute(
                """INSERT INTO public.staff_accounts
                   (id, organization_id, auth_user_id, verified_email, role,
                    enabled, verified)
                   VALUES (%s, %s, %s, %s, 'RECEPTIONIST', true, true)""",
                (actor, org, receptionist_user, receptionist_email),
            )
            receptionist_invitation = new_id()
            conn.execute(
                """INSERT INTO public.invitations
                   (id, organization_id, staff_id, purpose, target_email,
                    proof_digest, issued_by_staff_id, expires_at)
                   VALUES (%s, %s, %s, 'RECEPTIONIST', %s, %s, %s,
                           clock_timestamp() + interval '20 minutes')""",
                (
                    receptionist_invitation,
                    org,
                    actor,
                    receptionist_email,
                    hashlib.sha256(receptionist_invitation.encode()).hexdigest(),
                    doctor,
                ),
            )
            conn.execute(
                """UPDATE public.invitations SET state = 'ACCEPTED',
                   accepted_by_user_id = %s,
                   acceptance_reference = 'SYNTHETIC_RECEPTIONIST_ACCEPTANCE'
                   WHERE id = %s""",
                (receptionist_user, receptionist_invitation),
            )

        # Default PRICE policy is immutable; publish a configured version normally.
        conn.execute(
            """INSERT INTO public.clinic_policy_versions
               (organization_id, kind, version, payload)
               VALUES (%s, 'PRICE', 2, %s)""",
            (
                org,
                Jsonb(
                    {
                        "standard_new_exam_egp": 100,
                        "preferential_followup_egp": 30,
                        "urgent_base_egp": None,
                        "urgent_enabled": False,
                    }
                ),
            ),
        )
        conn.execute(
            """UPDATE public.clinic_policy_heads
               SET current_version = 2, revision = 2
               WHERE organization_id = %s AND kind = 'PRICE'""",
            (org,),
        )
        conn.execute(
            """INSERT INTO public.patients
               (id, organization_id, name_parts, date_of_birth)
               VALUES (%s, %s, %s, DATE '2001-01-01')""",
            (patient, org, ["Synthetic", "Cutover", "Alpha" if index == 0 else "Beta"]),
        )
        conn.execute(
            """INSERT INTO public.appointment_day_counters
               (organization_id, service_date, last_number) VALUES (%s, %s, 1)""",
            (org, date),
        )
        conn.execute(
            """INSERT INTO public.appointments
               (id, organization_id, patient_id, doctor_staff_id, service_date,
                status, service_type, classification, lane, booking_mode,
                current_assignment_id, current_number)
               VALUES (%s, %s, %s, %s, %s, 'Booked', 'NEW_EXAMINATION',
                       'ROUTINE', 'MAIN', 'OPEN', %s, 1)""",
            (appointment, org, patient, doctor, date, assignment),
        )
        conn.execute(
            """INSERT INTO public.appointment_number_assignments
               (id, organization_id, appointment_id, service_date, number)
               VALUES (%s, %s, %s, %s, 1)""",
            (assignment, org, appointment, date),
        )
        conn.execute(
            """INSERT INTO public.appointment_price_snapshots
               (organization_id, appointment_id, service_type, classification,
                base_egp, discount_type, discount_value, is_free, net_egp,
                pricing_version, creation_standard_base_egp)
               VALUES (%s, %s, 'NEW_EXAMINATION', 'ROUTINE',
                       100.00, 'NONE', 0, false, 100.00, 2, 100.00)""",
            (org, appointment),
        )
        conn.execute(
            """INSERT INTO public.appointment_history
               (id, organization_id, appointment_id, appointment_revision, action,
                new_status, new_service_date, new_number, actor_staff_id, actor_role,
                price_snapshot_appointment_id)
               VALUES (%s, %s, %s, 0, 'CREATED', 'Booked', %s, 1, %s,
                       %s, %s)""",
            (new_id(), org, appointment, date, actor, actor_role, appointment),
        )

        if index == 0:
            # Waiting attendance and its closure are required for genuine Visit admission.
            day, entry = new_id(), new_id()
            conn.execute(
                "UPDATE public.appointments SET status = 'Waiting', revision = 1 WHERE id = %s",
                (appointment,),
            )
            conn.execute(
                """INSERT INTO public.appointment_history
                   (id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status, old_service_date, new_service_date,
                    old_number, new_number, actor_staff_id, actor_role,
                    price_snapshot_appointment_id)
                   VALUES (%s, %s, %s, 1, 'STATUS', 'Booked', 'Waiting',
                           %s, %s, 1, 1, %s, 'DOCTOR_OWNER', %s)""",
                (new_id(), org, appointment, date, date, doctor, appointment),
            )
            conn.execute(
                """INSERT INTO public.queue_days
                   (id, organization_id, doctor_staff_id, service_date, lane,
                    queue_policy_version, arrival_counter)
                   VALUES (%s, %s, %s, %s, 'MAIN', 1, 1)""",
                (day, org, doctor, date),
            )
            conn.execute(
                """INSERT INTO public.queue_entries
                   (id, organization_id, queue_day_id, appointment_id, patient_id,
                    doctor_staff_id, service_date, lane, routine_rank, disposition)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'MAIN', 1, 'ACTIVE')""",
                (entry, org, day, appointment, patient, doctor, date),
            )
            conn.execute(
                """SELECT * FROM public.lock_doctor_admission_capacity(
                   %s, %s, %s, 'MAIN')""",
                (org, doctor, date),
            )
            conn.execute(
                """UPDATE public.appointments SET status = 'In Examination',
                   revision = 2 WHERE id = %s""",
                (appointment,),
            )
            conn.execute(
                """INSERT INTO public.appointment_history
                   (id, organization_id, appointment_id, appointment_revision,
                    action, old_status, new_status, old_service_date, new_service_date,
                    old_number, new_number, actor_staff_id, actor_role,
                    price_snapshot_appointment_id)
                   VALUES (%s, %s, %s, 2, 'STATUS', 'Waiting', 'In Examination',
                           %s, %s, 1, 1, %s, 'DOCTOR_OWNER', %s)""",
                (new_id(), org, appointment, date, date, doctor, appointment),
            )
            conn.execute(
                """UPDATE public.queue_entries SET disposition = 'CLOSED',
                   closure_history_revision = 2, revision = revision + 1 WHERE id = %s""",
                (entry,),
            )
            conn.execute(
                """UPDATE public.queue_days SET revision = revision + 1,
                   routine_cursor = 1 WHERE id = %s""",
                (day,),
            )
            conn.execute(
                """INSERT INTO public.visits
                   (id, organization_id, appointment_id, patient_id, doctor_staff_id,
                    service_date, admission_history_revision, admitted_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 2, clock_timestamp())""",
                (visit, org, appointment, patient, doctor, date),
            )
            # The 0040 guard requires an empty Visit at admission and the 0028
            # fence prohibits retroactively assigning legacy followup_decision.
            # A genuine guarded clinical correction records Doctor provenance.
            conn.execute(
                "SELECT set_config('app.clinical_change_kind', 'CORRECTION', true), "
                "set_config('app.clinical_reason', 'Synthetic examination correction', true)"
            )
            conn.execute(
                """UPDATE public.visits SET examination = 'Synthetic examination',
                   findings = 'Synthetic clinical finding' WHERE id = %s""",
                (visit,),
            )
            revision = conn.execute(
                """SELECT id FROM public.clinical_revisions
                   WHERE organization_id = %s AND visit_id = %s AND revision = 1""",
                (org, visit),
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO public.clinical_audit
                   (id, organization_id, doctor_staff_id, clinical_revision, correlation_id)
                   VALUES (%s, %s, %s, 1, %s)""",
                (new_id(), org, doctor, str(revision)),
            )

        conn.execute(
            """INSERT INTO public.idempotency_receipts
               (id, scope_kind, scope_id, actor_kind, actor_id, organization_id,
                staff_id, operation, idempotency_key, request_digest,
                result_projection, result_revision)
               VALUES (%s, 'TENANT', %s, 'STAFF', %s, %s, %s,
                       'appointments.book', %s, %s, %s, 0)""",
            (
                new_id(),
                org,
                actor,
                org,
                actor,
                f"synthetic-{appointment}",
                hashlib.sha256(appointment.encode()).hexdigest(),
                Jsonb(
                    {"appointment_id": appointment, "number": 1, "net_egp": "100.00"}
                ),
            ),
        )
        conn.execute(
            """INSERT INTO public.sensitive_audit
               (id, organization_id, actor_staff_id, actor_role, action,
                reason, correlation_id)
               VALUES (%s, %s, %s, 'DOCTOR_OWNER', 'SYNTHETIC_SETTINGS_CHANGE',
                       '0040 cutover fixture', %s)""",
            (new_id(), org, doctor, new_id()),
        )
        conn.execute(
            """INSERT INTO public.outbox_events
               (id, organization_id, event_type, aggregate_type, aggregate_id, payload)
               VALUES (%s, %s, 'APPOINTMENT_BOOKED', 'appointment', %s, %s)""",
            (
                new_id(),
                org,
                appointment,
                Jsonb({"appointment_id": appointment, "patient_id": patient}),
            ),
        )

    inventory = Populated0040Inventory(
        org_a_id=orgs[0],
        org_b_id=orgs[1],
        patient_a_id=patients[0],
        patient_b_id=patients[1],
        appointment_a_id=appointments[0],
        visit_a_id=visit,
        delivery_job_a_id=delivery_job,
        operator_id=operator,
        owner_user_ids=owner_users,
        receptionist_user_id=receptionist_user,
        nonce_reference=nonce_reference,
        expected_rows={},
    )
    rows = _retained_rows(conn, inventory)
    for table, values in rows.items():
        if not values:
            raise AssertionError(f"Unpopulated 0040 fixture table: {table}")
    conn.commit()
    return replace(inventory, expected_rows=rows)


def compare_retained_inventory(
    conn: psycopg.Connection, inventory: Populated0040Inventory
) -> None:
    """Require every captured source row and original field value to survive."""
    actual_rows = _retained_rows(conn, inventory)
    for table, originals in inventory.expected_rows.items():
        remaining = list(actual_rows[table])
        for row_index, original in enumerate(originals):
            matched = next(
                (
                    index
                    for index, row in enumerate(remaining)
                    if all(
                        key in row and row[key] == value
                        for key, value in original.items()
                    )
                ),
                None,
            )
            if matched is None:
                raise AssertionError(
                    f"Retained 0040 row/field regression: {table}, row index {row_index}"
                )
            remaining.pop(matched)
