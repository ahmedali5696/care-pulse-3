"""Tenant-scoped patient projection and authenticated keyset pagination."""

import base64
import binascii
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import Connection, func, select

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import ContractError
from carepulse.patients.contracts import ReadPatient, SearchPatients
from carepulse.patients.demographics import normalize_phone
from carepulse.patients.tables import patients

PATIENT_COLUMNS = tuple(patients.c[name] for name in ReadPatient.model_fields)


def patient_projection(row) -> dict:
    return ReadPatient.model_validate(dict(row)).model_dump(mode="json")


def visible_patient(connection: Connection, actor: AuthorizedStaff, patient_id: UUID):
    patient = (
        connection.execute(
            select(*PATIENT_COLUMNS).where(
                patients.c.organization_id == actor.organization_id,
                patients.c.id == patient_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if patient is None or (patient["archived"] and actor.role != "DOCTOR_OWNER"):
        raise ContractError(404)
    return patient


def _cursor_mac(secret: str, scope: bytes, patient_id: bytes) -> bytes:
    return hmac.digest(secret.encode(), b"B05:patients:" + scope + patient_id, "sha256")


def _after(cursor: str | None, secret: str, scope: bytes) -> UUID | None:
    if cursor is None:
        return None
    try:
        decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as invalid:
        raise ContractError(422) from invalid
    if len(decoded) != 48 or not hmac.compare_digest(
        decoded[16:], _cursor_mac(secret, scope, decoded[:16])
    ):
        raise ContractError(422)
    return UUID(bytes=decoded[:16])


def search_patients(
    connection: Connection, actor: AuthorizedStaff, query: SearchPatients, secret: str
) -> dict:
    if query.archived and actor.role != "DOCTOR_OWNER":
        raise ContractError(403)
    scope = hashlib.sha256(
        json.dumps(
            [
                str(actor.organization_id),
                actor.role,
                query.model_dump(exclude={"cursor"}),
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).digest()
    after = _after(query.cursor, secret, scope)
    statement = select(*PATIENT_COLUMNS).where(
        patients.c.organization_id == actor.organization_id,
        patients.c.archived == query.archived,
    )
    if query.name:
        statement = statement.where(
            func.strpos(
                func.lower(func.array_to_string(patients.c.name_parts, " ")),
                query.name.lower(),
            )
            > 0
        )
    if query.phone:
        normalized = normalize_phone(query.phone)
        statement = statement.where(
            patients.c.phone_normalized == normalized
            if normalized
            else func.strpos(patients.c.phone_raw, query.phone) > 0
        )
    if after is not None:
        statement = statement.where(patients.c.id > after)
    rows = (
        connection.execute(statement.order_by(patients.c.id).limit(query.limit + 1))
        .mappings()
        .all()
    )
    selected = rows[: query.limit]
    next_cursor = None
    if len(rows) > query.limit:
        patient_bytes = selected[-1]["id"].bytes
        next_cursor = base64.urlsafe_b64encode(
            patient_bytes + _cursor_mac(secret, scope, patient_bytes)
        ).decode()
    return {
        "items": [patient_projection(row) for row in selected],
        "next_cursor": next_cursor,
    }
