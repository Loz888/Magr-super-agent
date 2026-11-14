"""At most one explicitly approved daily signal; UTC/DST and catch-up are bounded."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from mgar_v3.domain import DomainError, digest
from mgar_v3.super_agent.source_autonomy_domain import AutonomyRule


@dataclass(frozen=True)
class DueSignal:
    local_date: date
    scheduled_at: datetime
    missed_coalesced: int
    signal_sha256: str


def latest_due(
    rule: AutonomyRule,
    *,
    rule_id: str,
    signed_at: datetime,
    now: datetime,
    last_local_date: date | None,
) -> DueSignal | None:
    if any(not isinstance(t, datetime) or t.utcoffset() is None for t in (signed_at, now)):
        raise DomainError("AUTONOMY_TIME_INVALID", "Current aware server time required", 422)
    schedule = rule.schedule
    if schedule is None or rule.mode == "disabled" or now >= rule.valid_until:
        return None
    zone = ZoneInfo(schedule.timezone)
    hour, minute = map(int, schedule.at.split(":"))
    lower = max(signed_at, rule.valid_from, now - timedelta(days=31))
    today = now.astimezone(zone).date()
    candidates = []
    for offset in range(32):
        day = today - timedelta(days=offset)
        if last_local_date is not None and day <= last_local_date:
            continue
        if day.weekday() not in schedule.weekdays:
            continue
        naive = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        local = naive.replace(tzinfo=zone, fold=0)
        utc = local.astimezone(UTC)
        # Nonexistent local times are skipped; a repeated hour uses fold0 once.
        if utc.astimezone(zone).replace(tzinfo=None) != naive:
            continue
        if lower <= utc <= now and utc < rule.valid_until:
            candidates.append((day, utc))
    if not candidates:
        return None
    day, at = max(candidates, key=lambda item: item[1])
    return DueSignal(day, at, len(candidates) - 1, digest([rule_id, day.isoformat(), schedule.at]))
