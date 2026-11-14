"""D-01: real tenant isolation, metadata roles, and provisional activation denial."""

from asyncio import CancelledError
from contextlib import nullcontext
from dataclasses import replace
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from carepulse.db import transaction
from runtime_identity_fixture import signed_test_transaction
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_identity_persistence import _invitation, _staff, _user
from test_provisioning import _activation_transaction
from test_request_boundary import _signed
from test_request_boundary import boundary_data as boundary_data  # noqa: PLC0414


@pytest.fixture
def tenants(tenant_factory):
    operator_id, create_tenant = tenant_factory
    return (
        operator_id,
        create_tenant("Synthetic clinic A"),
        create_tenant("Synthetic clinic B"),
    )


@pytest.fixture
def bound_tenants(boundary_data, tenant_factory):
    clinic = boundary_data
    operator_id, _ = tenant_factory
    session_id = f"synthetic-d01-{uuid4()}"
    with clinic.migration.begin() as connection:
        user, email = _user(connection)
        staff = _staff(connection, clinic.other, user, email)
        invite = _invitation(
            connection, clinic.other, operator_id, (email, token_urlsafe(32))
        )
        connection.execute(
            text(
                "UPDATE public.invitations SET state='ACCEPTED', accepted_by_user_id=:user WHERE id=:invite"
            ),
            {"user": user, "invite": invite},
        )
        connection.execute(
            text("UPDATE public.organizations SET state='ACTIVE' WHERE id=:tenant"),
            {"tenant": clinic.other},
        )
        connection.execute(
            text("""
                INSERT INTO public."session" (id, "userId", token, "expiresAt", "createdAt", "updatedAt",
                    "securityRevision", "absoluteDeadline", "lastActivity")
                VALUES (:id, :user, :token, now()+interval '1 hour', now(), now(),
                    0, now()+interval '1 hour', now())
            """),
            {"id": session_id, "user": user, "token": f"synthetic-{uuid4()}"},
        )
    owner_a = _signed(clinic)[0]
    owner_b = replace(
        owner_a,
        organization_id=str(clinic.other),
        staff_id=str(staff),
        session_id=session_id,
    )
    return clinic, operator_id, owner_a, owner_b


def _signed_scope(clinic, actor):
    return signed_test_transaction(
        clinic,
        actor,
        method="POST",
        path="/v1/patients",
        purpose="PATIENT_REGISTRY",
        body={"name_parts": ["Synthetic", "D01", "Patient"]},
        key=str(uuid4()),
    )


@pytest.fixture
def clinical_relation(role_engine):
    migration = role_engine("carepulse_migration")
    try:
        with migration.begin() as connection:
            connection.execute(
                text("""
                CREATE TABLE public.synthetic_d01_clinical (
                    organization_id uuid NOT NULL REFERENCES public.organizations(id),
                    id uuid NOT NULL,
                    parent_id uuid,
                    UNIQUE (organization_id, id),
                    FOREIGN KEY (organization_id, parent_id)
                        REFERENCES public.synthetic_d01_clinical (organization_id, id)
                )
            """)
            )
            connection.execute(
                text(
                    "ALTER TABLE public.synthetic_d01_clinical ENABLE ROW LEVEL SECURITY"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE public.synthetic_d01_clinical FORCE ROW LEVEL SECURITY"
                )
            )
            connection.execute(
                text("""
                CREATE POLICY synthetic_tenant_policy ON public.synthetic_d01_clinical
                TO carepulse_api
                USING (organization_id::text = public.f01_org())
                WITH CHECK (organization_id::text = public.f01_org())
            """)
            )
            connection.execute(
                text("""
                GRANT SELECT, INSERT, UPDATE ON public.synthetic_d01_clinical TO carepulse_api
            """)
            )
        yield
    finally:
        with migration.begin() as connection:
            connection.execute(
                text("DROP TABLE IF EXISTS public.synthetic_d01_clinical")
            )
        migration.dispose()


def test_metadata_isolated_but_operator_can_list_only_metadata(
    bound_tenants, role_engine
):
    clinic, operator_id, owner_a, owner_b = bound_tenants
    clinic_a, clinic_b = clinic.tenant, clinic.other
    runtime = clinic.runtime
    operator = role_engine("carepulse_operator")
    try:
        with _signed_scope(clinic, owner_a) as (connection, _):
            assert connection.scalars(
                text("SELECT id FROM public.organizations")
            ).all() == [clinic_a]
        with _signed_scope(clinic, owner_b) as (connection, _):
            assert connection.scalars(
                text("SELECT id FROM public.organizations")
            ).all() == [clinic_b]
        with transaction(runtime) as connection:
            assert (
                connection.scalars(text("SELECT id FROM public.organizations")).all()
                == []
            )
            assert (
                connection.scalars(
                    text("SELECT id FROM public.organizations WHERE state='ACTIVE'")
                ).all()
                == []
            )
        # B-03 requires individual operator authentication for metadata listing;
        # the DB role itself cannot bypass the restricted CLI/function gate.
        with pytest.raises(DBAPIError) as denied_list, operator.connect() as connection:
            connection.execute(
                text("SELECT id FROM public.organizations WHERE id IN (:a, :b)"),
                {"a": clinic_a, "b": clinic_b},
            )
        assert denied_list.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied_insert, operator.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO public.organizations (id, branch, created_by_operator_id)
                VALUES (:id, 'Unauthorized bypass', :operator_id)
                """),
                {"id": uuid4(), "operator_id": operator_id},
            )
        assert denied_insert.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as denied, runtime.connect() as connection:
            connection.execute(text("SELECT * FROM public.operator_identities"))
        assert denied.value.orig.sqlstate == "42501"
        with (
            pytest.raises(DBAPIError) as denied_digest,
            operator.connect() as connection,
        ):
            connection.execute(
                text("SELECT credential_digest FROM public.operator_identities")
            )
        assert denied_digest.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()


def test_ownerless_organization_cannot_activate_or_mutate_fixed_identity(
    tenants, role_engine
):
    operator_id, clinic_a, _ = tenants
    migration = role_engine("carepulse_migration")
    try:
        with (
            pytest.raises(DBAPIError) as denied_new_active,
            migration.begin() as connection,
        ):
            connection.execute(
                text("""
                INSERT INTO public.organizations (id, branch, created_by_operator_id, state)
                VALUES (:id, 'Synthetic invalid active', :operator_id, 'ACTIVE')
                """),
                {"id": uuid4(), "operator_id": operator_id},
            )
        assert denied_new_active.value.orig.sqlstate == "23514"
        for change in (
            "state = 'ACTIVE'",
            "id = gen_random_uuid()",
            "currency = 'USD'",
            "timezone = 'UTC'",
            "region = 'OTHER'",
        ):
            with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
                connection.execute(
                    text(f"UPDATE public.organizations SET {change} WHERE id = :id"),
                    {"id": clinic_a},
                )
            assert denied.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError) as denied, migration.begin() as connection:
            connection.execute(
                text("DELETE FROM public.organizations WHERE id = :id"),
                {"id": clinic_a},
            )
        assert denied.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert connection.execute(
                text("""
                SELECT region, timezone, currency, state, lifecycle_revision
                FROM public.organizations WHERE id = :id
            """),
                {"id": clinic_a},
            ).one() == ("EGYPT", "Africa/Cairo", "EGP", "PROVISIONING", 0)
        with migration.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.organizations SET state = 'SUSPENDED' WHERE id = :id"
                ),
                {"id": clinic_a},
            )
        with pytest.raises(DBAPIError) as never_active, migration.begin() as connection:
            connection.execute(
                text("UPDATE public.organizations SET state = 'ACTIVE' WHERE id = :id"),
                {"id": clinic_a},
            )
        assert never_active.value.orig.sqlstate == "23514"
        with migration.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT state, first_activated_at FROM public.organizations WHERE id = :id"
                ),
                {"id": clinic_a},
            ).one() == ("SUSPENDED", None)
    finally:
        migration.dispose()


def test_clinical_rls_denies_wrong_context_and_tenant_foreign_key(
    bound_tenants, clinical_relation, role_engine
):
    clinic, _, owner_a, owner_b = bound_tenants
    clinic_a, clinic_b = clinic.tenant, clinic.other
    runtime = clinic.runtime
    migration = role_engine("carepulse_migration")
    parent_id = uuid4()
    try:
        with _signed_scope(clinic, owner_a) as (connection, _):
            connection.execute(
                text("""
                INSERT INTO public.synthetic_d01_clinical (organization_id, id)
                VALUES (:organization_id, :id)
            """),
                {"organization_id": clinic_a, "id": parent_id},
            )
            assert connection.scalars(
                text("SELECT id FROM public.synthetic_d01_clinical")
            ).all() == [parent_id]
            with pytest.raises(DBAPIError) as denied_update, connection.begin_nested():
                connection.execute(
                    text("""
                    UPDATE public.synthetic_d01_clinical SET organization_id = :foreign_tenant
                    WHERE id = :id
                    """),
                    {"foreign_tenant": clinic_b, "id": parent_id},
                )
            assert denied_update.value.orig.sqlstate == "42501"
        with _signed_scope(clinic, owner_b) as (connection, _):
            assert (
                connection.scalars(
                    text("SELECT id FROM public.synthetic_d01_clinical")
                ).all()
                == []
            )
            with pytest.raises(DBAPIError) as foreign_key, connection.begin_nested():
                connection.execute(
                    text("""
                    INSERT INTO public.synthetic_d01_clinical (organization_id, id, parent_id)
                    VALUES (:organization_id, :id, :parent_id)
                """),
                    {
                        "organization_id": clinic_b,
                        "id": uuid4(),
                        "parent_id": parent_id,
                    },
                )
            assert foreign_key.value.orig.sqlstate == "23503"
            with pytest.raises(DBAPIError) as wrong_tenant, connection.begin_nested():
                connection.execute(
                    text("""
                    INSERT INTO public.synthetic_d01_clinical (organization_id, id)
                    VALUES (:organization_id, :id)
                """),
                    {"organization_id": clinic_a, "id": uuid4()},
                )
            assert wrong_tenant.value.orig.sqlstate == "42501"
        for context in (None, "", "invalid-uuid"):
            with runtime.begin() as connection:
                if context is not None:
                    connection.execute(
                        text("SELECT set_config('app.organization_id', :ctx, true)"),
                        {"ctx": context},
                    )
                # F-01 ignores even malformed legacy GUCs rather than casting
                # them (the pre-F01 synthetic policy raised 22P02).
                assert (
                    connection.scalars(
                        text("SELECT id FROM public.synthetic_d01_clinical")
                    ).all()
                    == []
                )
                with (
                    pytest.raises(DBAPIError) as without_tenant,
                    connection.begin_nested(),
                ):
                    connection.execute(
                        text("""
                        INSERT INTO public.synthetic_d01_clinical (organization_id, id)
                        VALUES (:organization_id, :id)
                    """),
                        {"organization_id": clinic_a, "id": uuid4()},
                    )
                assert without_tenant.value.orig.sqlstate == "42501"
        with migration.begin() as owner:
            assert (
                owner.scalars(
                    text("SELECT id FROM public.synthetic_d01_clinical")
                ).all()
                == []
            )
            with pytest.raises(DBAPIError) as force_rls, owner.begin_nested():
                owner.execute(
                    text("""
                    INSERT INTO public.synthetic_d01_clinical (organization_id, id)
                    VALUES (:organization_id, :id)
                """),
                    {"organization_id": clinic_a, "id": uuid4()},
                )
            assert force_rls.value.orig.sqlstate == "42501"
    finally:
        migration.dispose()

    operator = role_engine("carepulse_operator")
    try:
        with pytest.raises(DBAPIError) as denied, operator.connect() as connection:
            connection.execute(text("SELECT id FROM public.synthetic_d01_clinical"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        operator.dispose()


@pytest.mark.parametrize("outcome", ["commit", "rollback", "sql_error", "cancellation"])
def test_reused_runtime_pool_does_not_expose_prior_tenant(
    outcome, bound_tenants, clinical_relation
):
    clinic, _, owner_a, owner_b = bound_tenants
    clinic_a, clinic_b = clinic.tenant, clinic.other
    runtime = clinic.runtime
    record_id = uuid4()
    expected_failure = {
        "commit": nullcontext(),
        "rollback": pytest.raises(RuntimeError, match="synthetic rollback"),
        "sql_error": pytest.raises(DBAPIError),
        "cancellation": pytest.raises(CancelledError, match="synthetic cancellation"),
    }[outcome]
    try:
        with expected_failure, _signed_scope(clinic, owner_a) as (connection, _):
            pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert connection.scalars(
                text("SELECT id FROM public.organizations")
            ).all() == [clinic_a]
            connection.execute(
                text("""
                INSERT INTO public.synthetic_d01_clinical (organization_id, id)
                VALUES (:organization_id, :id)
                """),
                {"organization_id": clinic_a, "id": record_id},
            )
            if outcome == "rollback":
                raise RuntimeError("synthetic rollback")
            if outcome == "sql_error":
                connection.execute(text("SELECT 1 / 0"))
            if outcome == "cancellation":
                raise CancelledError("synthetic cancellation")
        with transaction(runtime) as connection:
            assert connection.scalar(text("SELECT pg_backend_pid()")) == pid
            assert (
                connection.scalars(text("SELECT id FROM public.organizations")).all()
                == []
            )
            assert (
                connection.scalars(
                    text("SELECT id FROM public.synthetic_d01_clinical")
                ).all()
                == []
            )
        with _signed_scope(clinic, owner_b) as (connection, _):
            assert connection.scalars(
                text("SELECT id FROM public.organizations")
            ).all() == [clinic_b]
            assert (
                connection.scalars(
                    text("SELECT id FROM public.synthetic_d01_clinical")
                ).all()
                == []
            )
        with _signed_scope(clinic, owner_a) as (connection, _):
            assert connection.scalars(
                text("SELECT id FROM public.synthetic_d01_clinical")
            ).all() == ([record_id] if outcome == "commit" else [])
    finally:
        runtime.dispose()


def test_parallel_metadata_creation_of_same_tenant_has_one_winner(
    tenant_factory, role_engine, two_transactions
):
    operator_id, _ = tenant_factory
    tenant_id = uuid4()

    def insert_once(connection):
        connection.execute(
            text("""
            INSERT INTO public.organizations (id, branch, created_by_operator_id)
            VALUES (:id, 'Synthetic concurrent', :operator_id)
        """),
            {"id": tenant_id, "operator_id": operator_id},
        )
        return "created"

    outcomes = two_transactions("carepulse_migration", insert_once)
    assert outcomes.count("created") == 1
    failures = [outcome for outcome in outcomes if isinstance(outcome, DBAPIError)]
    assert len(failures) == 1 and failures[0].orig.sqlstate == "23505"
    migration = role_engine("carepulse_migration")
    runtime = role_engine("carepulse_api")
    gate = role_engine("carepulse_gate")
    try:
        proof = token_urlsafe(32)
        with migration.begin() as connection:
            user, email = _user(connection)
            invite = _invitation(connection, tenant_id, operator_id, (email, proof))
        with _activation_transaction(
            runtime, gate, tenant_id, invite, proof, user
        ) as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM public.organizations WHERE id = :id
            """),
                    {"id": tenant_id},
                )
                == 1
            )
    finally:
        migration.dispose()
        runtime.dispose()
        gate.dispose()


def test_pooled_session_setting_cannot_impersonate_tenant_without_context(
    bound_tenants, clinical_relation
):
    clinic, _, owner_a, _ = bound_tenants
    clinic_a = clinic.tenant
    runtime = clinic.runtime
    record_id = uuid4()
    try:
        with _signed_scope(clinic, owner_a) as (connection, _):
            connection.execute(
                text("""
                INSERT INTO public.synthetic_d01_clinical (organization_id, id)
                VALUES (:organization_id, :id)
                """),
                {"organization_id": clinic_a, "id": record_id},
            )
        with runtime.begin() as connection:
            backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            connection.execute(
                text("SELECT set_config('app.organization_id', :context, false)"),
                {"context": str(clinic_a)},
            )
        with transaction(runtime) as connection:
            assert connection.scalar(text("SELECT pg_backend_pid()")) == backend_pid
            assert (
                connection.scalars(
                    text("SELECT id FROM public.synthetic_d01_clinical")
                ).all()
                == []
            )
            assert (
                connection.scalars(text("SELECT id FROM public.organizations")).all()
                == []
            )
    finally:
        runtime.dispose()
