"""Cron schedules for `serve`: standard 5-field expressions, evaluated in the server's time zone.

    ┌──────── minute (0-59)
    │ ┌────── hour (0-23)
    │ │ ┌──── day of month (1-31)
    │ │ │ ┌── month (1-12 or jan-dec)
    │ │ │ │ ┌ day of week (0-7 or sun-sat; 0 and 7 are Sunday)
    0 8 * * mon-fri

Supports `*`, lists (`1,15`), ranges (`1-5`), steps (`*/15`, `0-30/10`) and `@hourly`, `@daily`,
`@weekly`, `@monthly`, `@yearly`. Like classic cron, when both day fields are restricted a day matches if
either matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                      "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}  # fmt: skip
DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
SEARCH_YEARS = 5


class CronError(ValueError):
    pass


def _parse_field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> set[int]:
    def value(token: str) -> int:
        token = token.lower()
        if names and token in names:
            return names[token]
        if not token.isdigit():
            raise CronError(f"'{token}' is not a number")
        return int(token)

    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError("empty list item")
        rng, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step '/{step_text}'")
            step = int(step_text)
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = value(a), value(b)
        else:
            start = end = value(rng)
            if step_text:
                end = high
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise CronError(f"'{part}' is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        text = ALIASES.get(expression.strip().lower(), expression.strip())
        fields = text.split()
        if len(fields) != 5:
            raise CronError(f"'{expression}' needs 5 fields: minute hour day-of-month month day-of-week")
        try:
            minutes = _parse_field(fields[0], 0, 59)
            hours = _parse_field(fields[1], 0, 23)
            days = _parse_field(fields[2], 1, 31)
            months = _parse_field(fields[3], 1, 12, MONTHS)
            weekdays = {d % 7 for d in _parse_field(fields[4], 0, 7, DAYS)}
        except CronError as exc:
            raise CronError(f"invalid cron '{expression}': {exc}") from None
        return cls(
            expression=expression.strip(),
            minutes=frozenset(minutes),
            hours=frozenset(hours),
            days=frozenset(days),
            months=frozenset(months),
            weekdays=frozenset(weekdays),
            day_restricted=fields[2] != "*",
            weekday_restricted=fields[4] != "*",
        )

    def _day_matches(self, t: datetime) -> bool:
        dom = t.day in self.days
        dow = (t.isoweekday() % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime:
        """The first matching minute strictly after `after`, as an aware datetime in `tz`."""
        t = after.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        limit = t + timedelta(days=366 * SEARCH_YEARS)
        while t < limit:
            if t.month not in self.months:
                t = (t.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            elif not self._day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t += timedelta(minutes=1)
            else:
                return t.replace(tzinfo=tz)
        raise CronError(f"'{self.expression}' never matches within {SEARCH_YEARS} years")

    def describe(self) -> str:
        return f"cron {self.expression}"


def timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"unknown time zone '{name}' (use a name like 'UTC' or 'Asia/Manila')") from None
