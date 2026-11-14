"""Bind existing privileged writers to F-01's activated request."""

from alembic import op
from sqlalchemy import text

revision = "0043_f01_writer_boundary"
down_revision = "0042_f01_effective_grants"
branch_labels = None
depends_on = None

_ORG = "current_setting('app.organization_id', true)"
_STAFF = "current_setting('app.staff_id', true)"
_RUNTIME = "'carepulse_runtime'"
_API = "'carepulse_api'"
_RECOVERY = """ctx.purpose = 'OWNER_RECOVERY' AND ctx.role = 'BOOTSTRAP'
                    AND ctx.method = 'POST' AND ctx.stage = 'PRIMARY'"""
_BOOKING = """ctx.purpose = 'APPOINTMENT_BOOKING' AND ctx.method = 'POST'
                    AND ctx.path = '/v1/appointments' AND ctx.stage = 'PRIMARY'"""
_VISIT = """ctx.purpose = 'VISIT_FOLLOWUP' AND ctx.stage = 'PRIMARY'"""
_OWNER = """ctx.role = 'DOCTOR_OWNER' AND ctx.purpose = 'STAFF_MANAGEMENT'
                    AND ctx.stage = 'PRIMARY'"""


def _exists(predicate: str) -> str:
    return "EXISTS (SELECT 1 FROM public.f01_context() ctx WHERE " + predicate + ")"


def _guard(predicate: str) -> str:
    return (
        "IF NOT "
        + _exists(predicate)
        + " THEN RAISE EXCEPTION 'Request writer scope denied' "
        "USING ERRCODE = '42501'; END IF;\n            "
    )


def _rewrite(
    signature: str,
    replacements: tuple[tuple[str, str], ...],
    *,
    guard: str | None = None,
) -> None:
    # Read only the fixed, reviewed signatures. CREATE OR REPLACE preserves
    # owner, ACL, signature, and the historical implementation/lock sequence.
    bind = op.get_bind()
    definition = bind.execute(
        text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
        {"signature": "public." + signature},
    ).scalar_one()
    if not definition or not definition.startswith(
        "CREATE OR REPLACE FUNCTION public."
    ):
        raise RuntimeError("Expected historical function: " + signature)
    for before, after in replacements:
        if before not in definition:
            raise RuntimeError("Historical writer marker missing: " + signature)
        definition = definition.replace(before, after)
    if guard:
        if "BEGIN\n" not in definition:
            raise RuntimeError("Historical writer entry missing: " + signature)
        definition = definition.replace(
            "BEGIN\n", "BEGIN\n            " + _guard(guard), 1
        )
    # pg_get_functiondef returns literal percent signs (e.g. %ROWTYPE).
    # Do not send an empty parameter mapping to psycopg's placeholder parser.
    bind.exec_driver_sql(definition, execution_options={"no_parameters": True})


def upgrade() -> None:
    org = (_ORG, "public.f01_org()")
    staff = (_STAFF, "public.f01_staff()")
    role = (_RUNTIME, _API)
    # Auth/web metadata paths are explicitly separated from the activated
    # clinical API. Only the former retain their existing tenant routing hint.
    _rewrite(
        "lock_active_organization(uuid)",
        (
            (
                _ORG,
                "(CASE WHEN session_user IN ('carepulse_auth','carepulse_web') "
                "THEN "
                + _ORG
                + " WHEN session_user = 'carepulse_api' THEN public.f01_org() END)",
            ),
        ),
    )
    _rewrite(
        "verify_first_owner_preparation(uuid,uuid,text)",
        (
            (
                "IF clinic::text IS DISTINCT FROM",
                (
                    "IF session_user NOT IN "
                    "('carepulse_auth','carepulse_web') OR clinic::text IS DISTINCT FROM"
                ),
            ),
        ),
    )
    for signature, marker in (
        ("resolve_login_tenant(text)", "WHERE s.auth_user_id = native_user_id"),
        ("resolve_recovery_tenant(text)", "WHERE s.auth_user_id = native_user_id"),
        ("resolve_recovery_operation(text)", "WHERE target_digest ~"),
    ):
        _rewrite(
            signature,
            (
                (
                    marker,
                    "WHERE session_user IN ('carepulse_auth','carepulse_web') AND "
                    + marker[6:],
                ),
            ),
        )
    for signature in (
        "is_delivery_claim_owner(uuid,uuid,bigint)",
        "claimed_delivery_material(uuid,uuid,bigint)",
    ):
        _rewrite(
            signature,
            (
                (
                    "WHERE j.id = requested_job",
                    "WHERE session_user = 'carepulse_sender' AND j.id = requested_job",
                ),
            ),
        )
    _rewrite(
        "lock_native_session_authority(uuid,text,text)",
        (org,),
        guard="""ctx.organization_id = target_tenant::text
                  AND ctx.session_id = target_session
                  AND ctx.staff_id IS NOT NULL AND ctx.role IN ('DOCTOR_OWNER','RECEPTIONIST')
                  AND EXISTS (SELECT 1 FROM public.staff_accounts s
                    WHERE s.organization_id = target_tenant AND s.id = ctx.staff_id::uuid
                    AND s.auth_user_id = target_user)""",
    )
    _rewrite(
        "activate_first_owner(uuid,uuid,text,text,bigint,bigint,uuid)",
        (org,),
        guard="""ctx.purpose = 'OWNER_ACTIVATION' AND ctx.method = 'POST'
                  AND ctx.stage = 'PRIMARY' AND ctx.role = 'BOOTSTRAP'
                  AND ctx.organization_id = clinic::text AND ctx.target_id = invite
                  AND ctx.native_user_id = prepared_user
                  AND ctx.proof_digest = encode(sha256(convert_to(supplied_proof,'UTF8')),'hex')""",
    )
    for signature, predicate in (
        (
            "native_account_unbound_for_receptionist(uuid,text)",
            _OWNER + " AND ctx.method = 'POST' AND ctx.path = '/v1/staff/receptionists'"
            " AND ctx.native_user_id = $2 AND ctx.organization_id = $1::text",
        ),
        (
            "native_receptionist_credential_ready(uuid,uuid)",
            (
                "ctx.purpose = 'RECEPTIONIST_ACCEPT' AND ctx.role = 'BOOTSTRAP'"
                " AND ctx.method = 'POST' AND ctx.stage = 'PRIMARY'"
                " AND ctx.target_id = $2 AND ctx.organization_id = $1::text"
            ),
        ),
    ):
        _rewrite(
            signature,
            (
                org,
                role,
                (
                    "SELECT session_user = 'carepulse_api'",
                    "SELECT session_user = 'carepulse_api' AND " + _exists(predicate),
                ),
            ),
        )
    _rewrite(
        "set_receptionist_operational_manager(uuid,uuid,uuid,boolean,bigint,bigint,text,uuid)",
        (org, staff, role),
        guard=_OWNER
        + """
                  AND ctx.method = 'PATCH' AND ctx.path LIKE '%/permissions'
                  AND ctx.organization_id = p_tenant::text AND ctx.staff_id = p_actor::text
                  AND ctx.target_id = p_staff AND ctx.expected_security_revision = p_expected_security_revision
                  AND ctx.expected_staff_revision = p_expected_revision AND ctx.manager_enabled = p_enabled""",
    )
    _rewrite(
        "lock_owned_recovery(text,text)",
        (
            org,
            staff,
            role,
            (
                "WHERE session_user = 'carepulse_api'",
                "WHERE session_user = 'carepulse_api' AND "
                + _exists(
                    _RECOVERY + " AND ctx.native_user_id = $1 AND "
                    "(ctx.proof_digest = $2 OR ($2 IS NULL AND ctx.path = '/v1/staff/recovery/reserve'))"
                ),
            ),
        ),
    )
    _rewrite(
        "recovery_native_verified(uuid)",
        (
            org,
            (
                "WHERE r.id = operation_id",
                "WHERE "
                + _exists(
                    _RECOVERY
                    + """ AND ctx.staff_id = r.staff_id::text
                AND ctx.native_user_id = r.auth_user_id
                AND (ctx.proof_digest = r.proof_digest OR
                     (ctx.path = '/v1/staff/recovery/reserve' AND EXISTS (
                      SELECT 1 FROM public.staff_accounts s
                      WHERE s.organization_id = r.organization_id AND s.id = r.staff_id
                        AND s.reset_block_operation_id = r.id)))"""
                )
                + " AND r.id = operation_id",
            ),
        ),
    )
    _rewrite(
        "abandon_owned_recovery(uuid)",
        (
            org,
            (
                "IF NOT FOUND OR old_row.closed_at",
                "IF NOT FOUND OR NOT "
                + _exists(
                    _RECOVERY
                    + """
            AND ctx.path = '/v1/staff/recovery/reserve'
            AND ctx.staff_id = old_row.staff_id::text AND ctx.native_user_id = old_row.auth_user_id
            AND ctx.proof_digest IS DISTINCT FROM old_row.proof_digest"""
                )
                + " OR old_row.closed_at",
            ),
        ),
    )
    _rewrite(
        "finalize_verified_recovery(uuid)",
        (
            org,
            (
                "WHERE id = operation_id AND organization_id =",
                "WHERE id = operation_id AND "
                + _exists(
                    _RECOVERY
                    + """
            AND ctx.path = '/v1/staff/recovery/finalize'
            AND ctx.staff_id = public.recovery_operations.staff_id::text
            AND ctx.native_user_id = public.recovery_operations.auth_user_id
            AND ctx.proof_digest = public.recovery_operations.proof_digest"""
                )
                + " AND organization_id =",
            ),
        ),
    )
    _rewrite(
        "delete_staff_sessions(uuid,text)",
        (org,),
        guard="""
        ctx.purpose = 'STAFF_MANAGEMENT' AND ctx.role = 'DOCTOR_OWNER'
        AND ctx.stage = 'SESSION_CLEANUP' AND ctx.organization_id = tenant_id::text
        AND (ctx.path LIKE '%/disable' OR ctx.path LIKE '%/permissions')
        AND EXISTS (SELECT 1 FROM public.staff_accounts s
          WHERE s.organization_id = tenant_id AND s.id = ctx.target_id
          AND s.auth_user_id = delete_staff_sessions.native_user_id)""",
    )

    # Internal source/actor checks remain in the historical lock order, but
    # cannot accept a caller-controlled GUC as authority.
    source = (
        "ctx.organization_id = org::text AND (("
        + _BOOKING
        + """
        AND ctx.source_visit_id = visit) OR ("""
        + _VISIT
        + """
        AND ctx.target_id = visit AND ctx.method IN ('PATCH','POST')))"""
    )
    for signature in (
        "d08_context(uuid,uuid,jsonb)",
        "b10_lock_followup_prefix(uuid,uuid)",
    ):
        _rewrite(signature, (org, staff, role), guard=source)
    _rewrite(
        "create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)",
        (
            org,
            staff,
            role,
            (
                "PERFORM 1 FROM public.patients",
                _guard("ctx.patient_id = patient") + "PERFORM 1 FROM public.patients",
            ),
        ),
        guard=_BOOKING
        + " AND ctx.organization_id = org::text AND ctx.source_visit_id = visit",
    )
    _rewrite(
        "record_followup_source_decision(uuid,uuid,jsonb,jsonb)",
        (),
        guard=_VISIT
        + " AND ctx.method = 'PATCH' AND ctx.organization_id = org::text AND ctx.target_id = visit",
    )
    _rewrite(
        "extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)",
        (
            (
                "PERFORM public.b10_lock_followup_prefix(org, c.source_visit_id);",
                _guard(
                    _VISIT
                    + """ AND ctx.method = 'POST' AND ctx.organization_id = org::text
            AND ctx.target_id = c.source_visit_id AND ctx.target_appointment_id = c.appointment_id"""
                )
                + "PERFORM public.b10_lock_followup_prefix(org, c.source_visit_id);",
            ),
        ),
    )
    _rewrite(
        "b10_operational_source_proof(uuid,uuid)",
        (org, staff, role),
        guard="""ctx.organization_id = org::text AND ctx.purpose = 'APPOINTMENT_BOOKING'
          AND ((ctx.method = 'POST' AND ctx.stage IN ('PRIMARY','RECEIPT_RECOVERY')
                  AND ctx.source_visit_id = source_visit)
            OR (ctx.method = 'GET' AND ctx.stage = 'PRIMARY' AND EXISTS (
                SELECT 1 FROM public.visits v
                WHERE v.organization_id = org AND v.id = source_visit
                AND (v.appointment_id = ctx.target_id OR EXISTS (
                    SELECT 1 FROM public.followup_claims cl
                    WHERE cl.organization_id = org AND cl.source_visit_id = v.id
                    AND cl.appointment_id = ctx.target_id)))))""",
    )
    for signature, replacements in (
        ("guard_recovery_operator_provenance()", (role,)),
        ("guard_clinical_audit()", (role, staff)),
        ("guard_visit()", (role, staff)),
        ("lock_day_review_attendance(uuid)", (role, org)),
        ("require_confirmation_doctor(uuid,uuid)", (role, org, staff)),
        ("guard_admission_confirmation()", (role, staff)),
    ):
        _rewrite(signature, replacements)

    for signature in (
        "lock_active_organization(uuid)",
        "lock_native_session_authority(uuid,text,text)",
        "activate_first_owner(uuid,uuid,text,text,bigint,bigint,uuid)",
        "native_account_unbound_for_receptionist(uuid,text)",
        "native_receptionist_credential_ready(uuid,uuid)",
        "set_receptionist_operational_manager(uuid,uuid,uuid,boolean,bigint,bigint,text,uuid)",
        "lock_owned_recovery(text,text)",
        "recovery_native_verified(uuid)",
        "abandon_owned_recovery(uuid)",
        "finalize_verified_recovery(uuid)",
        "delete_staff_sessions(uuid,text)",
        "create_followup_claim_ordered(uuid,uuid,jsonb,jsonb)",
        "record_followup_source_decision(uuid,uuid,jsonb,jsonb)",
        "extend_followup_claim_ordered(uuid,uuid,jsonb,jsonb)",
        "b10_operational_source_proof(uuid,uuid)",
    ):
        op.execute(
            "GRANT EXECUTE ON FUNCTION public." + signature + " TO carepulse_api"
        )
    for signature in (
        "lock_active_organization(uuid)",
        "verify_first_owner_preparation(uuid,uuid,text)",
        "resolve_login_tenant(text)",
        "resolve_recovery_tenant(text)",
        "resolve_recovery_operation(text)",
    ):
        op.execute(
            "GRANT EXECUTE ON FUNCTION public." + signature + " TO carepulse_web"
        )

    # Unsupported Wave 3 operations and internal writer helpers are not callable
    # using any application runtime identity. Migration-owned callers retain the
    # existing internal invocation path.
    for signature in (
        "lock_doctor_admission_capacity(uuid,uuid,date,text)",
        "record_day_review(uuid,date,bigint,text,text)",
        "complete_examined_visit(uuid,uuid,bigint,boolean)",
        "transition_terminal_appointment(uuid,uuid,bigint,jsonb)",
        "d07_complete_examined_visit_raw(uuid,uuid,bigint,boolean)",
        "create_followup_claim(uuid,uuid,jsonb,jsonb)",
        "extend_followup_claim(uuid,uuid,jsonb,jsonb)",
        "b10_unchecked_claim_creation(uuid,uuid,jsonb,jsonb)",
        "d08_context(uuid,uuid,jsonb)",
        "b10_lock_followup_prefix(uuid,uuid)",
    ):
        op.execute(
            "REVOKE EXECUTE ON FUNCTION public."
            + signature
            + " FROM PUBLIC, carepulse_api, carepulse_web, carepulse_worker, carepulse_gate, carepulse_runtime"
        )


def downgrade() -> None:
    raise RuntimeError("F-01 writer boundary is forward-only")
