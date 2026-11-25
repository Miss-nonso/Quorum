"""Cron expressions evaluated in an IANA time zone, with explicit DST rules.

Syntax: the standard five fields ``minute hour day-of-month month day-of-week``
with ``*``, lists (``1,15``), ranges (``9-17``), steps (``*/15``, ``10-50/20``),
month and weekday names (``JAN``, ``MON``; Sunday is 0 or 7), and the macros
``@hourly @daily @midnight @weekly @monthly @yearly @annually``. As in Vixie
cron, when both day-of-month and day-of-week are restricted a day matches if
*either* does.

A schedule describes *local wall-clock* times. Converting them to instants:

* **Skipped local times** (spring forward, e.g. 02:30 on the day New York
  jumps from 02:00 to 03:00) fire at the next valid instant, which is the
  moment the clock jumps (03:00 local). Several skipped times in one gap
  collapse into that single firing.
* **Repeated local times** (fall back, e.g. 01:30 happening twice) fire
  once, on the first occurrence.

All results are timezone-aware UTC datetimes.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
WEEKDAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
# Leap-day schedules can wait up to 8 years (e.g. 2096 -> 2104 has no Feb 29).
SEARCH_HORIZON = timedelta(days=366 * 9)


class CronError(ValueError):
    """The expression or time zone is invalid, or the schedule never fires."""


@dataclass(frozen=True, slots=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, day: datetime) -> bool:
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        return dom and dow


def _parse_field(
    text: str, lo: int, hi: int, names: dict[str, int] | None = None
) -> tuple[frozenset[int], bool]:
    """Parse one field; return (allowed values, restricted?)."""

    def value(token: str) -> int:
        token = token.upper()
        if names and token in names:
            return names[token]
        if not token.isdigit():
            raise CronError(f"invalid value {token!r}")
        return int(token)

    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty list item in {text!r}")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"invalid step {step_text!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = value(a), value(b)
        else:
            start = value(base)
            end = hi if step_text else start
        if not (lo <= start <= hi and lo <= end <= hi):
            raise CronError(f"{part!r} is outside {lo}-{hi}")
        if start > end:
            raise CronError(f"range {part!r} is backwards")
        values.update(range(start, end + 1, step))
    # Vixie cron: a day field counts as restricted unless it starts with "*".
    return frozenset(values), not text.startswith("*")


def parse_cron(expression: str) -> CronSchedule:
    """Parse ``expression``; raise :class:`CronError` with a readable reason."""
    normalized = MACROS.get(expression.strip().lower(), expression.strip())
    fields = normalized.split()
    if len(fields) != 5:
        raise CronError("expected 5 fields: minute hour day-of-month month day-of-week")
    try:
        minutes, _ = _parse_field(fields[0], 0, 59)
        hours, _ = _parse_field(fields[1], 0, 23)
        days, dom_restricted = _parse_field(fields[2], 1, 31)
        months, _ = _parse_field(fields[3], 1, 12, MONTHS)
        weekdays_raw, dow_restricted = _parse_field(fields[4], 0, 7, WEEKDAYS)
    except CronError as exc:
        raise CronError(f"{expression!r}: {exc}") from None
    weekdays = frozenset(0 if d == 7 else d for d in weekdays_raw)
    return CronSchedule(
        expression=expression,
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
    )


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"unknown time zone {name!r}") from None


def _local_instant(naive: datetime, zone: ZoneInfo) -> datetime:
    """Map a local wall-clock time to the UTC instant it fires at (DST rules above)."""
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first.astimezone(UTC)  # an ordinary, unambiguous local time
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
        return first.astimezone(UTC)  # repeated (fall back): the first occurrence
    # Skipped (spring forward): fire at the transition, i.e. the first instant
    # after the gap. It lies between the two fold interpretations; bisect it.
    lo, hi = sorted((first.astimezone(UTC), second.astimezone(UTC)))
    target = hi.astimezone(zone).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(zone).utcoffset() == target:
            hi = mid
        else:
            lo = mid
    return hi.replace(microsecond=0)


def _next_candidate(schedule: CronSchedule, t: datetime, limit: datetime) -> datetime | None:
    """The earliest naive local time >= ``t`` matching every field, or None past ``limit``."""
    while t <= limit:
        if t.month not in schedule.months:
            year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
            t = t.replace(year=year, month=month, day=1, hour=0, minute=0)
            continue
        if not schedule.matches_day(t):
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in schedule.hours:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in schedule.minutes:
            t += timedelta(minutes=1)
            continue
        return t
    return None


def next_fire(schedule: CronSchedule, zone: ZoneInfo, after: datetime) -> datetime:
    """The first instant strictly after ``after`` (aware) at which the schedule fires.

    Raises:
        CronError: the schedule never fires (e.g. ``0 0 30 2 *``).
    """
    if after.tzinfo is None:
        raise ValueError("`after` must be timezone-aware")
    after = after.astimezone(UTC)
    local = after.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
    t = local
    limit = local + SEARCH_HORIZON
    while True:
        candidate = _next_candidate(schedule, t, limit)
        if candidate is None:
            break
        t = candidate
        instant = _local_instant(t, zone)
        if instant > after:
            return instant
        t += timedelta(minutes=1)
    raise CronError(f"{schedule.expression!r} never fires")


def validate_schedule(expression: str, timezone: str, now: datetime) -> datetime:
    """Parse and check a schedule; return its first fire time after ``now``."""
    return next_fire(parse_cron(expression), load_zone(timezone), now)
