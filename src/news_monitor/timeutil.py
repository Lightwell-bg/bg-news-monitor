"""Local time helpers: storage is always UTC, people see Europe/Sofia."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from news_monitor.storage.models import as_utc

#: The time zone shown to the administrator.
LOCAL_TZ = ZoneInfo("Europe/Sofia")


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC bounds [start, end) of one local calendar day.

    Midnight-to-midnight in Europe/Sofia, so a day with a clock change is
    23 or 25 hours long instead of a fixed 24.
    """
    start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def format_local_time(value: datetime) -> str:
    """Format a moment as HH:MM in Europe/Sofia (naive values are read as UTC)."""
    moment = as_utc(value)
    assert moment is not None
    return moment.astimezone(LOCAL_TZ).strftime("%H:%M")
