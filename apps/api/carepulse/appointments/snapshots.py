"""Validate supplied frozen EGP money; live pricing belongs to B-06."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext

from carepulse.policies.pricing import exact_net

PENNY = Decimal("0.01")


def _decimal(amount: object, *, money: bool) -> Decimal:
    if not isinstance(amount, (str, Decimal)):
        raise TypeError("Monetary input must be a decimal string or Decimal")
    try:
        number = Decimal(amount)
        if not number.is_finite() or number < 0:
            raise ValueError("Nonfinite or negative amount")
        if money:
            with localcontext() as context:
                context.prec = max(
                    28,
                    len(number.as_tuple().digits) + abs(number.as_tuple().exponent) + 8,
                )
                if number != number.quantize(PENNY):
                    raise ValueError("EGP amounts cannot contain fractional piastres")
    except InvalidOperation as invalid:
        raise ValueError("Invalid decimal amount") from invalid
    return number


def validate_price_snapshot(fields: Mapping[str, object]) -> dict:
    """Check a supplied snapshot; never calculate or invent a service price."""
    service_type = fields["service_type"]
    classification = fields["classification"]
    if service_type not in ("NEW_EXAMINATION", "FOLLOW_UP"):
        raise ValueError("Invalid service type")
    if classification not in ("ROUTINE", "URGENT", "EMERGENCY"):
        raise ValueError("Invalid appointment classification")
    if service_type == "FOLLOW_UP" and classification != "ROUTINE":
        raise ValueError("Follow-up must use Routine classification")
    base = _decimal(fields["base_egp"], money=True)
    net = _decimal(fields["net_egp"], money=True)
    standard = _decimal(fields["creation_standard_base_egp"], money=True)
    discount_type = fields["discount_type"]
    discount = _decimal(fields["discount_value"], money=discount_type != "PERCENT")
    reason = fields.get("discount_reason")
    if discount_type == "NONE":
        if discount != 0 or reason is not None:
            raise ValueError("No discount permits no reason or amount")
    elif discount_type in ("FIXED", "PERCENT"):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Discount requires a reason")
        if discount > (base if discount_type == "FIXED" else 100):
            raise ValueError("Discount exceeds applicable base")
    else:
        raise ValueError("Unknown discount type")
    expected = exact_net(base, discount_type, discount)
    if expected != net or fields["is_free"] is not (net == 0):
        raise ValueError("Net price or free status differs from frozen calculation")
    if (
        service_type == "NEW_EXAMINATION"
        and classification != "URGENT"
        and base != standard
    ):
        raise ValueError("Ordinary examination must use the standard base")
    if classification == "URGENT" and (base <= standard or net <= standard):
        raise ValueError("Urgent base and net must exceed standard base")
    pricing_version = fields["pricing_version"]
    if type(pricing_version) is not int or pricing_version < 1:
        raise ValueError("A positive pricing version is required")
    provenance = fields.get("entitlement_provenance")
    if (provenance is not None and not isinstance(provenance, dict)) or (
        service_type == "FOLLOW_UP" and not provenance
    ):
        raise ValueError("Invalid entitlement provenance")
    return {
        "service_type": service_type,
        "classification": classification,
        "base_egp": base,
        "discount_type": discount_type,
        "discount_value": discount,
        "discount_reason": reason,
        "is_free": net == 0,
        "net_egp": net,
        "pricing_version": pricing_version,
        "creation_standard_base_egp": standard,
        "entitlement_provenance": provenance,
    }
