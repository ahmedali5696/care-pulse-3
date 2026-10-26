"""B-05 real PostgreSQL lifecycle, revision, rollback and live-revocation races."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic
from uuid import UUID, uuid4

import pytest
from carepulse.db import transaction
from carepulse.identity.contracts import ContractError
from carepulse.patients.lifecycle import lock_patient_for_new_appointment
from sqlalchemy import text
from test_patients import PATIENT, create_patient, effects, private_request
from test_patients import boundary_data as boundary_data  # noqa: PLC0414 - pytest fixture export
from test_patients import patient_backend as patient_backend  # noqa: PLC0414 - pytest fixture export


def _parallel(*commands):
    barrier = Barrier(len(commands))

    def run(command):
        barrier.wait(timeout=10)
        return command()

    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        return [
            future.result(timeout=15)
            for future in [pool.submit(run, command) for command in commands]
        ]


def _wait_for_blocker(observer, holder_pid):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        observer.execute(text("SELECT pg_stat_clear_snapshot()"))
        if observer.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid))"
            ),
            {"holder": holder_pid},
        ):
            return
    pytest.fail("No real PostgreSQL lock waiter observed")


@pytest.mark.parametrize("kind", ["demographic", "lifecycle", "same_create_key"])
def test_competing_commands_have_one_effect_or_one_receipt(patient_backend, kind):
    clinic = patient_backend
    patient = create_patient(clinic)
    path = f"/v1/patients/{patient['id']}"
    before = effects(clinic)
    key = str(uuid4())

    def command():
        if kind == "demographic":
            return private_request(
                clinic,
                clinic.owner,
                path,
                method="PATCH",
                body={
                    "expected_demographic_revision": 0,
                    "phone_raw": "Concurrent edit",
                },
            )
        if kind == "lifecycle":
            return private_request(
                clinic,
                clinic.owner,
                path + "/archive",
                method="POST",
                body={"expected_lifecycle_revision": 0, "reason": "Concurrent archive"},
            )
        return private_request(
            clinic, clinic.owner, method="POST", body=PATIENT, key=key
        )

    responses = _parallel(command, command)
    assert sorted(status for status, _ in responses) == (
        [201, 201] if kind == "same_create_key" else [200, 409]
    )
    after = effects(clinic)
    assert after == (
        before[0] + (kind == "same_create_key"),
        before[1] + 1,
        before[2] + (kind == "same_create_key"),
        before[3],
        before[4],
    )
    if kind == "same_create_key":
        assert responses[0] == responses[1]


@pytest.mark.parametrize(
    "role,transition",
    [("owner", "archive"), ("receptionist", "archive"), ("owner", "unarchive")],
)
def test_archive_transition_and_patch_check_role_under_the_same_lifecycle_lock(
    patient_backend, role, transition
):
    clinic = patient_backend
    patient = create_patient(clinic)
    path = f"/v1/patients/{patient['id']}"
    revision = 0
    if transition == "unarchive":
        assert (
            private_request(
                clinic,
                clinic.owner,
                path + "/archive",
                method="POST",
                body={"expected_lifecycle_revision": 0, "reason": "Prepare archive"},
            )[0]
            == 200
        )
        revision = 1
    before = effects(clinic)
    patch, lifecycle = _parallel(
        lambda: private_request(
            clinic,
            getattr(clinic, role),
            path,
            method="PATCH",
            body={
                "expected_demographic_revision": 0,
                "food_allergies": "Synthetic concurrent intake",
            },
        ),
        lambda: private_request(
            clinic,
            clinic.owner,
            path + "/" + transition,
            method="POST",
            body={
                "expected_lifecycle_revision": revision,
                "reason": "Concurrent transition",
            },
        ),
    )
    assert lifecycle[0] == 200
    assert patch[0] in ((200, 404) if role == "receptionist" else (200,))
    final = private_request(clinic, clinic.owner, path)[1]["data"]
    assert final["archived"] == (transition == "archive")
    assert final["lifecycle_revision"] == revision + 1
    assert final["demographic_revision"] == (patch[0] == 200)
    assert effects(clinic) == (
        before[0],
        before[1] + 1 + (patch[0] == 200),
        *before[2:],
    )
    if role == "receptionist":
        before_denial = effects(clinic)
        assert (
            private_request(
                clinic,
                clinic.receptionist,
                path,
                method="PATCH",
                body={
                    "expected_demographic_revision": final["demographic_revision"],
                    "food_allergies": "Denied archived edit",
                },
            )[0]
            == 404
        )
        assert effects(clinic) == before_denial


def test_archive_waits_for_internal_new_id_guard_and_then_blocks_new_ids(
    patient_backend,
):
    clinic = patient_backend
    patient = create_patient(clinic)
    path = f"/v1/patients/{patient['id']}/archive"
    barrier = Barrier(2)

    def archive():
        barrier.wait(timeout=5)
        return private_request(
            clinic,
            clinic.owner,
            path,
            method="POST",
            body={"expected_lifecycle_revision": 0, "reason": "Guard serialization"},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(clinic.runtime, clinic.tenant) as guard:
            assert not lock_patient_for_new_appointment(
                guard, clinic.tenant, UUID(patient["id"])
            ).archived
            pid = guard.scalar(text("SELECT pg_backend_pid()"))
            pending = pool.submit(archive)
            barrier.wait(timeout=5)
            with clinic.migration.connect() as observer:
                _wait_for_blocker(observer, pid)
        assert pending.result(timeout=10)[0] == 200
    with transaction(clinic.runtime, clinic.tenant) as guard:
        with pytest.raises(ContractError) as denied:
            lock_patient_for_new_appointment(guard, clinic.tenant, UUID(patient["id"]))
        assert denied.value.status_code == 409


@pytest.mark.parametrize("revocation", ["tenant", "staff"])
@pytest.mark.parametrize("operation", ["create", "patch"])
def test_committed_revocation_prevents_waiting_patient_write_and_any_success_effect(
    patient_backend, revocation, operation
):
    clinic = patient_backend
    patient = create_patient(clinic)
    before = effects(clinic)
    barrier = Barrier(2)

    def mutation():
        barrier.wait(timeout=5)
        if operation == "create":
            return private_request(
                clinic,
                clinic.receptionist,
                method="POST",
                body=PATIENT,
                key=str(uuid4()),
            )
        return private_request(
            clinic,
            clinic.receptionist,
            f"/v1/patients/{patient['id']}",
            method="PATCH",
            body={
                "expected_demographic_revision": 0,
                "drug_allergies": "Must not commit",
            },
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with clinic.migration.begin() as revoker:
            revoker.execute(
                text("SELECT id FROM public.organizations WHERE id=:tenant FOR UPDATE"),
                {"tenant": clinic.tenant},
            )
            pid = revoker.scalar(text("SELECT pg_backend_pid()"))
            pending = pool.submit(mutation)
            barrier.wait(timeout=5)
            with clinic.migration.connect() as observer:
                _wait_for_blocker(observer, pid)
            if revocation == "tenant":
                revoker.execute(
                    text(
                        "UPDATE public.organizations SET state='SUSPENDED' WHERE id=:tenant"
                    ),
                    {"tenant": clinic.tenant},
                )
            else:
                revoker.execute(
                    text(
                        "UPDATE public.staff_accounts SET enabled=false, security_revision=security_revision+1 WHERE id=:id"
                    ),
                    {"id": clinic.receptionist.staff_id},
                )
        status, denied = pending.result(timeout=10)
    assert status in (401, 403) and "data" not in denied
    assert effects(clinic) == before


def test_failed_audit_rolls_back_patient_and_receipt_but_keeps_accepted_nonce(
    patient_backend,
):
    clinic = patient_backend
    before = effects(clinic)
    trigger = f"synthetic_b05_audit_{uuid4().hex}"
    with clinic.migration.begin() as connection:
        connection.execute(
            text(
                f"""CREATE FUNCTION public.{trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.organization_id = '{clinic.tenant}'::uuid THEN RAISE EXCEPTION 'synthetic audit fault' USING ERRCODE='23514'; END IF; RETURN NEW; END $$"""
            )
        )
        connection.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.sensitive_audit FOR EACH ROW EXECUTE FUNCTION public.{trigger}()"
            )
        )
        nonce_before = connection.scalar(
            text("SELECT count(*) FROM public.request_nonces")
        )
    try:
        status, failed = private_request(
            clinic, clinic.owner, method="POST", body=PATIENT, key=str(uuid4())
        )
        assert status == 503 and "data" not in failed
        assert effects(clinic) == before
        with clinic.migration.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM public.request_nonces"))
                == nonce_before + 1
            )
    finally:
        with clinic.migration.begin() as connection:
            connection.execute(
                text(f"DROP TRIGGER {trigger} ON public.sensitive_audit")
            )
            connection.execute(text(f"DROP FUNCTION public.{trigger}()"))
