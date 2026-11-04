"""Role-safe operational appointment reads and tenant-bound keyset pagination."""

import base64
import binascii
import hashlib
import hmac
import json
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select, text

from carepulse.appointments.booking_contracts import AppointmentQuery
from carepulse.appointments.followup_rules import (
    current_last_day_inclusive,
    intended_date_allowed,
)
from carepulse.appointments.followups import _approved_deadline
from carepulse.appointments.tables import (
    appointment_history,
    appointment_price_snapshots,
    appointments,
)
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.policies.pricing import quote_price
from carepulse.policies.pricing_service import _money_output, current_price_state
from carepulse.policies.schedule_service import _schedule_head

READ_FIELDS = (
    "id",
    "patient_id",
    "doctor_staff_id",
    "service_date",
    "status",
    "service_type",
    "classification",
    "lane",
    "booking_mode",
    "starts_at",
    "ends_at",
    "current_number",
    "revision",
)
PRICE_FIELDS = (
    "base_egp",
    "discount_type",
    "discount_value",
    "discount_reason",
    "is_free",
    "net_egp",
    "pricing_version",
    "creation_standard_base_egp",
)


def _appointment_statement(actor: AuthorizedStaff):
    return (
        select(
            *(appointments.c[name] for name in READ_FIELDS),
            *(appointment_price_snapshots.c[name] for name in PRICE_FIELDS),
        )
        .join(
            appointment_price_snapshots,
            (
                appointments.c.organization_id
                == appointment_price_snapshots.c.organization_id
            )
            & (appointments.c.id == appointment_price_snapshots.c.appointment_id),
        )
        .where(appointments.c.organization_id == actor.organization_id)
    )


def _projection(row) -> dict:
    projection = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in row.items()
        if key
        not in ("base_egp", "discount_value", "net_egp", "creation_standard_base_egp")
    }
    for key in ("id", "patient_id", "doctor_staff_id"):
        projection[key] = str(projection[key])
    for key in ("base_egp", "net_egp", "creation_standard_base_egp"):
        projection[key] = format(row[key], ".2f")
    projection["discount_value"] = format(
        row["discount_value"],
        "f" if row["discount_type"] == "PERCENT" else ".2f",
    )
    projection["number"] = projection.pop("current_number")
    return projection


def appointment_detail(
    connection: Connection, actor: AuthorizedStaff, appointment_id: UUID
) -> dict:
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    row = (
        connection.execute(
            _appointment_statement(actor).where(appointments.c.id == appointment_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ContractError(404)
    history = (
        connection.execute(
            select(
                appointment_history.c.action,
                appointment_history.c.old_status,
                appointment_history.c.new_status,
                appointment_history.c.old_service_date,
                appointment_history.c.new_service_date,
                appointment_history.c.old_number,
                appointment_history.c.new_number,
                appointment_history.c.appointment_revision,
                appointment_history.c.reason,
            )
            .where(
                appointment_history.c.organization_id == actor.organization_id,
                appointment_history.c.appointment_id == appointment_id,
            )
            .order_by(appointment_history.c.appointment_revision)
        )
        .mappings()
        .all()
    )
    return {
        **_projection(row),
        "history": [
            {
                key: value.isoformat() if hasattr(value, "isoformat") else value
                for key, value in entry.items()
            }
            for entry in history
        ],
    }


def followup_qualification(
    connection: Connection,
    actor: AuthorizedStaff,
    appointment_id: UUID,
    intended_date: date,
) -> dict:
    """Operational qualification of a selected source appointment, without Visit narrative."""
    source = appointment_detail(connection, actor, appointment_id)
    evidence = (
        connection.execute(
            text("""
            SELECT v.id, v.patient_id, v.doctor_staff_id,
                   v.clinical_revision, v.followup_decision,
                (SELECT h.occurred_at FROM public.appointment_history h
                 WHERE h.organization_id = v.organization_id
                   AND h.appointment_id = v.appointment_id
                   AND h.new_status = 'Completed'
                 ORDER BY h.appointment_revision DESC LIMIT 1) AS completed_at
            FROM public.visits v
            WHERE v.organization_id = :tenant AND v.appointment_id = :appointment
        """),
            {"tenant": actor.organization_id, "appointment": appointment_id},
        )
        .mappings()
        .one_or_none()
    )
    pricing = current_price_state(connection, actor.organization_id)
    try:
        last_day = current_last_day_inclusive(pricing["FOLLOWUP"]["payload"])
    except (TypeError, ValueError) as invalid:
        raise ContractError(409) from invalid
    eligible = False
    if (
        source["status"] == "Completed"
        and source["service_type"] == "NEW_EXAMINATION"
        and source["classification"] in ("ROUTINE", "URGENT", "EMERGENCY")
        and evidence
        and evidence["completed_at"] is not None
    ):
        decision = evidence["followup_decision"] or {}
        completed = evidence["completed_at"].astimezone(ZoneInfo("Africa/Cairo")).date()
        deadline = _approved_deadline(completed, decision, last_day)
        claim_state = connection.scalar(
            text("""
                    SELECT state FROM public.followup_claims
                    WHERE organization_id = :tenant AND source_visit_id = :visit
                      AND state IN ('ACTIVE','CONSUMED')
                    LIMIT 1
                """),
            {"tenant": actor.organization_id, "visit": evidence["id"]},
        )
        if claim_state == "ACTIVE":
            raise ContractError(409)
        if deadline is not None and evidence["clinical_revision"] >= 1:
            decision_actor = connection.scalar(
                text("""
                    SELECT actor_staff_id FROM public.clinical_revisions
                    WHERE organization_id = :tenant AND visit_id = :visit
                      AND change_kind IN ('DECISION_DEFAULT','DECISION_EXCEPTION')
                    ORDER BY revision DESC LIMIT 1
                """),
                {"tenant": actor.organization_id, "visit": evidence["id"]},
            )
            active_doctor = connection.scalar(
                text("""
                    SELECT id FROM public.staff_accounts
                    WHERE organization_id = :tenant AND role='DOCTOR_OWNER'
                      AND enabled AND verified
                """),
                {"tenant": actor.organization_id},
            )
            eligible = (
                claim_state != "CONSUMED"
                and evidence["patient_id"] == UUID(source["patient_id"])
                and evidence["doctor_staff_id"] == UUID(source["doctor_staff_id"])
                and evidence["doctor_staff_id"] == active_doctor
                and decision_actor == active_doctor
                and intended_date_allowed(completed, intended_date, deadline)
            )
    service_type = "FOLLOW_UP" if eligible else "NEW_EXAMINATION"
    standard = _money_output(pricing["PRICE"]["payload"]["standard_new_exam_egp"])
    base = _money_output(
        pricing["PRICE"]["payload"][
            "preferential_followup_egp" if eligible else "standard_new_exam_egp"
        ]
    )
    price_quote = None
    if base is not None and standard is not None:
        quote = quote_price(base, standard, service_type, "ROUTINE", "NONE", "0")
        price_quote = {
            "service_type": quote.service_type,
            "base_egp": format(quote.base_egp, ".2f"),
            "discount_type": "NONE",
            "discount_value": "0.00",
            "is_free": quote.is_free,
            "net_egp": format(quote.net_egp, ".2f"),
            "price_revision": pricing["PRICE"]["revision"],
            "followup_revision": pricing["FOLLOWUP"]["revision"],
        }
    qualification = {
        "patient_id": source["patient_id"],
        "intended_service_date": intended_date.isoformat(),
        "requested_service_type": "FOLLOW_UP",
        "actual_service_type": service_type,
        "preferential_available": eligible,
        "source_visit_id": str(evidence["id"]) if evidence else None,
        "source_revision": evidence["clinical_revision"] if evidence else None,
        "price_revision": pricing["PRICE"]["revision"],
        "followup_revision": pricing["FOLLOWUP"]["revision"],
        "schedule_revision": _schedule_head(connection, actor.organization_id)[
            "revision"
        ],
        "price_quote": price_quote,
    }
    if actor.role == "DOCTOR_OWNER" and evidence:
        qualification["doctor_followup_decision"] = evidence["followup_decision"]
    return qualification


def _cursor_signature(secret: str, scope: bytes, identifier: bytes) -> bytes:
    return hmac.digest(
        secret.encode(), b"B09:appointments:" + scope + identifier, "sha256"
    )


def _after(cursor: str | None, secret: str, scope: bytes) -> UUID | None:
    if cursor is None:
        return None
    try:
        decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as invalid:
        raise ContractError(422) from invalid
    if len(decoded) != 48 or not hmac.compare_digest(
        decoded[16:], _cursor_signature(secret, scope, decoded[:16])
    ):
        raise ContractError(422)
    return UUID(bytes=decoded[:16])


def list_appointments(
    connection: Connection, actor: AuthorizedStaff, query: AppointmentQuery, secret: str
) -> dict:
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    scope = hashlib.sha256(
        json.dumps(
            [
                str(actor.organization_id),
                actor.role,
                query.model_dump(mode="json", exclude={"cursor"}),
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).digest()
    after = _after(query.cursor, secret, scope)
    statement = _appointment_statement(actor)
    for name in ("patient_id", "service_date", "status", "lane"):
        selected = getattr(query, name)
        if selected is not None:
            statement = statement.where(appointments.c[name] == selected)
    if after is not None:
        statement = statement.where(appointments.c.id > after)
    rows = (
        connection.execute(statement.order_by(appointments.c.id).limit(query.limit + 1))
        .mappings()
        .all()
    )
    selected_rows = rows[: query.limit]
    next_cursor = None
    if len(rows) > query.limit:
        last = selected_rows[-1]["id"].bytes
        next_cursor = base64.urlsafe_b64encode(
            last + _cursor_signature(secret, scope, last)
        ).decode()
    return {
        "items": [_projection(row) for row in selected_rows],
        "next_cursor": next_cursor,
    }
