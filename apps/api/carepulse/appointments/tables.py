"""D-05 appointment Core tables. Alembic owns the database constraints."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="public")

appointments = Table(
    "appointments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("patient_id", UUID(as_uuid=True), nullable=False),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("service_type", Text, nullable=False),
    Column("classification", Text, nullable=False),
    Column("lane", Text, nullable=False),
    Column("booking_mode", Text, nullable=False),
    Column("starts_at", DateTime(timezone=True)),
    Column("ends_at", DateTime(timezone=True)),
    Column("current_assignment_id", UUID(as_uuid=True), nullable=False),
    Column("current_number", BigInteger, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "id", "service_type", "classification"),
    ForeignKeyConstraint(
        ["organization_id", "patient_id"],
        ["public.patients.organization_id", "public.patients.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "doctor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        [
            "organization_id",
            "id",
            "service_date",
            "current_assignment_id",
            "current_number",
        ],
        [
            "public.appointment_number_assignments.organization_id",
            "public.appointment_number_assignments.appointment_id",
            "public.appointment_number_assignments.service_date",
            "public.appointment_number_assignments.id",
            "public.appointment_number_assignments.number",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
)

Index(
    "appointments_one_counted_patient_day",
    appointments.c.organization_id,
    appointments.c.patient_id,
    appointments.c.service_date,
    unique=True,
    postgresql_where=appointments.c.status.in_(
        ("Booked", "Waiting", "In Examination", "Completed")
    ),
)

appointment_day_counters = Table(
    "appointment_day_counters",
    metadata,
    Column("organization_id", UUID(as_uuid=True), primary_key=True),
    Column("service_date", Date, primary_key=True),
    Column("last_number", BigInteger, nullable=False),
)

appointment_number_assignments = Table(
    "appointment_number_assignments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("number", BigInteger, nullable=False),
    Column("prior_assignment_id", UUID(as_uuid=True)),
    Column(
        "assigned_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "service_date", "number"),
    UniqueConstraint(
        "organization_id", "appointment_id", "service_date", "id", "number"
    ),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["organization_id", "prior_assignment_id"],
        [
            "public.appointment_number_assignments.organization_id",
            "public.appointment_number_assignments.id",
        ],
    ),
    ForeignKeyConstraint(
        ["organization_id", "service_date"],
        [
            "public.appointment_day_counters.organization_id",
            "public.appointment_day_counters.service_date",
        ],
    ),
)

appointment_price_snapshots = Table(
    "appointment_price_snapshots",
    metadata,
    Column("organization_id", UUID(as_uuid=True), primary_key=True),
    Column("appointment_id", UUID(as_uuid=True), primary_key=True),
    Column("service_type", Text, nullable=False),
    Column("classification", Text, nullable=False),
    Column("base_egp", Numeric, nullable=False),
    Column("discount_type", Text, nullable=False),
    Column("discount_value", Numeric, nullable=False, server_default=text("0")),
    Column("discount_reason", Text),
    Column("is_free", Boolean, nullable=False),
    Column("net_egp", Numeric, nullable=False),
    Column("pricing_kind", Text, nullable=False, server_default=text("'PRICE'")),
    Column("pricing_version", BigInteger, nullable=False),
    Column("creation_standard_base_egp", Numeric, nullable=False),
    Column("entitlement_provenance", JSONB(none_as_null=True)),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id", "service_type", "classification"],
        [
            "public.appointments.organization_id",
            "public.appointments.id",
            "public.appointments.service_type",
            "public.appointments.classification",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["organization_id", "pricing_kind", "pricing_version"],
        [
            "public.clinic_policy_versions.organization_id",
            "public.clinic_policy_versions.kind",
            "public.clinic_policy_versions.version",
        ],
    ),
)

appointment_history = Table(
    "appointment_history",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_revision", BigInteger, nullable=False),
    Column("action", Text, nullable=False),
    Column("old_status", Text),
    Column("new_status", Text, nullable=False),
    Column("old_service_date", Date),
    Column("new_service_date", Date, nullable=False),
    Column("old_number", BigInteger),
    Column("new_number", BigInteger, nullable=False),
    Column("actor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("actor_role", Text, nullable=False),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("reason", Text),
    Column("price_snapshot_appointment_id", UUID(as_uuid=True), nullable=False),
    Column("replacement_appointment_id", UUID(as_uuid=True)),
    UniqueConstraint("organization_id", "appointment_id", "appointment_revision"),
    ForeignKeyConstraint(
        ["organization_id", "appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "price_snapshot_appointment_id"],
        [
            "public.appointment_price_snapshots.organization_id",
            "public.appointment_price_snapshots.appointment_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["organization_id", "replacement_appointment_id"],
        ["public.appointments.organization_id", "public.appointments.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
)

Index(
    "appointment_history_one_creation",
    appointment_history.c.organization_id,
    appointment_history.c.appointment_id,
    unique=True,
    postgresql_where=appointment_history.c.action == "CREATED",
)
