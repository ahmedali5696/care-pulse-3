"""Pure EGP pricing, shared by advisory quotes and frozen confirmations."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, Inexact, localcontext

PENNY = Decimal("0.01")
MONEY_PATTERN = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]{1,2})?\Z")
PERCENT_PATTERN = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?\Z")


def require_numeric_money(value: Decimal) -> None:
    # PostgreSQL unconstrained NUMERIC stores at most 131072 integer digits.
    if value and value.adjusted() >= 131_072:
        raise ValueError("EGP amount exceeds PostgreSQL NUMERIC range")


def parse_money(value: str) -> Decimal:
    if not isinstance(value, str) or MONEY_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid EGP decimal string")
    amount = Decimal(value)
    require_numeric_money(amount)
    return amount


def parse_percent(value: str) -> Decimal:
    if not isinstance(value, str) or PERCENT_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid percentage decimal string")
    parsed = Decimal(value)
    if parsed > 100:
        raise ValueError("Percentage exceeds 100")
    return parsed


def _money(value: str | Decimal) -> Decimal:
    if isinstance(value, str):
        return parse_money(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("Invalid EGP amount")
    require_numeric_money(value)
    # Decimal can originate from a legacy JSON number in a trusted policy row.
    with localcontext() as ctx:
        ctx.prec = max(28, len(format(value, "f")) + 8)
        if value != value.quantize(PENNY):
            raise ValueError("Invalid EGP fractional scale")
    return value


def _percent(value: str | Decimal) -> Decimal:
    if isinstance(value, str):
        return parse_percent(value)
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        or value > 100
    ):
        raise ValueError("Invalid percentage")
    return value


def exact_net(base: Decimal, discount_type: str, discount_value: Decimal) -> Decimal:
    """Keep every intermediate digit; round the final nonnegative net once."""
    if discount_type not in ("NONE", "FIXED", "PERCENT"):
        raise ValueError("Invalid discount type")
    if not isinstance(base, Decimal) or not isinstance(discount_value, Decimal):
        raise TypeError("Decimal operands required")
    if (
        not base.is_finite()
        or not discount_value.is_finite()
        or min(base, discount_value) < 0
    ):
        raise ValueError("Invalid amount")
    if discount_type == "NONE" and discount_value != 0:
        raise ValueError("NONE discount must be zero")
    if discount_type == "FIXED" and discount_value > base:
        raise ValueError("Fixed discount exceeds base")
    if discount_type == "PERCENT" and discount_value > 100:
        raise ValueError("Percentage exceeds 100")
    base_text, discount_text = format(base, "f"), format(discount_value, "f")
    precision = len(base_text) + len(discount_text) + 8
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = True
        deduction = (
            base * discount_value / Decimal(100)
            if discount_type == "PERCENT"
            else (discount_value if discount_type == "FIXED" else Decimal(0))
        )
        unrounded = base - deduction
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = False
        return unrounded.quantize(PENNY, rounding=ROUND_HALF_UP)


def validate_discount_reason(discount_type: str, discount_reason: str | None) -> None:
    if discount_type == "NONE":
        if discount_reason is not None:
            raise ValueError("NONE discount cannot have a reason")
    elif discount_type in ("FIXED", "PERCENT"):
        if not isinstance(discount_reason, str) or not discount_reason.strip():
            raise ValueError("A discretionary discount requires a reason")
        if "\x00" in discount_reason:
            raise ValueError("Invalid discount reason characters")
        try:
            discount_reason.encode("utf-8")
        except UnicodeEncodeError as invalid:
            raise ValueError("Invalid discount reason characters") from invalid
    else:
        raise ValueError("Invalid discount type")


@dataclass(frozen=True)
class Quote:
    service_type: str
    base_egp: Decimal
    discount_type: str
    discount_value: Decimal
    is_free: bool
    net_egp: Decimal


def quote_price(
    base: str | Decimal,
    standard_base: str | Decimal,
    service_type: str,
    classification: str,
    discount_type: str,
    discount_value: str | Decimal,
) -> Quote:
    if service_type not in ("NEW_EXAMINATION", "FOLLOW_UP"):
        raise ValueError("Invalid service type")
    if classification not in ("ROUTINE", "URGENT", "EMERGENCY"):
        raise ValueError("Invalid classification")
    if service_type == "FOLLOW_UP" and classification != "ROUTINE":
        raise ValueError("Follow-up must be Routine")
    amount, standard = _money(base), _money(standard_base)
    if (
        service_type == "NEW_EXAMINATION"
        and classification != "URGENT"
        and amount != standard
    ):
        raise ValueError("Standard New Examination must use its configured base")
    value = (
        _percent(discount_value)
        if discount_type == "PERCENT"
        else _money(discount_value)
    )
    net = exact_net(amount, discount_type, value)
    if classification == "URGENT" and (amount <= standard or net <= standard):
        raise ValueError("Urgent premium must exceed standard base")
    return Quote(service_type, amount, discount_type, value, net == 0, net)
