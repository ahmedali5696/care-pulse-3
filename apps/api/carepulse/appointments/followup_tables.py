"""D-08 follow-up persistence metadata; Alembic owns DB constraints."""

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="public")

followup_claims = Table(
    "followup_claims",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("patient_id", UUID(as_uuid=True), nullable=False),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("source_visit_id", UUID(as_uuid=True), nullable=False),
    Column("source_appointment_id", UUID(as_uuid=True), nullable=False),
    Column("policy_snapshot", JSONB, nullable=False),
    Column("completion_local_date", Date, nullable=False),
    Column("approved_deadline", Date, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default=text("1")),
    Column("state", Text, nullable=False),
    Column("terminal_history_revision", BigInteger),
    Column("terminal_at", DateTime(timezone=True)),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "appointment_id"),
    UniqueConstraint("organization_id", "id", "source_visit_id", "appointment_id"),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "patient_id"],
        ["public.patients.organization_id", "public.patients.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "doctor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "source_visit_id"],
        ["public.visits.organization_id", "public.visits.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "source_appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        [
            "public.appointment_price_snapshots.organization_id",
            "public.appointment_price_snapshots.appointment_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id", "terminal_history_revision"],
        [
            "public.appointment_history.organization_id",
            "public.appointment_history.appointment_id",
            "public.appointment_history.appointment_revision",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
)

Index(
    "followup_source_unavailable",
    followup_claims.c.organization_id,
    followup_claims.c.source_visit_id,
    unique=True,
    postgresql_where=followup_claims.c.state.in_(("ACTIVE", "CONSUMED")),
)

followup_extensions = Table(
    "followup_extensions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("source_visit_id", UUID(as_uuid=True), nullable=False),
    Column("claim_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("prior_deadline", Date, nullable=False),
    Column("extended_deadline", Date, nullable=False),
    Column("issuing_doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("reason", Text, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expected_source_revision", BigInteger, nullable=False),
    Column("expected_claim_revision", BigInteger, nullable=False),
    Column("clinical_revision_id", UUID(as_uuid=True), nullable=False),
    Column("clinical_audit_id", UUID(as_uuid=True), nullable=False),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "clinical_revision_id"),
    UniqueConstraint("organization_id", "clinical_audit_id"),
    ForeignKeyConstraint(
        ["organization_id", "claim_id", "source_visit_id", "appointment_id"],
        [
            "public.followup_claims.organization_id",
            "public.followup_claims.id",
            "public.followup_claims.source_visit_id",
            "public.followup_claims.appointment_id",
        ],
    ),
    ForeignKeyConstraint(
        ["organization_id", "source_visit_id"],
        ["public.visits.organization_id", "public.visits.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "issuing_doctor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "clinical_revision_id"],
        ["public.clinical_revisions.organization_id", "public.clinical_revisions.id"],
    ),
)
