"""Shared private API field allowlists and sanitized HTTP contracts."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

_ERRORS = {
    401: ("UNAUTHENTICATED", "Authentication required"),
    403: ("FORBIDDEN", "Operation unavailable"),
    404: ("NOT_FOUND", "Resource not found"),
    409: ("CONFLICT", "Operation conflict"),
    422: ("INVALID_INPUT", "Invalid request"),
    429: ("RATE_LIMITED", "Too many requests"),
    503: ("UNAVAILABLE", "Service unavailable"),
}
_CONFLICT_CODES = {
    "CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "RESET_IN_PROGRESS",
    "STALE_REVISION",
}
_SERVER_OWNED_INPUT_FIELDS = frozenset(
    {
        "id",
        "organization_id",
        "tenant_id",
        "actor_id",
        "staff_id",
        "role",
        "enabled",
        "verified",
        "security_revision",
        "revision",
        "archived",
        "archived_at",
        "normalized_phone",
    }
)


class ContractError(Exception):
    """An expected denial; never contain untrusted values in its HTTP output."""

    def __init__(self, status_code: int, code: str | None = None) -> None:
        if status_code not in _ERRORS:
            raise ValueError("Unsupported HTTP status")
        self.status_code = status_code
        self.code = code
        super().__init__(_ERRORS[status_code][1])


@dataclass(frozen=True)
class ResourceFields:
    """Each resource declares its allowed fields for each live role explicitly."""

    readable: Mapping[str, frozenset[str]]
    writable: Mapping[str, frozenset[str]]


def _no_nested_relations(field_value: Any) -> bool:
    if isinstance(field_value, Mapping):
        return False
    if isinstance(field_value, (list, tuple)):
        return all(_no_nested_relations(element) for element in field_value)
    return True


def project_visible_resource(
    resource: Mapping[str, Any] | None,
    organization_id: UUID,
    role: str,
    fields: ResourceFields,
) -> dict[str, Any]:
    """A missing or foreign tenant row has the same externally visible denial."""
    if resource is None or resource.get("organization_id") != organization_id:
        raise ContractError(404)
    allowed = fields.readable.get(role)
    if allowed is None:
        raise ContractError(403)
    if any(
        not _no_nested_relations(resource[name]) for name in resource if name in allowed
    ):
        raise ContractError(403)
    return {name: datum for name, datum in resource.items() if name in allowed}


def permitted_write_fields(
    body: Mapping[str, Any], role: str, fields: ResourceFields
) -> dict[str, Any]:
    """Reject mass assignment; command-specific DTOs supply the exact field policy."""
    allowed = fields.writable.get(role)
    if allowed is None:
        raise ContractError(403)
    if (
        body.keys() & _SERVER_OWNED_INPUT_FIELDS
        or body.keys() - allowed
        or any(not _no_nested_relations(datum) for datum in body.values())
    ):
        raise ContractError(422)
    return dict(body)


def normalize_typed_body(body: BaseModel) -> bytes:
    """Canonical JSON from an already validated, extra-forbid command DTO.

    Omitted versus explicitly null patch fields stay different. Credential/reset
    flows use their separate keyed fingerprints, never this unkeyed digest.
    """
    if body.model_config.get("extra") != "forbid":
        raise ValueError("Request DTO must reject unknown fields")
    return json.dumps(
        body.model_dump(mode="json", exclude_unset=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalized_request_digest(body: BaseModel) -> str:
    return hashlib.sha256(normalize_typed_body(body)).hexdigest()


def require_matching_revision(expected_revision: int, current_revision: int) -> None:
    if type(expected_revision) is not int or expected_revision < 0:
        raise ContractError(422)
    if expected_revision != current_revision:
        raise ContractError(409, "STALE_REVISION")


def success_response(
    data: Any, revision: int, event_id: UUID | None = None, status_code: int = 200
) -> JSONResponse:
    if type(revision) is not int or revision < 0 or status_code not in (200, 201):
        raise ValueError("Invalid success revision or HTTP status")
    payload: dict[str, Any] = {"data": data, "revision": revision}
    if event_id is not None:
        payload["event_id"] = str(event_id)
    return JSONResponse(
        payload, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


def error_response(
    status_code: int, request_id: UUID | None = None, code: str | None = None
) -> JSONResponse:
    default_code, message = _ERRORS[status_code]
    if code is not None and (status_code != 409 or code not in _CONFLICT_CODES):
        raise ValueError("Unsupported public error code")
    return JSONResponse(
        {
            "error": {
                "code": code or default_code,
                "message": message,
                "request_id": str(request_id or uuid4()),
            }
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def contract_exception_handler(
    _request: Request, error: ContractError
) -> JSONResponse:
    return error_response(error.status_code, code=error.code)
