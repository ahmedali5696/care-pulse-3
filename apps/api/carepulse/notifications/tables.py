"""D-09 SQLAlchemy Core metadata; Alembic owns the executable DDL."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="public")

sensitive_audit = Table(
    "sensitive_audit",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id", UUID(as_uuid=True), ForeignKey("public.organizations.id")
    ),
    Column("actor_staff_id", UUID(as_uuid=True)),
    Column(
        "operator_id", UUID(as_uuid=True), ForeignKey("public.operator_identities.id")
    ),
    Column("actor_role", String, nullable=False),
    Column("action", String, nullable=False),
    Column("reason", String),
    Column("before_state", JSONB),
    Column("after_state", JSONB),
    Column("correlation_id", String, nullable=False),
    Column("recovery_operation_id", UUID(as_uuid=True)),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("date_trunc('second', clock_timestamp())"),
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "actor_staff_id", "recovery_operation_id"],
        [
            "public.recovery_operations.organization_id",
            "public.recovery_operations.staff_id",
            "public.recovery_operations.id",
        ],
    ),
)
Index(
    "sensitive_reset_once",
    sensitive_audit.c.recovery_operation_id,
    sensitive_audit.c.action,
    unique=True,
    postgresql_where=sensitive_audit.c.action == "PASSWORD_RESET_SUCCEEDED",
)

clinical_audit = Table(
    "clinical_audit",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column("doctor_staff_id", UUID(as_uuid=True), nullable=False),
    Column("clinical_revision", BigInteger, nullable=False),
    Column("before_ref", String),
    Column("after_ref", String),
    Column("correlation_id", String, nullable=False),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("date_trunc('second', clock_timestamp())"),
    ),
    ForeignKeyConstraint(
        ["organization_id", "doctor_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
)

outbox_events = Table(
    "outbox_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column("event_type", String, nullable=False),
    Column("aggregate_type", String, nullable=False),
    Column("aggregate_id", String, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("date_trunc('second', clock_timestamp())"),
    ),
)

delivery_jobs = Table(
    "delivery_jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column("staff_id", UUID(as_uuid=True)),
    Column("invitation_id", UUID(as_uuid=True)),
    Column("verification_id", String),
    Column("recovery_operation_id", UUID(as_uuid=True)),
    Column("proof_reference", String, nullable=False),
    Column("purpose", String, nullable=False),
    Column("recipient", String, nullable=False),
    Column("template", String, nullable=False),
    Column("logical_key", String, nullable=False),
    Column("proof_expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String, nullable=False, server_default=text("'READY'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column(
        "not_before",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    ),
    Column("claim_owner", UUID(as_uuid=True)),
    Column("claim_generation", BigInteger, nullable=False, server_default=text("0")),
    Column("claimed_at", DateTime(timezone=True)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("last_outcome", String),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    ),
    ForeignKeyConstraint(
        ["organization_id", "staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "purpose", "recipient", "invitation_id"],
        [
            "public.invitations.organization_id",
            "public.invitations.purpose",
            "public.invitations.target_email",
            "public.invitations.id",
        ],
    ),
    ForeignKeyConstraint(
        ["organization_id", "staff_id", "recovery_operation_id"],
        [
            "public.recovery_operations.organization_id",
            "public.recovery_operations.staff_id",
            "public.recovery_operations.id",
        ],
    ),
    UniqueConstraint("organization_id", "logical_key"),
    CheckConstraint("state IN ('READY','CLAIMED','SENT','FAILED','CANCELLED')"),
)
Index(
    "delivery_claim_candidates",
    delivery_jobs.c.state,
    delivery_jobs.c.not_before,
    delivery_jobs.c.lease_expires_at,
    postgresql_where=delivery_jobs.c.state.in_(("READY", "CLAIMED")),
)

protected_delivery_material = Table(
    "protected_delivery_material",
    metadata,
    Column(
        "job_id",
        UUID(as_uuid=True),
        ForeignKey("public.delivery_jobs.id"),
        primary_key=True,
    ),
    Column("format_version", Integer, nullable=False, server_default=text("1")),
    Column("delivery_key_id", String, nullable=False),
    Column("ciphertext_base64url", String, nullable=False),
)

request_nonces = Table(
    "request_nonces",
    metadata,
    Column("key_id", String, primary_key=True),
    Column("nonce", String, primary_key=True),
    Column("signed_at", DateTime(timezone=True), nullable=False),
    Column(
        "accepted_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    ),
)

idempotency_receipts = Table(
    "idempotency_receipts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("scope_kind", String, nullable=False),
    Column("scope_id", UUID(as_uuid=True), nullable=False),
    Column("actor_kind", String, nullable=False),
    Column("actor_id", UUID(as_uuid=True), nullable=False),
    Column(
        "organization_id", UUID(as_uuid=True), ForeignKey("public.organizations.id")
    ),
    Column("staff_id", UUID(as_uuid=True)),
    Column(
        "operator_id", UUID(as_uuid=True), ForeignKey("public.operator_identities.id")
    ),
    Column("operation", String, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("request_digest", String, nullable=False),
    Column("result_projection", JSONB),
    Column(
        "result_organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
    ),
    Column("result_revision", BigInteger),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    ),
    ForeignKeyConstraint(
        ["organization_id", "staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    UniqueConstraint(
        "scope_kind",
        "scope_id",
        "actor_kind",
        "actor_id",
        "operation",
        "idempotency_key",
    ),
)
