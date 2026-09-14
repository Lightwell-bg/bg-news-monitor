"""The «Настройки» screen of the Telegram administration panel.

Exactly one value is shown and editable here: the importance threshold used by
the pipeline to decide whether a news item deserves an administrator card.

Nothing else from the environment is read, rendered or written: tokens, keys,
chat identifiers and model names never reach this screen. The new value is
validated, applied to the running process and saved by
:class:`~news_monitor.config.runtime_store.RuntimeSettingsStore`, which owns a
single non-secret file and never touches ``.env``.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from news_monitor.config.runtime_store import (
    MAX_IMPORTANCE_THRESHOLD,
    MIN_IMPORTANCE_THRESHOLD,
    RuntimeSettingsError,
    RuntimeSettingsStore,
    parse_importance_threshold,
)
from news_monitor.config.settings import Settings
from news_monitor.telegram.admin_ui import (
    CANCELLED_MESSAGE,
    DENIED_MESSAGE,
    MAX_INPUT_LENGTH,
    NOTICE_LIMIT,
    SETTINGS_OPEN_ACTION,
    SETTINGS_PREFIX,
    SOURCES_LIST_ACTION,
    SOURCES_PREFIX,
    TOO_LONG_MESSAGE,
    AdminReply,
    PendingInputFilter,
    PendingInputStore,
    apply_callback_reply,
    button,
    cancel_button,
    decode_callback,
    denied_reply,
    encode_callback,
    invalid_button_reply,
    keyboard_of,
    send_reply,
)
from news_monitor.telegram.filters import AdminOnly

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = SETTINGS_PREFIX

ACTION_OPEN = SETTINGS_OPEN_ACTION
ACTION_THRESHOLD = "th"
ACTION_THRESHOLD_INPUT = "thin"
ACTION_CANCEL = "cxl"

#: Kind of step-by-step input owned by this panel.
INPUT_THRESHOLD = "threshold"
SETTINGS_INPUT_KINDS: frozenset[str] = frozenset({INPUT_THRESHOLD})

#: Offsets offered as buttons around the current threshold.
THRESHOLD_STEPS: tuple[int, ...] = (-10, -5, 5, 10)

COMMANDS: tuple[str, ...] = ("settings",)

SAVED_MESSAGE = "Сохранено и применено."


def build_callback_data(action: str, *parts: object) -> str:
    """Build a callback payload of this panel, refusing an oversized one."""
    return encode_callback(CALLBACK_PREFIX, action, *parts)


def parse_callback_payload(data: str | None) -> tuple[str, tuple[str, ...]] | None:
    """Parse our callback payload, returning None when it is not ours."""
    return decode_callback(CALLBACK_PREFIX, data)


def _button(text: str, action: str, *parts: object) -> InlineKeyboardButton | None:
    return button(CALLBACK_PREFIX, text, action, *parts)


def render_settings(threshold: int) -> str:
    """Render the settings screen with the current importance threshold."""
    return "\n".join(
        [
            "⚙️ <b>Настройки</b>",
            "",
            f"Порог важности: <b>{threshold}</b> "
            f"({MIN_IMPORTANCE_THRESHOLD}–{MAX_IMPORTANCE_THRESHOLD})",
            "Новость попадает в карточку на модерацию только при оценке "
            "не ниже порога.",
        ]
    )


def render_threshold_prompt(threshold: int) -> str:
    """Render the step-by-step question about the importance threshold."""
    return "\n".join(
        [
            "⚙️ <b>Настройки</b>",
            "",
            f"Текущий порог важности: <b>{threshold}</b>.",
            f"Отправьте новое значение от {MIN_IMPORTANCE_THRESHOLD} "
            f"до {MAX_IMPORTANCE_THRESHOLD} одним сообщением.",
        ]
    )


def settings_keyboard(threshold: int) -> InlineKeyboardMarkup:
    """Buttons that change the threshold without typing anything."""
    steps: list[InlineKeyboardButton | None] = []
    for offset in THRESHOLD_STEPS:
        target = min(MAX_IMPORTANCE_THRESHOLD, max(MIN_IMPORTANCE_THRESHOLD, threshold + offset))
        if target == threshold:
            continue
        sign = "➖" if offset < 0 else "➕"
        steps.append(_button(f"{sign} {target}", ACTION_THRESHOLD, target))
    return keyboard_of(
        steps,
        [_button("✏️ Ввести значение", ACTION_THRESHOLD_INPUT)],
        [
            button(SOURCES_PREFIX, "⬅ К источникам", SOURCES_LIST_ACTION),
            _button("\U0001f504 Обновить", ACTION_OPEN),
        ],
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    """The single button that abandons the step-by-step input."""
    return keyboard_of([cancel_button(CALLBACK_PREFIX, ACTION_CANCEL)])


class SettingsAdminService:
    """Shows and changes the runtime settings an administrator may edit."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: RuntimeSettingsStore,
        pending: PendingInputStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._pending = pending if pending is not None else PendingInputStore()

    @property
    def pending_inputs(self) -> PendingInputStore:
        """The shared slot of awaited step-by-step answers."""
        return self._pending

    # --------------------------------------------------------- authorization

    def is_authorized(self, user_id: int | None) -> bool:
        """Only configured numeric administrator ids may see these settings."""
        return self._settings.is_admin(user_id)

    # --------------------------------------------------------- entry points

    def execute_command(self, *, user_id: int | None) -> AdminReply:
        """Handle ``/settings``."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized settings command ignored")
            return denied_reply()
        self._pending.clear(user_id)
        return self.show_settings()

    def execute_callback(self, *, data: str | None, user_id: int | None) -> AdminReply:
        """Handle one settings button press."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized settings callback ignored")
            return denied_reply(view_only=True)
        parsed = parse_callback_payload(data)
        if parsed is None:
            return invalid_button_reply()
        action, parts = parsed
        if action != ACTION_THRESHOLD_INPUT:
            self._pending.clear(user_id)
        try:
            return self._run_callback(action, parts, user_id)
        except RuntimeSettingsError as error:
            message = str(error)[:NOTICE_LIMIT]
            return AdminReply(notice=message, alert=True)

    def _run_callback(
        self, action: str, parts: tuple[str, ...], user_id: int | None
    ) -> AdminReply:
        if action == ACTION_OPEN:
            return self.show_settings()
        if action == ACTION_CANCEL:
            reply = self.show_settings()
            return AdminReply(
                text=reply.text, keyboard=reply.keyboard, notice=CANCELLED_MESSAGE
            )
        if action == ACTION_THRESHOLD_INPUT:
            self._pending.start(user_id, INPUT_THRESHOLD)
            return AdminReply(
                text=render_threshold_prompt(self.threshold),
                keyboard=cancel_keyboard(),
                notice="Жду значение.",
            )
        if action == ACTION_THRESHOLD and parts:
            return self.set_threshold(parts[0])
        return invalid_button_reply()

    def execute_input(self, *, text: str, user_id: int | None) -> AdminReply:
        """Handle the message answering the threshold prompt."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized settings input ignored")
            self._pending.clear(user_id)
            return denied_reply()
        pending = self._pending.get(user_id)
        if pending is None or pending.kind not in SETTINGS_INPUT_KINDS:
            return AdminReply()

        value = " ".join(str(text or "").split())
        if len(value) > MAX_INPUT_LENGTH:
            return self._prompt_again(TOO_LONG_MESSAGE)
        try:
            reply = self.set_threshold(value)
        except RuntimeSettingsError as error:
            return self._prompt_again(str(error)[:NOTICE_LIMIT])
        self._pending.clear(user_id)
        return reply

    def has_pending_input(self, user_id: int | None) -> bool:
        """True while this panel is waiting for a value from ``user_id``."""
        pending = self._pending.get(user_id)
        return pending is not None and pending.kind in SETTINGS_INPUT_KINDS

    # -------------------------------------------------------------- actions

    @property
    def threshold(self) -> int:
        """The importance threshold used by the running pipeline right now."""
        return int(self._settings.importance_threshold)

    def show_settings(self) -> AdminReply:
        """Show the settings screen."""
        current = self.threshold
        return AdminReply(
            text=render_settings(current), keyboard=settings_keyboard(current)
        )

    def set_threshold(self, value: str | int) -> AdminReply:
        """Validate, save and apply a new importance threshold.

        The value is saved first: a stored value that the process then failed to
        apply is recoverable by a restart, while the opposite is silently lost.
        """
        number = parse_importance_threshold(value)
        saved = self._store.save_importance_threshold(number)
        self._settings.importance_threshold = saved
        logger.info("importance threshold changed to %s", saved)
        text = f"{render_settings(saved)}\n\nПорог важности: {saved}\n{SAVED_MESSAGE}"
        return AdminReply(
            text=text,
            keyboard=settings_keyboard(saved),
            notice=f"Порог важности: {saved}",
        )

    # -------------------------------------------------------------- private

    def _prompt_again(self, problem: str) -> AdminReply:
        text = "\n".join(
            [
                "⚙️ <b>Настройки</b>",
                "",
                f"⚠️ {problem}",
                "",
                f"Отправьте значение от {MIN_IMPORTANCE_THRESHOLD} "
                f"до {MAX_IMPORTANCE_THRESHOLD}.",
            ]
        )
        return AdminReply(
            text=text, keyboard=cancel_keyboard(), notice=problem, alert=True
        )


async def handle_settings_command(
    message: Message, service: SettingsAdminService
) -> None:
    """Handle ``/settings`` from an administrator."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await send_reply(message, service.execute_command(user_id=user_id))


async def handle_settings_callback(
    callback: CallbackQuery, service: SettingsAdminService
) -> None:
    """Handle one settings button press."""
    user_id = getattr(getattr(callback, "from_user", None), "id", None)
    reply = service.execute_callback(data=callback.data, user_id=user_id)
    await apply_callback_reply(callback, reply)


async def handle_settings_input(
    message: Message, service: SettingsAdminService
) -> None:
    """Handle the message answering the threshold prompt."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    reply = service.execute_input(
        text=getattr(message, "text", "") or "", user_id=user_id
    )
    if not reply.text and not reply.notice:
        return
    await send_reply(message, reply)


def build_settings_router(service: SettingsAdminService, settings: Settings) -> Router:
    """Create the router carrying the settings screen handlers."""
    router = Router(name="settings-admin")
    admin_only = AdminOnly(settings)
    ours = F.data.startswith(f"{CALLBACK_PREFIX}:")
    waiting = PendingInputFilter(service.pending_inputs, SETTINGS_INPUT_KINDS)

    @router.message(Command(*COMMANDS), admin_only)
    async def _on_command(message: Message) -> None:
        await handle_settings_command(message, service)

    @router.message(Command(*COMMANDS))
    async def _on_unauthorized_command(message: Message) -> None:
        logger.warning("unauthorized settings command ignored")
        await message.answer(DENIED_MESSAGE)

    @router.message(F.text, ~F.text.startswith("/"), admin_only, waiting)
    async def _on_input(message: Message) -> None:
        await handle_settings_input(message, service)

    @router.callback_query(ours, admin_only)
    async def _on_callback(callback: CallbackQuery) -> None:
        await handle_settings_callback(callback, service)

    @router.callback_query(ours)
    async def _on_unauthorized_callback(callback: CallbackQuery) -> None:
        logger.warning("unauthorized settings callback ignored")
        await callback.answer(DENIED_MESSAGE, show_alert=True)

    return router


__all__ = [
    "ACTION_CANCEL",
    "ACTION_OPEN",
    "ACTION_THRESHOLD",
    "ACTION_THRESHOLD_INPUT",
    "COMMANDS",
    "SETTINGS_INPUT_KINDS",
    "SettingsAdminService",
    "build_callback_data",
    "build_settings_router",
    "handle_settings_callback",
    "handle_settings_command",
    "handle_settings_input",
    "render_settings",
    "settings_keyboard",
]
