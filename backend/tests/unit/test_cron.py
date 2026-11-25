"""Cron parsing and next-fire computation, including DST in three zones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conductor.domain.cron import CronError, load_zone, next_fire, parse_cron, validate_schedule
from conductor.domain.priority import aging_bonus, effective_priority, worst_case_wait_s

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
LAGOS = ZoneInfo("Africa/Lagos")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def fires(expr: str, zone: ZoneInfo, start: datetime, count: int) -> list[datetime]:
    schedule = parse_cron(expr)
    out, t = [], start
    for _ in range(count):
        t = next_fire(schedule, zone, t)
        out.append(t)
    return out


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("expr", "field", "expected"),
    [
        ("*/15 * * * *", "minutes", {0, 15, 30, 45}),
        ("0 9-17/4 * * *", "hours", {9, 13, 17}),
        ("0 0 * JAN,jul *", "months", {1, 7}),
        ("0 0 * * MON-FRI", "weekdays", {1, 2, 3, 4, 5}),
        ("0 0 * * 7", "weekdays", {0}),  # 7 is Sunday too
        ("@hourly", "minutes", {0}),
    ],
)
def test_field_parsing(expr: str, field: str, expected: set[int]) -> None:
    assert getattr(parse_cron(expr), field) == expected


@pytest.mark.parametrize(
    ("expr", "reason"),
    [
        ("* * * *", "expected 5 fields"),
        ("60 * * * *", "outside 0-59"),
        ("* 24 * * *", "outside 0-23"),
        ("* * 0 * *", "outside 1-31"),
        ("* * * 13 *", "outside 1-12"),
        ("*/0 * * * *", "invalid step"),
        ("5-1 * * * *", "backwards"),
        ("a * * * *", "invalid value"),
        ("1,,2 * * * *", "empty list item"),
    ],
)
def test_invalid_expressions_are_explained(expr: str, reason: str) -> None:
    with pytest.raises(CronError, match=reason):
        parse_cron(expr)


def test_schedules_that_never_fire_are_rejected() -> None:
    with pytest.raises(CronError, match="never fires"):
        validate_schedule("0 0 30 2 *", "UTC", utc(2026, 1, 1))


def test_unknown_zone_is_rejected() -> None:
    with pytest.raises(CronError, match="unknown time zone"):
        load_zone("Mars/Olympus_Mons")


def test_naive_after_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire(parse_cron("* * * * *"), NY, datetime(2026, 1, 1))


# ------------------------------------------------------------------ ordinary schedules


def test_day_of_month_or_day_of_week_when_both_restricted() -> None:
    # 1st of the month OR any Monday (Vixie semantics).
    got = fires("0 12 1 * MON", ZoneInfo("UTC"), utc(2026, 6, 1, 13), 4)
    assert got == [
        utc(2026, 6, 8, 12),
        utc(2026, 6, 15, 12),
        utc(2026, 6, 22, 12),
        utc(2026, 6, 29, 12),
    ]


def test_star_step_in_day_of_month_counts_as_unrestricted() -> None:
    # "*/2" starts with "*": only the weekday restriction applies.
    got = fires("0 0 */2 * SUN", ZoneInfo("UTC"), utc(2026, 6, 1), 2)
    assert all(d.isoweekday() == 7 for d in got)


def test_leap_day_schedule_waits_for_the_next_leap_year() -> None:
    assert fires("0 0 29 2 *", ZoneInfo("UTC"), utc(2026, 1, 1), 1) == [utc(2028, 2, 29)]


def test_next_fire_is_strictly_after() -> None:
    schedule = parse_cron("30 9 * * *")
    assert next_fire(schedule, ZoneInfo("UTC"), utc(2026, 5, 1, 9, 30)) == utc(2026, 5, 2, 9, 30)


# ------------------------------------------------------------------ DST: America/New_York
# 2026-03-08 02:00 EST -> 03:00 EDT (07:00 UTC); 2026-11-01 02:00 EDT -> 01:00 EST (06:00 UTC).


def test_new_york_skipped_time_fires_at_the_jump() -> None:
    assert fires("30 2 * * *", NY, utc(2026, 3, 7, 12), 3) == [
        utc(2026, 3, 8, 7, 0),  # 02:30 does not exist; fires at 03:00 EDT
        utc(2026, 3, 9, 6, 30),  # 02:30 EDT
        utc(2026, 3, 10, 6, 30),
    ]


def test_new_york_several_skipped_times_collapse_into_one_firing() -> None:
    got = fires("*/15 2 * * *", NY, utc(2026, 3, 8, 6, 0), 2)
    assert got == [utc(2026, 3, 8, 7, 0), utc(2026, 3, 9, 6, 0)]


def test_new_york_repeated_time_fires_once_on_first_occurrence() -> None:
    assert fires("30 1 * * *", NY, utc(2026, 10, 31, 12), 3) == [
        utc(2026, 11, 1, 5, 30),  # 01:30 EDT, the first of two 01:30s
        utc(2026, 11, 2, 6, 30),  # next day, 01:30 EST
        utc(2026, 11, 3, 6, 30),
    ]


def test_new_york_repeated_hour_with_minute_steps_is_not_doubled() -> None:
    got = fires("*/30 1 * * *", NY, utc(2026, 11, 1, 4, 0), 3)
    assert got == [utc(2026, 11, 1, 5, 0), utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 0)]


def test_new_york_starting_inside_the_repeated_hour_skips_its_past_occurrence() -> None:
    # 06:10 UTC is 01:10 EST, the *second* pass through 01:xx: 01:30 already fired.
    assert fires("30 1 * * *", NY, utc(2026, 11, 1, 6, 10), 1) == [utc(2026, 11, 2, 6, 30)]


def test_new_york_hourly_across_spring_forward_has_no_gap_firing_twice() -> None:
    got = fires("0 * * * *", NY, utc(2026, 3, 8, 5, 30), 3)
    # 01:00 EST (06:00Z), then 02:00 is skipped -> 03:00 EDT (07:00Z), then 04:00 EDT.
    assert got == [utc(2026, 3, 8, 6), utc(2026, 3, 8, 7), utc(2026, 3, 8, 8)]


# ------------------------------------------------------------------ DST: Europe/London
# 2026-03-29 01:00 GMT -> 02:00 BST; 2026-10-25 02:00 BST -> 01:00 GMT.


def test_london_skipped_time_fires_at_the_jump() -> None:
    assert fires("30 1 * * *", LONDON, utc(2026, 3, 28, 12), 2) == [
        utc(2026, 3, 29, 1, 0),  # 01:30 does not exist; the clock reads 02:00 BST at 01:00Z
        utc(2026, 3, 30, 0, 30),  # 01:30 BST
    ]


def test_london_repeated_time_fires_once() -> None:
    assert fires("30 1 * * *", LONDON, utc(2026, 10, 24, 12), 2) == [
        utc(2026, 10, 25, 0, 30),  # first 01:30 (BST)
        utc(2026, 10, 26, 1, 30),  # GMT
    ]


# ------------------------------------------------------------------ Africa/Lagos (no DST)


def test_lagos_has_no_transitions_so_times_are_a_fixed_offset() -> None:
    got = fires("0 9 * * *", LAGOS, utc(2026, 3, 1), 3)
    assert got == [utc(2026, 3, 1, 8), utc(2026, 3, 2, 8), utc(2026, 3, 3, 8)]
    # Across the dates when New York and London shift, Lagos does not.
    assert fires("0 9 * * *", LAGOS, utc(2026, 10, 24, 12), 2) == [
        utc(2026, 10, 25, 8),
        utc(2026, 10, 26, 8),
    ]


# ------------------------------------------------------------------ properties

simple_fields = st.tuples(
    st.sampled_from(["*", "0", "*/7", "5,35", "10-20"]),
    st.sampled_from(["*", "3", "*/5", "0-2"]),
    st.sampled_from(["*", "1", "15", "*/3"]),
    st.sampled_from(["*", "2", "3,11"]),
    st.sampled_from(["*", "0", "1-5"]),
).map(" ".join)

instants = st.datetimes(
    min_value=datetime(2026, 1, 1),
    max_value=datetime(2027, 12, 31),
).map(lambda d: d.replace(tzinfo=UTC))


def brute_force(expr: str, after: datetime, horizon: timedelta) -> datetime | None:
    schedule = parse_cron(expr)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = after + horizon
    while t <= end:
        if (
            t.minute in schedule.minutes
            and t.hour in schedule.hours
            and t.month in schedule.months
            and schedule.matches_day(t)
        ):
            return t
        t += timedelta(minutes=1)
    return None


@settings(max_examples=60, deadline=None)
@given(simple_fields, instants)
def test_utc_matches_a_minute_by_minute_scan(expr: str, after: datetime) -> None:
    expected = brute_force(expr, after, timedelta(days=400))
    if expected is not None:
        assert next_fire(parse_cron(expr), ZoneInfo("UTC"), after) == expected


@settings(max_examples=150, deadline=None)
@given(simple_fields, instants, st.sampled_from([NY, LONDON, LAGOS]))
def test_firings_are_strictly_increasing_and_unique(
    expr: str, after: datetime, zone: ZoneInfo
) -> None:
    schedule = parse_cron(expr)
    first = next_fire(schedule, zone, after)
    second = next_fire(schedule, zone, first)
    assert after < first < second


@settings(max_examples=150, deadline=None)
@given(simple_fields, instants, st.sampled_from([NY, LONDON]))
def test_non_gap_firings_land_on_a_matching_local_time(
    expr: str, after: datetime, zone: ZoneInfo
) -> None:
    schedule = parse_cron(expr)
    fired = next_fire(schedule, zone, after).astimezone(zone)
    matches = (
        fired.minute in schedule.minutes
        and fired.hour in schedule.hours
        and fired.month in schedule.months
        and schedule.matches_day(fired)
    )
    # The only exception: a skipped local time, which fires at the jump (minute 0).
    jumped = fired.minute == 0 and (fired - timedelta(minutes=1)).utcoffset() != fired.utcoffset()
    assert matches or jumped


# ------------------------------------------------------------------ priority aging


def test_bonus_grows_with_waiting_and_is_capped() -> None:
    assert [aging_bonus(w, interval_s=10, max_bonus=3) for w in (-1, 0, 9.9, 10, 25, 1e9)] == [
        0,
        0,
        0,
        1,
        2,
        3,
    ]
    assert effective_priority(-5, 30, interval_s=10, max_bonus=200) == -2


def test_worst_case_wait_bounds_starvation() -> None:
    assert worst_case_wait_s(0, 10, interval_s=1, max_bonus=200) == 11
    assert worst_case_wait_s(0, 10, interval_s=1, max_bonus=10) is None, "cap too small"
    assert worst_case_wait_s(10, 0, interval_s=1, max_bonus=1) == 0


@given(
    st.integers(min_value=-100, max_value=100),
    st.floats(min_value=0, max_value=1e6),
    st.floats(min_value=0, max_value=1e6),
)
def test_effective_priority_never_decreases_while_waiting(p: int, a: float, b: float) -> None:
    lo, hi = sorted((a, b))
    assert effective_priority(p, lo, interval_s=5, max_bonus=200) <= effective_priority(
        p, hi, interval_s=5, max_bonus=200
    )
