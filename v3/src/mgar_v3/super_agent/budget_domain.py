"""Signed budget configuration and explicit UTC windows; no execution authority."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from mgar_v3.domain import Model
from mgar_v3.sources.domain import OpaqueId

MAX_BUDGET_UNITS = (1 << 63) - 1
BudgetUnits = Annotated[int, Field(strict=True, ge=0, le=MAX_BUDGET_UNITS)]
BudgetId = Annotated[OpaqueId, Field(max_length=200)]
ScopeType = Literal["organization", "project"]
Component = Literal["router", "worker", "verification", "recall", "subtask"]
LedgerKind = Literal["usage", "router"]
QuotaKind = Literal["candidate", "queue", "slot", "source", "notification"]
Period = Literal["hour", "day", "minute"]


class BudgetCaps(Model):
    total_micros: BudgetUnits = 0
    hour_micros: BudgetUnits = 0
    day_micros: BudgetUnits = 0
    total_tokens: BudgetUnits = 0
    hour_tokens: BudgetUnits = 0
    day_tokens: BudgetUnits = 0
    router_day_micros: BudgetUnits = 0
    run_micros: BudgetUnits = 0
    run_tokens: BudgetUnits = 0


class BudgetQuotas(Model):
    candidates_hour: BudgetUnits = 0
    queue: BudgetUnits = 0
    slots: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    source_minute: BudgetUnits = 0
    notifications_hour: BudgetUnits = 0


class PriceQuote(Model):
    model_id: BudgetId
    price_version: BudgetId
    input_micros_per_million: BudgetUnits
    output_micros_per_million: BudgetUnits


class BudgetDefinition(Model):
    enabled: bool = Field(default=False, strict=True)
    organization_id: BudgetId | None = None
    timezone: Annotated[str, Field(strict=True, min_length=1, max_length=100)] = "Asia/Shanghai"
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    prices: Annotated[tuple[PriceQuote, ...], Field(max_length=100)] = ()
    caps: BudgetCaps = Field(default_factory=BudgetCaps)
    quotas: BudgetQuotas = Field(default_factory=BudgetQuotas)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("A known IANA timezone is required") from None
        return value

    @field_validator("valid_from", "valid_until", mode="before")
    @classmethod
    def explicit_timestamp(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("An explicit timezone-aware ISO timestamp is required") from None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("An explicit timezone-aware timestamp is required")
        return value

    @model_validator(mode="after")
    def coherent_definition(self) -> Self:
        if self.valid_until.astimezone(UTC) <= self.valid_from.astimezone(UTC):
            raise ValueError("Budget validity must be a nonempty increasing interval")
        price_keys = [(p.model_id, p.price_version) for p in self.prices]
        if len(price_keys) != len(set(price_keys)):
            raise ValueError("A model and price version must identify one explicit price")
        if self.enabled and any(self.caps.model_dump().values()) and not self.prices:
            raise ValueError("Enabled nonzero budget caps require a signed price list")
        return self


class BudgetSave(Model):
    expected_revision: BudgetUnits
    definition: BudgetDefinition
    review_ref: BudgetId


def _wall_candidates(wall: datetime, zone: ZoneInfo) -> list[datetime]:
    """Return genuine instants for a wall boundary; do not invent a gap's time."""
    values = set()
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == wall:
            values.add(candidate)
    return sorted(values)


def _transition(start: datetime, end: datetime, zone: ZoneInfo) -> datetime | None:
    """Find an offset transition between nearby wall-hour boundaries, if present."""
    offset = start.astimezone(zone).utcoffset()
    if offset == end.astimezone(zone).utcoffset():
        return None
    while end - start > timedelta(microseconds=1):
        middle = start + (end - start) / 2
        if middle.astimezone(zone).utcoffset() == offset:
            start = middle
        else:
            end = middle
    return end


def window_bounds(at: datetime, timezone: str, period: Period) -> tuple[datetime, datetime]:
    """Return the containing UTC half-open local period, preserving repeated folds.

    Local days end at the next local midnight, so they can last 23 or 25 hours.
    Hour/minute boundaries also split at offset transitions. This handles partial
    hour transitions such as Lord Howe's repeated half hour without overlapping
    windows or assigning a genuine instant to a nonexistent local hour.
    """
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("A timezone-aware instant is required")
    if period not in {"hour", "day", "minute"}:
        raise ValueError("Unknown budget window period")
    zone = ZoneInfo(timezone)
    instant = at.astimezone(UTC)
    local = instant.astimezone(zone)
    wall = local.replace(tzinfo=None, second=0, microsecond=0)
    if period == "day":
        midnight = wall.replace(hour=0, minute=0)
        # A midnight in a gap maps to the first valid instant of that local day.
        start = midnight.replace(tzinfo=zone).astimezone(UTC)
        end = (midnight + timedelta(days=1)).replace(tzinfo=zone).astimezone(UTC)
        if not start <= instant < end:
            raise ValueError("Local day could not be represented conservatively")
        return start, end
    step = timedelta(hours=1) if period == "hour" else timedelta(minutes=1)
    if period == "hour":
        wall = wall.replace(minute=0)
    candidates = [
        value
        for boundary in (wall - step, wall, wall + step, wall + step * 2)
        for value in _wall_candidates(boundary, zone)
    ]
    # A minute immediately before a DST jump may have no valid next wall
    # boundary nearby. The instant's offset supplies bounded candidates; any
    # transition inside them is split below rather than treated as extra time.
    offset = local.utcoffset()
    assert offset is not None
    projected = wall.replace(tzinfo=UTC) - offset
    candidates.extend((projected, projected + step))
    start = max(value for value in candidates if value <= instant)
    end = min(value for value in candidates if value > instant)
    transition = _transition(start, end, zone)
    if transition is not None:
        if transition <= instant:
            start = max(start, transition)
        else:
            end = min(end, transition)
    return start, end
