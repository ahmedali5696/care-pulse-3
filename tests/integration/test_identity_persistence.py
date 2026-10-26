"""D-02: persisted Better Auth and tenant identity invariants on disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from carepulse.db import transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def _user(connection):
    user_id = f"synthetic-{uuid4()}"
    email = f"{uuid4()}@example.invalid"
    connection.execute(
        text("""
            INSERT INTO public."user"
                (id, name, email, "emailVerified", "createdAt", "updatedAt")
            VALUES (:id, 'Synthetic owner', :email, true, clock_timestamp(), clock_timestamp())
        """),
        {"id": user_id, "email": email},
    )
    return user_id, email


def _staff(connection, tenant_id, user_id, email, role="DOCTOR_OWNER"):
    staff_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.staff_accounts
                (id, organization_id, auth_user_id, verified_email, role, enabled, verified)
            VALUES (:id, :tenant, :user, :email, :role, true, true)
        """),
        {
            "id": staff_id,
            "tenant": tenant_id,
            "user": user_id,
            "email": email,
            "role": role,
        },
    )
    return staff_id


def _invitation(
    connection, tenant_id, operator_id, proof, *, lifetime_seconds=120, invite_id=None
):
    invite_id = invite_id or uuid4()
    connection.execute(
        text("""
            INSERT INTO public.invitations
                (id, organization_id, purpose, target_email, proof_digest,
                 issued_by_operator_id, issued_at, expires_at)
            VALUES (:id, :tenant, 'FIRST_OWNER', :email, :proof, :operator,
                    clock_timestamp() - interval '10 seconds',
                    clock_timestamp() + (:lifetime * interval '1 second'))
        """),
        {
            "id": invite_id,
            "tenant": tenant_id,
            "email": proof[0],
            "lifetime": lifetime_seconds,
            "proof": sha256(proof[1].encode()).hexdigest(),
            "operator": operator_id,
        },
    )
    return invite_id


def _reservation(connection, tenant_id, staff_id, user_id, proof):
    operation_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO public.recovery_operations
                (id, organization_id, staff_id, auth_user_id, purpose, proof_digest,
                 proof_reference, proof_expires_at, request_fingerprint,
                 fingerprint_key_version, captured_staff_revision,
                 captured_lifecycle_revision, reserved_security_revision)
            VALUES (:id, :tenant, :staff, :user, 'PASSWORD_RESET', :digest,
                    'synthetic-ci', clock_timestamp() + interval '2 minutes', :fingerprint,
                    'synthetic-v1',
                    (SELECT revision + 1 FROM public.staff_accounts WHERE id = :staff),
                    (SELECT lifecycle_revision FROM public.organizations WHERE id = :tenant),
                    (SELECT security_revision + 1 FROM public.staff_accounts WHERE id = :staff))
        """),
        {
            "id": operation_id,
            "tenant": tenant_id,
            "staff": staff_id,
            "user": user_id,
            "digest": sha256(proof.encode()).hexdigest(),
            "fingerprint": sha256(("fingerprint-" + proof).encode()).hexdigest(),
        },
    )
    connection.execute(
        text("""
            UPDATE public.staff_accounts
            SET reset_block_operation_id = :operation, security_revision = security_revision + 1
            WHERE id = :staff
        """),
        {"operation": operation_id, "staff": staff_id},
    )
    return operation_id


@pytest.fixture
def identity(role_engine, tenant_factory):
    operator_id, create_tenant = tenant_factory
    tenant_id = create_tenant("Synthetic D-02 clinic")
    migration = role_engine("carepulse_migration")
    try:
        with migration.begin() as connection:
            user_id, email = _user(connection)
            staff_id = _staff(connection, tenant_id, user_id, email)
        yield migration, operator_id, tenant_id, user_id, email, staff_id
    finally:
        migration.dispose()


def test_native_models_and_server_session_columns_are_persisted(identity, role_engine):
    migration, _, tenant_id, user_id, _, _ = identity
    with migration.connect() as connection:
        tables = set(
            connection.scalars(
                text("""
            SELECT tablename FROM pg_tables WHERE schemaname = 'public'
        """)
            )
        )
        assert {
            "user",
            "account",
            "session",
            "verification",
            "staff_accounts",
            "invitations",
            "recovery_operations",
            "auth_recovery_completions",
        } <= tables
        columns = set(
            connection.scalars(
                text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'session'
        """)
            )
        )
        assert {
            "id",
            "userId",
            "token",
            "expiresAt",
            "securityRevision",
            "absoluteDeadline",
            "lastActivity",
        } <= columns
    with pytest.raises(DBAPIError) as missing, migration.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO public."session"
                (id, "userId", token, "expiresAt", "createdAt", "updatedAt")
            VALUES (:id, :user, :token, clock_timestamp() + interval '1 hour',
                    clock_timestamp(), clock_timestamp())
        """),
            {
                "id": f"native-{uuid4()}",
                "user": user_id,
                "token": f"synthetic-{uuid4()}",
            },
        )
    assert missing.value.orig.sqlstate == "23502"

    runtime = role_engine("carepulse_runtime")
    operator = role_engine("carepulse_operator")
    try:
        with transaction(runtime, tenant_id) as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.staff_accounts WHERE auth_user_id = :user
            """),
                    {"user": user_id},
                )
                == 1
            )
        with transaction(runtime) as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM public.staff_accounts"))
                == 0
            )
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT * FROM public.staff_accounts"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        runtime.dispose()
        operator.dispose()


def test_runtime_cannot_read_native_account_secrets(identity, role_engine):
    migration, _, tenant, _, _, _ = identity
    with migration.begin() as connection:
        unbound_user, _ = _user(connection)
        connection.execute(
            text('UPDATE public."user" SET "emailVerified"=false WHERE id=:user'),
            {"user": unbound_user},
        )
    runtime = role_engine("carepulse_runtime")
    auth = role_engine("carepulse_auth")
    try:
        for column in ("password", '"accessToken"', '"refreshToken"', '"idToken"', "*"):
            with (
                pytest.raises(DBAPIError) as denied,
                transaction(runtime, tenant) as connection,
            ):
                connection.execute(text(f'SELECT {column} FROM public."account"'))
            assert denied.value.orig.sqlstate == "42501"
        with transaction(auth) as connection:
            connection.execute(text('SELECT password FROM public."account" LIMIT 1'))
        with transaction(runtime, tenant) as connection:
            assert connection.scalar(
                text(
                    "SELECT public.native_account_unbound_for_receptionist(:tenant, :user)"
                ),
                {"tenant": tenant, "user": unbound_user},
            )
            assert not connection.scalar(
                text(
                    "SELECT public.native_account_unbound_for_receptionist(:tenant, :user)"
                ),
                {"tenant": uuid4(), "user": unbound_user},
            )
            assert not connection.scalar(
                text(
                    "SELECT public.native_account_unbound_for_receptionist(:tenant, :user)"
                ),
                {"tenant": tenant, "user": "synthetic-nonexistent"},
            )
            assert not connection.scalar(
                text(
                    "SELECT public.native_receptionist_credential_ready(:tenant, :invite)"
                ),
                {"tenant": tenant, "invite": uuid4()},
            )
    finally:
        runtime.dispose()
        auth.dispose()


def test_native_session_captured_authority_is_immutable_but_activity_advances(identity):
    migration, _, _, native_user, _, _ = identity
    session_id = f"synthetic-{uuid4()}"
    with migration.begin() as connection:
        other_user, _ = _user(connection)
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token, clock_timestamp() + interval '1 hour',
                        clock_timestamp(), clock_timestamp(), 3,
                        clock_timestamp() + interval '30 minutes', clock_timestamp())
            """),
            {"id": session_id, "user": native_user, "token": f"synthetic-{uuid4()}"},
        )
    with migration.begin() as connection:
        before = connection.scalar(
            text('SELECT "lastActivity" FROM public."session" WHERE id = :id'),
            {"id": session_id},
        )
        connection.execute(
            text(
                'UPDATE public."session" SET "lastActivity" = clock_timestamp() WHERE id = :id'
            ),
            {"id": session_id},
        )
        after = connection.scalar(
            text('SELECT "lastActivity" FROM public."session" WHERE id = :id'),
            {"id": session_id},
        )
        assert after >= before

    for statement, bindings in (
        (
            'UPDATE public."session" SET "absoluteDeadline" = "absoluteDeadline" + interval \'1 hour\' WHERE id = :id',
            {},
        ),
        (
            'UPDATE public."session" SET "securityRevision" = "securityRevision" + 1 WHERE id = :id',
            {},
        ),
        (
            'UPDATE public."session" SET "userId" = :other WHERE id = :id',
            {"other": other_user},
        ),
    ):
        with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
            connection.execute(text(statement), {"id": session_id, **bindings})
        assert denied.value.orig.sqlstate == "23514"


def test_prepared_native_user_alone_has_no_clinic_association(
    role_engine, tenant_factory
):
    _, create_tenant = tenant_factory
    tenant_id = create_tenant("Synthetic unaccepted clinic")
    migration = role_engine("carepulse_migration")
    runtime = role_engine("carepulse_runtime")
    try:
        with migration.begin() as connection:
            user_id, _ = _user(connection)
        with transaction(runtime, tenant_id) as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.staff_accounts WHERE auth_user_id = :user
            """),
                    {"user": user_id},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.runtime_active_tenant_ids
            """)
                )
                == 0
            )
    finally:
        runtime.dispose()
        migration.dispose()


def test_first_owner_invitation_is_single_use_and_reissue_requires_revocation(
    identity, tenant_factory
):
    migration, operator_id, tenant_id, user_id, email, _ = identity
    with migration.begin() as connection:
        invitation_id = _invitation(
            connection, tenant_id, operator_id, (email, "proof-a")
        )
    with pytest.raises(DBAPIError) as duplicate, migration.begin() as connection:
        _invitation(connection, tenant_id, operator_id, (email, "proof-b"))
    assert duplicate.value.orig.sqlstate == "23505"

    replacement = uuid4()
    with migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.invitations
            SET state = 'REVOKED', revocation_reference = 'synthetic-reissue',
                superseded_by_id = :replacement
            WHERE id = :id
        """),
            {"id": invitation_id, "replacement": replacement},
        )
        assert (
            _invitation(
                connection,
                tenant_id,
                operator_id,
                (email, "proof-b"),
                invite_id=replacement,
            )
            == replacement
        )
        connection.execute(
            text("""
            UPDATE public.invitations
            SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :id
        """),
            {"user": user_id, "id": replacement},
        )
    with pytest.raises(DBAPIError) as replay, migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.invitations SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :id
        """),
            {"user": user_id, "id": replacement},
        )
    assert replay.value.orig.sqlstate == "23514"
    with pytest.raises(DBAPIError) as revoked_replay, migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.invitations
                SET state = 'ACCEPTED', accepted_by_user_id = :user
                WHERE id = :id
            """),
            {"user": user_id, "id": invitation_id},
        )
    assert revoked_replay.value.orig.sqlstate == "23514"
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT state, accepted_at IS NOT NULL FROM public.invitations WHERE id = :id
        """),
            {"id": replacement},
        ).one() == ("ACCEPTED", True)
        assert connection.execute(
            text("""
                SELECT state, superseded_by_id FROM public.invitations WHERE id = :id
            """),
            {"id": invitation_id},
        ).one() == ("REVOKED", replacement)

    foreign_operator, create_tenant = tenant_factory
    foreign_tenant = create_tenant("Synthetic other invitation clinic")
    with migration.begin() as connection:
        foreign_invitation = _invitation(
            connection, foreign_tenant, foreign_operator, (email, "foreign-proof")
        )
    with pytest.raises(DBAPIError) as wrong_tenant, migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.invitations
                SET state = 'REVOKED', revocation_reference = 'synthetic-cross-tenant',
                    superseded_by_id = :replacement
                WHERE id = :id
            """),
            {"id": foreign_invitation, "replacement": replacement},
        )
    assert wrong_tenant.value.orig.sqlstate == "23503"


def test_expired_invitation_denied_at_database_clock(identity):
    migration, operator_id, tenant_id, user_id, email, _ = identity
    with migration.begin() as connection:
        expired = _invitation(
            connection,
            tenant_id,
            operator_id,
            (email, "expired-proof"),
            lifetime_seconds=0,
        )
    with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.invitations SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :id
        """),
            {"id": expired, "user": user_id},
        )
    assert denied.value.orig.sqlstate == "23514"


def test_activation_requires_accepted_verified_owner_and_last_owner_cannot_disable(
    identity,
):
    migration, operator_id, tenant_id, user_id, email, staff_id = identity
    with pytest.raises(DBAPIError) as ownerless, migration.begin() as connection:
        connection.execute(
            text("UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :id"),
            {"id": tenant_id},
        )
    assert ownerless.value.orig.sqlstate == "23514"

    with migration.begin() as connection:
        invited = _invitation(
            connection, tenant_id, operator_id, (email, "activate-proof")
        )
        connection.execute(
            text("""
            UPDATE public.invitations SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :id
        """),
            {"id": invited, "user": user_id},
        )
        connection.execute(
            text("UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :id"),
            {"id": tenant_id},
        )

    for statement, parameters in (
        (
            "UPDATE public.staff_accounts SET enabled = false, security_revision = security_revision + 1 WHERE id = :id",
            {"id": staff_id},
        ),
        (
            "UPDATE public.staff_accounts SET verified = false, security_revision = security_revision + 1 WHERE id = :id",
            {"id": staff_id},
        ),
        (
            'UPDATE public."user" SET "emailVerified" = false WHERE id = :id',
            {"id": user_id},
        ),
    ):
        with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
            connection.execute(text(statement), parameters)
        assert denied.value.orig.sqlstate == "23514"


def test_competing_owner_inserts_allow_only_one_owner(
    tenant_factory, role_engine, two_transactions
):
    _, create_tenant = tenant_factory
    tenant_id = create_tenant("Synthetic simultaneous owner clinic")

    def create_owner(connection):
        native_user, email = _user(connection)
        return _staff(connection, tenant_id, native_user, email)

    outcomes = two_transactions("carepulse_migration", create_owner)
    assert (
        len([outcome for outcome in outcomes if not isinstance(outcome, Exception)])
        == 1
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, DBAPIError)]
    assert len(errors) == 1 and errors[0].orig.sqlstate == "23505"
    migration = role_engine("carepulse_migration")
    try:
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.staff_accounts
                WHERE organization_id = :tenant AND role = 'DOCTOR_OWNER'
            """),
                    {"tenant": tenant_id},
                )
                == 1
            )
    finally:
        migration.dispose()


def test_distinct_recovery_proofs_cannot_reserve_same_user_concurrently(
    identity, two_transactions
):
    migration, _, tenant_id, user_id, _, staff_id = identity

    def reserve(connection):
        proof = f"proof-{uuid4()}"
        return _reservation(connection, tenant_id, staff_id, user_id, proof)

    outcomes = two_transactions("carepulse_migration", reserve)
    assert (
        len([outcome for outcome in outcomes if not isinstance(outcome, Exception)])
        == 1
    )
    failures = [outcome for outcome in outcomes if isinstance(outcome, DBAPIError)]
    assert len(failures) == 1 and failures[0].orig.sqlstate == "23505"
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.recovery_operations WHERE auth_user_id = :user
        """),
                {"user": user_id},
            )
            == 1
        )


@pytest.mark.parametrize("role", ["carepulse_runtime", "carepulse_auth"])
def test_application_roles_cannot_forge_native_recovery_completion(
    identity, role_engine, role
):
    migration, operator_id, tenant_id, user_id, email, staff_id = identity
    with migration.begin() as connection:
        invitation = _invitation(
            connection,
            tenant_id,
            operator_id,
            (email, f"synthetic-native-boundary-{uuid4()}"),
        )
        connection.execute(
            text("""
                UPDATE public.invitations SET state = 'ACCEPTED',
                    accepted_by_user_id = :user WHERE id = :invitation
            """),
            {"user": user_id, "invitation": invitation},
        )
        connection.execute(
            text("UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :tenant"),
            {"tenant": tenant_id},
        )
        operation = _reservation(
            connection,
            tenant_id,
            staff_id,
            user_id,
            f"synthetic-auth-forgery-{uuid4()}",
        )

    application = role_engine(role)
    try:
        for statement in (
            """
                UPDATE public.recovery_operations
                   SET state='AUTH_DONE', auth_fence=1, auth_done_at=clock_timestamp()
                 WHERE id=:operation
            """,
            """
                INSERT INTO public.auth_recovery_completions
                    (id, proof_digest, auth_user_id, request_fingerprint,
                     reserved_security_revision, auth_fence, completed_at)
                SELECT id, proof_digest, auth_user_id, request_fingerprint,
                       reserved_security_revision, 1, clock_timestamp()
                  FROM public.recovery_operations WHERE id=:operation
            """,
            """
                INSERT INTO public.recovery_native_effects
                    (id, auth_fence, proof_consumed_at, credential_changed_at)
                VALUES (:operation, 1, clock_timestamp(), clock_timestamp())
            """,
            """
                INSERT INTO public.sensitive_audit
                    (id, organization_id, actor_staff_id, actor_role,
                     action, correlation_id, recovery_operation_id)
                VALUES (:audit_id, :tenant, :staff, 'SYSTEM',
                        'PASSWORD_RESET_SUCCEEDED', :correlation, :operation)
            """,
        ):
            with (
                pytest.raises(DBAPIError) as denied,
                transaction(application, tenant_id) as connection,
            ):
                connection.execute(
                    text(statement),
                    {
                        "operation": operation,
                        "tenant": tenant_id,
                        "staff": staff_id,
                        "audit_id": uuid4(),
                        "correlation": str(operation),
                    },
                )
            assert denied.value.orig.sqlstate == "42501"
    finally:
        application.dispose()

    with migration.connect() as connection:
        state = connection.execute(
            text("""
                SELECT r.state, r.auth_fence, r.auth_done_at,
                       s.reset_block_operation_id,
                       (SELECT count(*) FROM public.auth_recovery_completions c
                        WHERE c.id = r.id),
                       (SELECT count(*) FROM public.sensitive_audit a
                        WHERE a.recovery_operation_id = r.id
                          AND a.action = 'PASSWORD_RESET_SUCCEEDED')
                  FROM public.recovery_operations r
                  JOIN public.staff_accounts s ON s.id = r.staff_id
                 WHERE r.id = :operation
            """),
            {"operation": operation},
        ).one()
    assert state == ("RESERVED", 0, None, operation, 0, 0)


def test_synthetic_completion_without_native_effect_cannot_finalize(
    identity, role_engine
):
    migration, operator_id, tenant_id, user_id, email, staff_id = identity
    with migration.begin() as connection:
        invitation = _invitation(
            connection, tenant_id, operator_id, (email, "synthetic-missing-native")
        )
        connection.execute(
            text("""
                UPDATE public.invitations
                   SET state='ACCEPTED', accepted_by_user_id=:user
                 WHERE id=:invitation
            """),
            {"user": user_id, "invitation": invitation},
        )
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:tenant"),
            {"tenant": tenant_id},
        )
        operation = _reservation(
            connection,
            tenant_id,
            staff_id,
            user_id,
            f"synthetic-missing-effect-{uuid4()}",
        )
        # Migration-only synthetic fixture: immutable completion matching on paper,
        # but no supported Better Auth proof consumption or credential write.
        connection.execute(
            text("""
                UPDATE public.recovery_operations
                   SET state='AUTH_DONE', auth_fence=1,
                       auth_done_at=clock_timestamp()
                 WHERE id=:operation
            """),
            {"operation": operation},
        )
        connection.execute(
            text("""
                INSERT INTO public.auth_recovery_completions
                    (id, proof_digest, auth_user_id, request_fingerprint,
                     reserved_security_revision, auth_fence, completed_at)
                SELECT id, proof_digest, auth_user_id, request_fingerprint,
                       reserved_security_revision, auth_fence, auth_done_at
                  FROM public.recovery_operations WHERE id=:operation
            """),
            {"operation": operation},
        )

    runtime = role_engine("carepulse_runtime")
    try:
        with (
            pytest.raises(DBAPIError) as denied,
            transaction(runtime, tenant_id) as connection,
        ):
            connection.execute(
                text("SELECT public.finalize_verified_recovery(:operation)"),
                {"operation": operation},
            )
        assert denied.value.orig.sqlstate == "23514"
    finally:
        runtime.dispose()

    with migration.connect() as connection:
        result = connection.execute(
            text("""
                SELECT r.state, s.reset_block_operation_id,
                       (SELECT count(*) FROM public.recovery_native_effects e
                        WHERE e.id=r.id),
                       (SELECT count(*) FROM public.sensitive_audit a
                        WHERE a.recovery_operation_id=r.id
                          AND a.action='PASSWORD_RESET_SUCCEEDED')
                  FROM public.recovery_operations r
                  JOIN public.staff_accounts s ON s.id=r.staff_id
                 WHERE r.id=:operation
            """),
            {"operation": operation},
        ).one()
    assert result == ("AUTH_DONE", operation, 0, 0)


@pytest.mark.parametrize(
    "statement",
    [
        'UPDATE public."account" SET password = NULL WHERE "userId" = :user',
        'DELETE FROM public."session" WHERE "userId" = :user',
        'DELETE FROM public."verification" WHERE value = :user',
        'UPDATE public."user" SET name = name WHERE id = :user',
    ],
)
def test_clinic_runtime_cannot_mutate_native_identity(identity, role_engine, statement):
    _, _, tenant_id, user_id, _, _ = identity
    runtime = role_engine("carepulse_runtime")
    try:
        with (
            pytest.raises(DBAPIError) as denied,
            transaction(runtime, tenant_id) as connection,
        ):
            connection.execute(text(statement), {"user": user_id})
        assert denied.value.orig.sqlstate == "42501"
    finally:
        runtime.dispose()


def test_runtime_owner_recovery_row_lock_is_scoped_without_update_grant(
    identity, role_engine
):
    migration, operator_id, tenant_id, user_id, email, staff_id = identity
    proof = f"synthetic-owned-lock-{uuid4()}"
    with migration.begin() as connection:
        invitation = _invitation(
            connection, tenant_id, operator_id, (email, f"synthetic-lock-{uuid4()}")
        )
        connection.execute(
            text(
                "UPDATE public.invitations SET state='ACCEPTED', accepted_by_user_id=:user "
                "WHERE id=:invitation"
            ),
            {"user": user_id, "invitation": invitation},
        )
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:tenant"),
            {"tenant": tenant_id},
        )
        operation_id = _reservation(connection, tenant_id, staff_id, user_id, proof)

    runtime = role_engine("carepulse_runtime")
    try:
        with (
            pytest.raises(DBAPIError) as denied,
            transaction(runtime, tenant_id) as connection,
        ):
            connection.execute(
                text("SELECT id FROM public.recovery_operations FOR UPDATE")
            )
        assert denied.value.orig.sqlstate == "42501"

        with transaction(runtime, tenant_id) as connection:
            statement = text(
                "SELECT id FROM public.lock_owned_recovery(:user, :digest)"
            )
            digest = sha256(proof.encode()).hexdigest()
            params = {"user": user_id, "digest": digest}
            # The definer must require the Owner identity validated by the caller.
            assert connection.execute(statement, params).all() == []
            connection.execute(
                text("SELECT set_config('app.staff_id', :staff, true)"),
                {"staff": str(staff_id)},
            )
            assert connection.execute(statement, params).scalar_one() == operation_id
            assert (
                connection.execute(
                    statement, {"user": user_id, "digest": None}
                ).scalar_one()
                == operation_id
            )
            assert (
                connection.execute(
                    statement, {"user": "other-user", "digest": digest}
                ).all()
                == []
            )
            assert (
                connection.execute(
                    statement, {"user": user_id, "digest": "0" * 64}
                ).all()
                == []
            )
        with transaction(runtime, uuid4()) as connection:
            connection.execute(
                text("SELECT set_config('app.staff_id', :staff, true)"),
                {"staff": str(staff_id)},
            )
            assert connection.execute(statement, params).all() == []
    finally:
        runtime.dispose()


def test_runtime_session_cleanup_preserves_live_receptionist_authority(
    identity, role_engine
):
    migration, operator_id, tenant_id, owner_id, owner_email, _ = identity
    session_id = f"synthetic-cleanup-{uuid4()}"
    with migration.begin() as connection:
        invitation = _invitation(
            connection,
            tenant_id,
            operator_id,
            (owner_email, f"synthetic-cleanup-activation-{uuid4()}"),
        )
        connection.execute(
            text("""
                UPDATE public.invitations
                   SET state='ACCEPTED', accepted_by_user_id=:owner
                 WHERE id=:invitation
            """),
            {"owner": owner_id, "invitation": invitation},
        )
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:tenant"),
            {"tenant": tenant_id},
        )
        receptionist_user, receptionist_email = _user(connection)
        receptionist_staff = _staff(
            connection, tenant_id, receptionist_user, receptionist_email, "RECEPTIONIST"
        )
        connection.execute(
            text("""
                INSERT INTO public."session"
                    (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                     "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token, clock_timestamp() + interval '1 hour',
                        clock_timestamp(), clock_timestamp(), 0,
                        clock_timestamp() + interval '1 hour', clock_timestamp())
            """),
            {
                "id": session_id,
                "user": receptionist_user,
                "token": f"synthetic-cleanup-token-{uuid4()}",
            },
        )

    runtime = role_engine("carepulse_runtime")
    try:
        with transaction(runtime, tenant_id) as connection:
            assert (
                connection.scalar(
                    text("SELECT public.delete_staff_sessions(:tenant, :user)"),
                    {"tenant": tenant_id, "user": receptionist_user},
                )
                == 0
            )

        for sql in (
            'SELECT id FROM public."session" WHERE id=:id FOR UPDATE',
            'UPDATE public."session" SET "userAgent" = \'forged\' WHERE id=:id',
            'DELETE FROM public."session" WHERE id=:id',
        ):
            with (
                pytest.raises(DBAPIError) as denied,
                transaction(runtime, tenant_id) as connection,
            ):
                connection.execute(text(sql), {"id": session_id})
            assert denied.value.orig.sqlstate == "42501"

        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text('SELECT count(*) FROM public."session" WHERE id=:id'),
                    {"id": session_id},
                )
                == 1
            )

        with migration.begin() as connection:
            connection.execute(
                text("""
                    UPDATE public.staff_accounts
                       SET security_revision = security_revision + 1
                     WHERE organization_id=:tenant AND id=:staff
                """),
                {"tenant": tenant_id, "staff": receptionist_staff},
            )

        with transaction(runtime, tenant_id) as connection:
            assert (
                connection.scalar(
                    text("SELECT public.delete_staff_sessions(:tenant, :user)"),
                    {"tenant": tenant_id, "user": receptionist_user},
                )
                == 1
            )
        with migration.connect() as connection:
            assert (
                connection.scalar(
                    text('SELECT count(*) FROM public."session" WHERE id=:id'),
                    {"id": session_id},
                )
                == 0
            )
    finally:
        runtime.dispose()


@pytest.mark.parametrize("requested_tenant", [None, "foreign"])
def test_auth_role_cannot_change_blocked_owner_without_exact_tenant(
    identity, role_engine, requested_tenant
):
    migration, _, tenant_id, user_id, _, staff_id = identity
    account_id = f"synthetic-b04-auth-scope-{uuid4()}"
    original_password = "synthetic-credential-unchanged"
    with migration.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO public."account"
                    (id, "accountId", "providerId", "userId", password,
                     "createdAt", "updatedAt")
                VALUES (:id, :user, 'credential', :user, :password,
                        clock_timestamp(), clock_timestamp())
            """),
            {"id": account_id, "user": user_id, "password": original_password},
        )
        _reservation(connection, tenant_id, staff_id, user_id, f"scope-{uuid4()}")

    auth = role_engine("carepulse_auth")
    scope = uuid4() if requested_tenant == "foreign" else None
    try:
        with (
            pytest.raises(DBAPIError) as denied,
            transaction(auth, scope) as connection,
        ):
            connection.execute(
                text("""
                    UPDATE public."account" SET password=:password
                     WHERE id=:id
                """),
                {"id": account_id, "password": "synthetic-forged-password"},
            )
        assert denied.value.orig.sqlstate == "23514"
    finally:
        auth.dispose()
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text('SELECT password FROM public."account" WHERE id=:id'),
                {"id": account_id},
            )
            == original_password
        )


def test_expired_abandonment_cannot_remove_block_without_successor(
    identity, role_engine
):
    migration, _, tenant_id, user_id, _, staff_id = identity
    with migration.begin() as connection:
        operation = _reservation(
            connection, tenant_id, staff_id, user_id, f"synthetic-abandon-{uuid4()}"
        )
    # Controlled CI fixture crosses the immutable expiry guard to exercise a
    # real expired transaction against the runtime role.
    with migration.begin() as connection:
        connection.execute(
            text("""
                ALTER TABLE public.recovery_operations
                    DISABLE TRIGGER recovery_operations_mutation_guard
            """)
        )
        try:
            connection.execute(
                text("""
                    UPDATE public.recovery_operations
                       SET created_at=clock_timestamp()-interval '2 hours',
                           proof_expires_at=clock_timestamp()-interval '1 hour'
                     WHERE id=:operation
                """),
                {"operation": operation},
            )
            # Flush the deferred recovery/FK checks before altering trigger DDL.
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            connection.execute(
                text("""
                    ALTER TABLE public.recovery_operations
                        ENABLE TRIGGER recovery_operations_mutation_guard
                """)
            )

    runtime = role_engine("carepulse_runtime")
    try:
        with (
            pytest.raises(DBAPIError) as denied,
            transaction(runtime, tenant_id) as connection,
        ):
            assert (
                connection.scalar(
                    text("SELECT public.abandon_owned_recovery(:operation)"),
                    {"operation": operation},
                )
                == "ABORTED"
            )
            connection.execute(
                text("""
                    UPDATE public.staff_accounts
                       SET reset_block_operation_id=NULL
                     WHERE organization_id=:tenant AND id=:staff
                """),
                {"tenant": tenant_id, "staff": staff_id},
            )
        assert denied.value.orig.sqlstate == "23514"
    finally:
        runtime.dispose()

    with migration.connect() as connection:
        assert connection.execute(
            text("""
                SELECT r.state, r.closed_at, s.reset_block_operation_id
                  FROM public.recovery_operations r
                  JOIN public.staff_accounts s ON s.id=r.staff_id
                 WHERE r.id=:operation
            """),
            {"operation": operation},
        ).one() == ("RESERVED", None, operation)


def test_recovery_block_belongs_to_its_operation_and_completion_is_immutable(identity):
    migration, _, tenant_id, user_id, _, staff_id = identity
    with migration.begin() as connection:
        operation = _reservation(
            connection, tenant_id, staff_id, user_id, "owned-proof"
        )
        other_user, other_email = _user(connection)
        other_staff = _staff(
            connection, tenant_id, other_user, other_email, "RECEPTIONIST"
        )

    with pytest.raises(DBAPIError) as foreign_block, migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.staff_accounts SET reset_block_operation_id = :operation
            WHERE id = :staff
        """),
            {"operation": operation, "staff": other_staff},
        )
    assert foreign_block.value.orig.sqlstate in {"23503", "23514"}

    with migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.recovery_operations
            SET auth_fence = 1, state = 'AUTH_DONE', auth_done_at = clock_timestamp()
            WHERE id = :id
        """),
            {"id": operation},
        )
        connection.execute(
            text("""
            INSERT INTO public.auth_recovery_completions
                (id, proof_digest, auth_user_id, request_fingerprint,
                 reserved_security_revision, auth_fence, completed_at)
            SELECT id, proof_digest, auth_user_id, request_fingerprint,
                   reserved_security_revision, auth_fence, auth_done_at
            FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        )
    for statement in (
        "UPDATE public.auth_recovery_completions SET auth_fence = 2 WHERE id = :id",
        "DELETE FROM public.auth_recovery_completions WHERE id = :id",
    ):
        with pytest.raises(DBAPIError) as immutable, migration.begin() as connection:
            connection.execute(text(statement), {"id": operation})
        assert immutable.value.orig.sqlstate == "23514"


def test_completion_evidence_cannot_misbind_another_proof(identity):
    migration, _, tenant_id, user_id, _, staff_id = identity
    with migration.begin() as connection:
        operation = _reservation(connection, tenant_id, staff_id, user_id, "auth-proof")
    with pytest.raises(DBAPIError) as forged, migration.begin() as connection:
        connection.execute(
            text("""
                UPDATE public.recovery_operations
                SET auth_fence = 1, state = 'AUTH_DONE', auth_done_at = clock_timestamp()
                WHERE id = :id
            """),
            {"id": operation},
        )
        connection.execute(
            text("""
            INSERT INTO public.auth_recovery_completions
                (id, proof_digest, auth_user_id, request_fingerprint,
                 reserved_security_revision, auth_fence)
            VALUES (:id, :forged, :user, :fingerprint, 1, 1)
        """),
            {
                "id": operation,
                "forged": sha256(b"other-proof").hexdigest(),
                "user": user_id,
                "fingerprint": sha256(b"unrelated-password").hexdigest(),
            },
        )
    assert forged.value.orig.sqlstate in {"23503", "23514"}


def test_completion_timestamp_must_match_reserved_auth_effect(identity):
    migration, _, tenant_id, user_id, _, staff_id = identity
    with migration.begin() as connection:
        operation = _reservation(
            connection, tenant_id, staff_id, user_id, "timestamp-proof"
        )
    with pytest.raises(DBAPIError) as mismatched, migration.begin() as connection:
        connection.execute(
            text("""
            UPDATE public.recovery_operations
            SET state = 'AUTH_DONE', auth_done_at = clock_timestamp(), auth_fence = 1
            WHERE id = :id
        """),
            {"id": operation},
        )
        connection.execute(
            text("""
            INSERT INTO public.auth_recovery_completions
                (id, proof_digest, auth_user_id, request_fingerprint,
                 reserved_security_revision, auth_fence, completed_at)
            SELECT id, proof_digest, auth_user_id, request_fingerprint,
                   reserved_security_revision, auth_fence,
                   auth_done_at + interval '1 second'
            FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        )
    assert mismatched.value.orig.sqlstate == "23514"
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT state, auth_done_at FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        ).one() == ("RESERVED", None)


def test_competing_auth_completion_has_one_durable_winner(identity, two_transactions):
    migration, _, tenant_id, native_user, _, staff_id = identity
    with migration.begin() as connection:
        operation = _reservation(
            connection, tenant_id, staff_id, native_user, "auth-race"
        )

    def complete_once(connection):
        connection.execute(
            text("""
            UPDATE public.recovery_operations
            SET state = 'AUTH_DONE', auth_done_at = clock_timestamp(), auth_fence = auth_fence + 1
            WHERE id = :id
        """),
            {"id": operation},
        )
        connection.execute(
            text("""
            INSERT INTO public.auth_recovery_completions
                (id, proof_digest, auth_user_id, request_fingerprint,
                 reserved_security_revision, auth_fence, completed_at)
            SELECT id, proof_digest, auth_user_id, request_fingerprint,
                   reserved_security_revision, auth_fence, auth_done_at
            FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        )
        return "completed"

    outcomes = two_transactions("carepulse_migration", complete_once)
    assert outcomes.count("completed") == 1
    failed = [outcome for outcome in outcomes if isinstance(outcome, DBAPIError)]
    assert len(failed) == 1 and failed[0].orig.sqlstate in {"23505", "23514"}
    with migration.connect() as connection:
        assert (
            connection.scalar(
                text("""
            SELECT count(*) FROM public.auth_recovery_completions WHERE id = :id
        """),
                {"id": operation},
            )
            == 1
        )
        assert connection.execute(
            text("""
            SELECT state, auth_fence FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        ).one() == ("AUTH_DONE", 1)


def test_closed_recovery_proof_remains_unique(identity):
    migration, operator_id, tenant_id, native_user, email, staff_id = identity
    proof = "synthetic-consumed-proof"
    with migration.begin() as connection:
        invitation = _invitation(
            connection, tenant_id, operator_id, (email, "activate-closed-proof")
        )
        connection.execute(
            text("""
            UPDATE public.invitations SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :invitation
        """),
            {"invitation": invitation, "user": native_user},
        )
        connection.execute(
            text("UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :tenant"),
            {"tenant": tenant_id},
        )
        operation = _reservation(connection, tenant_id, staff_id, native_user, proof)
        connection.execute(
            text("""
            UPDATE public.recovery_operations
            SET state = 'AUTH_DONE', auth_done_at = clock_timestamp(), auth_fence = 1
            WHERE id = :id
        """),
            {"id": operation},
        )
        connection.execute(
            text("""
            INSERT INTO public.auth_recovery_completions
                (id, proof_digest, auth_user_id, request_fingerprint,
                 reserved_security_revision, auth_fence, completed_at)
            SELECT id, proof_digest, auth_user_id, request_fingerprint,
                   reserved_security_revision, auth_fence, auth_done_at
            FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        )
        connection.execute(
            text("""
            UPDATE public.recovery_operations
            SET state = 'FINALIZED', closed_at = clock_timestamp() WHERE id = :id
        """),
            {"id": operation},
        )
        connection.execute(
            text("""
            UPDATE public.staff_accounts SET reset_block_operation_id = NULL WHERE id = :id
        """),
            {"id": staff_id},
        )
        connection.execute(
            text("""
            INSERT INTO public.sensitive_audit
              (id, organization_id, actor_staff_id, actor_role,
               action, correlation_id, recovery_operation_id)
            VALUES (:id, :tenant, :staff, 'SYSTEM',
                    'PASSWORD_RESET_SUCCEEDED', :correlation, :operation)
        """),
            {
                "id": uuid4(),
                "tenant": tenant_id,
                "staff": staff_id,
                "correlation": str(operation),
                "operation": operation,
            },
        )

    with pytest.raises(DBAPIError) as replay, migration.begin() as connection:
        other_user, other_email = _user(connection)
        other_staff = _staff(
            connection, tenant_id, other_user, other_email, "RECEPTIONIST"
        )
        _reservation(connection, tenant_id, other_staff, other_user, proof)
    assert replay.value.orig.sqlstate == "23505"
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT state, closed_at IS NOT NULL
            FROM public.recovery_operations WHERE id = :id
        """),
            {"id": operation},
        ).one() == ("FINALIZED", True)


def test_competing_activation_and_last_owner_disable_cannot_leave_active_ownerless(
    identity,
):
    migration, operator_id, tenant_id, user_id, email, staff_id = identity
    with migration.begin() as connection:
        invitation = _invitation(
            connection, tenant_id, operator_id, (email, "competing-proof")
        )
        connection.execute(
            text("""
            UPDATE public.invitations SET state = 'ACCEPTED', accepted_by_user_id = :user
            WHERE id = :id
        """),
            {"id": invitation, "user": user_id},
        )

    barrier = Barrier(2)

    def run(statement):
        engine = migration
        try:
            with engine.begin() as connection:
                barrier.wait(timeout=10)
                connection.execute(
                    text(statement), {"id": tenant_id, "staff": staff_id}
                )
            return "committed"
        except DBAPIError as error:
            return error.orig.sqlstate

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(
            run, "UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :id"
        )
        b = executor.submit(
            run,
            "UPDATE public.staff_accounts SET enabled = false, security_revision = security_revision + 1 WHERE id = :staff",
        )
        assert sorted((a.result(), b.result())) == ["23514", "committed"]
    with migration.connect() as connection:
        assert connection.execute(
            text("""
            SELECT o.state, s.enabled FROM public.organizations o
            JOIN public.staff_accounts s ON s.organization_id = o.id
            WHERE o.id = :id
        """),
            {"id": tenant_id},
        ).one() in (("ACTIVE", True), ("PROVISIONING", False))
