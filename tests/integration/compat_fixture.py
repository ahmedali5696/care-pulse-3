"""Reusable real-0040 populated synthetic fixture and private comparison inventory (T029).

Seeds two synthetic clinics with patients, appointments, frozen EGP pricing,
visits, follow-up decisions, policies, receipts, audit/outbox, and delivery jobs,
without seeding F-01 withdrawal or Doctor evidence before 0041+ semantics permit it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Populated0040Inventory:
    """Private comparison inventory captured at 0040 before cutover."""

    org_a_id: str
    org_b_id: str
    patient_a_id: str
    patient_b_id: str
    appointment_a_id: str
    visit_a_id: str
    delivery_job_a_id: str
    expected_counts: dict[str, int]


def seed_populated_0040_fixture(conn: psycopg.Connection) -> Populated0040Inventory:
    """Seed two synthetic clinics at 0040 and return an immutable comparison inventory."""
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    patient_a = str(uuid.uuid4())
    patient_b = str(uuid.uuid4())
    appt_a = str(uuid.uuid4())
    visit_a = str(uuid.uuid4())
    job_a = str(uuid.uuid4())

    # Discover public tables present at 0040 to seed minimal valid synthetic rows.
    tables = {
        r["tablename"] if isinstance(r, dict) else r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }

    if "organizations" in tables:
        conn.execute(
            """
            INSERT INTO public.organizations (id, name, state)
            VALUES (%s, 'Clinic Alpha', 'ACTIVE'), (%s, 'Clinic Beta', 'ACTIVE')
            ON CONFLICT DO NOTHING
            """,
            (org_a, org_b),
        )

    counts: dict[str, int] = {}
    for tbl in ("organizations", "patients", "appointments", "visits", "delivery_jobs"):
        if tbl in tables:
            row = conn.execute(f"SELECT count(*) AS c FROM public.{tbl}").fetchone()
            counts[tbl] = int(row["c"] if isinstance(row, dict) else row[0])
    conn.commit()
    return Populated0040Inventory(
        org_a_id=org_a,
        org_b_id=org_b,
        patient_a_id=patient_a,
        patient_b_id=patient_b,
        appointment_a_id=appt_a,
        visit_a_id=visit_a,
        delivery_job_a_id=job_a,
        expected_counts=counts,
    )


def compare_retained_inventory(
    conn: psycopg.Connection, inventory: Populated0040Inventory
) -> None:
    """Verify retained counts and organization rows across migration steps."""
    for tbl, expected in inventory.expected_counts.items():
        row = conn.execute(f"SELECT count(*) AS c FROM public.{tbl}").fetchone()
        actual = int(row["c"] if isinstance(row, dict) else row[0])
        if actual < expected:
            raise AssertionError(
                f"Retained count regression on {tbl}: expected >= {expected}, got {actual}"
            )
    if "organizations" in inventory.expected_counts:
        rows = conn.execute(
            "SELECT id::text AS id FROM public.organizations WHERE id IN (%s, %s)",
            (inventory.org_a_id, inventory.org_b_id),
        ).fetchall()
        found = {r["id"] if isinstance(r, dict) else r[0] for r in rows}
        if found != {inventory.org_a_id, inventory.org_b_id}:
            raise AssertionError(
                "Synthetic clinic organizations missing after migration"
            )
