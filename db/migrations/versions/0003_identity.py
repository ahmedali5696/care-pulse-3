"""Better Auth 1.7.12 native persistence and tenant-bound identity safeguards."""

from alembic import op

revision = "0003_identity"
down_revision = "0002_organizations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Derived from the public better-auth/db getSchema API, using the installed
    # 1.7.12 default models. IDs are Better Auth's native text IDs, not UUIDs.
    op.execute("""
        CREATE TABLE public."user" (
            id text PRIMARY KEY,
            name text NOT NULL,
            email text NOT NULL UNIQUE,
            "emailVerified" boolean NOT NULL DEFAULT false,
            image text,
            "createdAt" timestamptz NOT NULL,
            "updatedAt" timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE public."session" (
            id text PRIMARY KEY,
            "expiresAt" timestamptz NOT NULL,
            token text NOT NULL UNIQUE,
            "createdAt" timestamptz NOT NULL,
            "updatedAt" timestamptz NOT NULL,
            "ipAddress" text,
            "userAgent" text,
            "userId" text NOT NULL REFERENCES public."user"(id) ON DELETE CASCADE,
            "securityRevision" integer NOT NULL CHECK ("securityRevision" >= 0),
            "absoluteDeadline" timestamptz NOT NULL,
            "lastActivity" timestamptz NOT NULL
        )
    """)
    op.execute('CREATE INDEX "session_userId_idx" ON public."session" ("userId")')
    op.execute("""
        CREATE FUNCTION public.guard_session_authority() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF (NEW."userId", NEW."securityRevision", NEW."absoluteDeadline")
               IS DISTINCT FROM
               (OLD."userId", OLD."securityRevision", OLD."absoluteDeadline") THEN
                RAISE EXCEPTION 'Session authority cannot be rebound'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_session_authority() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER session_authority_immutable
        BEFORE UPDATE ON public."session"
        FOR EACH ROW EXECUTE FUNCTION public.guard_session_authority()
    """)
    op.execute("""
        CREATE TABLE public."account" (
            id text PRIMARY KEY,
            "accountId" text NOT NULL,
            "providerId" text NOT NULL,
            "userId" text NOT NULL REFERENCES public."user"(id) ON DELETE CASCADE,
            "accessToken" text,
            "refreshToken" text,
            "idToken" text,
            "accessTokenExpiresAt" timestamptz,
            "refreshTokenExpiresAt" timestamptz,
            scope text,
            password text,
            "createdAt" timestamptz NOT NULL,
            "updatedAt" timestamptz NOT NULL
        )
    """)
    op.execute('CREATE INDEX "account_userId_idx" ON public."account" ("userId")')
    op.execute("""
        CREATE TABLE public."verification" (
            id text PRIMARY KEY,
            identifier text NOT NULL,
            value text NOT NULL,
            "expiresAt" timestamptz NOT NULL,
            "createdAt" timestamptz NOT NULL,
            "updatedAt" timestamptz NOT NULL
        )
    """)
    op.execute(
        'CREATE INDEX verification_identifier_idx ON public."verification" (identifier)'
    )

    op.execute("""
        CREATE TABLE public.staff_accounts (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            auth_user_id text NOT NULL UNIQUE REFERENCES public."user"(id),
            verified_email text NOT NULL CHECK (btrim(verified_email) <> ''),
            role text NOT NULL CHECK (role IN ('DOCTOR_OWNER', 'RECEPTIONIST')),
            enabled boolean NOT NULL DEFAULT false,
            verified boolean NOT NULL DEFAULT false,
            bulk_reschedule boolean NOT NULL DEFAULT false,
            revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
            security_revision bigint NOT NULL DEFAULT 0 CHECK (security_revision >= 0),
            reset_block_operation_id uuid,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (organization_id, id),
            UNIQUE (organization_id, id, auth_user_id),
            CHECK (role <> 'DOCTOR_OWNER' OR NOT bulk_reschedule)
        )
    """)
    # Stronger than the enabled-only partial uniqueness: one Doctor row ever per
    # tenant, including while disabled, so a replacement cannot transfer Owner.
    op.execute("""
        CREATE UNIQUE INDEX staff_one_doctor_per_tenant
        ON public.staff_accounts (organization_id) WHERE role = 'DOCTOR_OWNER'
    """)
    op.execute("""
        CREATE TABLE public.invitations (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            staff_id uuid,
            purpose text NOT NULL CHECK (purpose IN ('FIRST_OWNER', 'RECEPTIONIST')),
            target_email text NOT NULL CHECK (btrim(target_email) <> ''),
            proof_digest text NOT NULL,
            state text NOT NULL DEFAULT 'ISSUED'
                CHECK (state IN ('ISSUED', 'ACCEPTED', 'REVOKED')),
            revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
            issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            expires_at timestamptz NOT NULL,
            accepted_at timestamptz,
            accepted_by_user_id text REFERENCES public."user"(id),
            revoked_at timestamptz,
            superseded_by_id uuid,
            issued_by_staff_id uuid,
            issued_by_operator_id uuid REFERENCES public.operator_identities(id),
            acceptance_reference text,
            revocation_reference text,
            CHECK (expires_at > issued_at),
            CHECK (proof_digest ~ '^[0-9a-f]{64}$'),
            CHECK (superseded_by_id IS NULL OR superseded_by_id <> id),
            CHECK ((purpose = 'FIRST_OWNER' AND staff_id IS NULL) OR
                   (purpose = 'RECEPTIONIST' AND staff_id IS NOT NULL)),
            CHECK ((purpose = 'FIRST_OWNER' AND issued_by_operator_id IS NOT NULL
                       AND issued_by_staff_id IS NULL) OR
                   (purpose = 'RECEPTIONIST' AND issued_by_staff_id IS NOT NULL
                       AND issued_by_operator_id IS NULL)),
            CHECK ((state = 'ISSUED' AND accepted_at IS NULL AND revoked_at IS NULL
                         AND accepted_by_user_id IS NULL AND superseded_by_id IS NULL) OR
                   (state = 'ACCEPTED' AND accepted_at IS NOT NULL AND revoked_at IS NULL
                         AND accepted_by_user_id IS NOT NULL AND superseded_by_id IS NULL) OR
                   (state = 'REVOKED' AND revoked_at IS NOT NULL AND accepted_at IS NULL
                         AND accepted_by_user_id IS NULL)),
            UNIQUE (purpose, proof_digest),
            UNIQUE (organization_id, purpose, target_email, id),
            FOREIGN KEY (organization_id, purpose, target_email, superseded_by_id)
                REFERENCES public.invitations (organization_id, purpose, target_email, id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (organization_id, staff_id)
                REFERENCES public.staff_accounts (organization_id, id),
            FOREIGN KEY (organization_id, issued_by_staff_id)
                REFERENCES public.staff_accounts (organization_id, id)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX invitations_one_issued_first_owner
        ON public.invitations (organization_id)
        WHERE purpose = 'FIRST_OWNER' AND state = 'ISSUED'
    """)
    op.execute("""
        CREATE TABLE public.recovery_operations (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES public.organizations(id),
            staff_id uuid NOT NULL,
            auth_user_id text NOT NULL REFERENCES public."user"(id),
            purpose text NOT NULL CHECK (purpose = 'PASSWORD_RESET'),
            proof_digest text NOT NULL CHECK (proof_digest ~ '^[0-9a-f]{64}$'),
            proof_reference text NOT NULL CHECK (btrim(proof_reference) <> ''),
            proof_expires_at timestamptz NOT NULL,
            request_fingerprint text NOT NULL
                CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
            fingerprint_key_version text NOT NULL CHECK (btrim(fingerprint_key_version) <> ''),
            captured_staff_revision bigint NOT NULL CHECK (captured_staff_revision >= 0),
            captured_lifecycle_revision bigint NOT NULL CHECK (captured_lifecycle_revision >= 0),
            reserved_security_revision bigint NOT NULL CHECK (reserved_security_revision > 0),
            state text NOT NULL DEFAULT 'RESERVED'
                CHECK (state IN ('RESERVED', 'AUTH_DONE', 'FINALIZED', 'ABORTED')),
            invalidated boolean NOT NULL DEFAULT false,
            auth_fence bigint NOT NULL DEFAULT 0 CHECK (auth_fence >= 0),
            invalidation_reference text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            auth_done_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            closed_at timestamptz,
            UNIQUE (purpose, proof_digest),
            UNIQUE (organization_id, staff_id, id),
            UNIQUE (id, proof_digest, auth_user_id, request_fingerprint,
                    reserved_security_revision, auth_fence),
            FOREIGN KEY (organization_id, staff_id, auth_user_id)
                REFERENCES public.staff_accounts (organization_id, id, auth_user_id),
            CHECK ((state IN ('FINALIZED', 'ABORTED')) = (closed_at IS NOT NULL)),
            CHECK (proof_expires_at > created_at),
            CHECK ((state IN ('AUTH_DONE', 'FINALIZED') OR
                    (state = 'ABORTED' AND auth_done_at IS NOT NULL)) =
                   (auth_done_at IS NOT NULL)),
            CHECK (auth_done_at IS NULL OR auth_done_at < proof_expires_at),
            CHECK (NOT invalidated OR invalidation_reference IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX recovery_one_unresolved_user
        ON public.recovery_operations (auth_user_id) WHERE closed_at IS NULL
    """)
    op.execute("""
        ALTER TABLE public.staff_accounts ADD CONSTRAINT staff_reset_block_owned
        FOREIGN KEY (organization_id, id, reset_block_operation_id)
        REFERENCES public.recovery_operations (organization_id, staff_id, id)
        DEFERRABLE INITIALLY DEFERRED
    """)
    op.execute("""
        CREATE TABLE public.auth_recovery_completions (
            id uuid PRIMARY KEY,
            proof_digest text NOT NULL CHECK (proof_digest ~ '^[0-9a-f]{64}$'),
            auth_user_id text NOT NULL REFERENCES public."user"(id),
            request_fingerprint text NOT NULL
                CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
            reserved_security_revision bigint NOT NULL CHECK (reserved_security_revision > 0),
            auth_fence bigint NOT NULL CHECK (auth_fence > 0),
            completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (id, proof_digest, auth_user_id, request_fingerprint,
                         reserved_security_revision, auth_fence)
                REFERENCES public.recovery_operations
                    (id, proof_digest, auth_user_id, request_fingerprint,
                     reserved_security_revision, auth_fence)
                DEFERRABLE INITIALLY DEFERRED
        )
    """)

    # Mutation guards run with migration-owned rights, so runtime RLS or an
    # empty/forged tenant GUC cannot conceal the affected Owner from a trigger.
    op.execute("""
        CREATE FUNCTION public.guard_identity_row() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_TABLE_NAME = 'staff_accounts' THEN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.role = 'DOCTOR_OWNER' THEN
                        RAISE EXCEPTION 'Owner replacement is forbidden' USING ERRCODE = '23514';
                    END IF;
                    PERFORM 1 FROM public.organizations WHERE id = OLD.organization_id FOR UPDATE;
                    RETURN OLD;
                END IF;
                PERFORM 1 FROM public.organizations WHERE id = NEW.organization_id FOR UPDATE;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.revision <> 0 OR NEW.security_revision <> 0 OR
                       NEW.reset_block_operation_id IS NOT NULL THEN
                        RAISE EXCEPTION 'Invalid initial staff authority' USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF (NEW.id, NEW.organization_id, NEW.auth_user_id,
                        NEW.verified_email, NEW.role,
                        NEW.created_at) IS DISTINCT FROM
                       (OLD.id, OLD.organization_id, OLD.auth_user_id,
                        OLD.verified_email, OLD.role,
                        OLD.created_at) THEN
                        RAISE EXCEPTION 'Immutable staff association' USING ERRCODE = '23514';
                    END IF;
                    IF NEW.security_revision < OLD.security_revision THEN
                        RAISE EXCEPTION 'Security revision cannot decrease' USING ERRCODE = '23514';
                    END IF;
                    IF ((NEW.enabled, NEW.verified, NEW.bulk_reschedule) IS DISTINCT FROM
                        (OLD.enabled, OLD.verified, OLD.bulk_reschedule) OR
                        (NEW.reset_block_operation_id IS NOT NULL AND
                         NEW.reset_block_operation_id IS DISTINCT FROM
                         OLD.reset_block_operation_id)) AND
                       NEW.security_revision <= OLD.security_revision THEN
                        RAISE EXCEPTION 'Security change requires a fresh revision'
                            USING ERRCODE = '23514';
                    END IF;
                    NEW.revision := OLD.revision + 1;
                    NEW.updated_at := clock_timestamp();
                END IF;
                RETURN NEW;
            END IF;
            IF TG_TABLE_NAME = 'invitations' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Invitation deletion is forbidden' USING ERRCODE = '23514';
                END IF;
                PERFORM 1 FROM public.organizations WHERE id = NEW.organization_id FOR UPDATE;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.state <> 'ISSUED' OR NEW.revision <> 0 THEN
                        RAISE EXCEPTION 'Invitation must start issued' USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF (NEW.id, NEW.organization_id, NEW.staff_id, NEW.purpose,
                        NEW.target_email, NEW.proof_digest, NEW.issued_at, NEW.expires_at,
                        NEW.issued_by_staff_id, NEW.issued_by_operator_id) IS DISTINCT FROM
                       (OLD.id, OLD.organization_id, OLD.staff_id, OLD.purpose,
                        OLD.target_email, OLD.proof_digest, OLD.issued_at, OLD.expires_at,
                        OLD.issued_by_staff_id, OLD.issued_by_operator_id) OR
                       OLD.state <> 'ISSUED' OR NEW.state NOT IN ('ACCEPTED', 'REVOKED') THEN
                        RAISE EXCEPTION 'Invalid invitation transition' USING ERRCODE = '23514';
                    END IF;
                    IF NEW.state = 'ACCEPTED' THEN
                        IF clock_timestamp() >= OLD.expires_at THEN
                            RAISE EXCEPTION 'Invitation expired' USING ERRCODE = '23514';
                        END IF;
                        NEW.accepted_at := clock_timestamp();
                    ELSE
                        NEW.revoked_at := clock_timestamp();
                    END IF;
                    NEW.revision := OLD.revision + 1;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_TABLE_NAME = 'recovery_operations' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Recovery operation deletion is forbidden' USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    IF (NEW.id, NEW.organization_id, NEW.staff_id, NEW.auth_user_id,
                        NEW.purpose, NEW.proof_digest, NEW.proof_reference,
                        NEW.proof_expires_at, NEW.request_fingerprint,
                        NEW.fingerprint_key_version, NEW.captured_staff_revision,
                        NEW.captured_lifecycle_revision, NEW.reserved_security_revision,
                        NEW.created_at) IS DISTINCT FROM
                       (OLD.id, OLD.organization_id, OLD.staff_id, OLD.auth_user_id,
                        OLD.purpose, OLD.proof_digest, OLD.proof_reference,
                        OLD.proof_expires_at, OLD.request_fingerprint,
                        OLD.fingerprint_key_version, OLD.captured_staff_revision,
                        OLD.captured_lifecycle_revision, OLD.reserved_security_revision,
                        OLD.created_at) OR NEW.auth_fence < OLD.auth_fence OR
                        (OLD.auth_done_at IS NOT NULL AND
                         NEW.auth_done_at IS DISTINCT FROM OLD.auth_done_at) OR
                        (OLD.invalidated AND NOT NEW.invalidated) OR
                        (OLD.invalidation_reference IS NOT NULL AND
                         NEW.invalidation_reference IS DISTINCT FROM OLD.invalidation_reference) OR
                        NEW.state NOT IN ('RESERVED', 'AUTH_DONE', 'FINALIZED', 'ABORTED') OR
                        (OLD.state = 'RESERVED' AND NEW.state NOT IN
                           ('RESERVED', 'AUTH_DONE', 'ABORTED')) OR
                        (OLD.state = 'AUTH_DONE' AND NEW.state NOT IN
                           ('AUTH_DONE', 'FINALIZED', 'ABORTED')) OR
                        OLD.state IN ('FINALIZED', 'ABORTED') THEN
                        RAISE EXCEPTION 'Invalid recovery operation mutation' USING ERRCODE = '23514';
                    END IF;
                    NEW.updated_at := clock_timestamp();
                ELSIF NEW.state <> 'RESERVED' OR NEW.auth_fence <> 0 OR
                      NEW.invalidated OR NEW.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'Invalid initial recovery reservation' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Completion evidence is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_identity_row() FROM PUBLIC")
    for table in (
        "staff_accounts",
        "invitations",
        "recovery_operations",
        "auth_recovery_completions",
    ):
        op.execute(f"""
            CREATE TRIGGER {table}_mutation_guard
            BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.guard_identity_row()
        """)

    # A completed auth effect must have exact immutable matching evidence,
    # inserted in the same transaction. A completion cannot remain on RESERVED.
    op.execute("""
        CREATE FUNCTION public.check_recovery_completion() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            operation_id uuid;
            operation_row public.recovery_operations%ROWTYPE;
            staff_block uuid;
            matching boolean;
        BEGIN
            IF TG_TABLE_NAME = 'staff_accounts' THEN
                SELECT id INTO operation_id FROM public.recovery_operations
                WHERE organization_id = NEW.organization_id AND staff_id = NEW.id
                  AND closed_at IS NULL;
                SELECT reset_block_operation_id INTO staff_block
                FROM public.staff_accounts
                WHERE organization_id = NEW.organization_id AND id = NEW.id;
                IF staff_block IS DISTINCT FROM operation_id THEN
                    RAISE EXCEPTION 'Staff reset block must own unresolved operation'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END IF;
            operation_id := NEW.id;
            SELECT * INTO operation_row FROM public.recovery_operations
            WHERE id = operation_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Missing recovery operation' USING ERRCODE = '23514';
            END IF;
            SELECT EXISTS (
                SELECT 1 FROM public.auth_recovery_completions c
                WHERE c.id = operation_row.id
                  AND c.proof_digest = operation_row.proof_digest
                  AND c.auth_user_id = operation_row.auth_user_id
                  AND c.request_fingerprint = operation_row.request_fingerprint
                  AND c.reserved_security_revision = operation_row.reserved_security_revision
                  AND c.auth_fence = operation_row.auth_fence
                  AND c.completed_at = operation_row.auth_done_at
            ) INTO matching;
            IF operation_row.auth_done_at IS NOT NULL THEN
                IF NOT matching THEN
                    RAISE EXCEPTION 'Missing matching auth completion' USING ERRCODE = '23514';
                END IF;
            ELSIF EXISTS (
                SELECT 1 FROM public.auth_recovery_completions WHERE id = operation_id
            ) THEN
                RAISE EXCEPTION 'Completion before auth effect' USING ERRCODE = '23514';
            END IF;
            SELECT reset_block_operation_id INTO staff_block
            FROM public.staff_accounts WHERE organization_id = operation_row.organization_id
              AND id = operation_row.staff_id;
            IF (operation_row.closed_at IS NULL AND
                staff_block IS DISTINCT FROM operation_id) OR
               (operation_row.closed_at IS NOT NULL AND staff_block = operation_id) THEN
                RAISE EXCEPTION 'Recovery operation does not own staff block'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.check_recovery_completion() FROM PUBLIC")
    for table in ("recovery_operations", "auth_recovery_completions"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER {table}_completion_constraint
            AFTER INSERT OR UPDATE ON public.{table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION public.check_recovery_completion()
        """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER staff_reset_block_constraint
        AFTER INSERT OR UPDATE ON public.staff_accounts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.check_recovery_completion()
    """)

    # D-01's existing trigger remains installed. Replacing its function in the
    # same transactional migration opens lifecycle transitions only after the
    # deferred database checks below have been installed.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_organization_foundation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Organization deletion is forbidden' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'PROVISIONING' OR NEW.first_activated_at IS NOT NULL THEN
                    RAISE EXCEPTION 'New tenant must be provisioning' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF (NEW.id, NEW.region, NEW.timezone, NEW.currency,
                    NEW.created_by_operator_id, NEW.created_at) IS DISTINCT FROM
                   (OLD.id, OLD.region, OLD.timezone, OLD.currency,
                    OLD.created_by_operator_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'Immutable organization identity' USING ERRCODE = '23514';
                END IF;
                IF (OLD.state = 'PROVISIONING' AND NEW.state NOT IN
                     ('PROVISIONING', 'ACTIVE', 'SUSPENDED')) OR
                   (OLD.state = 'ACTIVE' AND NEW.state NOT IN ('ACTIVE', 'SUSPENDED')) OR
                   (OLD.state = 'SUSPENDED' AND NEW.state NOT IN ('SUSPENDED', 'ACTIVE')) OR
                   (OLD.state = 'SUSPENDED' AND NEW.state = 'ACTIVE' AND
                     OLD.first_activated_at IS NULL) THEN
                    RAISE EXCEPTION 'Invalid organization lifecycle' USING ERRCODE = '23514';
                END IF;
                IF OLD.state = 'PROVISIONING' AND NEW.state = 'ACTIVE' THEN
                    NEW.first_activated_at := clock_timestamp();
                ELSIF NEW.first_activated_at IS DISTINCT FROM OLD.first_activated_at THEN
                    RAISE EXCEPTION 'Immutable activation provenance' USING ERRCODE = '23514';
                END IF;
                NEW.lifecycle_revision := OLD.lifecycle_revision + 1;
                NEW.updated_at := clock_timestamp();
            END IF;
            RETURN NEW;
        END
        $$
    """)

    op.execute("""
        CREATE FUNCTION public.assert_active_owner() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            org_id uuid;
            org_state text;
            first_activation timestamptz;
            actual_owners integer;
        BEGIN
            IF TG_TABLE_NAME = 'organizations' THEN
                org_id := NEW.id;
            ELSIF TG_TABLE_NAME = 'staff_accounts' THEN
                org_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.organization_id
                               ELSE NEW.organization_id END;
            ELSE
                SELECT s.organization_id INTO org_id FROM public.staff_accounts s
                WHERE s.auth_user_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.id
                                            ELSE NEW.id END;
            END IF;
            IF org_id IS NULL THEN
                RETURN NULL;
            END IF;
            SELECT state, first_activated_at INTO org_state, first_activation
            FROM public.organizations WHERE id = org_id FOR UPDATE;
            IF org_state = 'ACTIVE' THEN
                SELECT count(*) INTO actual_owners
                FROM public.staff_accounts s
                JOIN public."user" u ON u.id = s.auth_user_id
                WHERE s.organization_id = org_id
                  AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                  AND u."emailVerified" AND u.email = s.verified_email;
                IF actual_owners <> 1 THEN
                    RAISE EXCEPTION 'ACTIVE tenant requires one verified enabled Owner'
                        USING ERRCODE = '23514';
                END IF;
                IF TG_TABLE_NAME = 'organizations' AND OLD.state = 'PROVISIONING' THEN
                    IF first_activation IS NULL OR NOT EXISTS (
                        SELECT 1 FROM public.invitations i
                        JOIN public.staff_accounts s ON s.organization_id = i.organization_id
                        JOIN public."user" u ON u.id = s.auth_user_id
                        WHERE i.organization_id = org_id
                          AND i.purpose = 'FIRST_OWNER' AND i.state = 'ACCEPTED'
                          AND i.accepted_by_user_id = u.id
                          AND i.target_email = s.verified_email
                          AND s.role = 'DOCTOR_OWNER' AND s.enabled AND s.verified
                          AND i.accepted_at < i.expires_at
                    ) THEN
                        RAISE EXCEPTION 'Activation requires accepted first-Owner invitation'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
            END IF;
            RETURN NULL;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.assert_active_owner() FROM PUBLIC")
    op.execute("""
        CREATE CONSTRAINT TRIGGER organizations_owner_constraint
        AFTER INSERT OR UPDATE ON public.organizations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_active_owner()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER staff_owner_constraint
        AFTER INSERT OR UPDATE OR DELETE ON public.staff_accounts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_active_owner()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER native_user_owner_constraint
        AFTER UPDATE OR DELETE ON public."user"
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.assert_active_owner()
    """)

    # The auth tables are intentionally nonclinical: Better Auth's adapter
    # needs global identity lookups. Operators receive no authentication grants.
    for table in ("user", "session", "account", "verification"):
        op.execute(f'''
            GRANT SELECT, INSERT, UPDATE, DELETE ON public."{table}" TO carepulse_runtime
        ''')
    for table in (
        "staff_accounts",
        "invitations",
        "recovery_operations",
        "auth_recovery_completions",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_access ON public.{table}
            FOR ALL TO carepulse_runtime
            USING (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
            WITH CHECK (organization_id =
                NULLIF(current_setting('app.organization_id', true), '')::uuid)
        """
            if table != "auth_recovery_completions"
            else """
            CREATE POLICY auth_completion_tenant_access ON public.auth_recovery_completions
            FOR ALL TO carepulse_runtime
            USING (EXISTS (SELECT 1 FROM public.recovery_operations r
                           WHERE r.id = auth_recovery_completions.id AND r.organization_id =
                           NULLIF(current_setting('app.organization_id', true), '')::uuid))
            WITH CHECK (EXISTS (SELECT 1 FROM public.recovery_operations r
                                WHERE r.id = auth_recovery_completions.id AND r.organization_id =
                                NULLIF(current_setting('app.organization_id', true), '')::uuid))
        """
        )
        op.execute(f"""
            CREATE POLICY {table}_migration_access ON public.{table}
            FOR ALL TO carepulse_migration USING (true) WITH CHECK (true)
        """)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON public.{table} TO carepulse_runtime"
        )


def downgrade() -> None:
    # Restoring D-01's activation closure is unsafe after any activation or
    # persisted identity. Refuse a destructive rollback instead of deleting
    # operational auth and recovery history or stranding an ACTIVE tenant.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.organizations
                WHERE state <> 'PROVISIONING' OR first_activated_at IS NOT NULL
            ) OR EXISTS (SELECT 1 FROM public.staff_accounts)
              OR EXISTS (SELECT 1 FROM public.invitations)
              OR EXISTS (SELECT 1 FROM public.recovery_operations)
              OR EXISTS (SELECT 1 FROM public.auth_recovery_completions)
              OR EXISTS (SELECT 1 FROM public."user")
              OR EXISTS (SELECT 1 FROM public."account")
              OR EXISTS (SELECT 1 FROM public."session")
              OR EXISTS (SELECT 1 FROM public."verification") THEN
                RAISE EXCEPTION 'Cannot downgrade populated identity schema'
                    USING ERRCODE = '23514';
            END IF;
        END
        $$
    """)
    op.execute("DROP TRIGGER staff_reset_block_constraint ON public.staff_accounts")
    op.execute(
        "DROP TRIGGER recovery_operations_completion_constraint ON public.recovery_operations"
    )
    op.execute(
        "DROP TRIGGER auth_recovery_completions_completion_constraint ON public.auth_recovery_completions"
    )
    op.execute("DROP FUNCTION public.check_recovery_completion()")
    op.execute('DROP TRIGGER native_user_owner_constraint ON public."user"')
    op.execute("DROP TRIGGER staff_owner_constraint ON public.staff_accounts")
    op.execute("DROP TRIGGER organizations_owner_constraint ON public.organizations")
    op.execute("DROP FUNCTION public.assert_active_owner()")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.guard_organization_foundation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Organization deletion is forbidden' USING ERRCODE = '23514';
            END IF;
            IF NEW.state <> 'PROVISIONING' THEN
                RAISE EXCEPTION 'Organization activation requires D-02 guard'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.first_activated_at IS NOT NULL THEN
                RAISE EXCEPTION 'Organization has not been activated'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.region, NEW.timezone, NEW.currency,
                    NEW.created_by_operator_id, NEW.created_at) IS DISTINCT FROM
                   (OLD.id, OLD.region, OLD.timezone, OLD.currency,
                    OLD.created_by_operator_id, OLD.created_at) THEN
                    RAISE EXCEPTION 'Immutable organization identity'
                        USING ERRCODE = '23514';
                END IF;
                NEW.lifecycle_revision := OLD.lifecycle_revision + 1;
                NEW.updated_at := now();
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("DROP TABLE public.auth_recovery_completions")
    op.execute(
        "ALTER TABLE public.staff_accounts DROP CONSTRAINT staff_reset_block_owned"
    )
    op.execute("DROP TABLE public.recovery_operations")
    op.execute("DROP TABLE public.invitations")
    op.execute("DROP TABLE public.staff_accounts")
    op.execute("DROP FUNCTION public.guard_identity_row()")
    op.execute('DROP TRIGGER session_authority_immutable ON public."session"')
    op.execute("DROP FUNCTION public.guard_session_authority()")
    for table in ("verification", "account", "session", "user"):
        op.execute(f'DROP TABLE public."{table}"')
