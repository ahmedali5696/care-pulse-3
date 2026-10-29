"""Pure half-open UTC intervals and simultaneous occupancy."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Interval:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        for instant in (self.starts_at, self.ends_at):
            if not isinstance(instant, datetime) or instant.utcoffset() != timedelta(0):
                raise ValueError("Interval endpoints must be timezone-aware UTC")
        if self.starts_at >= self.ends_at:
            raise ValueError("Interval must have positive duration")


def intersects(first: Interval, second: Interval) -> bool:
    return first.starts_at < second.ends_at and second.starts_at < first.ends_at


def peak_occupancy(intervals: Iterable[Interval]) -> int:
    # an added 09:00–10:00 interval can coexist with separate 09:00–09:30 and 09:30–10:00 reservations: peak is two, not three
    endpoints = sorted(
        endpoint
        for interval in intervals
        for endpoint in ((interval.starts_at, 1), (interval.ends_at, -1))
    )
    occupancy = peak = 0
    for _, change in endpoints:
        occupancy += change
        peak = max(peak, occupancy)
    return peak
