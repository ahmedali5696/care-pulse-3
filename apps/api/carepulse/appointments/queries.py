"""Role-safe operational appointment reads and tenant-bound keyset pagination."""

import base64
import binascii
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import Connection, select

from carepulse.appointments.booking_contracts import AppointmentQuery
from carepulse.appointments.tables import (
    appointment_history,
    appointment_price_snapshots,
    appointments,
)
from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError

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
