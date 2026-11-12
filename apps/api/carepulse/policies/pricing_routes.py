"""Signed and live-authorized pricing operations for Wave 2 B-06."""

from fastapi import APIRouter, Request
from pydantic import ValidationError

from carepulse.identity.authorization import (
    can_manage_operational_settings,
    protected_transaction,
)
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    success_response,
)
from carepulse.identity.receipts import (
    ReceiptConflict,
    TenantReceiptQuery,
    require_committed_tenant_receipt,
)
from carepulse.policies.pricing_contracts import PricingPatch, QuoteQuery
from carepulse.policies.pricing_service import read_pricing, update_pricing

router = APIRouter(prefix="/v1/settings/pricing")


def _transaction(request: Request):
    if request.method != "GET" and request.query_params:
        raise ContractError(422)
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "SETTINGS_PRICING":
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
    )


@router.get("")
def get_pricing(request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        query = QuoteQuery.model_validate(dict(params))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request) as (connection, actor):
        result = read_pricing(connection, actor, query)
        return success_response(result, result["price_revision"])


@router.patch("")
def patch_pricing(command: PricingPatch, request: Request):
    key = request.scope["carepulse.identity"].idempotency_key
    try:
        with _transaction(request) as (connection, actor):
            result = update_pricing(connection, actor, command, key)
            return success_response(result, result["price_revision"])
    except ReceiptConflict:
        with _transaction(request) as (connection, actor):
            if not can_manage_operational_settings(actor):
                raise ContractError(403)
            result, revision = require_committed_tenant_receipt(
                connection,
                TenantReceiptQuery(
                    actor,
                    "settings.pricing.update",
                    key,
                    normalized_request_digest(command),
                ),
            )
            return success_response(result, revision)
