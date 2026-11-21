"""Independent, non-effectful security and retained-evidence checks for cutover (T045/46).

This module never changes the fenced target. Product effect tests require an
independently prepared, equivalently bound disposable database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import psycopg
from psycopg import sql

from ops.compat.control import read_control_record
from ops.compat.manifest import GenerationManifest, verify_manifest_pair
from ops.compat.probes import (
    KNOWN_REVISIONS,
    probe_alembic_head,
    probe_rls_and_triggers,
    probe_role_attributes,
    probe_target_identity,
)


class VerificationError(RuntimeError):
    """The target is not independently proven safe; leave its ingress fenced."""


@dataclass(frozen=True)
class SecurityProof:
    target_id: str
    cluster_id: str
    db_name: str
    owner_epoch: int
    phase: str
    head: str
    source_manifest_id: str
    target_manifest_id: str
    checked_roles: tuple[str, ...]


@dataclass(frozen=True)
class RetainedBaseline:
    """Trusted original 0040 capture; values are never included in diagnostics.

    expected_rows uses the original column names and JSON-compatible row values
    (as produced by to_jsonb). private_rows holds separately captured F-01 rows
    for records that already existed at the relevant committed checkpoint.
    """

    target_id: str
    cluster_id: str
    db_name: str
    tenant_ids: tuple[str, str]
    expected_rows: Mapping[str, tuple[Mapping[str, object], ...]]
    nonce_reference: tuple[str, str]
    private_rows: Mapping[str, tuple[Mapping[str, object], ...]] | None = None


@dataclass(frozen=True)
class RetentionProof:
    target_id: str
    owner_epoch: int
    head: str
    checked_tables: tuple[str, ...]
    checked_rows: int
    unresolved_work: int


_SOURCE_ROLES = (
    "carepulse_migration",
    "carepulse_runtime",
    "carepulse_auth",
    "carepulse_sender",
    "carepulse_operator",
    "carepulse_policy_seed",
)
_TARGET_ROLES = (
    *_SOURCE_ROLES,
    "carepulse_api",
    "carepulse_web",
    "carepulse_worker",
    "carepulse_gate",
    "carepulse_identity_owner",
)
_PRODUCT_ROLES = (
    "carepulse_runtime",
    "carepulse_api",
    "carepulse_web",
    "carepulse_worker",
    "carepulse_gate",
    "carepulse_auth",
    "carepulse_sender",
    "carepulse_operator",
)
_ORIGINAL_TABLES = frozenset(
    {
        "organizations",
        "operator_identities",
        "user",
        '"user"',
        "staff_accounts",
        "invitations",
        "patients",
        "clinic_policy_versions",
        "clinic_policy_heads",
        "appointment_day_counters",
        "appointments",
        "appointment_number_assignments",
        "appointment_price_snapshots",
        "appointment_history",
        "queue_days",
        "queue_entries",
        "doctor_controls",
        "visits",
        "clinical_revisions",
        "clinical_audit",
        "completion_attestations",
        "followup_source_state",
        "followup_claims",
        "followup_extensions",
        "followup_source_decision_events",
        "followup_authorization_events",
        "idempotency_receipts",
        "sensitive_audit",
        "outbox_events",
        "delivery_jobs",
        "protected_delivery_material",
        "request_nonces",
        "verification",
        "auth_login_attempts",
    }
)
_PRIVATE_TABLES = frozenset({"nonces", "requests", "bindings", "activations"})
_MANDATORY_RETAINED = frozenset(
    {
        "organizations",
        "patients",
        "appointments",
        "appointment_price_snapshots",
        "appointment_number_assignments",
        "appointment_history",
        "visits",
        "idempotency_receipts",
        "sensitive_audit",
        "outbox_events",
        "delivery_jobs",
        "request_nonces",
    }
)


def _require_privilege(
    conn: psycopg.Connection,
    function: str,
    role: str,
    obj: str,
    privilege: str,
    allowed: bool,
) -> None:
    """Use PostgreSQL effective rights rather than merely parsing GRANT strings."""
    if function not in {
        "has_database_privilege",
        "has_schema_privilege",
        "has_table_privilege",
        "has_sequence_privilege",
        "has_function_privilege",
    }:
        raise VerificationError("Unsupported privilege probe")
    granted = conn.execute(
        sql.SQL("SELECT {}(%s, %s, %s) AS permitted").format(sql.Identifier(function)),
        (role, obj, privilege),
    ).fetchone()["permitted"]
    if granted is None or bool(granted) != allowed:
        raise VerificationError("Effective privilege boundary mismatch")


def _require_column(
    conn: psycopg.Connection,
    role: str,
    table: str,
    column: str,
    privilege: str,
    allowed: bool,
) -> None:
    granted = conn.execute(
        "SELECT has_column_privilege(%s, %s, %s, %s) AS permitted",
        (role, table, column, privilege),
    ).fetchone()["permitted"]
    if granted is None or bool(granted) != allowed:
        raise VerificationError("Column privilege boundary mismatch")


def verify_fenced_security(
    observer: psycopg.Connection,
    *,
    source_manifest: GenerationManifest,
    target_manifest: GenerationManifest,
    expected_epoch: int,
    expected_head: str,
) -> SecurityProof:
    """Verify committed target, effective security and head without product effects.

    The observer must be a direct privileged read-only *invocation* (no SET ROLE).
    Its connection must not be borrowed from an application pool. A proof is a
    snapshot, not a license to reopen: T047/48 must recheck it at their own CAS.
    """
    verify_manifest_pair(source_manifest, target_manifest)
    binding = source_manifest.target_binding
    if expected_head not in KNOWN_REVISIONS or expected_epoch < 1:
        raise VerificationError("Unknown expected cutover state")
    identity = probe_target_identity(
        observer,
        expected_cluster_id=binding.cluster_id,
        expected_db_name=binding.db_name,
    )
    if identity.session_user != identity.current_user or identity.session_user not in (
        "carepulse_bootstrap",
        "carepulse_migration",
    ):
        raise VerificationError(
            "Verification requires an independent maintenance observer"
        )
    control = read_control_record(observer)
    head = probe_alembic_head(observer, expected_head=expected_head)
    if (
        control.target_id != binding.target_id
        or control.cluster_id != binding.cluster_id
        or control.db_name != binding.db_name
        or control.owner_epoch != expected_epoch
        or control.source_manifest_id != source_manifest.manifest_id
        or control.target_manifest_id != target_manifest.manifest_id
        or control.phase not in ("CLOSED", "DRAINING", "MIGRATING", "VERIFYING")
        or control.active_generation != "none"
        or (head != KNOWN_REVISIONS[0] and not control.source_sealed)
    ):
        raise VerificationError("Fenced target, manifest or epoch mismatch")

    roles = _SOURCE_ROLES if head == KNOWN_REVISIONS[0] else _TARGET_ROLES
    found = probe_role_attributes(observer, roles)
    if set(found) != set(roles):
        raise VerificationError("Required service roles absent")
    for role, snap in found.items():
        if (
            snap.rolsuper
            or snap.rolbypassrls
            or snap.rolcreaterole
            or snap.rolcreatedb
            or snap.rolinherit
        ):
            raise VerificationError("Unexpected service role privilege or inheritance")
        if role in ("carepulse_policy_seed", "carepulse_identity_owner"):
            if snap.rolcanlogin:
                raise VerificationError("Private owner may not log in")
        elif (
            role != "carepulse_runtime" or head == KNOWN_REVISIONS[0]
        ) and not snap.rolcanlogin:
            raise VerificationError("Required service login disabled")
        if role != "carepulse_migration" and (snap.member_of or snap.set_memberships):
            raise VerificationError("Product/owner role has role-membership escalation")
    if head != KNOWN_REVISIONS[0] and (
        "carepulse_identity_owner" not in found["carepulse_migration"].set_memberships
        or "carepulse_policy_seed" not in found["carepulse_migration"].set_memberships
    ):
        raise VerificationError("Maintenance identity owner SET authority missing")

    # Validate indirect SET/INHERIT routes, even if the direct membership list looks normal.
    for role in _PRODUCT_ROLES:
        if role not in found:
            continue
        for elevated in (
            "carepulse_migration",
            "carepulse_policy_seed",
            "carepulse_identity_owner",
        ):
            if elevated not in found and elevated != "carepulse_migration":
                continue
            row = observer.execute(
                "SELECT pg_has_role(%s, %s, 'SET') AS can_set, pg_has_role(%s, %s, 'USAGE') AS can_inherit",
                (role, elevated, role, elevated),
            ).fetchone()
            if row["can_set"] or row["can_inherit"]:
                raise VerificationError("Product role can assume maintenance authority")

    for schema_name in ("public", "compat_control"):
        row = observer.execute(
            "SELECT nspowner::regrole::text AS owner FROM pg_namespace WHERE nspname = %s",
            (schema_name,),
        ).fetchone()
        if row is None or row["owner"] != "carepulse_migration":
            raise VerificationError("Security schema ownership mismatch")
    if head != KNOWN_REVISIONS[0]:
        for schema_name, owner in (
            ("f01_private", "carepulse_identity_owner"),
            ("f01_crypto", "carepulse_migration"),
        ):
            row = observer.execute(
                "SELECT nspowner::regrole::text AS owner FROM pg_namespace WHERE nspname = %s",
                (schema_name,),
            ).fetchone()
            if row is None or row["owner"] != owner:
                raise VerificationError("F-01 schema ownership mismatch")
        extension = observer.execute(
            "SELECT n.nspname AS schema, e.extowner::regrole::text AS owner FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pgcrypto'"
        ).fetchone()
        if (
            extension is None
            or extension["schema"] != "f01_crypto"
            # roles.sql and T030 create pgcrypto as the bootstrap administrator.
            or extension["owner"] not in ("carepulse_bootstrap", "carepulse_migration")
        ):
            raise VerificationError("Cryptography extension is not restricted")

    # PostgreSQL PUBLIC is represented by grantee OID zero (not a login role).
    bad_public = observer.execute(
        """
        SELECT count(*) AS n FROM (
          SELECT 1 FROM pg_namespace n, LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) acl
          WHERE n.nspname IN ('public','compat_control','f01_private','f01_crypto') AND acl.grantee=0
          UNION ALL
          SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
            LATERAL aclexplode(COALESCE(c.relacl,acldefault((CASE WHEN c.relkind='S' THEN 'S' ELSE 'r' END)::"char",c.relowner))) acl
          WHERE n.nspname IN ('public','compat_control','f01_private') AND c.relkind IN ('r','p','v','m','S') AND acl.grantee=0
          UNION ALL
          SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace,
            LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) acl
          WHERE n.nspname IN ('compat_control','f01_private','f01_crypto') AND acl.grantee=0
          UNION ALL
          SELECT 1 FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace,
            LATERAL aclexplode(d.defaclacl) acl
          WHERE n.nspname IN ('compat_control','f01_private','f01_crypto') AND acl.grantee=0
        ) unexpected
        """
    ).fetchone()["n"]
    if bad_public:
        raise VerificationError(
            "PUBLIC retains sensitive schema, relation or routine authority"
        )

    if head != KNOWN_REVISIONS[0]:
        private_objects = observer.execute(
            "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='f01_private' AND c.relkind IN ('r','p','S')"
        ).fetchall()
        private_names = {
            r["relname"]
            for r in private_objects
            if r["relname"]
            in (
                "keys",
                "policy",
                "clock_state",
                "nonces",
                "requests",
                "bindings",
                "activations",
            )
        }
        if private_names != {
            "keys",
            "policy",
            "clock_state",
            "nonces",
            "requests",
            "bindings",
            "activations",
        } or any(r["owner"] != "carepulse_identity_owner" for r in private_objects):
            raise VerificationError(
                "Private F-01 relation ownership or schema mismatch"
            )
        for role in _PRODUCT_ROLES:
            if role not in found:
                continue
            _require_privilege(
                observer, "has_schema_privilege", role, "f01_private", "USAGE", False
            )
            _require_privilege(
                observer, "has_schema_privilege", role, "f01_crypto", "USAGE", False
            )
        _require_privilege(
            observer,
            "has_schema_privilege",
            "carepulse_identity_owner",
            "public",
            "CREATE",
            False,
        )
        for function, allowed_roles in (
            ("public.f01_register_proof(bytea,text,bytea,bytea)", ("carepulse_gate",)),
            ("public.f01_bind(bigint,text,integer,xid8)", ("carepulse_gate",)),
            ("public.f01_activate()", ("carepulse_api",)),
            ("public.f01_cleanup()", ("carepulse_gate",)),
            ("public.f01_context()", ("carepulse_api", "carepulse_migration")),
            ("public.f01_org()", ("carepulse_api", "carepulse_migration")),
            ("public.f01_staff()", ("carepulse_api", "carepulse_migration")),
        ):
            row = observer.execute(
                "SELECT p.proowner::regrole::text AS owner, p.prosecdef, p.proconfig, EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute FROM pg_proc p WHERE p.oid=to_regprocedure(%s)",
                (function,),
            ).fetchone()
            if (
                row is None
                or row["owner"] != "carepulse_identity_owner"
                or not row["prosecdef"]
                or row["public_execute"]
                or not any(
                    str(cfg).startswith("search_path=pg_catalog")
                    for cfg in (row["proconfig"] or [])
                )
            ):
                raise VerificationError(
                    "F-01 privileged routine owner or PUBLIC boundary mismatch"
                )
            for role in (*_PRODUCT_ROLES, "carepulse_migration"):
                _require_privilege(
                    observer,
                    "has_function_privilege",
                    role,
                    function,
                    "EXECUTE",
                    role in allowed_roles,
                )
        # No arbitrary product can execute private routines; definer-only access is through reviewed public entry points.
        forbidden_private = observer.execute(
            """
            SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='f01_private' AND (
              p.proowner <> 'carepulse_identity_owner'::regrole OR
              EXISTS (SELECT 1 FROM unnest(%s::text[]) r WHERE has_function_privilege(r,p.oid,'EXECUTE'))
            )
            """,
            (list(_PRODUCT_ROLES),),
        ).fetchone()["n"]
        if forbidden_private:
            raise VerificationError("Private F-01 routine access or ownership drift")

    if head in KNOWN_REVISIONS[2:]:
        _require_privilege(
            observer,
            "has_schema_privilege",
            "carepulse_runtime",
            "public",
            "USAGE",
            False,
        )
        revoked = observer.execute(
            """
            SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m')
              AND (has_table_privilege('carepulse_runtime',c.oid,'SELECT')
                   OR has_table_privilege('carepulse_runtime',c.oid,'INSERT')
                   OR has_table_privilege('carepulse_runtime',c.oid,'UPDATE')
                   OR has_table_privilege('carepulse_runtime',c.oid,'DELETE'))
            """
        ).fetchone()["n"]
        if revoked:
            raise VerificationError("Legacy runtime retains relation authority")
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_api",
            "public.patients",
            "SELECT",
            True,
        )
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_api",
            "public.appointments",
            "INSERT",
            True,
        )
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_api",
            "public.delivery_jobs",
            "UPDATE",
            False,
        )
        _require_column(
            observer,
            "carepulse_sender",
            "public.verification",
            "expiresAt",
            "SELECT",
            True,
        )
        _require_column(
            observer,
            "carepulse_api",
            "public.protected_delivery_material",
            "ciphertext_base64url",
            "SELECT",
            False,
        )
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_worker",
            "public.patients",
            "SELECT",
            False,
        )
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_web",
            "public.patients",
            "SELECT",
            False,
        )
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_gate",
            "public.patients",
            "SELECT",
            False,
        )
        _require_privilege(
            observer,
            "has_table_privilege",
            "carepulse_web",
            "public.auth_login_attempts",
            "INSERT",
            True,
        )
        _require_column(
            observer,
            "carepulse_api",
            "public.delivery_jobs",
            "claim_owner",
            "SELECT",
            head in KNOWN_REVISIONS[2:4],
        )
        probe_rls_and_triggers(
            observer,
            required_force_rls_tables=(
                "public.organizations",
                "public.patients",
                "public.user",
                "public.verification",
            ),
            required_triggers={"public.patients": ("patients_f01_operation_guard",)},
        )
        patient_guard = observer.execute(
            """
            SELECT t.tgenabled, p.proowner::regrole::text AS owner, p.prosecdef
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgrelid='public.patients'::regclass
              AND t.tgname='patients_f01_operation_guard'
              AND t.tgfoid=to_regprocedure('public.f01_guard_patient_operation()')
              AND NOT t.tgisinternal
            """
        ).fetchone()
        if (
            patient_guard is None
            or patient_guard["tgenabled"] != "O"
            or patient_guard["owner"] != "carepulse_migration"
            or patient_guard["prosecdef"]
        ):
            raise VerificationError(
                "Patient operation trigger is disabled or redirected"
            )
        policy_rows = observer.execute(
            """
            SELECT tablename,policyname,roles,qual,with_check
            FROM pg_policies WHERE schemaname='public'
              AND ((tablename IN ('patients','appointments','visits','staff_accounts','delivery_jobs')
                    AND policyname='f01_api_read')
                   OR (tablename='verification' AND policyname='f01_verification_sender_read'))
            """
        ).fetchall()
        required = {
            ("patients", "f01_api_read"),
            ("appointments", "f01_api_read"),
            ("visits", "f01_api_read"),
            ("staff_accounts", "f01_api_read"),
            ("delivery_jobs", "f01_api_read"),
            ("verification", "f01_verification_sender_read"),
        }
        if {(r["tablename"], r["policyname"]) for r in policy_rows} != required or any(
            not r["qual"]
            or set(r["roles"])
            != {
                "carepulse_sender"
                if r["tablename"] == "verification"
                else "carepulse_api"
            }
            or "f01_context" not in r["qual"]
            and r["tablename"] != "verification"
            for r in policy_rows
        ):
            raise VerificationError("F-01 service RLS policy coverage or scope drift")
        # Explicit sequence and column grants must not restore the retired shared role.
        column_or_sequence = observer.execute(
            """
            SELECT (SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped
                      AND a.attacl IS NOT NULL AND EXISTS (
                        SELECT 1 FROM aclexplode(a.attacl) acl WHERE acl.grantee='carepulse_runtime'::regrole))
                 + (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relkind='S'
                      AND (has_sequence_privilege('carepulse_runtime',c.oid,'USAGE')
                        OR has_sequence_privilege('carepulse_runtime',c.oid,'SELECT')))
                 AS n
            """
        ).fetchone()["n"]
        if column_or_sequence:
            raise VerificationError("Legacy column/sequence grants were restored")

    if head in KNOWN_REVISIONS[5:]:
        trigger_rows = observer.execute(
            """
            SELECT t.tgname, t.tgenabled, p.proowner::regrole::text AS owner,
                   p.prosecdef, p.proconfig, p.proname AS function_name,
                   tn.nspname || '.' || c.relname AS table_name,
                   pg_get_triggerdef(t.oid) AS definition
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace tn ON tn.oid=c.relnamespace
            WHERE n.nspname='f01_private' AND NOT t.tgisinternal
            """
        ).fetchall()
        expected_triggers = {
            "f01_capture_revocation": ("public.staff_accounts", "capture_revocation"),
            "f01_capture_revocation_audit": (
                "public.sensitive_audit",
                "capture_revocation_audit",
            ),
        }
        if head == KNOWN_REVISIONS[-1]:
            expected_triggers.update(
                {
                    "z_f01_capture_audit_patient": (
                        "public.patients",
                        "capture_audit_patient",
                    ),
                    "z_f01_capture_audit_appointment": (
                        "public.appointments",
                        "capture_audit_appointment",
                    ),
                    "a_f01_guard_audit_appointment_history": (
                        "public.appointment_history",
                        "guard_audit_appointment_history",
                    ),
                    "a_f01_guard_staff_account_insert": (
                        "public.staff_accounts",
                        "guard_staff_account_insert",
                    ),
                    "f01_capture_staff_audit": (
                        "public.staff_accounts",
                        "capture_staff_audit",
                    ),
                    "f01_capture_invitation_audit": (
                        "public.invitations",
                        "capture_invitation_audit",
                    ),
                    "a_f01_guard_staff_audit": (
                        "public.sensitive_audit",
                        "guard_staff_audit",
                    ),
                }
            )
        if (
            len(trigger_rows) != len(expected_triggers)
            or {
                r["tgname"]: (r["table_name"], r["function_name"]) for r in trigger_rows
            }
            != expected_triggers
        ) or any(
            r["tgenabled"] != "O"
            or r["owner"] != "carepulse_identity_owner"
            or not r["prosecdef"]
            or not r["definition"]
            or not any(
                str(cfg).startswith("search_path=pg_catalog")
                for cfg in (r["proconfig"] or [])
            )
            for r in trigger_rows
        ):
            raise VerificationError(
                "F-01 provenance triggers or definer integrity drift"
            )
    if head == KNOWN_REVISIONS[-1]:
        for function, expected_owner in (
            (
                "public.f01_counted_appointment_intervals(uuid,date)",
                "carepulse_migration",
            ),
            (
                "public.f01_schedule_review_appointments(uuid,date)",
                "carepulse_migration",
            ),
            ("public.f01_audit_targets()", "carepulse_identity_owner"),
        ):
            for role in (*_PRODUCT_ROLES, "carepulse_migration"):
                _require_privilege(
                    observer,
                    "has_function_privilege",
                    role,
                    function,
                    "EXECUTE",
                    role in ("carepulse_api", "carepulse_migration"),
                )
            row = observer.execute(
                "SELECT p.proowner::regrole::text AS owner, p.prosecdef, p.proconfig, EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute FROM pg_proc p WHERE p.oid=to_regprocedure(%s)",
                (function,),
            ).fetchone()
            if (
                row is None
                or row["owner"] != expected_owner
                or not row["prosecdef"]
                or row["public_execute"]
                or not any(
                    str(cfg).startswith("search_path=pg_catalog")
                    for cfg in (row["proconfig"] or [])
                )
            ):
                raise VerificationError("F-01 projection function owner or ACL drift")
        # Validate initialized policy and key metadata without retrieving secret bytes.
        policy = observer.execute(
            "SELECT count(*) AS n FROM f01_private.policy WHERE singleton AND signed_request_freshness_seconds>0 AND signed_request_max_skew_seconds>=0 AND session_idle_minutes>0"
        ).fetchone()["n"]
        keys = observer.execute(
            """
            SELECT count(*) FILTER (WHERE status='ACTIVE') AS active,
                   count(*) FILTER (WHERE status NOT IN ('ACTIVE','RETIRING','REVOKED')
                     OR (status='ACTIVE' AND issuance_cutoff_ms IS NOT NULL)
                     OR (status='RETIRING' AND issuance_cutoff_ms IS NULL)
                     OR octet_length(secret)<>32) AS invalid
            FROM f01_private.keys WHERE status<>'REVOKED'
            """
        ).fetchone()
        if policy != 1 or keys["active"] < 1 or keys["invalid"]:
            raise VerificationError("F-01 key or policy readiness not proven")

    if read_control_record(observer) != control or probe_alembic_head(observer) != head:
        raise VerificationError("Security changed during independent observation")
    return SecurityProof(
        target_id=binding.target_id,
        cluster_id=binding.cluster_id,
        db_name=binding.db_name,
        owner_epoch=expected_epoch,
        phase=control.phase,
        head=head,
        source_manifest_id=source_manifest.manifest_id,
        target_manifest_id=target_manifest.manifest_id,
        checked_roles=roles,
    )


def verify_retained_evidence(
    observer: psycopg.Connection,
    *,
    baseline: RetainedBaseline,
    security: SecurityProof,
) -> RetentionProof:
    """Compare trusted 0040 values and extant F-01 provenance without rewriting it."""
    if (
        baseline.target_id != security.target_id
        or baseline.cluster_id != security.cluster_id
        or baseline.db_name != security.db_name
        or len(set(baseline.tenant_ids)) != 2
        or not all(baseline.tenant_ids)
        or not _MANDATORY_RETAINED.issubset(baseline.expected_rows)
        or not set(baseline.expected_rows).issubset(_ORIGINAL_TABLES)
        or (
            baseline.private_rows is not None
            and not set(baseline.private_rows).issubset(_PRIVATE_TABLES)
        )
    ):
        raise VerificationError("Incomplete or mismatched trusted retained baseline")
    state = read_control_record(observer)
    if (
        state.target_id != security.target_id
        or state.owner_epoch != security.owner_epoch
        or state.phase != security.phase
        or state.active_generation != "none"
        or probe_alembic_head(observer) != security.head
    ):
        raise VerificationError("Security proof is stale or refers to another target")

    checked = 0
    for table, originals in baseline.expected_rows.items():
        if table == "organizations":
            predicate, args = (
                sql.SQL("id = ANY(%s::uuid[])"),
                (list(baseline.tenant_ids),),
            )
        elif table == "operator_identities":
            ids = tuple(str(row.get("id", "")) for row in originals)
            predicate, args = sql.SQL("id::text = ANY(%s)"), (list(ids),)
        elif table in ("user", '"user"'):
            ids = tuple(str(row.get("id", "")) for row in originals)
            predicate, args = sql.SQL("id = ANY(%s)"), (list(ids),)
        elif table == "request_nonces":
            predicate, args = (
                sql.SQL("key_id = %s AND nonce = %s"),
                baseline.nonce_reference,
            )
        elif table == "protected_delivery_material":
            predicate, args = (
                sql.SQL(
                    "job_id IN (SELECT id FROM public.delivery_jobs WHERE organization_id = ANY(%s::uuid[]))"
                ),
                (list(baseline.tenant_ids),),
            )
        elif table in ("verification", "auth_login_attempts"):
            ids = tuple(str(row.get("id", "")) for row in originals)
            if not ids:
                continue
            predicate, args = sql.SQL("id::text = ANY(%s)"), (list(ids),)
        else:
            predicate, args = (
                sql.SQL("organization_id = ANY(%s::uuid[])"),
                (list(baseline.tenant_ids),),
            )
        actual = [
            r["row"]
            for r in observer.execute(
                sql.SQL("SELECT to_jsonb(t) AS row FROM public.{} t WHERE ").format(
                    sql.Identifier(table.strip('"'))
                )
                + predicate,
                args,
            )
        ]
        for original in originals:
            matched = next(
                (
                    index
                    for index, row in enumerate(actual)
                    if all(k in row and row[k] == v for k, v in original.items())
                ),
                None,
            )
            if matched is None:
                raise VerificationError(
                    "Original clinical, receipt or delivery evidence changed or vanished"
                )
            actual.pop(matched)
            checked += 1

    if security.head != KNOWN_REVISIONS[0]:
        # A consumed predecessor nonce remains a private tombstone; never manufacture one.
        preserved = observer.execute(
            "SELECT EXISTS (SELECT 1 FROM f01_private.nonces WHERE http_key_id=%s AND nonce=%s) AS ok",
            baseline.nonce_reference,
        ).fetchone()["ok"]
        if not preserved:
            raise VerificationError("Predecessor nonce replay tombstone missing")
        missing_nonces = observer.execute(
            """
            SELECT count(*) AS n FROM public.request_nonces n
            WHERE NOT EXISTS (SELECT 1 FROM f01_private.nonces t
              WHERE t.http_key_id=n.key_id AND t.nonce=n.nonce)
            """
        ).fetchone()["n"]
        if missing_nonces:
            raise VerificationError(
                "Original transport nonce registry lost private tombstones"
            )
        # Unknown F-01 transaction outcomes must remain unresolved, never credited as success.
        uncertain = observer.execute(
            """
            SELECT count(*) AS n FROM f01_private.bindings b
            WHERE pg_xact_status(b.xid) IS DISTINCT FROM 'committed'
              AND pg_xact_status(b.xid) IS DISTINCT FROM 'aborted'
            """
        ).fetchone()["n"]
        if uncertain:
            raise VerificationError(
                "Unresolved F-01 transaction binding; remain fenced"
            )
        if baseline.private_rows is None:
            present = observer.execute(
                "SELECT (SELECT count(*) FROM f01_private.requests) + (SELECT count(*) FROM f01_private.bindings) + (SELECT count(*) FROM f01_private.activations) AS n"
            ).fetchone()["n"]
            if present:
                raise VerificationError(
                    "F-01 provenance exists without trusted preservation baseline"
                )
        else:
            for table, originals in baseline.private_rows.items():
                actual = [
                    r["row"]
                    for r in observer.execute(
                        sql.SQL(
                            "SELECT to_jsonb(t) AS row FROM f01_private.{} t"
                        ).format(sql.Identifier(table))
                    )
                ]
                for original in originals:
                    matched = next(
                        (
                            i
                            for i, row in enumerate(actual)
                            if all(
                                k in row and row[k] == v for k, v in original.items()
                            )
                        ),
                        None,
                    )
                    if matched is None:
                        raise VerificationError(
                            "Original private request, binding or activation provenance changed"
                        )
                    actual.pop(matched)
                    checked += 1
            # Versions 0045/46 add evidence fields; lack of those fields on old
            # requests is not approval to fabricate historical revocation/actor proof.
            if security.head in KNOWN_REVISIONS[5:]:
                invalid = observer.execute(
                    "SELECT count(*) AS n FROM f01_private.activations a WHERE a.revocation_audit_id IS NOT NULL AND (a.revoked_staff_id IS NULL OR NOT EXISTS (SELECT 1 FROM public.sensitive_audit s WHERE s.id=a.revocation_audit_id))"
                ).fetchone()["n"]
                if invalid:
                    raise VerificationError(
                        "Unmatched committed F-01 revocation evidence"
                    )
            if security.head == KNOWN_REVISIONS[-1]:
                invalid = observer.execute(
                    "SELECT count(*) AS n FROM f01_private.activations a WHERE a.audit_appointment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.appointments p WHERE p.id=a.audit_appointment_id)"
                ).fetchone()["n"]
                if invalid:
                    raise VerificationError("Unmatched F-01 appointment actor evidence")

    unresolved = observer.execute(
        "SELECT count(*) AS n FROM compat_control.work_evidence WHERE target_id=%s AND disposition IN ('PENDING','UNKNOWN')",
        (security.target_id,),
    ).fetchone()["n"]
    claimed = observer.execute(
        "SELECT count(*) AS n FROM public.delivery_jobs WHERE state='CLAIMED'"
    ).fetchone()["n"]
    if unresolved or claimed:
        raise VerificationError("Unresolved sender or admitted work must remain fenced")
    if (
        read_control_record(observer) != state
        or probe_alembic_head(observer) != security.head
    ):
        raise VerificationError("Retained evidence verification was invalidated")
    return RetentionProof(
        target_id=security.target_id,
        owner_epoch=security.owner_epoch,
        head=security.head,
        checked_tables=tuple(sorted(baseline.expected_rows)),
        checked_rows=checked,
        unresolved_work=0,
    )
