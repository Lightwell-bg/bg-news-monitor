"""Rendering of the moderation card and of the channel publication."""

from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from news_monitor.storage.models import NewsItem

CALLBACK_PREFIX = "news"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_REGENERATE = "regenerate"

BUTTON_APPROVE = "\u2705 \u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c"
BUTTON_REJECT = "\u274c \u041e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c"
BUTTON_REGENERATE = "\U0001f504 \u041f\u0435\u0440\u0435\u0433\u0435\u043d\u0435\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c"
BUTTON_OPEN_SOURCE = "\U0001f517 \u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a"

#: Project links appended under every channel post as (emoji, label, url).
CHANNEL_FOOTER_LINKS: tuple[tuple[str, str, str], ...] = (
    ("\U0001f4f0", "\u0412\u0430\u0436\u043d\u044b\u0435 \u043d\u043e\u0432\u043e\u0441\u0442\u0438 \u043e \u0436\u0438\u0437\u043d\u0438 \u0432 \u0411\u043e\u043b\u0433\u0430\u0440\u0438\u0438", "https://t.me/bginfosu"),
    ("\U0001f9ed", "\u0412\u044b\u0431\u043e\u0440 \u043e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u044f \u0434\u043b\u044f \u0436\u0438\u0437\u043d\u0438 \u0432 \u0411\u043e\u043b\u0433\u0430\u0440\u0438\u0438", "https://t.me/bginfosu_bot"),
    ("\U0001f4ac", "\u0417\u0430\u0434\u0430\u0442\u044c \u0432\u043e\u043f\u0440\u043e\u0441", "https://t.me/bginfosuai_bot"),
    ("\U0001f3e0", "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0436\u0438\u043b\u044c\u044f", "https://t.me/RentalReviewsBG"),
    ("\U0001f6d2", "\u0421\u043e\u0432\u043c\u0435\u0441\u0442\u043d\u044b\u0435 \u043f\u043e\u043a\u0443\u043f\u043a\u0438", "https://t.me/soobschaeu"),
    ("\U0001f1e7\U0001f1ec", "\u0411\u043e\u043b\u0433\u0430\u0440\u0441\u043a\u0438\u0439 \u044f\u0437\u044b\u043a \u0434\u043b\u044f \u0436\u0438\u0437\u043d\u0438", "https://t.me/vira_bulgarianbot"),
)


def build_callback_data(action: str, item_id: int) -> str:
    """Build the callback payload for a moderation button."""
    if action not in (ACTION_APPROVE, ACTION_REJECT, ACTION_REGENERATE):
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
    if action not in (ACTION_APPROVE, ACTION_REJECT, ACTION_REGENERATE):
        return None
    try:
        item_id = int(raw_id)
    except ValueError:
        return None
    if item_id <= 0:
        return None
    return action, item_id


def build_keyboard(item: NewsItem) -> InlineKeyboardMarkup:
    """Build the four moderation buttons for one item."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BUTTON_APPROVE,
                    callback_data=build_callback_data(ACTION_APPROVE, item.id),
                ),
                InlineKeyboardButton(
                    text=BUTTON_REJECT,
                    callback_data=build_callback_data(ACTION_REJECT, item.id),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=BUTTON_REGENERATE,
                    callback_data=build_callback_data(ACTION_REGENERATE, item.id),
                ),
                InlineKeyboardButton(text=BUTTON_OPEN_SOURCE, url=item.url),
            ],
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
    return "\n".join(lines)


def render_channel_post(item: NewsItem) -> str:
    """Render the channel publication.

    The source link is always appended: a post without it is never produced.
    """
    if not item.url:
        raise ValueError("cannot render a publication without a source url")
    lines: list[str] = []
    if item.title_ru:
        lines.append(f"<b>{escape(item.title_ru)}</b>")
        lines.append("")
    if item.draft_ru:
        lines.append(escape(item.draft_ru))
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
        f'{emoji} <a href="{escape(url, quote=True)}">{escape(label)}</a>'
        for emoji, label, url in CHANNEL_FOOTER_LINKS
    ]
    return "<blockquote>" + "\n".join(rows) + "</blockquote>"
