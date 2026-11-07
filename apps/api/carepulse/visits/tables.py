"""D-07 Visit metadata; Alembic owns PostgreSQL constraints and triggers."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="public")

visits = Table(
    "visits",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("patient_id", UUID(as_uuid=True), nullable=False),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("admission_history_revision", BigInteger, nullable=False),
    Column("admitted_at", DateTime(timezone=True), nullable=False),
    Column("clinical_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("examination", Text),
    Column("findings", Text),
    Column("diagnosis", Text),
    Column("medical_notes", Text),
    Column("treatment", Text),
    Column("tests", Text),
    Column("followup_decision", JSONB(none_as_null=True)),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "appointment_id"),
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
        ["organization_id", "appointment_id", "admission_history_revision"],
        [
            "public.appointment_history.organization_id",
            "public.appointment_history.appointment_id",
            "public.appointment_history.appointment_revision",
        ],
    ),
)

clinical_revisions = Table(
    "clinical_revisions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("visit_id", UUID(as_uuid=True), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("old_values", JSONB, nullable=False),
    Column("new_values", JSONB, nullable=False),
    Column("actor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("actor_role", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("reason", Text),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "visit_id", "revision"),
    ForeignKeyConstraint(
        ["organization_id", "visit_id"],
        ["public.visits.organization_id", "public.visits.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
)

completion_attestations = Table(
    "completion_attestations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("visit_id", UUID(as_uuid=True), nullable=False),
    Column("completion_history_revision", BigInteger, nullable=False),
    Column("examination_occurred", Boolean, nullable=False),
    Column("actor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("actor_role", Text, nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("sensitive_audit_id", UUID(as_uuid=True), nullable=False),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "appointment_id"),
    UniqueConstraint("organization_id", "sensitive_audit_id"),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "visit_id"],
        ["public.visits.organization_id", "public.visits.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id", "completion_history_revision"],
        [
            "public.appointment_history.organization_id",
            "public.appointment_history.appointment_id",
            "public.appointment_history.appointment_revision",
        ],
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "sensitive_audit_id"],
        ["public.sensitive_audit.organization_id", "public.sensitive_audit.id"],
    ),
)

followup_source_state = Table(
    "followup_source_state",
    metadata,
    Column("organization_id", UUID(as_uuid=True), primary_key=True),
    Column("visit_id", UUID(as_uuid=True), primary_key=True),
    Column("source_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("provenance_state", Text, nullable=False, server_default=text("'UNKNOWN'")),
    ForeignKeyConstraint(
        ["organization_id", "visit_id"],
        ["public.visits.organization_id", "public.visits.id"],
    ),
)
