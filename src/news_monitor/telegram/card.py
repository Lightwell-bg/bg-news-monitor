"""Rendering of the moderation card and of the channel publication."""

from __future__ import annotations

import re
from html import escape, unescape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from news_monitor.storage.models import NewsItem, NewsStatus, as_utc
from news_monitor.timeutil import format_local_time

#: Telegram limit of visible message text after HTML parsing (UTF-16 units).
TELEGRAM_MESSAGE_LIMIT = 4096
_TAG_RE = re.compile(r"<[^>]*>")

CALLBACK_PREFIX = "news"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_REGENERATE = "regenerate"
ACTION_PUBLISH_NOW = "now"
ACTION_CANCEL_SCHEDULE = "unsched"
ACTIONS: tuple[str, ...] = (
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_REGENERATE,
    ACTION_PUBLISH_NOW,
    ACTION_CANCEL_SCHEDULE,
)

#: Generic label, used when the current maximum delay is not known.
BUTTON_APPROVE = "\u2705 \u041e\u0434\u043e\u0431\u0440\u0438\u0442\u044c \u0441 \u0437\u0430\u0434\u0435\u0440\u0436\u043a\u043e\u0439"
BUTTON_PUBLISH_NOW = "\u26a1 \u0421\u0440\u0430\u0437\u0443"
BUTTON_PUBLISH_SCHEDULED_NOW = "\u26a1 \u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c \u0441\u0435\u0439\u0447\u0430\u0441"
BUTTON_CANCEL_SCHEDULE = "\u21a9\ufe0f \u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c"
BUTTON_REJECT = "\u274c \u041e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c"
BUTTON_REGENERATE = "\U0001f504 \u041f\u0435\u0440\u0435\u0433\u0435\u043d\u0435\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c"
BUTTON_OPEN_SOURCE = "\U0001f517 \u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a"

#: Project links appended under every channel post: rows of (emoji, label, url).
CHANNEL_FOOTER_ROWS: tuple[tuple[tuple[str, str, str], ...], ...] = (
    (
        ("\U0001f4c4","\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b \u0438 \u0436\u0438\u0437\u043d\u044c \u0432 \u0411\u043e\u043b\u0433\u0430\u0440\u0438\u0438", "https://t.me/bginfosu"),
        ("\U0001f4ac","\u0412\u043e\u043f\u0440\u043e\u0441\u044b \u043e \u0411\u043e\u043b\u0433\u0430\u0440\u0438\u0438", "https://t.me/bginfosuchat"),
    ),
    (
        ("\U0001f9ed","\u041f\u043e\u0434\u0431\u043e\u0440 \u0412\u041d\u0416 \u0438 \u041f\u041c\u0416", "https://t.me/bginfosu_bot"),
        ("\U0001f916","\u041f\u043e\u043c\u043e\u0449\u043d\u0438\u043a BGINFO", "https://t.me/bginfosuai_bot"),
    ),
    (
        ("\U0001f6d2","\u0410\u043a\u0446\u0438\u0438 Lidl, Kaufland, BILLA", "https://t.me/promoseubot?startapp=s_tg-news"),
        ("\U0001f4f0","\u041d\u043e\u0432\u043e\u0441\u0442\u0438 \u0411\u043e\u043b\u0433\u0430\u0440\u0438\u0438", "https://t.me/bginfo_bgnews"),
    ),
    (
        ("\U0001f3e0","\u041e\u0442\u0437\u044b\u0432\u044b \u043e\u0431 \u0430\u0440\u0435\u043d\u0434\u0435", "https://t.me/RentalReviewsBG"),
        ("\U0001f91d","\u0421\u043e\u0432\u043c\u0435\u0441\u0442\u043d\u044b\u0435 \u043f\u043e\u043a\u0443\u043f\u043a\u0438", "https://t.me/soobschaeu"),
        ("\U0001f1e7\U0001f1ec","\u0411\u043e\u043b\u0433\u0430\u0440\u0441\u043a\u0438\u0439 \u0441 VIRA", "https://t.me/vira_bulgarianbot?start=news"),
    ),
)


def build_callback_data(action: str, item_id: int) -> str:
    """Build the callback payload for a moderation button."""
    if action not in ACTIONS:
        raise ValueError(f"unknown moderation action: {action!r}")
    return f"{CALLBACK_PREFIX}:{action}:{item_id}"


def parse_callback_data(data: str | None) -> tuple[str, int] | None:
    """Parse a callback payload, returning None when it is not ours or malformed."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action, raw_id = parts[1], parts[2]
    if action not in ACTIONS:
        return None
    try:
        item_id = int(raw_id)
    except ValueError:
        return None
    if item_id <= 0:
        return None
    return action, item_id


def approve_label(delay_max_minutes: int | None = None) -> str:
    """Label of the approve button, naming the delay window when it is known."""
    if delay_max_minutes is None:
        return BUTTON_APPROVE
    return f"✅ Одобрить (5–{delay_max_minutes} мин)"


def build_keyboard(
    item: NewsItem, *, delay_max_minutes: int | None = None
) -> InlineKeyboardMarkup:
    """Build the moderation buttons of an item that awaits a decision."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=approve_label(delay_max_minutes),
                    callback_data=build_callback_data(ACTION_APPROVE, item.id),
                ),
                InlineKeyboardButton(
                    text=BUTTON_PUBLISH_NOW,
                    callback_data=build_callback_data(ACTION_PUBLISH_NOW, item.id),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=BUTTON_REJECT,
                    callback_data=build_callback_data(ACTION_REJECT, item.id),
                ),
                InlineKeyboardButton(
                    text=BUTTON_REGENERATE,
                    callback_data=build_callback_data(ACTION_REGENERATE, item.id),
                ),
            ],
            [InlineKeyboardButton(text=BUTTON_OPEN_SOURCE, url=item.url)],
        ]
    )


def build_scheduled_keyboard(item: NewsItem) -> InlineKeyboardMarkup:
    """Buttons of an item that is approved and waits for its publication time."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BUTTON_PUBLISH_SCHEDULED_NOW,
                    callback_data=build_callback_data(ACTION_PUBLISH_NOW, item.id),
                ),
                InlineKeyboardButton(
                    text=BUTTON_CANCEL_SCHEDULE,
                    callback_data=build_callback_data(ACTION_CANCEL_SCHEDULE, item.id),
                ),
            ],
            [InlineKeyboardButton(text=BUTTON_OPEN_SOURCE, url=item.url)],
        ]
    )


def _format_time(item: NewsItem) -> str:
    if item.published_at is None:
        return "время неизвестно"
    return item.published_at.strftime("%d.%m.%Y %H:%M UTC")


def render_admin_card(item: NewsItem) -> str:
    """Render the private moderation card in Telegram HTML."""
    lines = [
        f"<b>{escape(item.source_name or item.source_id)}</b> · {escape(_format_time(item))}",
        f"Оценка: <b>{item.importance if item.importance is not None else '—'}</b>/100",
    ]
    if item.ai_reason:
        lines.append(f"Причина: {escape(item.ai_reason)}")
    lines.append("")
    if item.title_ru:
        lines.append(f"<b>{escape(item.title_ru)}</b>")
    if item.original_title:
        lines.append(f"<i>{escape(item.original_title)}</i>")
    if item.draft_ru:
        lines.append("")
        lines.append(escape(item.draft_ru))
    lines.append("")
    safe_url = escape(item.url, quote=True)
    lines.append(f'Источник: <a href="{safe_url}">{safe_url}</a>')
    if item.status_enum is NewsStatus.SCHEDULED and item.scheduled_at is not None:
        local = format_local_time(as_utc(item.scheduled_at))
        lines.append("")
        lines.append(f"⏰ Публикация в {local} (София)")
    return "\n".join(lines)


def render_channel_post(item: NewsItem) -> str:
    """Render the channel publication.

    The source link is always appended: a post without it is never produced.
    The visible text never exceeds Telegram's limit: when it would, only the
    draft is shortened (the item itself is not changed), so the title, the
    source link and the footer stay intact.
    """
    if not item.url:
        raise ValueError("cannot render a publication without a source url")
    draft = item.draft_ru or ""
    post = _render_post(item, draft)
    if _visible_length(post) <= TELEGRAM_MESSAGE_LIMIT:
        return post

    # Largest draft prefix that fits; the rendered text is measured each time
    # because escaping (&amp;) and UTF-16 pairs change the length.
    low, high = 0, len(draft)
    best = _render_post(item, "…")
    while low <= high:
        mid = (low + high) // 2
        candidate = _render_post(item, _shorten(draft, mid))
        if _visible_length(candidate) <= TELEGRAM_MESSAGE_LIMIT:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _shorten(text: str, limit: int) -> str:
    """Cut ``text`` to about ``limit`` characters on a word boundary, add an ellipsis."""
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit * 0.8:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def _visible_length(html_text: str) -> int:
    """Length Telegram counts: tags removed, entities decoded, UTF-16 code units."""
    plain = unescape(_TAG_RE.sub("", html_text))
    return len(plain.encode("utf-16-le")) // 2


def _render_post(item: NewsItem, draft: str) -> str:
    lines: list[str] = []
    if item.title_ru:
        lines.append(f"<b>{escape(item.title_ru)}</b>")
        lines.append("")
    if draft:
        lines.append(escape(draft))
        lines.append("")
    source_name = escape(item.source_name or item.source_id)
    safe_url = escape(item.url, quote=True)
    lines.append(f'Источник: <a href="{safe_url}">{source_name}</a>')
    # Escaped as well: a raw ampersand breaks Telegram HTML parsing.
    lines.append(safe_url)
    lines.append("")
    lines.append(_render_footer())
    return "\n".join(lines)


def _render_footer() -> str:
    """Render the project links as one compact quote under the post.

    The links come after the source link, so Telegram still builds the link
    preview from the article and not from a project channel.
    """
    rows = [
        " · ".join(
            f'{emoji} <a href="{escape(url, quote=True)}">{escape(label)}</a>'
            for emoji, label, url in row
        )
        for row in CHANNEL_FOOTER_ROWS
    ]
    return "<blockquote>" + "\n".join(rows) + "</blockquote>"
