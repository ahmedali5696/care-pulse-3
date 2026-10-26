"""D-03 patient identity, tenant isolation, and immutable lifecycle evidence."""

from alembic import op

revision = "0014_patients"
down_revision = "0013_b04_native_secret_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.patients (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            name_parts text[] NOT NULL,
            phone_raw text,
            phone_normalized text,
            date_of_birth date,
            declared_age numeric,
            age_reference_date date,
            preliminary_medical_history text,
            chronic_illnesses text,
            drug_allergies text,
            food_allergies text,
            prior_examination_notes text,
            preliminary_test_information text,
            archived boolean NOT NULL DEFAULT false,
            archived_at timestamptz,
            demographic_revision bigint NOT NULL DEFAULT 0,
            lifecycle_revision bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT patients_tenant_id_unique UNIQUE (organization_id, id),
            CONSTRAINT patients_name_parts_valid CHECK (
                array_ndims(name_parts) = 1
                AND cardinality(name_parts) IN (3, 4)
                AND array_position(name_parts, NULL) IS NULL
                AND btrim(name_parts[1]) <> ''
                AND btrim(name_parts[2]) <> ''
                AND btrim(name_parts[3]) <> ''
                AND (cardinality(name_parts) = 3 OR btrim(name_parts[4]) <> '')
            ),
            CONSTRAINT patients_age_evidence_valid CHECK (
                (
                    date_of_birth IS NOT NULL
                    AND isfinite(date_of_birth)
                    AND declared_age IS NULL AND age_reference_date IS NULL
                ) OR (
                    date_of_birth IS NULL
                    AND declared_age IS NOT NULL
                    AND declared_age >= 0
                    AND declared_age < 'Infinity'::numeric
                    AND age_reference_date IS NOT NULL
                    AND isfinite(age_reference_date)
                )
            ),
            CONSTRAINT patients_normalized_phone_valid CHECK (
                phone_normalized IS NULL
                OR (phone_raw IS NOT NULL
                    AND phone_normalized ~ '^\\+[1-9][0-9]{7,14}$')
            ),
            CONSTRAINT patients_revisions_valid CHECK (
                demographic_revision >= 0 AND lifecycle_revision >= 0
            ),
            CONSTRAINT patients_archive_date_valid CHECK (
                (archived AND archived_at IS NOT NULL)
                OR (NOT archived AND archived_at IS NULL)
            )
        )
    """)
    op.execute("""
        CREATE INDEX patients_tenant_active_id
        ON public.patients (organization_id, archived, id)
    """)
    # array_to_string() is STABLE, so it cannot appear in an expression index.
    op.execute("""
        CREATE INDEX patients_tenant_name_id
        ON public.patients (organization_id, archived, name_parts, id)
    """)
    op.execute("""
        CREATE INDEX patients_tenant_phone_id
        ON public.patients (organization_id, phone_normalized, id)
    """)
    op.execute("""
        CREATE FUNCTION public.guard_patient_lifecycle() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Patient deletion is forbidden'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.organization_id, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.id, OLD.organization_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'Immutable patient identity'
                        USING ERRCODE = '23514';
                END IF;
                IF (NEW.name_parts, NEW.phone_raw, NEW.phone_normalized,
                    NEW.date_of_birth, NEW.declared_age, NEW.age_reference_date,
                    NEW.preliminary_medical_history, NEW.chronic_illnesses,
                    NEW.drug_allergies, NEW.food_allergies,
                    NEW.prior_examination_notes, NEW.preliminary_test_information)
                   IS DISTINCT FROM
                   (OLD.name_parts, OLD.phone_raw, OLD.phone_normalized,
                    OLD.date_of_birth, OLD.declared_age, OLD.age_reference_date,
                    OLD.preliminary_medical_history, OLD.chronic_illnesses,
                    OLD.drug_allergies, OLD.food_allergies,
                    OLD.prior_examination_notes, OLD.preliminary_test_information) THEN
                    IF NEW.demographic_revision <> OLD.demographic_revision + 1 THEN
                        RAISE EXCEPTION 'Stale demographic revision'
                            USING ERRCODE = '23514';
                    END IF;
                ELSIF NEW.demographic_revision <> OLD.demographic_revision THEN
                    RAISE EXCEPTION 'Unmatched demographic revision'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.archived IS DISTINCT FROM OLD.archived THEN
                    IF NEW.lifecycle_revision <> OLD.lifecycle_revision + 1 THEN
                        RAISE EXCEPTION 'Stale patient lifecycle revision'
                            USING ERRCODE = '23514';
                    END IF;
                    NEW.archived_at := CASE WHEN NEW.archived
                        THEN clock_timestamp() ELSE NULL END;
                ELSIF NEW.lifecycle_revision <> OLD.lifecycle_revision
                   OR NEW.archived_at IS DISTINCT FROM OLD.archived_at THEN
                    RAISE EXCEPTION 'Unmatched lifecycle revision'
                        USING ERRCODE = '23514';
                END IF;
                NEW.updated_at := clock_timestamp();
            END IF;
            IF NEW.date_of_birth IS NOT NULL AND
               NEW.date_of_birth > (now() AT TIME ZONE 'Africa/Cairo')::date
               OR NEW.age_reference_date IS NOT NULL AND
               NEW.age_reference_date > (now() AT TIME ZONE 'Africa/Cairo')::date THEN
                RAISE EXCEPTION 'Future patient age evidence'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_patient_lifecycle() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER patients_lifecycle_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.patients
        FOR EACH ROW EXECUTE FUNCTION public.guard_patient_lifecycle()
    """)
    op.execute("ALTER TABLE public.patients ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.patients FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY patients_runtime_tenant_access ON public.patients
        FOR ALL TO carepulse_runtime
        USING (organization_id =
               NULLIF(current_setting('app.organization_id', true), '')::uuid)
        WITH CHECK (organization_id =
                    NULLIF(current_setting('app.organization_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE POLICY patients_migration_access ON public.patients
        FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.patients TO carepulse_runtime")


def downgrade() -> None:
    raise RuntimeError("D-03 patient identity migration is forward-only")
