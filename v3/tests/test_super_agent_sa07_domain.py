"""Budget units and civil-time windows are explicit before runtime accounting."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from mgar_v3.super_agent.budget_domain import (
    MAX_BUDGET_UNITS,
    BudgetCaps,
    BudgetDefinition,
    BudgetQuotas,
    BudgetSave,
    PriceQuote,
    window_bounds,
)

START = datetime(2026, 11, 11, tzinfo=UTC)
END = START + timedelta(days=1)
PRICE = {
    "model_id": "model-reviewed",
    "price_version": "price-reviewed-v1",
    "input_micros_per_million": 300000,
    "output_micros_per_million": 1200000,
}


@pytest.mark.parametrize("field", tuple(BudgetCaps.model_fields))
@pytest.mark.parametrize("value", [True, -1, 1.5, "1", MAX_BUDGET_UNITS + 1])
def test_caps_reject_coercion_negative_and_bigint_overflow(field, value):
    with pytest.raises(ValidationError):
        BudgetCaps.model_validate({field: value})


@pytest.mark.parametrize("field", tuple(BudgetQuotas.model_fields))
@pytest.mark.parametrize("value", [True, -1, 1.5, "1", MAX_BUDGET_UNITS + 1])
def test_quotas_reject_coercion_negative_and_bigint_overflow(field, value):
    with pytest.raises(ValidationError):
        BudgetQuotas.model_validate({field: value})


@pytest.mark.parametrize("field", ["input_micros_per_million", "output_micros_per_million"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "1", MAX_BUDGET_UNITS + 1])
def test_prices_preserve_exact_integer_units(field, value):
    with pytest.raises(ValidationError):
        PriceQuote.model_validate({**PRICE, field: value})


def test_initial_definition_is_disabled_zero_and_not_a_call_count_wallet():
    definition = BudgetDefinition(valid_from=START, valid_until=END)
    assert definition.enabled is False
    assert set(definition.caps.model_dump().values()) == {0}
    assert set(definition.quotas.model_dump().values()) == {0}
    assert definition.prices == () and definition.organization_id is None
    assert definition.timezone == "Asia/Shanghai"
    assert not any("calls" in name for name in BudgetCaps.model_fields)
    with pytest.raises(ValidationError):
        BudgetQuotas(slots=3)
    assert BudgetQuotas(slots=2).slots == 2


def test_enabled_nonzero_caps_require_explicit_unambiguous_prices():
    common = {"enabled": True, "valid_from": START, "valid_until": END, "caps": {"day_micros": 5}}
    with pytest.raises(ValidationError):
        BudgetDefinition.model_validate(common)
    price = PriceQuote.model_validate(PRICE)
    assert BudgetDefinition.model_validate({**common, "prices": [PRICE]}).prices == (price,)
    with pytest.raises(ValidationError):
        BudgetDefinition.model_validate(
            {**common, "prices": [PRICE, {**PRICE, "input_micros_per_million": 1}]}
        )
    # One price revision can list different models, each exactly once.
    both = BudgetDefinition.model_validate(
        {**common, "prices": [PRICE, {**PRICE, "model_id": "other-reviewed-model"}]}
    )
    assert len(both.prices) == 2


@pytest.mark.parametrize("value", [
    datetime(2026, 11, 11), "2026-11-11T00:00:00", 0, True,
    "0", "1800000000", "1800000000000", "-1", "1.5", "20261111", "2026-11-11",
])
def test_definition_requires_explicit_aware_timestamps(value):
    with pytest.raises(ValidationError):
        BudgetDefinition(valid_from=value, valid_until=END)


def test_definition_interval_boolean_and_timezone_are_validated():
    for before, after in ((START, START), (END, START)):
        with pytest.raises(ValidationError):
            BudgetDefinition(valid_from=before, valid_until=after)
    for enabled in (1, "true"):
        with pytest.raises(ValidationError):
            BudgetDefinition(enabled=enabled, valid_from=START, valid_until=END)
    for zone in ("", "Unknown/Timezone", "../UTC"):
        with pytest.raises(ValidationError):
            BudgetDefinition(timezone=zone, valid_from=START, valid_until=END)
    parsed = BudgetDefinition.model_validate(
        {"valid_from": "2026-11-11T00:00:00+08:00", "valid_until": "2026-11-12T00:00:00+08:00"}
    )
    assert parsed.valid_from.utcoffset() == timedelta(hours=8)


def test_save_binds_review_to_strict_revision_and_preserves_opaque_identity():
    definition = BudgetDefinition(
        organization_id=" Org-Case-敏感 ", valid_from=START, valid_until=END
    )
    saved = BudgetSave(expected_revision=0, definition=definition, review_ref=" Review-Case ")
    assert saved.definition.organization_id == " Org-Case-敏感 "
    assert saved.review_ref == " Review-Case "
    for revision in (True, -1, "0", 0.5, MAX_BUDGET_UNITS + 1):
        with pytest.raises(ValidationError):
            BudgetSave(expected_revision=revision, definition=definition, review_ref="review")
    for value in ("", " ", "control\x00", "x" * 201):
        with pytest.raises(ValidationError):
            BudgetSave(expected_revision=0, definition=definition, review_ref=value)


@pytest.mark.parametrize("period,duration", [("minute", 60), ("hour", 3600), ("day", 86400)])
def test_windows_use_half_open_utc_bounds_for_fractional_offset_zones(period, duration):
    at = datetime(2026, 11, 11, 8, 37, 55, tzinfo=UTC)
    start, end = window_bounds(at, "Asia/Kathmandu", period)
    assert start.tzinfo is UTC and end.tzinfo is UTC
    assert start <= at < end and (end - start).total_seconds() == duration
    assert window_bounds(end, "Asia/Kathmandu", period)[0] == end
    assert window_bounds(start, "Asia/Kathmandu", period) == (start, end)


def test_fall_back_repeated_hour_has_two_distinct_nonoverlapping_windows():
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    a, b = window_bounds(first, zone.key, "hour"), window_bounds(second, zone.key, "hour")
    assert a == (datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 6, tzinfo=UTC))
    assert b == (a[1], datetime(2026, 11, 1, 7, tzinfo=UTC))


@pytest.mark.parametrize("day,hours", [(datetime(2026, 3, 8), 23), (datetime(2026, 11, 1), 25)])
def test_local_day_uses_next_midnight_instead_of_twenty_four_utc_hours(day, hours):
    zone = ZoneInfo("America/New_York")
    at = day.replace(hour=12, tzinfo=zone)
    start, end = window_bounds(at, zone.key, "day")
    assert (end - start) == timedelta(hours=hours)
    assert start.astimezone(zone).hour == end.astimezone(zone).hour == 0
    assert start <= at.astimezone(UTC) < end


def test_spring_forward_minute_does_not_need_a_nonexistent_next_wall_boundary():
    zone = ZoneInfo("America/New_York")
    at = datetime(2026, 3, 8, 1, 59, 59, tzinfo=zone)
    start, end = window_bounds(at, zone.key, "minute")
    assert end - start == timedelta(minutes=1)
    assert end.astimezone(zone).hour == 3
    assert window_bounds(end, zone.key, "minute")[0] == end


@pytest.mark.parametrize(
    "at,expected_start,expected_end",
    [
        ("2026-04-04T14:45:00+00:00", "2026-04-04T14:00:00+00:00", "2026-04-04T15:00:00+00:00"),
        ("2026-04-04T15:15:00+00:00", "2026-04-04T15:00:00+00:00", "2026-04-04T15:30:00+00:00"),
        ("2026-10-03T15:45:00+00:00", "2026-10-03T15:30:00+00:00", "2026-10-03T16:00:00+00:00"),
    ],
)
def test_half_hour_dst_transition_does_not_overlap_or_invent_an_hour(
    at, expected_start, expected_end
):
    instant = datetime.fromisoformat(at)
    actual = window_bounds(instant, "Australia/Lord_Howe", "hour")
    assert actual == (datetime.fromisoformat(expected_start), datetime.fromisoformat(expected_end))
    assert actual[0] <= instant < actual[1]


def test_window_refuses_naive_instant_and_unknown_period():
    with pytest.raises(ValueError):
        window_bounds(datetime(2026, 11, 11), "UTC", "hour")
    with pytest.raises(ValueError):
        window_bounds(START, "UTC", "week")
