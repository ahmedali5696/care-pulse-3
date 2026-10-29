"""B-06 tenant-authorized pricing reads and atomic Owner policy publication."""

import json
import re
from decimal import Decimal, localcontext
from uuid import uuid4

from sqlalchemy import Connection, text

from carepulse.identity.authorization import AuthorizedStaff
from carepulse.identity.contracts import (
    ContractError,
    normalized_request_digest,
    require_matching_revision,
)
from carepulse.identity.receipts import (
    Receipt,
    TenantReceiptQuery,
    insert_tenant_receipt,
    resolve_tenant_receipt,
)
from carepulse.notifications.evidence import append_sensitive_audit
from carepulse.policies.pricing import parse_money, quote_price
from carepulse.policies.pricing_contracts import PricingPatch, QuoteQuery
from carepulse.policies.versions import (
    StalePolicyRevision,
    append_version,
    lock_policy_heads,
)


def current_price_state(connection: Connection, tenant) -> dict:
    """Read active policy payloads; transaction caller acquires any needed locks."""
    rows = (
        connection.execute(
            text("""
            SELECT h.kind, h.revision, h.current_version, v.payload::text AS payload
            FROM public.clinic_policy_heads AS h
            JOIN public.clinic_policy_versions AS v ON
                (v.organization_id,v.kind,v.version) =
                (h.organization_id,h.kind,h.current_version)
            WHERE h.organization_id = :tenant AND h.kind IN ('PRICE','FOLLOWUP')
        """),
            {"tenant": tenant},
        )
        .mappings()
        .all()
    )
    if {row["kind"] for row in rows} != {"PRICE", "FOLLOWUP"}:
        raise ContractError(409)
    return {
        row["kind"]: {
            "payload": json.loads(
                row["payload"], parse_float=Decimal, parse_int=Decimal
            ),
            "revision": row["revision"],
            "version": row["current_version"],
        }
        for row in rows
    }


def _money_output(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise ContractError(409)
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid saved price")
        # Format creates the wire's fixed EGP scale without passing through float.
        if amount.as_tuple().exponent < -2:
            with localcontext() as ctx:
                ctx.prec = max(28, len(format(amount, "f")) + 8)
                if amount != amount.quantize(Decimal("0.01")):
                    raise ValueError("Invalid saved price scale")
        return format(amount, ".2f")
    except (ValueError, ArithmeticError) as invalid:
        raise ContractError(409) from invalid


def _noncanonical_legacy_money(value: object) -> str | None:
    """Identify old JSON numbers that lose value at two-decimal EGP scale."""
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    if value.as_tuple().exponent >= -2:
        return None
    try:
        _money_output(value)
    except ContractError:
        return format(value, "f")
    return None


def _price_payload_json(value: object) -> str:
    """Preserve all untouched D-04 JSON numeric scales, even in extra fields."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Invalid policy JSON number")
        return format(value, "f")
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(key)}:{_price_payload_json(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_price_payload_json(item) for item in value) + "]"
    if isinstance(value, float):
        raise TypeError("Binary float cannot encode a policy JSON number")
    return json.dumps(value, allow_nan=False)


def _projection(state: dict, owner: bool) -> dict:
    pricing, followup = state["PRICE"], state["FOLLOWUP"]
    price = pricing["payload"]
    values = {}
    remediation = {}
    for name in (
        "standard_new_exam_egp",
        "preferential_followup_egp",
        "urgent_base_egp",
    ):
        raw = _noncanonical_legacy_money(price[name])
        if raw is not None:
            values[name] = None
            if owner:
                remediation[name] = raw
        else:
            values[name] = _money_output(price[name])
    result = {
        "standard_new_exam_egp": values["standard_new_exam_egp"],
        "preferential_followup_egp": values["preferential_followup_egp"],
        "price_revision": pricing["revision"],
        "followup_revision": followup["revision"],
    }
    if owner:
        result.update(
            urgent_base_egp=values["urgent_base_egp"],
            urgent_enabled=price["urgent_enabled"],
            completion_day_zero=followup["payload"]["completion_day_zero"],
            followup_last_day_inclusive=int(followup["payload"]["last_day_inclusive"]),
        )
        if remediation:
            result["legacy_price_remediation"] = remediation
    return result


def read_pricing(
    connection: Connection, actor: AuthorizedStaff, quote_query: QuoteQuery
) -> dict:
    if actor.role not in ("DOCTOR_OWNER", "RECEPTIONIST"):
        raise ContractError(403)
    state = current_price_state(connection, actor.organization_id)
    result = _projection(state, actor.role == "DOCTOR_OWNER")
    if quote_query.service_type is not None:
        base_key = (
            "standard_new_exam_egp"
            if quote_query.service_type == "NEW_EXAMINATION"
            else "preferential_followup_egp"
        )
        base = result[base_key]
        if base is None:
            raise ContractError(409)
        try:
            quotation = quote_price(
                base,
                result["standard_new_exam_egp"] or base,
                quote_query.service_type,
                "ROUTINE",
                quote_query.discount_type,
                quote_query.discount_value,
            )
        except ValueError as invalid:
            raise ContractError(422) from invalid
        result["price_quote"] = {
            "service_type": quotation.service_type,
            "base_egp": format(quotation.base_egp, ".2f"),
            "discount_type": quotation.discount_type,
            "discount_value": format(
                quotation.discount_value,
                "f" if quotation.discount_type == "PERCENT" else ".2f",
            ),
            "is_free": quotation.is_free,
            "net_egp": format(quotation.net_egp, ".2f"),
            "price_revision": state["PRICE"]["revision"],
            **(
                {"followup_revision": state["FOLLOWUP"]["revision"]}
                if quotation.service_type == "FOLLOW_UP"
                else {}
            ),
        }
    return result


def _has_operating_periods(connection: Connection, tenant) -> bool:
    return bool(
        connection.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM public.clinic_schedule_intervals AS i
            JOIN public.clinic_policy_heads AS h ON
                h.organization_id = i.organization_id AND h.kind = 'SCHEDULE'
                AND h.current_version = i.policy_version
            WHERE i.organization_id = :tenant AND i.policy_kind = 'SCHEDULE'
                AND i.interval_kind IN ('WORK', 'OVERRIDE_WORK')
                AND i.starts_at < i.ends_at
        )
    """),
            {"tenant": tenant},
        )
    )


def update_pricing(
    connection: Connection,
    actor: AuthorizedStaff,
    command: PricingPatch,
    idempotency_key: str,
) -> dict:
    if actor.role != "DOCTOR_OWNER":
        raise ContractError(403)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise ContractError(422)
    receipt_query = TenantReceiptQuery(
        actor,
        "settings.pricing.update",
        idempotency_key,
        normalized_request_digest(command),
    )
    stored = resolve_tenant_receipt(connection, receipt_query)
    if stored is not None:
        return stored[0]

    tenant = actor.organization_id
    try:
        lock_policy_heads(
            connection,
            tenant,
            {"SCHEDULE": "SHARE", "FOLLOWUP": "UPDATE", "PRICE": "UPDATE"},
        )
    except StalePolicyRevision as invalid:
        raise ContractError(409, "STALE_REVISION") from invalid
    state = current_price_state(connection, tenant)
    require_matching_revision(
        command.expected_price_revision, state["PRICE"]["revision"]
    )
    provided = command.model_fields_set
    if "followup_last_day_inclusive" in provided:
        require_matching_revision(
            command.expected_followup_revision, state["FOLLOWUP"]["revision"]
        )

    price = dict(state["PRICE"]["payload"])
    followup = dict(state["FOLLOWUP"]["payload"])
    for name in (
        "standard_new_exam_egp",
        "preferential_followup_egp",
        "urgent_base_egp",
    ):
        if name in provided:
            price[name] = getattr(command, name)
    if "urgent_enabled" in provided:
        if command.urgent_enabled is None:
            raise ContractError(422)
        price["urgent_enabled"] = command.urgent_enabled
    if "followup_last_day_inclusive" in provided:
        followup["last_day_inclusive"] = command.followup_last_day_inclusive

    price_requested = bool(
        provided
        & {
            "standard_new_exam_egp",
            "preferential_followup_egp",
            "urgent_base_egp",
            "urgent_enabled",
        }
    )
    try:
        for name in (
            "standard_new_exam_egp",
            "preferential_followup_egp",
            "urgent_base_egp",
        ):
            if name not in provided and isinstance(price[name], Decimal):
                if _noncanonical_legacy_money(price[name]) is None:
                    _money_output(price[name])
                continue  # Preserve every untouched D-04 numeric type and scale.
            formatted = _money_output(price[name])
            if formatted is not None:
                parse_money(formatted)
            if price_requested:
                price[name] = formatted
        standard = price["standard_new_exam_egp"]
        premium = price["urgent_base_egp"]
        if price["urgent_enabled"] and (
            standard is None
            or premium is None
            or Decimal(str(premium)) <= Decimal(str(standard))
            or not _has_operating_periods(connection, tenant)
        ):
            raise ValueError(
                "Urgent requires a greater fixed premium and operating periods"
            )
    except (ValueError, ArithmeticError) as invalid:
        raise ContractError(422) from invalid
    changed_price = price_requested and price != state["PRICE"]["payload"]
    changed_followup = followup != state["FOLLOWUP"]["payload"]
    # Corrected fields are canonical; uncorrected D-04 numbers retain exact scale.
    if changed_price:
        try:
            append_version(
                connection,
                tenant,
                "PRICE",
                _price_payload_json(price),
                state["PRICE"]["revision"],
                actor_staff_id=str(actor.staff_id),
            )
        except StalePolicyRevision as invalid:
            raise ContractError(409, "STALE_REVISION") from invalid
    if changed_followup:
        try:
            append_version(
                connection,
                tenant,
                "FOLLOWUP",
                followup,
                state["FOLLOWUP"]["revision"],
                actor_staff_id=str(actor.staff_id),
            )
        except StalePolicyRevision as invalid:
            raise ContractError(409, "STALE_REVISION") from invalid
    if not changed_price and not changed_followup:
        raise ContractError(422)
    updated = current_price_state(connection, tenant)
    result = _projection(updated, True)
    append_sensitive_audit(
        connection,
        {
            "organization_id": tenant,
            "actor_staff_id": actor.staff_id,
            "actor_role": actor.role,
            "action": "settings.pricing.update",
            "reason": command.reason,
            "before_state": {
                "price_revision": state["PRICE"]["revision"],
                "followup_revision": state["FOLLOWUP"]["revision"],
            },
            "after_state": {
                "price_revision": updated["PRICE"]["revision"],
                "followup_revision": updated["FOLLOWUP"]["revision"],
                "changed_fields": sorted(
                    provided
                    - {
                        "reason",
                        "expected_price_revision",
                        "expected_followup_revision",
                    }
                ),
            },
            "correlation_id": str(uuid4()),
        },
    )
    insert_tenant_receipt(
        connection,
        Receipt(
            actor_id=actor.staff_id,
            operation=receipt_query.operation,
            idempotency_key=idempotency_key,
            request_digest=receipt_query.request_digest,
            result_projection=result,
            result_revision=updated["PRICE"]["revision"],
            organization_id=tenant,
            staff_id=actor.staff_id,
        ),
    )
    return result
