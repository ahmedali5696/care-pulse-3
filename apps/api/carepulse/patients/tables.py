"""D-03 patient storage metadata. Alembic owns executable DDL."""

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData(schema="public")

patients = Table(
    "patients",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "organization_id",
        UUID(as_uuid=True),
        ForeignKey("public.organizations.id"),
        nullable=False,
    ),
    Column("name_parts", ARRAY(Text), nullable=False),
    Column("phone_raw", Text),
    Column("phone_normalized", Text),
    Column("date_of_birth", Date),
    Column("declared_age", Numeric),
    Column("age_reference_date", Date),
    Column("preliminary_medical_history", Text),
    Column("chronic_illnesses", Text),
    Column("drug_allergies", Text),
    Column("food_allergies", Text),
    Column("prior_examination_notes", Text),
    Column("preliminary_test_information", Text),
    Column("archived", Boolean, nullable=False, server_default=text("false")),
    Column("archived_at", DateTime(timezone=True)),
    Column(
        "demographic_revision", BigInteger, nullable=False, server_default=text("0")
    ),
    Column("lifecycle_revision", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    UniqueConstraint("organization_id", "id", name="patients_tenant_id_unique"),
    CheckConstraint(
        "array_ndims(name_parts) = 1 AND cardinality(name_parts) IN (3, 4) "
        "AND array_position(name_parts, NULL) IS NULL "
        "AND name_parts[1] ~ '[^[:space:]]' "
        "AND name_parts[2] ~ '[^[:space:]]' "
        "AND name_parts[3] ~ '[^[:space:]]' "
        "AND (cardinality(name_parts) = 3 "
        "OR name_parts[4] ~ '[^[:space:]]')",
        name="patients_name_parts_valid",
    ),
    CheckConstraint(
        "(date_of_birth IS NOT NULL AND isfinite(date_of_birth) "
        "AND declared_age IS NULL "
        "AND age_reference_date IS NULL) OR "
        "(date_of_birth IS NULL AND declared_age IS NOT NULL "
        "AND declared_age >= 0 AND declared_age < 'Infinity'::numeric "
        "AND age_reference_date IS NOT NULL "
        "AND isfinite(age_reference_date))",
        name="patients_age_evidence_valid",
    ),
    CheckConstraint(
        "phone_normalized IS NULL OR "
        "(phone_raw IS NOT NULL "
        "AND phone_normalized ~ '^\\+[1-9][0-9]{7,14}$')",
        name="patients_normalized_phone_valid",
    ),
    CheckConstraint(
        "demographic_revision >= 0 AND lifecycle_revision >= 0",
        name="patients_revisions_valid",
    ),
    CheckConstraint(
        "(archived AND archived_at IS NOT NULL) OR "
        "(NOT archived AND archived_at IS NULL)",
        name="patients_archive_date_valid",
    ),
)

Index(
    "patients_tenant_active_id",
    patients.c.organization_id,
    patients.c.archived,
    patients.c.id,
)
Index(
    "patients_tenant_name_id",
    patients.c.organization_id,
    patients.c.archived,
    patients.c.name_parts,
    patients.c.id,
)
Index(
    "patients_tenant_phone_id",
    patients.c.organization_id,
    patients.c.phone_normalized,
    patients.c.id,
)
