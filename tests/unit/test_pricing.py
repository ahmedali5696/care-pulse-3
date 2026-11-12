"""B-06 exact-money quote and confirmation contracts (no database)."""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from carepulse.appointments.snapshots import validate_price_snapshot
from carepulse.policies.pricing import (
    exact_net,
    parse_money,
    parse_percent,
    quote_price,
    validate_discount_reason,
)


@pytest.mark.parametrize(
    ("discount_type", "discount_value", "expected"),
    [
        ("NONE", "0", "100.00"),
        ("FIXED", "10.00", "90.00"),
        ("PERCENT", "10", "90.00"),
        ("PERCENT", "0", "100.00"),
        ("PERCENT", "100", "0.00"),
        ("PERCENT", "12.345", "87.66"),
    ],
)
def test_discount_quotes_round_once(discount_type, discount_value, expected):
    quote = quote_price(
        "100.00",
        "100.00",
        "NEW_EXAMINATION",
        "ROUTINE",
        discount_type,
        discount_value,
    )
    assert quote.net_egp == Decimal(expected)
    assert quote.discount_value == Decimal(discount_value)
    assert quote.is_free is (expected == "0.00")
    with pytest.raises(FrozenInstanceError):
        quote.net_egp = Decimal(0)


def test_half_piaster_rounds_half_up_only_at_final_net():
    quote = quote_price("10.05", "10.05", "NEW_EXAMINATION", "ROUTINE", "PERCENT", "50")
    assert quote.net_egp == Decimal("5.03")
    assert exact_net(Decimal("10.05"), "PERCENT", Decimal(50)) == Decimal("5.03")


def test_tiny_percentage_preserves_digits_beyond_default_decimal_precision():
    amount = "1234567890123456789012345678901.00"
    for base, percent, expected in (
        ("1.00", "0.500000000000000000000000000001", "0.99"),
        (amount, "0.000000000000000000000000000000000000000000000001", amount),
    ):
        quote = quote_price(
            base, base, "NEW_EXAMINATION", "ROUTINE", "PERCENT", percent
        )
        assert quote.discount_value == Decimal(percent)
        assert quote.net_egp == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "-1",
        "NaN",
        "Infinity",
        "1e2",
        "100.001",
        " 1.00",
        "+1.00",
        "01.00",
        100,
        1.0,
        None,
    ],
)
def test_money_rejects_invalid_text_and_json_numbers(raw):
    with pytest.raises(ValueError):
        parse_money(raw)


@pytest.mark.parametrize(
    "raw",
    ["-1", "NaN", "Infinity", "1e2", "100.01", " 12", "01", 12, 0.5, None],
)
def test_percent_rejects_invalid_text_and_json_numbers(raw):
    with pytest.raises(ValueError):
        parse_percent(raw)


def test_percent_has_no_fractional_digit_cap():
    raw = "0." + "0" * 100 + "1"
    assert parse_percent(raw) == Decimal(raw)
    assert quote_price(
        "1.00", "1.00", "NEW_EXAMINATION", "ROUTINE", "PERCENT", raw
    ).discount_value == Decimal(raw)


@pytest.mark.parametrize(
    ("base", "type_", "discount"),
    [
        ("100.00", "FIXED", "100.01"),
        ("0.00", "FIXED", "0.01"),
        ("100.00", "PERCENT", "100.01"),
        ("100.00", "NONE", "0.01"),
        ("100.00", "MYSTERY", "0"),
        ("-1.00", "NONE", "0"),
        ("100.001", "NONE", "0"),
        ("100.00", "PERCENT", "NaN"),
    ],
)
def test_quote_rejects_invalid_base_and_discount(base, type_, discount):
    with pytest.raises(ValueError):
        quote_price(base, "100.00", "NEW_EXAMINATION", "ROUTINE", type_, discount)


def test_free_zero_base_and_reason_free_advisory_quote():
    for kind, value in (("NONE", "0"), ("PERCENT", "20")):
        quote = quote_price("0.00", "0.00", "NEW_EXAMINATION", "ROUTINE", kind, value)
        assert quote.is_free and quote.net_egp == Decimal("0.00")
    validate_discount_reason("NONE", None)
    with pytest.raises(ValueError):
        validate_discount_reason("PERCENT", None)
    validate_discount_reason("PERCENT", "Approved courtesy")


@pytest.mark.parametrize(
    ("type_", "reason"),
    [
        ("NONE", ""),
        ("NONE", "a reason"),
        ("FIXED", None),
        ("FIXED", "  "),
        ("PERCENT", None),
        ("PERCENT", "\t"),
        ("MYSTERY", "a reason"),
    ],
)
def test_confirmation_reason_is_independently_required(type_, reason):
    with pytest.raises(ValueError):
        validate_discount_reason(type_, reason)


@pytest.mark.parametrize("type_", ["FIXED", "PERCENT"])
@pytest.mark.parametrize("reason", ["Approved\x00reduction", "Approved\ud800reduction"])
def test_confirmation_reason_rejects_postgresql_unrepresentable_characters(
    type_, reason
):
    with pytest.raises(ValueError, match="Invalid discount reason characters"):
        validate_discount_reason(type_, reason)


@pytest.mark.parametrize("type_", ["FIXED", "PERCENT"])
def test_confirmation_reason_accepts_untrimmed_nonblank_unicode(type_):
    validate_discount_reason(type_, "  مساعدة مالية  ")


@pytest.mark.parametrize("type_", ["FIXED", "PERCENT"])
def test_full_discount_is_free_and_requires_confirmation_reason(type_):
    value = "100.00" if type_ == "FIXED" else "100"
    quote = quote_price("100.00", "100.00", "NEW_EXAMINATION", "ROUTINE", type_, value)
    assert quote.is_free and quote.net_egp == Decimal(0)
    with pytest.raises(ValueError):
        validate_discount_reason(type_, "")
    validate_discount_reason(type_, "Owner-approved discretionary free visit")


@pytest.mark.parametrize(
    ("base", "standard", "type_", "discount"),
    [
        ("100.00", "100.00", "NONE", "0"),
        ("101.00", "100.00", "FIXED", "1.00"),
        ("101.00", "100.00", "PERCENT", "0.99"),
        ("120.00", "100.00", "PERCENT", "100"),
        ("120.00", "100.00", "FIXED", "120.00"),
    ],
)
def test_urgent_requires_premium_base_and_rounded_net(base, standard, type_, discount):
    with pytest.raises(ValueError):
        quote_price(base, standard, "NEW_EXAMINATION", "URGENT", type_, discount)
    assert quote_price(
        "120.00", "100.00", "NEW_EXAMINATION", "URGENT", "FIXED", "19.99"
    ).net_egp == Decimal("100.01")


def test_service_type_is_explicit_and_uses_its_own_base():
    standard = quote_price(
        "100.00", "100.00", "NEW_EXAMINATION", "ROUTINE", "NONE", "0"
    )
    followup = quote_price("30.00", "100.00", "FOLLOW_UP", "ROUTINE", "PERCENT", "10")
    assert standard.service_type == "NEW_EXAMINATION"
    assert followup.service_type == "FOLLOW_UP"
    assert (standard.base_egp, followup.base_egp, followup.net_egp) == (
        Decimal("100.00"),
        Decimal("30.00"),
        Decimal("27.00"),
    )
    for classification in ("URGENT", "EMERGENCY"):
        with pytest.raises(ValueError):
            quote_price("30.00", "100.00", "FOLLOW_UP", classification, "NONE", "0")
    with pytest.raises(ValueError):
        quote_price("30.00", "100.00", "NEW_EXAMINATION", "ROUTINE", "NONE", "0")


@pytest.mark.parametrize(
    ("service", "classification", "base", "standard", "kind", "value", "reason"),
    [
        ("NEW_EXAMINATION", "ROUTINE", "100.00", "100.00", "NONE", "0", None),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "100.00",
            "100.00",
            "FIXED",
            "100.00",
            "Authorized free",
        ),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "100.00",
            "100.00",
            "PERCENT",
            "12.345",
            "Approved",
        ),
        ("NEW_EXAMINATION", "ROUTINE", "10.05", "10.05", "PERCENT", "50", "Approved"),
        ("NEW_EXAMINATION", "URGENT", "120.00", "100.00", "NONE", "0", None),
        ("FOLLOW_UP", "ROUTINE", "30.00", "100.00", "PERCENT", "100", "Approved"),
        ("NEW_EXAMINATION", "ROUTINE", "0.00", "0.00", "NONE", "0", None),
        ("NEW_EXAMINATION", "ROUTINE", "0.00", "0.00", "PERCENT", "10", "Approved"),
        (
            "NEW_EXAMINATION",
            "ROUTINE",
            "1234567890123456789012345678901.00",
            "1234567890123456789012345678901.00",
            "PERCENT",
            "0.000000000000000000000000000000000000000000000001",
            "Approved",
        ),
    ],
)
def test_confirmed_quote_agrees_with_frozen_snapshot(
    service, classification, base, standard, kind, value, reason
):
    quote = quote_price(base, standard, service, classification, kind, value)
    validate_discount_reason(kind, reason)
    snapshot = validate_price_snapshot(
        {
            "service_type": service,
            "classification": classification,
            "base_egp": base,
            "discount_type": kind,
            "discount_value": value,
            "discount_reason": reason,
            "is_free": quote.is_free,
            "net_egp": format(quote.net_egp, ".2f"),
            "pricing_version": 1,
            "creation_standard_base_egp": standard,
            "entitlement_provenance": {"synthetic": True}
            if service == "FOLLOW_UP"
            else None,
        }
    )
    assert snapshot["net_egp"] == quote.net_egp
    assert snapshot["discount_value"] == quote.discount_value
    assert snapshot["service_type"] == quote.service_type
