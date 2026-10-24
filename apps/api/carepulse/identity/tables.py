"""D-02 PostgreSQL identity metadata used by the shared domain transactions.

Alembic owns DDL. Better Auth 1.7.12 owns native user/account/session/verification
models and credentials; this module deliberately does not duplicate those tables.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData(schema="public")

staff_accounts = Table(
    "staff_accounts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column(
        "auth_user_id",
        String,
        ForeignKey("public.user.id"),
        nullable=False,
        unique=True,
    ),
    Column("verified_email", String, nullable=False),
    Column("role", String, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("verified", Boolean, nullable=False, server_default=text("false")),
    Column("bulk_reschedule", Boolean, nullable=False, server_default=text("false")),
    Column("revision", BigInteger, nullable=False, server_default=text("0")),
    Column("security_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("reset_block_operation_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("organization_id", "id"),
    UniqueConstraint("organization_id", "id", "auth_user_id"),
    CheckConstraint("role IN ('DOCTOR_OWNER', 'RECEPTIONIST')"),
    CheckConstraint("role <> 'DOCTOR_OWNER' OR NOT bulk_reschedule"),
)
Index(
    "staff_one_doctor_per_tenant",
    staff_accounts.c.organization_id,
    unique=True,
    postgresql_where=staff_accounts.c.role == "DOCTOR_OWNER",
)

invitations = Table(
    "invitations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column("staff_id", UUID(as_uuid=True)),
    Column("purpose", String, nullable=False),
    Column("target_email", String, nullable=False),
    Column("proof_digest", String, nullable=False),
    Column("state", String, nullable=False, server_default=text("'ISSUED'")),
    Column("revision", BigInteger, nullable=False, server_default=text("0")),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("accepted_at", DateTime(timezone=True)),
    Column("accepted_by_user_id", String, ForeignKey("public.user.id")),
    Column("revoked_at", DateTime(timezone=True)),
    Column("superseded_by_id", UUID(as_uuid=True)),
    Column("issued_by_staff_id", UUID(as_uuid=True)),
    Column(
        "issued_by_operator_id",
        UUID(as_uuid=True),
        ForeignKey("public.operator_identities.id"),
    ),
    Column("acceptance_reference", String),
    Column("revocation_reference", String),
    ForeignKeyConstraint(
        ["organization_id", "staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "issued_by_staff_id"],
        ["public.staff_accounts.organization_id", "public.staff_accounts.id"],
    ),
    ForeignKeyConstraint(
        ["organization_id", "purpose", "target_email", "superseded_by_id"],
        [
            "public.invitations.organization_id",
            "public.invitations.purpose",
            "public.invitations.target_email",
            "public.invitations.id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    UniqueConstraint("purpose", "proof_digest"),
    UniqueConstraint("organization_id", "purpose", "target_email", "id"),
    CheckConstraint("purpose IN ('FIRST_OWNER', 'RECEPTIONIST')"),
    CheckConstraint("state IN ('ISSUED', 'ACCEPTED', 'REVOKED')"),
    CheckConstraint("superseded_by_id IS NULL OR superseded_by_id <> id"),
)
Index(
    "invitations_one_issued_first_owner",
    invitations.c.organization_id,
    unique=True,
    postgresql_where=(invitations.c.purpose == "FIRST_OWNER")
    & (invitations.c.state == "ISSUED"),
)

recovery_operations = Table(
    "recovery_operations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column("staff_id", UUID(as_uuid=True), nullable=False),
    Column("auth_user_id", String, ForeignKey("public.user.id"), nullable=False),
    Column("purpose", String, nullable=False),
    Column("proof_digest", String, nullable=False),
    Column("proof_reference", String, nullable=False),
    Column("proof_expires_at", DateTime(timezone=True), nullable=False),
    Column("request_fingerprint", String, nullable=False),
    Column("fingerprint_key_version", String, nullable=False),
    Column("captured_staff_revision", BigInteger, nullable=False),
    Column("captured_lifecycle_revision", BigInteger, nullable=False),
    Column("reserved_security_revision", BigInteger, nullable=False),
    Column("state", String, nullable=False, server_default=text("'RESERVED'")),
    Column("invalidated", Boolean, nullable=False, server_default=text("false")),
    Column("auth_fence", BigInteger, nullable=False, server_default=text("0")),
    Column("invalidation_reference", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("auth_done_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["organization_id", "staff_id", "auth_user_id"],
        [
            "public.staff_accounts.organization_id",
            "public.staff_accounts.id",
            "public.staff_accounts.auth_user_id",
        ],
    ),
    UniqueConstraint("purpose", "proof_digest"),
    UniqueConstraint("organization_id", "staff_id", "id"),
    UniqueConstraint(
        "id",
        "proof_digest",
        "auth_user_id",
        "request_fingerprint",
        "reserved_security_revision",
        "auth_fence",
    ),
    CheckConstraint("state IN ('RESERVED', 'AUTH_DONE', 'FINALIZED', 'ABORTED')"),
)
Index(
    "recovery_one_unresolved_user",
    recovery_operations.c.auth_user_id,
    unique=True,
    postgresql_where=recovery_operations.c.closed_at.is_(None),
)
staff_accounts.append_constraint(
    ForeignKeyConstraint(
        ["organization_id", "id", "reset_block_operation_id"],
        [
            "public.recovery_operations.organization_id",
            "public.recovery_operations.staff_id",
            "public.recovery_operations.id",
        ],
        name="staff_reset_block_owned",
        deferrable=True,
        initially="DEFERRED",
    )
)

auth_recovery_completions = Table(
    "auth_recovery_completions",
    metadata,
    # The completion's ID *is* the immutable recovery operation UUID. The
    # supported Better Auth plugin adapter sees a string-compatible ID.
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("proof_digest", String, nullable=False),
    Column("auth_user_id", String, ForeignKey("public.user.id"), nullable=False),
    Column("request_fingerprint", String, nullable=False),
    Column("reserved_security_revision", BigInteger, nullable=False),
    Column("auth_fence", BigInteger, nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        [
            "id",
            "proof_digest",
            "auth_user_id",
            "request_fingerprint",
            "reserved_security_revision",
            "auth_fence",
        ],
        [
            "public.recovery_operations.id",
            "public.recovery_operations.proof_digest",
            "public.recovery_operations.auth_user_id",
            "public.recovery_operations.request_fingerprint",
            "public.recovery_operations.reserved_security_revision",
            "public.recovery_operations.auth_fence",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
)
