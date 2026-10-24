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
                    'synthetic-v1', 0, 0, 1)
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
    migration, _, tenant_id, native_user, _, staff_id = identity
    proof = "synthetic-consumed-proof"
    with migration.begin() as connection:
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
