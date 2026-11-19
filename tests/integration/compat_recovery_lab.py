"""Shared disposable 0040-origin setup for real PostgreSQL recovery tests."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg
from sqlalchemy import URL, create_engine

from ops.compat.control import (
    claim_owner_epoch,
    release_exclusive_drain_permit,
    release_owner_lock,
)
from ops.compat.drain import drain_and_settle, observe_drain_complete
from ops.compat.launch import (
    ObservedRuntimeUnit,
    mark_source_sealed_in_control,
    sha256_file,
)
from ops.compat.manifest import SOURCE_HEAD, GenerationManifest
from ops.compat.migrate import (
    advance_one_successor,
    enter_migrating_phase,
    observe_committed_successor,
)
from ops.compat.preflight import PUBLISHED_SUCCESSORS, run_source_preflight
from ops.compat.prerequisites import apply_missing_prerequisites
from ops.compat.verify import RetainedBaseline
from tests.integration.compat_ci_helpers import (
    build_manifest,
    connect_role,
    disposable_database,
    ensure_roles_provisioned,
    get_cluster_id,
    grant_db_access,
    read_secret,
    read_source_secret,
    require_compat_ci,
    upgrade_to_revision,
)
from tests.integration.compat_fixture import (
    Populated0040Inventory,
    seed_populated_0040_fixture,
)
from tests.integration.compat_matrix_fixture import (
    initial_activate_matched_target,
    provision_target_control,
)


@dataclass(frozen=True)
class RecoveryLab:
    dbname: str
    target_id: str
    cluster_id: str
    source_manifest: GenerationManifest
    target_manifest: GenerationManifest
    inventory: Populated0040Inventory

    def retained_baseline(self) -> RetainedBaseline:
        return RetainedBaseline(
            target_id=self.target_id,
            cluster_id=self.cluster_id,
            db_name=self.dbname,
            tenant_ids=(self.inventory.org_a_id, self.inventory.org_b_id),
            expected_rows=self.inventory.expected_rows,
            nonce_reference=self.inventory.nonce_reference,
        )


def inspected_prelaunch_pair(
    lab: RecoveryLab,
) -> tuple[GenerationManifest, GenerationManifest, tuple[ObservedRuntimeUnit, ...]]:
    """Bind candidate prelaunch checks to image IDs measured by Docker CI."""
    record = json.loads(Path("/tmp/compat-observed.json").read_text(encoding="utf-8"))
    adapter_files = {
        "source-api": "ops/compat/source-api.py",
        "source-web": "ops/compat/source-web.cjs",
        "source-worker": "ops/compat/source-jobs.py",
        "source-sender": "ops/compat/source-jobs.py",
        "candidate-api": "apps/api/carepulse/compat/admission.py",
        "candidate-web": "apps/web/compat/admission.ts",
        "candidate-worker": "apps/api/carepulse/compat/admission.py",
        "candidate-sender": "apps/api/carepulse/compat/admission.py",
    }
    required_secrets = {
        "source-api": {"source_delivery_public_key"},
        "source-sender": {"source_delivery_private_key"},
        "candidate-api": {"api_db_password", "gate_db_password"},
        "candidate-web": {"web_db_password", "auth_db_password"},
        "candidate-worker": {"worker_db_password"},
        "candidate-sender": {"sender_db_password"},
    }
    root = Path(__file__).resolve().parents[2]
    manifests = []
    candidate_units = []
    for base in (lab.source_manifest, lab.target_manifest):
        participants = {}
        for service, previous in base.participants.items():
            name = f"{base.generation}-{service}"
            observed = record["services"][name]
            if name in required_secrets and not required_secrets[name].issubset(
                observed["secrets"]
            ):
                raise AssertionError("Mandatory candidate credential mount is missing")
            source_file = root / adapter_files[name]
            if not source_file.is_file() or not observed["entry"]:
                raise AssertionError("Measured compatibility adapter is unavailable")
            peer = (
                (observed["api_peer"],)
                if service == "web" and observed["api_peer"]
                else (
                    (f"{observed['smtp_peer']}:1025",)
                    if service == "sender" and observed["smtp_peer"]
                    else ()
                )
            )
            participants[service] = replace(
                previous,
                image_digest=observed["image_id"],
                entry_command=tuple(observed["entry"]),
                adapter_digest=sha256_file(source_file),
                trusted_peers=peer,
            )
            if base.generation == "candidate":
                candidate_units.append(
                    ObservedRuntimeUnit(
                        service=service,
                        generation="candidate",
                        image_digest=observed["image_id"],
                        entry_command=tuple(observed["entry"]),
                        adapter_file=source_file,
                        adapter_mount_readonly=bool(observed["adapter_readonly"]),
                        import_reachable=source_file.is_file(),
                        configured_peers=peer,
                        mounted_credentials=previous.allowed_credentials,
                        target_id=lab.target_id,
                        smtp_egress_allowed=service == "sender",
                    )
                )
        manifests.append(
            replace(
                base,
                source_sha=(
                    record["candidate_sha"]
                    if base.generation == "candidate"
                    else base.source_sha
                ),
                participants=participants,
            )
        )
    return manifests[0], manifests[1], tuple(candidate_units)


def original_sender_engine(dbname: str):
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_sender",
            password=read_source_secret("carepulse_sender"),
            host="db",
            port=5432,
            database=dbname,
        ),
        hide_parameters=True,
    )


@dataclass
class CommittedCutover:
    owner: psycopg.Connection
    admin: psycopg.Connection
    observer: psycopg.Connection
    owner_epoch: int


def seed_synthetic_0041_identity(admin: psycopg.Connection) -> None:
    """Initialize only disposable synthetic F-01 policy and key at their first head."""
    values = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "foundation-policy.json"
        ).read_text(encoding="utf-8")
    )
    if values.get("profile") != "synthetic":
        raise ValueError("F-01 test setup requires the approved synthetic policy")
    key_id = os.environ.get("CP_DB_IDENTITY_KEY_ID", "synthetic-db-ci-v1")
    key = bytes.fromhex(
        Path("/run/secrets/db_identity_key").read_text(encoding="utf-8").strip()
    )
    if len(key) != 32:
        raise ValueError("Invalid synthetic identity test key")
    with admin.transaction():
        admin.execute("SET LOCAL ROLE carepulse_identity_owner")
        admin.execute(
            """
            INSERT INTO f01_private.policy (
                signed_request_freshness_seconds,
                signed_request_max_skew_seconds, session_idle_minutes
            ) VALUES (%s, %s, %s)
            """,
            (
                values["signed_request_freshness_seconds"],
                values["signed_request_max_skew_seconds"],
                values["session_idle_minutes"],
            ),
        )
        admin.execute(
            "INSERT INTO f01_private.keys (key_id, secret, status) VALUES (%s, %s, 'ACTIVE')",
            (key_id, key),
        )


@contextmanager
def committed_0046_cutover(lab: RecoveryLab) -> Iterator[CommittedCutover]:
    """Apply exactly the published six successors to a populated 0040 copy."""
    owner = connect_role("carepulse_migration", dbname=lab.dbname)
    admin = connect_role("carepulse_bootstrap", dbname=lab.dbname)
    observer = connect_role("carepulse_bootstrap", dbname=lab.dbname, autocommit=True)
    try:
        preflight = run_source_preflight(
            observer,
            source_manifest=lab.source_manifest,
            target_manifest=lab.target_manifest,
            us1_checkpoint_executed=True,
        )
        epoch = claim_owner_epoch(
            owner,
            target_id=lab.target_id,
            expected_cluster_id=lab.cluster_id,
            expected_db_name=lab.dbname,
        ).owner_epoch
        drain_and_settle(owner, target_id=lab.target_id, owner_epoch=epoch)
        mark_source_sealed_in_control(owner, target_id=lab.target_id, owner_epoch=epoch)
        proof = observe_drain_complete(
            observer,
            target_id=lab.target_id,
            expected_epoch=epoch,
            consumers_stopped=True,
            require_source_sealed=True,
        )
        apply_missing_prerequisites(
            admin,
            observer,
            owner_conn=owner,
            preflight=preflight,
            drain_proof=proof,
            owner_epoch=epoch,
            role_passwords={
                name: read_secret(name)
                for name in (
                    "carepulse_api",
                    "carepulse_web",
                    "carepulse_worker",
                    "carepulse_gate",
                )
            },
        )
        enter_migrating_phase(owner, target_id=lab.target_id, owner_epoch=epoch)
        predecessor = SOURCE_HEAD
        for successor in PUBLISHED_SUCCESSORS:
            advance_one_successor(
                owner,
                target_id=lab.target_id,
                owner_epoch=epoch,
                expected_predecessor=predecessor,
                target_successor=successor,
            )
            observe_committed_successor(
                observer,
                target_id=lab.target_id,
                expected_epoch=epoch,
                expected_head=successor,
            )
            if successor == PUBLISHED_SUCCESSORS[0]:
                seed_synthetic_0041_identity(admin)
            predecessor = successor
        yield CommittedCutover(owner, admin, observer, epoch)
    finally:
        observer.close()
        admin.close()
        if not owner.closed:
            release_exclusive_drain_permit(owner, lab.target_id)
            release_owner_lock(owner, lab.target_id)
            owner.close()


@contextmanager
def populated_0040_lab(prefix: str) -> Iterator[RecoveryLab]:
    require_compat_ci()
    with disposable_database(prefix) as dbname:
        admin = connect_role("carepulse_bootstrap", dbname=dbname, autocommit=True)
        try:
            ensure_roles_provisioned(
                admin, source_only=True, separate_source_secrets=True
            )
            grant_db_access(admin, dbname, source_only=True)
            cluster_id = get_cluster_id(admin)
        finally:
            admin.close()
        upgrade_to_revision(dbname, SOURCE_HEAD)
        target_id = f"t-{dbname}"
        source = build_manifest(
            generation="source",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        candidate = build_manifest(
            generation="candidate",
            target_id=target_id,
            cluster_id=cluster_id,
            db_name=dbname,
        )
        mig = connect_role("carepulse_migration", dbname=dbname)
        try:
            provision_target_control(
                mig,
                target_id=target_id,
                cluster_id=cluster_id,
                db_name=dbname,
                source_manifest_id=source.manifest_id,
                target_manifest_id=candidate.manifest_id,
            )
            initial_activate_matched_target(
                mig,
                manifest=source,
                expected_target_id=target_id,
            )
        finally:
            mig.close()
        seed = connect_role("carepulse_bootstrap", dbname=dbname)
        try:
            inventory = seed_populated_0040_fixture(seed)
        finally:
            seed.close()
        yield RecoveryLab(dbname, target_id, cluster_id, source, candidate, inventory)
