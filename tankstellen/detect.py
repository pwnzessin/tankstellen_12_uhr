"""Detection of violations of the 12:00 rule (section 2 KPAnG).

Rule: fuel prices may only be *increased* once per calendar day, at 12:00.
Decreases are allowed at any time.

We only ever see snapshots, so an increase seen between two polls happened
somewhere in the window (prev_ts, ts]. We can therefore only prove a violation
when that window does not contain 12:00 (Europe/Berlin), or when it is a
second increase on the same calendar day.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

# Observations further apart than this are not compared: the window would be
# so large that it tells us nothing (e.g. the collector was not running).
DEFAULT_MAX_GAP = timedelta(minutes=30)

# Tolerance for float comparison (prices have 3 decimals).
EPS = 0.0005

VIOLATION = "violation"
OK_NOON = "ok_noon"
MULTIPLE = "multiple_per_day"


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def berlin_day(dt: datetime) -> date:
    return dt.astimezone(BERLIN).date()


def noons_in_window(start: datetime, end: datetime) -> list[datetime]:
    """All 12:00:00 Europe/Berlin instants t with start < t <= end."""
    result = []
    day = start.astimezone(BERLIN).date()
    last = end.astimezone(BERLIN).date()
    while day <= last:
        noon = datetime.combine(day, time(12, 0), tzinfo=BERLIN)
        if start < noon <= end:
            result.append(noon)
        day += timedelta(days=1)
    return result


def classify_increase(
    prev_ts: datetime, ts: datetime, earlier_increases_that_day: int
) -> str:
    """Classify one observed increase.

    earlier_increases_that_day: number of already recorded increases for the
    same station+fuel on the same Berlin calendar day (of `ts`).
    """
    if not noons_in_window(prev_ts, ts):
        return VIOLATION
    if earlier_increases_that_day >= 1:
        return MULTIPLE
    return OK_NOON


def find_increase(prev_price: float | None, price: float | None) -> bool:
    return (
        prev_price is not None
        and price is not None
        and price > prev_price + EPS
    )
