"""D-11 dated review metadata; Alembic owns database constraints."""

from sqlalchemy import (
    BigInteger,
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

day_reviews = Table(
    "day_reviews",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("service_date", Date, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default=text("1")),
    Column("disposition", Text, nullable=False),
    Column("actor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("audit_event_id", UUID(as_uuid=True), nullable=False),
    Column("notes", Text),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "service_date"),
    UniqueConstraint("organization_id", "audit_event_id"),
    ForeignKeyConstraint(["organization_id"], ["public.organizations.id"]),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "audit_event_id"],
        ["public.sensitive_audit.organization_id", "public.sensitive_audit.id"],
    ),
)

day_review_history = Table(
    "day_review_history",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("day_review_id", UUID(as_uuid=True), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("old_disposition", Text),
    Column("disposition", Text, nullable=False),
    Column("old_notes", Text),
    Column("notes", Text),
    Column("old_actor_staff_id", UUID(as_uuid=True)),
    Column("old_occurred_at", DateTime(timezone=True)),
    Column("actor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("audit_event_id", UUID(as_uuid=True), nullable=False),
    UniqueConstraint("organization_id", "day_review_id", "revision"),
    UniqueConstraint("organization_id", "audit_event_id"),
    ForeignKeyConstraint(
        ["organization_id", "day_review_id"],
        ["public.day_reviews.organization_id", "public.day_reviews.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "old_actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "audit_event_id"],
        ["public.sensitive_audit.organization_id", "public.sensitive_audit.id"],
    ),
)
