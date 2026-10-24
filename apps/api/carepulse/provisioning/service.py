"""Restricted operator-facing clinic metadata and lifecycle operations."""

from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from carepulse.provisioning.operators import VerifiedOperator


def audit_operator_action(
    connection,
    operator: VerifiedOperator,
    clinic: UUID | None,
    action: str,
    reason: str,
    correlation: str,
) -> None:
    connection.execute(
        text("""
          SELECT public.append_operator_metadata_audit(
            :actor, :digest, :audit, :clinic, :action, :reason, :correlation)
        """),
        {
            "actor": operator.id,
            "digest": operator.credential_digest,
            "audit": uuid4(),
            "clinic": clinic,
            "action": action,
            "reason": reason,
            "correlation": correlation,
        },
    )


def list_clinics(engine: Engine, operator: VerifiedOperator, reason: str) -> list[dict]:
    with engine.begin() as connection:
        clinics = (
            connection.execute(
                text(
                    "SELECT * FROM public.operator_list_clinics(:actor, :digest, :reason)"
                ),
                {
                    "actor": operator.id,
                    "digest": operator.credential_digest,
                    "reason": reason,
                },
            )
            .mappings()
            .all()
        )
    return [
        {
            key: val.isoformat()
            if hasattr(val, "isoformat")
            else str(val)
            if isinstance(val, UUID)
            else val
            for key, val in clinic.items()
        }
        for clinic in clinics
    ]


def change_clinic_state(
    engine: Engine,
    operator: VerifiedOperator,
    clinic: UUID,
    expected_revision: int,
    state: str,
    reason: str,
) -> dict:
    with engine.begin() as connection:
        revision = connection.scalar(
            text("""
                SELECT public.operator_change_clinic_state(
                    :actor, :digest, :clinic, :revision, :state)
            """),
            {
                "actor": operator.id,
                "digest": operator.credential_digest,
                "clinic": clinic,
                "revision": expected_revision,
                "state": state,
            },
        )
        audit_operator_action(
            connection,
            operator,
            clinic,
            "CLINIC_SUSPENDED" if state == "SUSPENDED" else "CLINIC_REACTIVATED",
            reason,
            f"{clinic}:{expected_revision + 1}:{state}",
        )
    return {"organization_id": str(clinic), "state": state, "revision": revision}
