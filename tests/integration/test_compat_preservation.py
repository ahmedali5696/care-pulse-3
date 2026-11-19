"""T053: preserve original tenant/actor/receipt evidence across real 0040→0046."""

from uuid import uuid4

import pytest

from ops.compat.control import read_control_record
from ops.compat.manifest import CANDIDATE_HEAD
from ops.compat.verify import (
    VerificationError,
    verify_fenced_security,
    verify_retained_evidence,
)
from tests.integration.compat_ci_helpers import make_sa_engine
from tests.integration.compat_fixture import compare_retained_inventory
from tests.integration.compat_recovery_lab import (
    committed_0046_cutover,
    populated_0040_lab,
)


def test_historical_receipts_actor_and_consumed_nonce_survive_each_real_successor() -> (
    None
):
    with (
        populated_0040_lab("compat_preserve") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        source_data = lab.inventory.expected_rows
        assert len(source_data["idempotency_receipts"]) == 2
        assert all(
            row["actor_kind"] == "STAFF"
            and row["request_digest"]
            and row["result_projection"]["net_egp"] == "100.00"
            and row["scope_kind"] == "TENANT"
            for row in source_data["idempotency_receipts"]
        )
        assert len(source_data["visits"]) == 1
        assert len(source_data["clinical_audit"]) == 1
        assert (
            source_data["clinical_audit"][0]["doctor_staff_id"]
            == (source_data["visits"][0]["doctor_staff_id"])
        )
        compare_retained_inventory(cutover.observer, lab.inventory)
        security = verify_fenced_security(
            cutover.observer,
            source_manifest=lab.source_manifest,
            target_manifest=lab.target_manifest,
            expected_epoch=cutover.owner_epoch,
            expected_head=CANDIDATE_HEAD,
        )
        preservation = verify_retained_evidence(
            cutover.observer,
            baseline=lab.retained_baseline(),
            security=security,
        )
        assert preservation.checked_rows >= 20
        assert preservation.unresolved_work == 0
        tombstone = cutover.observer.execute(
            """
            SELECT count(*) AS n FROM f01_private.nonces
            WHERE http_key_id=%s AND nonce=%s
            """,
            lab.inventory.nonce_reference,
        ).fetchone()["n"]
        assert tombstone == 1
        assert (
            cutover.observer.execute(
                "SELECT count(*) AS n FROM f01_private.activations"
            ).fetchone()["n"]
            == 0
        )

        # A real API database login without a signed F-01 operation binding
        # cannot read either clinic's patients, even though table SELECT exists.
        api = make_sa_engine("carepulse_api", dbname=lab.dbname)
        try:
            with api.begin() as connection:
                count = connection.exec_driver_sql(
                    "SELECT count(*) FROM public.patients"
                ).scalar_one()
                assert count == 0
        finally:
            api.dispose()
        compare_retained_inventory(cutover.observer, lab.inventory)
        assert read_control_record(cutover.observer).phase == "MIGRATING"


def test_uncertain_sender_provenance_prevents_releasing_retained_evidence() -> None:
    with (
        populated_0040_lab("compat_preserve_unknown") as lab,
        committed_0046_cutover(lab) as cutover,
    ):
        # Persist an authentic unresolved category, without claiming an SMTP
        # transport acceptance or synthesizing a successful delivery.
        attempt_id = f"synthetic-unknown-{uuid4().hex}"
        cutover.owner.execute(
            """
            INSERT INTO compat_control.work_evidence (
                attempt_id, target_id, owner_epoch, generation, service,
                invocation_class, correlation_id, disposition, created_by_role
            ) VALUES (%s,%s,%s,'source','sender','sender_claim',%s,
                      'UNKNOWN','carepulse_sender')
            """,
            (
                attempt_id,
                lab.target_id,
                cutover.owner_epoch,
                lab.inventory.delivery_job_a_id,
            ),
        )
        cutover.owner.commit()
        security = verify_fenced_security(
            cutover.observer,
            source_manifest=lab.source_manifest,
            target_manifest=lab.target_manifest,
            expected_epoch=cutover.owner_epoch,
            expected_head=CANDIDATE_HEAD,
        )
        with pytest.raises(VerificationError, match="Unresolved"):
            verify_retained_evidence(
                cutover.observer,
                baseline=lab.retained_baseline(),
                security=security,
            )
        assert (
            cutover.observer.execute(
                "SELECT disposition FROM compat_control.work_evidence WHERE attempt_id=%s",
                (attempt_id,),
            ).fetchone()["disposition"]
            == "UNKNOWN"
        )
        compare_retained_inventory(cutover.observer, lab.inventory)
        assert read_control_record(cutover.observer).active_generation == "none"
