"""D-06 queue persistence metadata; no operational queue commands."""

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
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData(schema="public")

queue_days = Table(
    "queue_days",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("lane", Text, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default=text("1")),
    Column("queue_policy_kind", Text, nullable=False, server_default=text("'QUEUE'")),
    Column("queue_policy_version", BigInteger, nullable=False),
    Column("arrival_counter", BigInteger, nullable=False, server_default=text("0")),
    Column("routine_cursor", BigInteger, nullable=False, server_default=text("0")),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "service_date", "lane"),
    ForeignKeyConstraint(
        ["organization_id", "doctor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "queue_policy_kind", "queue_policy_version"],
        [
            "public.clinic_policy_versions.organization_id",
            "public.clinic_policy_versions.kind",
            "public.clinic_policy_versions.version",
        ],
    ),
)

queue_entries = Table(
    "queue_entries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("queue_day_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("patient_id", UUID(as_uuid=True), nullable=False),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("lane", Text, nullable=False),
    Column("arrival_sequence", BigInteger, nullable=False),
    Column("attested_by_staff_id", UUID(as_uuid=True), nullable=False),
    Column("attested_at", DateTime(timezone=True), nullable=False),
    Column("routine_rank", BigInteger),
    Column(
        "ordinary_head_admitted", Boolean, nullable=False, server_default=text("false")
    ),
    Column("disposition", Text, nullable=False),
    Column("closure_history_revision", BigInteger),
    Column("closure_queue_revision", BigInteger),
    Column("closure_routine_cursor", BigInteger),
    Column("revision", BigInteger, nullable=False, server_default=text("1")),
    UniqueConstraint("organization_id", "id"),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
    ),
)

doctor_controls = Table(
    "doctor_controls",
    metadata,
    Column("organization_id", UUID(as_uuid=True), primary_key=True),
    Column("doctor_staff_id", UUID(as_uuid=True), primary_key=True),
    Column("revision", BigInteger, nullable=False, server_default=text("1")),
    Column(
        "capacity_policy_kind", Text, nullable=False, server_default=text("'CAPACITY'")
    ),
    Column("capacity_policy_version", BigInteger, nullable=False),
    ForeignKeyConstraint(
        ["organization_id", "doctor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
)

doctor_admission_confirmations = Table(
    "doctor_admission_confirmations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("issuing_doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("patient_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("lane", Text, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("appointment_revision", BigInteger, nullable=False),
    Column("queue_day_revision", BigInteger, nullable=False),
    Column("control_revision", BigInteger, nullable=False),
    Column("capacity_revision", BigInteger, nullable=False),
    Column("admission_revision", BigInteger, nullable=False),
    Column("state", Text, nullable=False),
    Column("consuming_admission_revision", BigInteger),
    Column("consumed_at", DateTime(timezone=True)),
    Column("revoked_by_staff_id", UUID(as_uuid=True)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoke_reason", Text),
    UniqueConstraint("organization_id", "id"),
)
