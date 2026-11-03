"""Calendar-day qualification rules for Doctor-authorized follow-up care."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")


def cairo_completion_date(completed_at: datetime) -> date:
    if completed_at.utcoffset() is None:
        raise ValueError("Completion instant must include a timezone")
    return completed_at.astimezone(CAIRO).date()


def default_deadline(completion_date: date) -> date:
    return completion_date + timedelta(days=7)


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
