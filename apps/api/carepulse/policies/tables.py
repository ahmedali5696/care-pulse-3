"""D-04 storage metadata; the forward-only Alembic revision owns DDL."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    SmallInteger,
    Table,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="public")

policy_versions = Table(
    "clinic_policy_versions",
    metadata,
    Column("organization_id", UUID(as_uuid=True), nullable=False, primary_key=True),
    Column("kind", Text, nullable=False, primary_key=True),
    Column("version", BigInteger, nullable=False, primary_key=True),
    Column("payload", JSONB, nullable=False),
    Column("effective_on", Date),
    Column("pending_effective", Boolean, nullable=False, server_default=text("false")),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("created_by_staff_id", UUID(as_uuid=True)),
    ForeignKeyConstraint(["organization_id"], ["public.organizations.id"]),
    ForeignKeyConstraint(
        ["organization_id", "created_by_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    CheckConstraint("version >= 1"),
)
policy_heads = Table(
    "clinic_policy_heads",
    metadata,
    Column("organization_id", UUID(as_uuid=True), nullable=False, primary_key=True),
    Column("kind", Text, nullable=False, primary_key=True),
    Column("current_version", BigInteger, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("queue_effective_on", Date),
    Column("queue_pending", Boolean, nullable=False, server_default=text("false")),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    ForeignKeyConstraint(
        ["organization_id", "kind", "current_version"],
        [
            "public.clinic_policy_versions.organization_id",
            "public.clinic_policy_versions.kind",
            "public.clinic_policy_versions.version",
        ],
    ),
)
schedule_intervals = Table(
    "clinic_schedule_intervals",
    metadata,
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("policy_kind", Text, nullable=False, server_default=text("'SCHEDULE'")),
    Column("policy_version", BigInteger, nullable=False),
    Column("interval_kind", Text, nullable=False),
    Column("weekday", SmallInteger),
    Column("service_date", Date),
    Column("starts_at", Time, nullable=False),
    Column("ends_at", Time, nullable=False),
    ForeignKeyConstraint(
        ["organization_id", "policy_kind", "policy_version"],
        [
            "public.clinic_policy_versions.organization_id",
            "public.clinic_policy_versions.kind",
            "public.clinic_policy_versions.version",
        ],
    ),
    CheckConstraint("starts_at < ends_at"),
)
schedule_holidays = Table(
    "clinic_schedule_holidays",
    metadata,
    Column("organization_id", UUID(as_uuid=True), nullable=False, primary_key=True),
    Column("policy_kind", Text, nullable=False, server_default=text("'SCHEDULE'")),
    Column("policy_version", BigInteger, nullable=False, primary_key=True),
    Column("service_date", Date, nullable=False, primary_key=True),
    ForeignKeyConstraint(
        ["organization_id", "policy_kind", "policy_version"],
        [
            "public.clinic_policy_versions.organization_id",
            "public.clinic_policy_versions.kind",
            "public.clinic_policy_versions.version",
        ],
    ),
)
policy_previews = Table(
    "clinic_policy_previews",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("policy_kind", Text, nullable=False),
    Column("policy_version", BigInteger, nullable=False),
    Column("policy_revision", BigInteger, nullable=False),
    Column(
        "service_day_revision", BigInteger, nullable=False, server_default=text("0")
    ),
    Column("actor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date),
    Column("selected_refs", JSONB, nullable=False),
    Column("affected_refs", JSONB, nullable=False),
    Column("record_revisions", JSONB, nullable=False),
    Column("candidate_payload", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    ForeignKeyConstraint(
        ["organization_id", "policy_kind", "policy_version"],
        [
            "public.clinic_policy_versions.organization_id",
            "public.clinic_policy_versions.kind",
            "public.clinic_policy_versions.version",
        ],
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
)
