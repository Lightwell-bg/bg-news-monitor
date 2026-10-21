"""Local time helpers: storage is always UTC, people see Europe/Sofia."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from news_monitor.storage.models import as_utc

#: The time zone shown to the administrator.
LOCAL_TZ = ZoneInfo("Europe/Sofia")


def format_local_time(value: datetime) -> str:
    """Format a moment as HH:MM in Europe/Sofia (naive values are read as UTC)."""
    moment = as_utc(value)
    assert moment is not None
    return moment.astimezone(LOCAL_TZ).strftime("%H:%M")
