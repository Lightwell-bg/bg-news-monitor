"""The «Модераторы» screen of the Telegram administration panel and ``/start``.

Administrators (``TELEGRAM_ADMIN_IDS``) assign and revoke moderators here and
choose who receives the daily moderation report. Moderators are stored in the
database through :class:`~news_monitor.telegram.access.AccessControl`; report
recipients are stored in ``runtime.yaml`` through
:class:`~news_monitor.config.runtime_store.RuntimeSettingsStore`.

Everything is behind :class:`~news_monitor.telegram.filters.AdminOnly`. The only
thing a stranger or a moderator can do is ``/start``, which shows their own
Telegram ID (or their role) and nothing else.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from news_monitor.config.runtime_store import RuntimeSettingsError, RuntimeSettingsStore
from news_monitor.config.settings import Settings
from news_monitor.reports import build_daily_report
from news_monitor.telegram.access import MAX_MODERATORS, AccessControl, AddModeratorResult
from news_monitor.telegram.admin_ui import (
    CANCELLED_MESSAGE,
    DENIED_MESSAGE,
    MAX_INPUT_LENGTH,
    MODERATORS_OPEN_ACTION,
    MODERATORS_PREFIX,
    NOTICE_LIMIT,
    SETTINGS_OPEN_ACTION,
    SETTINGS_PREFIX,
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
from news_monitor.timeutil import LOCAL_TZ

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = MODERATORS_PREFIX

ACTION_OPEN = MODERATORS_OPEN_ACTION
ACTION_ADD = "add"
ACTION_CANCEL = "cxl"
ACTION_DELETE = "del"
ACTION_DELETE_CONFIRM = "delok"
ACTION_REPORT = "rep"
ACTION_TOGGLE = "tg"
ACTION_TODAY = "today"
ACTION_PAGE = "pg"

#: Moderators per list page; keeps the message far below Telegram's limit.
MODERATORS_PER_PAGE = 10

#: Kind of step-by-step input owned by this panel.
INPUT_ADD_MODERATOR = "mod_add"
MODERATOR_INPUT_KINDS: frozenset[str] = frozenset({INPUT_ADD_MODERATOR})

COMMANDS: tuple[str, ...] = ("moderators",)

#: Identifier of the "pick a user" request, echoed back in ``users_shared``.
USER_REQUEST_ID = 7301
#: Telegram user ids are well below this; anything above is a typo.
MAX_USER_ID = 10**13
#: User-controlled names are shown cut to this length.
NAME_LIMIT = 64

NO_NAME = "без имени"
ALREADY_REMOVED_MESSAGE = "Модератор уже удалён"

WELCOME_TEXT = (
    "Вас назначили модератором канала. Карточки новостей будут приходить сюда. "
    "Доступно только «Одобрить» и «Отклонить»."
)

ADMIN_START_TEXT = "Вы администратор. Команды: /sources, /settings, /moderators"
MODERATOR_START_TEXT = (
    "Вы модератор. Карточки новостей приходят сюда, "
    "можно только одобрять и отклонять."
)


def own_id_line(user_id: int | None) -> str:
    """The user's own Telegram ID, shown by ``/start`` for every role."""
    return f"Ваш Telegram ID: <code>{int(user_id) if user_id is not None else 0}</code>"


def stranger_start_text(user_id: int | None) -> str:
    """What anyone without a role learns: only their own Telegram ID."""
    return (
        f"{own_id_line(user_id)}. "
        "Отправьте его администратору канала, чтобы он назначил вас модератором."
    )


def build_callback_data(action: str, *parts: object) -> str:
    """Build a callback payload of this panel, refusing an oversized one."""
    return encode_callback(CALLBACK_PREFIX, action, *parts)


def parse_callback_payload(data: str | None) -> tuple[str, tuple[str, ...]] | None:
    """Parse our callback payload, returning None when it is not ours."""
    return decode_callback(CALLBACK_PREFIX, data)


def _button(text: str, action: str, *parts: object):
    return button(CALLBACK_PREFIX, text, action, *parts)


def _back_button():
    return button(SETTINGS_PREFIX, "⬅ К настройкам", SETTINGS_OPEN_ACTION)


def clean_name(value: object) -> str:
    """A user-controlled name: single-spaced and cut to :data:`NAME_LIMIT`."""
    return " ".join(str(value or "").split())[:NAME_LIMIT]


def safe_name(value: object) -> str:
    """The same name made safe for Telegram HTML."""
    return html.escape(clean_name(value), quote=False)


# ----------------------------------------------------------------- rendering


@dataclass(frozen=True, slots=True)
class ModeratorView:
    """One moderator as shown in the list."""

    user_id: int
    name: str = ""

    @property
    def label(self) -> str:
        """Plain text label for a button."""
        return clean_name(self.name) or f"ID {self.user_id}"


def page_count(total: int) -> int:
    """Number of list pages (at least one, even for an empty list)."""
    return max(1, -(-int(total) // MODERATORS_PER_PAGE))


def clamp_page(page: int, total: int) -> int:
    """A stale or bogus page number moved into the valid range."""
    return min(max(int(page), 0), page_count(total) - 1)


def _page_slice(moderators: Sequence[ModeratorView], page: int) -> tuple[int, list[ModeratorView]]:
    page = clamp_page(page, len(moderators))
    start = page * MODERATORS_PER_PAGE
    return page, list(moderators[start : start + MODERATORS_PER_PAGE])


def render_moderators(moderators: Sequence[ModeratorView], page: int = 0) -> str:
    """Render one page of the list of moderators with the total count."""
    page, shown = _page_slice(moderators, page)
    pages = page_count(len(moderators))
    lines = ["👥 <b>Модераторы</b>", ""]
    if moderators:
        first = page * MODERATORS_PER_PAGE + 1
        for number, moderator in enumerate(shown, start=first):
            name = safe_name(moderator.name) or NO_NAME
            lines.append(f"{number}. {name} (ID {moderator.user_id})")
    else:
        lines.append("Модераторов пока нет.")
    lines.append("")
    lines.append(f"Назначено: {len(moderators)} из {MAX_MODERATORS}")
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}")
    lines.append(
        "Модератор получает карточки новостей и может только «Одобрить» "
        "и «Отклонить»."
    )
    return "\n".join(lines)


def moderators_keyboard(
    moderators: Sequence[ModeratorView], page: int = 0
) -> InlineKeyboardMarkup:
    """Add, delete (one button per listed moderator), paging, report and back."""
    page, shown = _page_slice(moderators, page)
    pages = page_count(len(moderators))
    rows = [[_button("➕ Добавить", ACTION_ADD)]]
    for moderator in shown:
        rows.append([_button(f"🗑 {moderator.label}", ACTION_DELETE, moderator.user_id)])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_button("◀ Назад", ACTION_PAGE, page - 1))
        if page < pages - 1:
            nav.append(_button("Вперёд ▶", ACTION_PAGE, page + 1))
        rows.append(nav)
    rows.append([_button("📊 Отчёт", ACTION_REPORT)])
    rows.append([_back_button()])
    return keyboard_of(*rows)


def render_delete_confirm(moderator: ModeratorView) -> str:
    """Render the question before a moderator is removed."""
    name = safe_name(moderator.name) or f"ID {moderator.user_id}"
    return "\n".join(
        [
            "👥 <b>Модераторы</b>",
            "",
            f"Удалить {name}?",
            "Он перестанет получать карточки и не сможет одобрять и отклонять.",
        ]
    )


def delete_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """The yes/no buttons of the delete confirmation."""
    return keyboard_of(
        [
            _button("Да, удалить", ACTION_DELETE_CONFIRM, user_id),
            _button("Отмена", ACTION_OPEN),
        ]
    )


def render_add_prompt() -> str:
    """Instructions for assigning a moderator."""
    return "\n".join(
        [
            "👥 <b>Новый модератор</b>",
            "",
            "Выберите человека одним из способов:",
            "1. Нажмите кнопку «👤 Выбрать пользователя» внизу и выберите его в списке.",
            "2. Отправьте числовой Telegram ID (его показывает бот по команде /start).",
            "3. Перешлите сюда любое сообщение этого человека.",
            "",
            "Чтобы отказаться, нажмите «Отмена».",
        ]
    )


def add_cancel_keyboard() -> InlineKeyboardMarkup:
    """The single inline button that abandons the assignment."""
    return keyboard_of([cancel_button(CALLBACK_PREFIX, ACTION_CANCEL)])


def user_picker_keyboard() -> ReplyKeyboardMarkup:
    """One-time reply keyboard with the native Telegram user picker."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="👤 Выбрать пользователя",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=USER_REQUEST_ID,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def render_report_screen(
    report_time: str, admins: Sequence[tuple[int, str, bool]]
) -> str:
    """Render the report settings; ``admins`` is (id, label, receives)."""
    lines = [
        "📊 <b>Сводка модерации</b>",
        "",
        f"Сводка за прошедший день приходит каждый день в {html.escape(report_time)} "
        "(время София).",
        "",
        "Кому присылать:",
    ]
    if not any(receives for _id, _label, receives in admins):
        lines.append("Никто не получает сводку")
    else:
        for _id, label, receives in admins:
            if receives:
                lines.append(f"✅ {html.escape(label, quote=False)}")
    return "\n".join(lines)


def report_keyboard(admins: Sequence[tuple[int, str, bool]]) -> InlineKeyboardMarkup:
    """Toggle buttons for the recipients, the test send and back."""
    rows = []
    for admin_id, label, receives in admins:
        mark = "✅" if receives else "⬜"
        rows.append(
            [_button(f"{mark} {label}", ACTION_TOGGLE, admin_id, 0 if receives else 1)]
        )
    rows.append([_button("📨 Прислать сводку за сегодня", ACTION_TODAY)])
    rows.append([_button("⬅ К модераторам", ACTION_OPEN)])
    return keyboard_of(*rows)


# --------------------------------------------------------------------- input


@dataclass(frozen=True, slots=True)
class IncomingInput:
    """What an administrator sent while the add prompt was open."""

    text: str = ""
    shared_request_id: int | None = None
    shared_users: tuple[tuple[int, str], ...] = ()
    forward_user: tuple[int, str, bool] | None = None  # id, name, is_bot
    forward_hidden: bool = False


@dataclass(frozen=True, slots=True)
class InputOutcome:
    """Result of one awaited answer.

    ``done`` means the prompt is finished and the reply keyboard must be
    removed; ``followup`` is the refreshed list shown after it.
    """

    reply: AdminReply
    done: bool = False
    followup: AdminReply | None = None


def incoming_from_message(message: Message) -> IncomingInput:
    """Extract the parts of a message the add flow understands."""
    shared = getattr(message, "users_shared", None)
    shared_users: tuple[tuple[int, str], ...] = ()
    request_id = None
    if shared is not None:
        request_id = getattr(shared, "request_id", None)
        shared_users = tuple(
            (
                int(user.user_id),
                clean_name(
                    f"{getattr(user, 'first_name', '') or ''} "
                    f"{getattr(user, 'last_name', '') or ''}"
                ),
            )
            for user in (getattr(shared, "users", None) or [])
        )
    forward_user = None
    hidden = False
    origin = getattr(message, "forward_origin", None)
    if isinstance(origin, MessageOriginUser):
        sender = origin.sender_user
        forward_user = (
            int(sender.id),
            clean_name(getattr(sender, "full_name", "") or ""),
            bool(getattr(sender, "is_bot", False)),
        )
    elif isinstance(origin, MessageOriginHiddenUser):
        hidden = True
    return IncomingInput(
        text=getattr(message, "text", "") or "",
        shared_request_id=request_id,
        shared_users=shared_users,
        forward_user=forward_user,
        forward_hidden=hidden,
    )


def parse_user_id(text: str) -> int | None:
    """A positive Telegram id typed as digits, or None."""
    value = text.strip()
    if not value.isascii() or not value.isdigit():
        return None
    number = int(value)
    if number <= 0 or number > MAX_USER_ID:
        return None
    return number


# ------------------------------------------------------------------- service


class ModeratorsAdminService:
    """Assigns moderators and manages the daily report recipients."""

    def __init__(
        self,
        *,
        access: AccessControl,
        store: RuntimeSettingsStore,
        repository: Any,
        bot: Any,
        settings: Settings,
        pending: PendingInputStore | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._access = access
        self._store = store
        self._repository = repository
        self._bot = bot
        self._settings = settings
        self._pending = pending if pending is not None else PendingInputStore()
        self._today = today or (lambda: datetime.now(LOCAL_TZ).date())
        self._chat_names: dict[int, str] = {}

    @property
    def pending_inputs(self) -> PendingInputStore:
        """The shared slot of awaited step-by-step answers."""
        return self._pending

    # --------------------------------------------------------- authorization

    def is_authorized(self, user_id: int | None) -> bool:
        """Only configured numeric administrator ids may use this panel."""
        return self._access.is_admin(user_id)

    def start_text(self, user_id: int | None) -> str:
        """The ``/start`` answer for the role of ``user_id``."""
        if self._access.is_admin(user_id):
            return f"{ADMIN_START_TEXT}\n{own_id_line(user_id)}"
        if self._access.is_moderator(user_id):
            return f"{MODERATOR_START_TEXT}\n{own_id_line(user_id)}"
        return stranger_start_text(user_id)

    # --------------------------------------------------------- entry points

    async def execute_command(self, *, user_id: int | None) -> AdminReply:
        """Handle ``/moderators``."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized moderators command ignored")
            return denied_reply()
        self._pending.clear(user_id)
        return await self.show_list()

    async def execute_callback(self, *, data: str | None, user_id: int | None) -> AdminReply:
        """Handle one button press of this panel."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized moderators callback ignored")
            return denied_reply(view_only=True)
        parsed = parse_callback_payload(data)
        if parsed is None:
            return invalid_button_reply()
        action, parts = parsed
        if action != ACTION_ADD:
            self._pending.clear(user_id)
        try:
            return await self._run_callback(action, parts, user_id)
        except RuntimeSettingsError as error:
            return AdminReply(notice=str(error)[:NOTICE_LIMIT], alert=True)

    async def _run_callback(
        self, action: str, parts: tuple[str, ...], user_id: int | None
    ) -> AdminReply:
        if action == ACTION_OPEN:
            return await self.show_list()
        if action == ACTION_PAGE:
            page = self._parse_id(parts[0]) if parts else 0
            return await self.show_list(page=page or 0)
        if action == ACTION_CANCEL:
            reply = await self.show_list()
            return AdminReply(
                text=reply.text, keyboard=reply.keyboard, notice=CANCELLED_MESSAGE
            )
        if action == ACTION_ADD:
            if len(self._access.moderator_ids) >= MAX_MODERATORS:
                return await self.show_list(
                    notice=f"Достигнут предел: {MAX_MODERATORS} модераторов."
                )
            self._pending.start(user_id, INPUT_ADD_MODERATOR)
            return AdminReply(
                text=render_add_prompt(),
                keyboard=add_cancel_keyboard(),
                notice="Жду пользователя.",
            )
        if action == ACTION_DELETE and parts:
            return await self._confirm_delete(parts[0])
        if action == ACTION_DELETE_CONFIRM and parts:
            return await self._delete(parts[0])
        if action == ACTION_REPORT:
            return await self.show_report()
        if action == ACTION_TOGGLE and len(parts) >= 2:
            return await self._toggle(parts[0], parts[1])
        if action == ACTION_TODAY:
            return await self._send_today(user_id)
        return invalid_button_reply()

    def has_pending_input(self, user_id: int | None) -> bool:
        """True while this panel waits for a person from ``user_id``."""
        pending = self._pending.get(user_id)
        return pending is not None and pending.kind in MODERATOR_INPUT_KINDS

    # ------------------------------------------------------------- the list

    async def _views(self) -> list[ModeratorView]:
        rows = await self._repository.list_moderators()
        return [
            ModeratorView(user_id=int(row.user_id), name=getattr(row, "display_name", "") or "")
            for row in rows
        ]

    async def show_list(self, *, notice: str = "", page: int = 0) -> AdminReply:
        """Show one page of the list of moderators (a stale page is clamped)."""
        views = await self._views()
        return AdminReply(
            text=render_moderators(views, page),
            keyboard=moderators_keyboard(views, page),
            notice=notice,
        )

    async def _confirm_delete(self, raw_id: str) -> AdminReply:
        user_id = self._parse_id(raw_id)
        view = next((v for v in await self._views() if v.user_id == user_id), None)
        if view is None:
            return await self.show_list(notice=ALREADY_REMOVED_MESSAGE)
        return AdminReply(
            text=render_delete_confirm(view),
            keyboard=delete_confirm_keyboard(view.user_id),
        )

    async def _delete(self, raw_id: str) -> AdminReply:
        user_id = self._parse_id(raw_id)
        removed = user_id is not None and await self._access.remove_moderator(user_id)
        if not removed:
            return await self.show_list(notice=ALREADY_REMOVED_MESSAGE)
        return await self.show_list(notice="Модератор удалён")

    @staticmethod
    def _parse_id(raw: str) -> int | None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- input

    async def execute_input(
        self, *, incoming: IncomingInput, user_id: int | None
    ) -> InputOutcome:
        """Handle the message answering the add prompt."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized moderators input ignored")
            self._pending.clear(user_id)
            return InputOutcome(reply=denied_reply())
        if not self.has_pending_input(user_id):
            return InputOutcome(reply=AdminReply())

        if incoming.shared_users:
            if incoming.shared_request_id != USER_REQUEST_ID:
                return self._hint("Не удалось понять выбор. Попробуйте ещё раз.")
            new_id, name = incoming.shared_users[0]
            return await self._finish_add(user_id, new_id, name)
        if incoming.forward_hidden:
            return self._hint(
                "Этот человек скрыл свой аккаунт, поэтому Telegram не показывает его ID. "
                "Попросите его отправить боту команду /start и сообщить вам ID, "
                "который покажет бот, затем отправьте этот ID сюда."
            )
        if incoming.forward_user is not None:
            new_id, name, is_bot = incoming.forward_user
            if is_bot:
                return self._hint("Бота нельзя назначить модератором.")
            return await self._finish_add(user_id, new_id, name)

        text = " ".join(str(incoming.text or "").split())
        if not text:
            return self._hint(
                "Жду пользователя: нажмите «👤 Выбрать пользователя», отправьте "
                "числовой ID или перешлите сообщение этого человека."
            )
        if len(text) > MAX_INPUT_LENGTH:
            return self._hint("Сообщение слишком длинное. Отправьте только числовой ID.")
        if text.startswith("/"):
            return self._hint(
                "Сейчас я жду пользователя для роли модератора. "
                "Завершите ввод или нажмите «Отмена»."
            )
        number = parse_user_id(text)
        if number is None:
            if text.isascii() and text.lstrip("-").isdigit():
                return self._hint(
                    "ID должен быть положительным числом (до 13 цифр). "
                    "Проверьте и отправьте ещё раз."
                )
            return self._hint(
                "Не похоже на ID. Отправьте число, например 123456789, "
                "выберите человека кнопкой или перешлите его сообщение."
            )
        return await self._finish_add(user_id, number, "")

    @staticmethod
    def _hint(text: str) -> InputOutcome:
        return InputOutcome(
            reply=AdminReply(
                text=f"👥 <b>Новый модератор</b>\n\n⚠️ {html.escape(text, quote=False)}",
                keyboard=add_cancel_keyboard(),
                notice=text,
                alert=False,
            )
        )

    async def _finish_add(self, admin_id: int | None, new_id: int, name: str) -> InputOutcome:
        if new_id <= 0 or new_id > MAX_USER_ID:
            return self._hint("ID должен быть положительным числом (до 13 цифр).")
        name = clean_name(name)
        # Consume the slot before any await: of two concurrent answers exactly
        # one proceeds, and a slot another panel opens meanwhile is never erased.
        slot = self._pending.take(admin_id, INPUT_ADD_MODERATOR)
        if slot is None:
            return InputOutcome(reply=AdminReply())
        try:
            result = await self._access.add_moderator(new_id, name, admin_id)
        except Exception:
            self._pending.reopen(admin_id, slot)
            raise
        who = safe_name(name) or f"ID {new_id}"
        lines: list[str]
        if result is AddModeratorResult.IS_ADMIN:
            lines = ["Это администратор — у него и так все права."]
        elif result is AddModeratorResult.LIMIT_REACHED:
            lines = [f"Достигнут предел: {MAX_MODERATORS} модераторов."]
        elif result is AddModeratorResult.ALREADY_MODERATOR:
            lines = [f"{who} уже модератор."]
        else:
            lines = [f"✅ {who} назначен модератором."]
            lines.extend(await self._send_welcome(new_id))
        return InputOutcome(
            reply=AdminReply(text="\n".join(lines), notice=lines[0]),
            done=True,
            followup=await self.show_list(),
        )

    async def _send_welcome(self, new_id: int) -> list[str]:
        """Tell the new moderator; explain to the admin what to do if that fails."""
        try:
            await self._bot.send_message(new_id, WELCOME_TEXT)
        except Exception as exc:  # noqa: BLE001 - the role is assigned either way
            logger.info("cannot greet the new moderator: %s", type(exc).__name__)
        else:
            return ["Ему отправлено уведомление."]
        username = ""
        try:
            me = await self._bot.get_me()
            username = getattr(me, "username", "") or ""
        except Exception as exc:  # noqa: BLE001 - the hint works without the name
            logger.debug("cannot read the bot username: %s", type(exc).__name__)
        opener = f"@{html.escape(username, quote=False)}" if username else "бота"
        return [
            f"Человек ещё не запускал бота. Попросите его открыть {opener} и нажать "
            "«Запустить» (Start) — иначе карточки до него не дойдут."
        ]

    # --------------------------------------------------------------- report

    async def _chat_label(self, admin_id: int) -> str:
        name = self._chat_names.get(admin_id)
        if name is None:
            name = ""
            try:
                chat = await self._bot.get_chat(admin_id)
                name = clean_name(
                    getattr(chat, "full_name", None) or getattr(chat, "title", None) or ""
                )
            except Exception as exc:  # noqa: BLE001 - fall back to the bare id
                logger.debug("cannot read an admin name: %s", type(exc).__name__)
            if name:
                self._chat_names[admin_id] = name
        return name or f"ID {admin_id}"

    async def _admin_rows(self) -> list[tuple[int, str, bool]]:
        recipients = set(self._store.get_report_recipients(self._settings))
        owner = self._access.owner_id
        rows = []
        for admin_id in self._settings.admin_ids:
            label = await self._chat_label(admin_id)
            if admin_id == owner:
                label = f"{label} (владелец)"
            rows.append((admin_id, label, admin_id in recipients))
        return rows

    async def show_report(self, *, notice: str = "") -> AdminReply:
        """Show who receives the daily report."""
        rows = await self._admin_rows()
        return AdminReply(
            text=render_report_screen(self._settings.daily_report_time, rows),
            keyboard=report_keyboard(rows),
            notice=notice,
        )

    async def _toggle(self, raw_id: str, raw_state: str) -> AdminReply:
        admin_id = self._parse_id(raw_id)
        if admin_id is None or not self._settings.is_admin(admin_id):
            return await self.show_report(notice="Этот человек больше не администратор")
        want = raw_state == "1"
        current = set(self._store.get_report_recipients(self._settings))
        if (admin_id in current) == want:
            return await self.show_report(notice="Уже так и настроено")
        if want:
            current.add(admin_id)
        else:
            current.discard(admin_id)
        ordered = [a for a in self._settings.admin_ids if a in current]
        self._store.set_report_recipients(self._settings, ordered)
        return await self.show_report(notice="Сохранено")

    async def _send_today(self, user_id: int | None) -> AdminReply:
        text = await build_daily_report(self._repository, self._today())
        try:
            await self._bot.send_message(
                user_id, text, parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as exc:  # noqa: BLE001 - tell the admin, do not crash
            logger.warning("cannot send the report on demand: %s", type(exc).__name__)
            return AdminReply(notice="Не удалось отправить сводку.", alert=True)
        return AdminReply(notice="Сводка отправлена вам в личные сообщения.")


# ------------------------------------------------------------------ handlers


async def handle_moderators_command(
    message: Message, service: ModeratorsAdminService
) -> None:
    """Handle ``/moderators`` from an administrator."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await send_reply(message, await service.execute_command(user_id=user_id))


async def handle_moderators_callback(
    callback: CallbackQuery, service: ModeratorsAdminService
) -> None:
    """Handle one button press of the moderators panel."""
    user_id = getattr(getattr(callback, "from_user", None), "id", None)
    reply = await service.execute_callback(data=callback.data, user_id=user_id)
    await apply_callback_reply(callback, reply)

    parsed = parse_callback_payload(callback.data)
    message = getattr(callback, "message", None)
    if parsed is None or message is None or not service.is_authorized(user_id):
        return
    action = parsed[0]
    if action == ACTION_ADD and service.has_pending_input(user_id):
        await message.answer(
            "Нажмите кнопку ниже, чтобы выбрать человека.",
            reply_markup=user_picker_keyboard(),
        )
    elif action == ACTION_CANCEL:
        await message.answer(CANCELLED_MESSAGE, reply_markup=ReplyKeyboardRemove())


async def handle_moderators_input(
    message: Message, service: ModeratorsAdminService
) -> None:
    """Handle the message answering the add prompt."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    outcome = await service.execute_input(
        incoming=incoming_from_message(message), user_id=user_id
    )
    reply = outcome.reply
    if not reply.text and not reply.notice:
        return
    if outcome.done:
        await message.answer(
            reply.text or reply.notice,
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
            disable_web_page_preview=True,
        )
        if outcome.followup is not None:
            await send_reply(message, outcome.followup)
        return
    await send_reply(message, reply)


async def handle_start(message: Message, service: ModeratorsAdminService) -> None:
    """Handle ``/start`` for every role."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await message.answer(
        service.start_text(user_id), parse_mode="HTML", disable_web_page_preview=True
    )


def build_moderators_router(service: ModeratorsAdminService, settings: Settings) -> Router:
    """Create the router carrying the moderators panel and ``/start``."""
    router = Router(name="moderators-admin")
    admin_only = AdminOnly(settings)
    ours = F.data.startswith(f"{CALLBACK_PREFIX}:")
    waiting = PendingInputFilter(service.pending_inputs, MODERATOR_INPUT_KINDS)

    @router.message(Command(*COMMANDS), admin_only)
    async def _on_command(message: Message) -> None:
        await handle_moderators_command(message, service)

    @router.message(Command(*COMMANDS))
    async def _on_unauthorized_command(message: Message) -> None:
        logger.warning("unauthorized moderators command ignored")
        await message.answer(DENIED_MESSAGE)

    # Any message type (users_shared and stickers have no text) is an answer
    # while the add prompt is open; other panels' commands are routed earlier.
    @router.message(admin_only, waiting)
    async def _on_input(message: Message) -> None:
        await handle_moderators_input(message, service)

    @router.message(Command("start"))
    async def _on_start(message: Message) -> None:
        await handle_start(message, service)

    @router.callback_query(ours, admin_only)
    async def _on_callback(callback: CallbackQuery) -> None:
        await handle_moderators_callback(callback, service)

    @router.callback_query(ours)
    async def _on_unauthorized_callback(callback: CallbackQuery) -> None:
        logger.warning("unauthorized moderators callback ignored")
        await callback.answer(DENIED_MESSAGE, show_alert=True)

    return router


__all__ = [
    "ACTION_ADD",
    "ACTION_CANCEL",
    "ACTION_DELETE",
    "ACTION_DELETE_CONFIRM",
    "ACTION_OPEN",
    "ACTION_REPORT",
    "ACTION_TODAY",
    "ACTION_TOGGLE",
    "COMMANDS",
    "MODERATOR_INPUT_KINDS",
    "IncomingInput",
    "InputOutcome",
    "ModeratorView",
    "ModeratorsAdminService",
    "USER_REQUEST_ID",
    "build_callback_data",
    "build_moderators_router",
    "handle_moderators_callback",
    "handle_moderators_command",
    "handle_moderators_input",
    "handle_start",
    "incoming_from_message",
    "moderators_keyboard",
    "render_moderators",
    "report_keyboard",
    "user_picker_keyboard",
]
