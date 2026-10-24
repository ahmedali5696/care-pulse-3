"""D-09 idempotency receipts; callers own the domain transaction and live grants."""

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from carepulse.identity.authorization import AuthorizedStaff


class ReceiptConflict(Exception):
    """A committed key exists, or a competing request owns it."""


@dataclass(frozen=True)
class Receipt:
    actor_id: UUID
    operation: str
    idempotency_key: str
    request_digest: str
    result_projection: dict
    result_revision: int
    organization_id: UUID | None = None
    staff_id: UUID | None = None
    result_organization_id: UUID | None = None


def _check_receipt(receipt: Receipt) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", receipt.request_digest):
        raise ValueError("Invalid normalized request digest")
    if not receipt.operation or not receipt.idempotency_key:
        raise ValueError("Receipt fields are required")
    if receipt.result_revision < 0:
        raise ValueError("Invalid receipt revision")


def insert_tenant_receipt(connection: Connection, receipt: Receipt) -> UUID:
    """Commit with domain effects; a duplicate rejects the entire caller transaction."""
    _check_receipt(receipt)
    if receipt.organization_id is None or receipt.staff_id != receipt.actor_id:
        raise ValueError("Tenant receipt requires the acting clinic staff")
    return _insert_receipt(connection, receipt, "TENANT", receipt.organization_id)


def insert_operator_create_receipt(
    connection: Connection, receipt: Receipt, verified_operator_digest: str
) -> UUID:
    """Store pre-tenant clinic creation under the verified operator's identity."""
    _check_receipt(receipt)
    if (
        receipt.organization_id is not None
        or receipt.staff_id is not None
        or receipt.operation != "clinics.create"
        or receipt.result_organization_id is None
        or receipt.result_projection
    ):
        raise ValueError("Operator-create receipt cannot contain tenant/staff scope")
    receipt_id = uuid4()
    try:
        connection.execute(
            text("""
                SELECT public.write_operator_create_receipt(
                    :operator, :digest, :id, :key, :request_digest, :clinic, :revision)
            """),
            {
                "operator": receipt.actor_id,
                "digest": verified_operator_digest,
                "id": receipt_id,
                "key": receipt.idempotency_key,
                "request_digest": receipt.request_digest,
                "clinic": receipt.result_organization_id,
                "revision": receipt.result_revision,
            },
        )
    except IntegrityError as conflict:
        if conflict.orig.sqlstate != "23505":
            raise
        raise ReceiptConflict("Idempotency key already committed") from conflict
    return receipt_id


def _insert_receipt(
    connection: Connection, receipt: Receipt, scope_kind: str, scope_id: UUID
) -> UUID:
    receipt_id = uuid4()
    inserted = connection.scalar(
        text("""
            INSERT INTO public.idempotency_receipts
                (id, scope_kind, scope_id, actor_kind, actor_id, organization_id,
                 operator_id,
                 staff_id, operation, idempotency_key, request_digest,
                 result_projection, result_organization_id, result_revision)
            VALUES (:id, :scope_kind, :scope_id, :actor_kind, :actor_id, :organization_id,
                    :operator_id, :staff_id, :operation, :idempotency_key,
                    :request_digest, CAST(:result_projection AS jsonb),
                    :result_organization_id, :result_revision)
            ON CONFLICT (scope_kind, scope_id, actor_kind, actor_id, operation,
                         idempotency_key) DO NOTHING RETURNING id
        """),
        {
            **{
                key: value
                for key, value in vars(receipt).items()
                if key != "result_projection"
            },
            "result_projection": (
                None
                if scope_kind == "OPERATOR_CREATE"
                else json.dumps(receipt.result_projection, allow_nan=False)
            ),
            "id": receipt_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "actor_kind": "OPERATOR" if scope_kind == "OPERATOR_CREATE" else "STAFF",
            "operator_id": receipt.actor_id
            if scope_kind == "OPERATOR_CREATE"
            else None,
        },
    )
    if inserted is None:
        # Let the outer transaction roll back any earlier domain work. The winner
        # is read only in a NEW transaction after live authorization is rechecked.
        raise ReceiptConflict("Idempotency key already committed")
    return inserted


def lookup_operator_create_receipt(
    connection: Connection,
    operator_id: UUID,
    idempotency_key: str,
    verified_operator_digest: str,
):
    """Only enabled operator metadata; never returns or searches tenant receipts."""
    return (
        connection.execute(
            text("""
            SELECT * FROM public.read_operator_create_receipt(:operator_id, :digest, :key)
        """),
            {
                "operator_id": operator_id,
                "key": idempotency_key,
                "digest": verified_operator_digest,
            },
        )
        .mappings()
        .one_or_none()
    )


def lookup_tenant_receipt(
    connection: Connection,
    organization_id: UUID,
    staff_id: UUID,
    operation: str,
    idempotency_key: str,
):
    """A live own-tenant staff authorization is mandatory even for cached results."""
    return (
        connection.execute(
            text("""
            SELECT r.request_digest, r.result_projection, r.result_revision
            FROM public.idempotency_receipts AS r
            JOIN public.staff_accounts AS s
              ON (s.organization_id, s.id) = (r.organization_id, r.staff_id)
            JOIN public.organizations AS o ON o.id = s.organization_id
            WHERE r.scope_kind = 'TENANT' AND r.actor_kind = 'STAFF'
              AND r.scope_id = :tenant AND r.organization_id = :tenant
              AND r.staff_id = :staff AND r.actor_id = :staff
              AND s.enabled AND s.verified AND s.reset_block_operation_id IS NULL
              AND o.state = 'ACTIVE'
              AND r.operation = :operation AND r.idempotency_key = :key
        """),
            {
                "tenant": organization_id,
                "staff": staff_id,
                "operation": operation,
                "key": idempotency_key,
            },
        )
        .mappings()
        .one_or_none()
    )


def assert_same_request(stored_digest: str, supplied_digest: str) -> None:
    if stored_digest != supplied_digest:
        raise ReceiptConflict("Idempotency key has a different request")


@dataclass(frozen=True)
class TenantReceiptQuery:
    actor: AuthorizedStaff
    operation: str
    idempotency_key: str
    request_digest: str


@dataclass(frozen=True)
class OperatorReceiptQuery:
    operator_id: UUID
    idempotency_key: str
    request_digest: str


def resolve_tenant_receipt(
    connection: Connection, query: TenantReceiptQuery
) -> tuple[dict[str, Any], int] | None:
    """Call after current session, revision and role are checked under T020 locks.

    Recheck resource/field visibility before returning this committed projection.
    New work, receipt, audit and outbox must use the same domain transaction.
    """
    stored = lookup_tenant_receipt(
        connection,
        query.actor.organization_id,
        query.actor.staff_id,
        query.operation,
        query.idempotency_key,
    )
    if stored is None:
        return None
    assert_same_request(stored["request_digest"], query.request_digest)
    if (
        not isinstance(stored["result_projection"], dict)
        or type(stored["result_revision"]) is not int
    ):
        raise ValueError("Invalid committed tenant receipt")
    return stored["result_projection"], stored["result_revision"]


def resolve_operator_create_receipt(
    connection: Connection, query: OperatorReceiptQuery, verified_operator_digest: str
) -> tuple[UUID, int] | None:
    """Call only after verifying current operator authority, before clinic allocation."""
    stored = lookup_operator_create_receipt(
        connection, query.operator_id, query.idempotency_key, verified_operator_digest
    )
    if stored is None:
        return None
    assert_same_request(stored["request_digest"], query.request_digest)
    return stored["result_organization_id"], stored["result_revision"]
