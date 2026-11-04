"""Calendar-day qualification rules for Doctor-authorized follow-up care."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")


def cairo_completion_date(completed_at: datetime) -> date:
    if completed_at.utcoffset() is None:
        raise ValueError("Completion instant must include a timezone")
    return completed_at.astimezone(CAIRO).date()


def current_last_day_inclusive(policy: dict) -> int:
    """Read the persisted clinic window; reject corrupt policy instead of guessing."""
    value = policy.get("last_day_inclusive")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError("Invalid FOLLOWUP last-day policy")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("Invalid FOLLOWUP last-day policy")
    if not 0 <= value <= 365 or value != int(value):
        raise ValueError("Invalid FOLLOWUP last-day policy")
    return int(value)


def default_deadline(completion_date: date, last_day_inclusive: int) -> date:
    if not 0 <= last_day_inclusive <= 365:
        raise ValueError("Invalid FOLLOWUP last-day policy")
    return completion_date + timedelta(days=last_day_inclusive)


def intended_date_allowed(completion: date, intended: date, deadline: date) -> bool:
    return completion <= intended <= deadline


def validate_exception_deadline(completion: date, deadline: date, reason: str) -> date:
    if deadline < completion or not reason.strip():
        raise ValueError("Doctor deadline requires a valid date and reason")
    return deadline


def extension_must_not_shorten(prior: date, new: date) -> date:
    if new < prior:
        raise ValueError("Follow-up extension cannot shorten the previous deadline")
    return new
