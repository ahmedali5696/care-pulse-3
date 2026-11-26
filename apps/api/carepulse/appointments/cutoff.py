"""Resolve the configured cutoff for an original Cairo service day."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from carepulse.policies.schedule import NoShowCutoff

_CAIRO = ZoneInfo("Africa/Cairo")


def _valid_instants(wall: datetime) -> list[datetime]:
    candidates = [
        wall.replace(tzinfo=_CAIRO, fold=fold).astimezone(UTC) for fold in (0, 1)
    ]
    return [
        instant
        for instant in candidates
        if instant.astimezone(_CAIRO).replace(tzinfo=None) == wall
    ]


def resolve_cutoff(service_date: date, policy: NoShowCutoff) -> datetime | None:
    """Return the later repeated instant or the first valid instant after a gap."""
    if not policy.enabled or policy.local_time is None:
        return None
    wall = datetime.combine(service_date, policy.local_time)
    candidates = _valid_instants(wall)
    if candidates:
        return max(candidates)

    # The forward round-trip bounds the gap; search at timezone transition
    # precision rather than shifting the cutoff by the full offset change.
    forward = max(
        wall.replace(tzinfo=_CAIRO, fold=fold)
        .astimezone(UTC)
        .astimezone(_CAIRO)
        .replace(tzinfo=None)
        for fold in (0, 1)
    )
    low, high = 0, int((forward - wall).total_seconds())
    while high - low > 1:
        midpoint = (low + high) // 2
        if _valid_instants(wall + timedelta(seconds=midpoint)):
            high = midpoint
        else:
            low = midpoint
    return max(_valid_instants(wall + timedelta(seconds=high)))


def cutoff_due(service_date: date, policy: NoShowCutoff, now: datetime) -> bool:
    if now.utcoffset() is None:
        raise ValueError("Cutoff comparison requires an aware instant")
    cutoff = resolve_cutoff(service_date, policy)
    return cutoff is not None and now.astimezone(UTC) >= cutoff


def apply_selected_cutoff(
    engine: Engine, organization_id: UUID, service_date: date, policy_version: int
) -> int:
    """Process one bounded selection, committing each authorized record separately."""
    scope = {
        "tenant": organization_id,
        "day": service_date,
        "version": policy_version,
    }
    with engine.begin() as connection:
        candidates = connection.execute(
            text(
                "SELECT * FROM public.f01_b20_cutoff_candidates(:tenant,:day,:version)"
            ),
            scope,
        ).all()
    for target, revision, scope_token in candidates:
        with engine.begin() as connection:
            connection.scalar(
                text("""SELECT public.f01_b20_cutoff_appointment(
                    :tenant,:day,:version,:target,:revision,:token)"""),
                {**scope, "target": target, "revision": revision, "token": scope_token},
            )
    return len(candidates)
