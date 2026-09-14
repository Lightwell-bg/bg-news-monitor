"""Shared building blocks of the Telegram administration panel.

The source panel and the settings panel render the same kind of answer, encode
callbacks the same way and share one step-by-step input slot per administrator.
Those primitives live here so the two panels never have to import each other.

Nothing in this module reads configuration or secrets: it only formats text,
builds keyboards and remembers which administrator was asked for a value.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable

from aiogram.filters import BaseFilter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

logger = logging.getLogger(__name__)

#: Callback namespaces. Each panel owns one, so a button of the other panel is
#: simply not routed to it and the moderation router keeps every other update.
SOURCES_PREFIX = "src"
SETTINGS_PREFIX = "cfg"

#: Entry actions used by the cross-panel navigation buttons.
SOURCES_LIST_ACTION = "list"
SETTINGS_OPEN_ACTION = "open"

#: Telegram refuses callback data longer than 64 bytes.
CALLBACK_LIMIT = 64
#: An answer to a callback query is a toast, not a message.
NOTICE_LIMIT = 190
#: Telegram refuses a message longer than 4096 characters; keep a safe margin.
TEXT_LIMIT = 3900
#: A step-by-step answer longer than this is a mistake, not a setting.
MAX_INPUT_LENGTH = 400
#: A forgotten prompt must not silently swallow a much later message.
PENDING_TTL_SECONDS = 900.0
#: Upper bound on remembered prompts, so the slot map cannot grow without end.
MAX_PENDING_ENTRIES = 64

DENIED_MESSAGE = "Недостаточно прав для этого действия."
INVALID_BUTTON_MESSAGE = "Некорректная кнопка."
CANCELLED_MESSAGE = "Ввод отменён."
TOO_LONG_MESSAGE = f"Сообщение длиннее {MAX_INPUT_LENGTH} символов."


def clip(text: str) -> str:
    """Keep a rendered view inside the Telegram message limit.

    Whole lines are dropped, never a part of one, so the HTML of the remaining
    text stays balanced and Telegram can still parse it.
    """
    if len(text) <= TEXT_LIMIT:
        return text
    head = text[:TEXT_LIMIT]
    cut = head.rfind("\n")
    if cut > TEXT_LIMIT // 2:
        head = head[:cut]
    return f"{head}\n…"


@dataclass(frozen=True, slots=True)
class AdminReply:
    """A rendered answer: full view, short toast and alert flag."""

    text: str = ""
    keyboard: InlineKeyboardMarkup | None = None
    notice: str = ""
    alert: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", clip(self.text))
        object.__setattr__(self, "notice", self.notice[:NOTICE_LIMIT])


def denied_reply(*, view_only: bool = False) -> AdminReply:
    """The single answer given to anyone who is not an administrator."""
    return AdminReply(
        text="" if view_only else DENIED_MESSAGE,
        notice=DENIED_MESSAGE,
        alert=True,
    )


def invalid_button_reply() -> AdminReply:
    """The answer to a callback payload this panel cannot understand."""
    return AdminReply(notice=INVALID_BUTTON_MESSAGE, alert=True)


def encode_callback(prefix: str, action: str, *parts: object) -> str:
    """Build a callback payload, refusing one that Telegram would reject."""
    data = ":".join([prefix, action, *(str(part) for part in parts)])
    if len(data.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError("callback payload too long")
    return data


def decode_callback(prefix: str, data: str | None) -> tuple[str, tuple[str, ...]] | None:
    """Parse a callback payload, returning ``None`` when it is not ours."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != prefix:
        return None
    return parts[1], tuple(parts[2:])


def button(
    prefix: str, text: str, action: str, *parts: object
) -> InlineKeyboardButton | None:
    """Build one inline button, or ``None`` when its payload cannot be encoded."""
    try:
        return InlineKeyboardButton(
            text=text, callback_data=encode_callback(prefix, action, *parts)
        )
    except ValueError:  # pragma: no cover - only for an unusually long source id
        logger.warning("button skipped: callback payload too long")
        return None


def keyboard_of(
    *rows: Iterable[InlineKeyboardButton | None],
) -> InlineKeyboardMarkup:
    """Drop the buttons that could not be built and the rows left empty."""
    cleaned = [[item for item in row if item is not None] for row in rows]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in cleaned if row])


def cancel_button(prefix: str, action: str, *parts: object) -> InlineKeyboardButton | None:
    """The button that abandons a step-by-step input."""
    return button(prefix, "✖️ Отмена", action, *parts)


# ----------------------------------------------------------- pending input


@dataclass(frozen=True, slots=True)
class PendingInput:
    """One value the panel is waiting for from one administrator."""

    kind: str
    target: str = ""
    extra: str = ""
    started_at: float = 0.0


class PendingInputStore:
    """At most one awaited value per administrator, shared by every panel.

    A single slot per user is deliberate: starting a new prompt replaces the
    previous one, so two panels can never both claim the next message.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PENDING_TTL_SECONDS,
        max_entries: int = MAX_PENDING_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: dict[int, PendingInput] = {}

    def start(
        self, user_id: int | None, kind: str, *, target: str = "", extra: str = ""
    ) -> PendingInput | None:
        """Remember that ``user_id`` was asked for a value of ``kind``."""
        if user_id is None:
            return None
        self._purge()
        if user_id not in self._entries and len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key].started_at)
            self._entries.pop(oldest, None)
        pending = PendingInput(
            kind=kind, target=target, extra=extra, started_at=self._clock()
        )
        self._entries[user_id] = pending
        return pending

    def get(self, user_id: int | None) -> PendingInput | None:
        """Return the awaited value, forgetting one that has expired."""
        if user_id is None:
            return None
        pending = self._entries.get(user_id)
        if pending is None:
            return None
        if self._clock() - pending.started_at > self._ttl:
            self._entries.pop(user_id, None)
            return None
        return pending

    def clear(self, user_id: int | None) -> PendingInput | None:
        """Forget the awaited value, returning it when there was one."""
        if user_id is None:
            return None
        return self._entries.pop(user_id, None)

    def _purge(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, pending in self._entries.items()
            if now - pending.started_at > self._ttl
        ]
        for key in expired:
            self._entries.pop(key, None)


class PendingInputFilter(BaseFilter):
    """Passes only while the panel is waiting for a value of its own kinds."""

    def __init__(self, store: PendingInputStore, kinds: Iterable[str]) -> None:
        self.store = store
        self.kinds = frozenset(kinds)

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        pending = self.store.get(getattr(user, "id", None))
        return pending is not None and pending.kind in self.kinds


# ---------------------------------------------------------------- delivery


async def send_reply(message: Message, reply: AdminReply) -> None:
    """Send a rendered answer as a new message."""
    await message.answer(
        reply.text or reply.notice or DENIED_MESSAGE,
        parse_mode="HTML",
        reply_markup=reply.keyboard,
        disable_web_page_preview=True,
    )


async def apply_callback_reply(callback: CallbackQuery, reply: AdminReply) -> None:
    """Answer the button press and refresh the panel message when needed."""
    await callback.answer(reply.notice[:NOTICE_LIMIT], show_alert=reply.alert)
    if not reply.text:
        return
    message = getattr(callback, "message", None)
    if message is None:
        return
    try:
        await message.edit_text(
            reply.text,
            parse_mode="HTML",
            reply_markup=reply.keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 - a failed edit must not crash the bot
        logger.warning("cannot refresh the admin view: %s", type(exc).__name__)
