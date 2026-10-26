"""D-03 reusable patient demographic input boundaries."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from carepulse.patients.demographics import (
    normalize_phone,
    validate_age_evidence,
    validate_name_parts,
)

TODAY = date(2026, 10, 26)


@pytest.mark.parametrize(
    "parts",
    [
        ["Ahmed", "Ali", "Hassan"],
        ["Ahmed", "Ali", "Hassan", "Salem"],
        ["عبد", "الرحمن", "محمد"],
    ],
)
def test_three_or_four_name_parts_preserve_exact_entered_values(parts):
    assert validate_name_parts(parts) == tuple(parts)


@pytest.mark.parametrize(
    "parts",
    [[], ["A", "B"], ["A", "B", "C", "D", "E"], ["A", " ", "C"], ["A", None, "C"]],
)
def test_invalid_name_parts_rejected(parts):
    with pytest.raises(ValueError):
        validate_name_parts(parts)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (None, None),
        ("01012345678", "+201012345678"),
        ("010 1234 5678", "+201012345678"),
        ("+20 101 234 5678", "+201012345678"),
        ("00201012345678", "+201012345678"),
        ("+44 7700 900123", "+447700900123"),
        ("shared-family-number", None),
        ("0101234", None),
        ("", None),
        ("+9999999999999999999", None),
    ],
)
def test_contact_normalizes_only_supported_unambiguous_e164(raw, normalized):
    assert normalize_phone(raw) == normalized


@pytest.mark.parametrize(
    ("dob", "age", "reference"),
    [
        (date(2000, 2, 29), None, None),
        (None, 0, TODAY),
        (None, Decimal("29.5"), date(2020, 1, 1)),
        (None, Decimal(300), TODAY),
    ],
)
def test_valid_age_alternatives_do_not_invent_dob_or_age_maximum(dob, age, reference):
    validate_age_evidence(dob, age, reference, TODAY)


@pytest.mark.parametrize(
    ("dob", "age", "reference"),
    [
        (None, None, None),
        (None, 12, None),
        (None, None, TODAY),
        (date(2000, 1, 1), 20, TODAY),
        (date(2000, 1, 1), None, TODAY),
        (TODAY + timedelta(days=1), None, None),
        (None, -1, TODAY),
        (None, Decimal("NaN"), TODAY),
        (None, Decimal("Infinity"), TODAY),
        (None, 20, TODAY + timedelta(days=1)),
        (None, float("inf"), TODAY),
    ],
)
def test_incomplete_conflicting_future_and_nonfinite_age_evidence_rejected(
    dob, age, reference
):
    with pytest.raises(ValueError):
        validate_age_evidence(dob, age, reference, TODAY)
