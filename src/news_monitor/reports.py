"""Daily moderation report: who decided what, sent to the chosen administrators."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from typing import Protocol

from news_monitor.storage.models import (
    EVENT_APPROVE,
    EVENT_CANCEL,
    EVENT_PUBLISH_NOW,
    EVENT_REJECT,
    ModerationEvent,
    NewsStatus,
    as_utc,
)
from news_monitor.storage.repository import NewsRepository
from news_monitor.timeutil import LOCAL_TZ, local_day_bounds

logger = logging.getLogger(__name__)

__all__ = ["build_daily_report", "local_day_bounds", "send_daily_report"]

#: Keeps the report well inside the 4096 character Telegram limit.
REPORT_LIMIT = 3900
MAX_ITEM_LINES = 30
TITLE_LIMIT = 80

_ICONS: dict[str, str] = {
    EVENT_APPROVE: "✅",
    EVENT_PUBLISH_NOW: "⚡",
    EVENT_REJECT: "❌",
    EVENT_CANCEL: "↩️",
}


class ReportSender(Protocol):
    """Minimal Telegram sending surface used by the report."""

    async def send_message(self, chat_id: object, text: str, **kwargs: object) -> object:
        ...


@dataclass
class _Tally:
    """Decisions of one person over the reported day."""

    user_id: int
    name: str = ""
    approved: int = 0
    rejected: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return self.approved + self.rejected + self.cancelled

    @property
    def label(self) -> str:
        return escape(self.name) if self.name else f"ID {self.user_id}"


def _u16(text: str) -> int:
    """Length in UTF-16 code units, which is how Telegram counts a message."""
    return len(text.encode("utf-16-le")) // 2


def _shorten(title: str) -> str:
    title = " ".join(title.split())
    if len(title) <= TITLE_LIMIT:
        return title
    return title[: TITLE_LIMIT - 1].rstrip() + "…"


def _tally(events: Iterable[ModerationEvent]) -> list[_Tally]:
    tallies: dict[int, _Tally] = {}
    for event in events:
        entry = tallies.setdefault(event.user_id, _Tally(user_id=event.user_id))
        if event.user_name.strip():
            entry.name = event.user_name.strip()  # the latest known name wins
        if event.action in (EVENT_APPROVE, EVENT_PUBLISH_NOW):
            entry.approved += 1
        elif event.action == EVENT_REJECT:
            entry.rejected += 1
        elif event.action == EVENT_CANCEL:
            entry.cancelled += 1
    return sorted(tallies.values(), key=lambda t: (-t.total, t.label))


def _decider_line(entry: _Tally) -> str:
    line = f"{entry.label} — одобрено {entry.approved}, отклонено {entry.rejected}"
    if entry.cancelled > 0:
        line += f", отменено {entry.cancelled}"
    return line


def _local_clock(moment: datetime) -> str:
    return as_utc(moment).astimezone(LOCAL_TZ).strftime("%H:%M")  # type: ignore[union-attr]


async def build_daily_report(repository: NewsRepository, day: date) -> str:
    """Render the report of one local calendar day as Telegram HTML."""
    start, end = local_day_bounds(day)
    events = await repository.list_events(start, end)
    pending = await repository.count_by_status(NewsStatus.AWAITING_APPROVAL)

    header = f"📊 <b>Отчёт модерации за {day.strftime('%d.%m.%Y')}</b>"
    footer = f"Ожидают решения сейчас: {pending}"
    if not events:
        return "\n".join([header, "", "За день решений не было.", "", footer])

    tallies = _tally(events)
    names = {t.user_id: t.label for t in tallies}
    budget = REPORT_LIMIT - _u16(header) - _u16(footer) - 60

    decider_lines: list[str] = []
    used = 0
    for position, entry in enumerate(tallies):
        line = _decider_line(entry)
        if used + _u16(line) + 1 > budget // 2:
            decider_lines.append(f"…и ещё {len(tallies) - position} чел.")
            break
        decider_lines.append(line)
        used += _u16(line) + 1

    remaining = budget - used
    item_lines: list[str] = []
    shown = 0
    for event in events[:MAX_ITEM_LINES]:
        item = await repository.get(event.item_id)
        title = ""
        if item is not None:
            title = item.title_ru or item.original_title or ""
        line = (
            f"{_ICONS.get(event.action, '•')} {_local_clock(event.created_at)} "
            f"{escape(_shorten(title))} — {names[event.user_id]}"
        )
        if _u16(line) + 1 > remaining:
            break
        item_lines.append(line)
        remaining -= _u16(line) + 1
        shown += 1
    hidden = len(events) - shown

    parts = [header, "", *decider_lines, "", *item_lines]
    if hidden > 0:
        parts.append(f"…и ещё {hidden}")
    parts.extend(["", footer])
    return "\n".join(parts)


async def send_daily_report(
    bot: ReportSender,
    repository: NewsRepository,
    recipients: Iterable[int],
    day: date,
) -> int:
    """Send the report of ``day`` to every recipient; never raises.

    Returns the number of recipients it was delivered to. A failure for one
    recipient is logged (type only) and does not stop the others.
    """
    try:
        text = await build_daily_report(repository, day)
    except Exception as exc:  # noqa: BLE001 - the scheduler job must survive
        logger.error("cannot build the daily report: %s", type(exc).__name__)
        return 0

    delivered = 0
    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id, text, parse_mode="HTML", disable_web_page_preview=True
            )
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - try the next recipient
            logger.warning("cannot send the daily report: %s", type(exc).__name__)
    return delivered
