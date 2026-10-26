"""Patient identity validation shared by persistence and the future registry API."""

import re
from datetime import date
from decimal import Decimal

_EGYPT_MOBILE = re.compile(r"01[0125][0-9]{8}\Z")
_INTERNATIONAL = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_PHONE_SEPARATORS = re.compile(r"[\s().-]")


def validate_name_parts(parts: object) -> tuple[str, ...]:
    if (
        not isinstance(parts, (tuple, list))
        or len(parts) not in (3, 4)
        or any(not isinstance(part, str) or not part.strip() for part in parts)
    ):
        raise ValueError("Patient name requires exactly three or four nonempty parts")
    return tuple(parts)


def normalize_phone(phone_raw: str | None) -> str | None:
    """Keep raw contact separately; derive E.164 only from unambiguous input."""
    if phone_raw is None:
        return None
    if not isinstance(phone_raw, str):
        raise TypeError("Invalid phone input")
    compact = _PHONE_SEPARATORS.sub("", phone_raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if _EGYPT_MOBILE.fullmatch(compact):
        return "+20" + compact[1:]
    return compact if _INTERNATIONAL.fullmatch(compact) else None


def validate_age_evidence(
    date_of_birth: date | None,
    declared_age: Decimal | int | None,
    age_reference_date: date | None,
    current_date: date,
) -> None:
    if (
        type(current_date) is not date
        or (date_of_birth is not None and type(date_of_birth) is not date)
        or (age_reference_date is not None and type(age_reference_date) is not date)
    ):
        raise ValueError("Invalid patient date")
    if date_of_birth is not None:
        if declared_age is not None or age_reference_date is not None:
            raise ValueError("Date of birth and declared age are mutually exclusive")
        if date_of_birth > current_date:
            raise ValueError("Date of birth cannot be in the future")
        return
    if declared_age is None or age_reference_date is None:
        raise ValueError("Declared age requires its reference date")
    if type(declared_age) not in (int, Decimal):
        raise ValueError("Invalid declared age")
    age = Decimal(declared_age)
    if not age.is_finite() or age < 0 or age_reference_date > current_date:
        raise ValueError("Invalid declared age or reference date")
