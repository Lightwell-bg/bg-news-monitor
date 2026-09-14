"""Administration of ``sources.yaml`` from Telegram.

Only the numeric ids configured in ``TELEGRAM_ADMIN_IDS`` (or the legacy
``TELEGRAM_ADMIN_ID``) may see or change anything here. Authorization is checked
twice, exactly like moderation: once by the router filter and once inside the
service, so a handler registered without the filter still cannot edit the file.

The panel shown to the administrator is made of inline buttons only: it never
prints a technical source id and never asks for one. A value that cannot be a
button (a section, a topic, an exact interval) is collected by a short
step-by-step prompt that can always be cancelled with a button.

The service never touches ``.env``, never prints a setting that could hold a
secret and writes only to the single file owned by :class:`SourcesStore`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from typing import Callable

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from news_monitor.config.settings import Settings
from news_monitor.config.sources import SourceConfig
from news_monitor.config.sources_store import (
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    EditResult,
    SourcesStore,
    SourcesStoreError,
    parse_flag,
    parse_interval,
    parse_position,
)
from news_monitor.sources.registry import available_adapters
from news_monitor.telegram.admin_ui import (
    CANCELLED_MESSAGE,
    DENIED_MESSAGE,
    MAX_INPUT_LENGTH,
    NOTICE_LIMIT,
    SETTINGS_OPEN_ACTION,
    SETTINGS_PREFIX,
    SOURCES_LIST_ACTION,
    SOURCES_PREFIX,
    TEXT_LIMIT,
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

CALLBACK_PREFIX = SOURCES_PREFIX
INTERVAL_STEP_MINUTES = 5

ACTION_LIST = SOURCES_LIST_ACTION
ACTION_OPEN = "open"
ACTION_ENABLE = "on"
ACTION_DISABLE = "off"
ACTION_INTERVAL = "iv"
ACTION_INTERVAL_INPUT = "ivin"
ACTION_SECTIONS = "secs"
ACTION_SECTION_ADD = "sadd"
ACTION_SECTION_DELETE = "sdel"
ACTION_TOPICS_IN = "tin"
ACTION_TOPICS_EX = "tex"
ACTION_TOPIC_ADD = "tadd"
ACTION_TOPIC_DELETE = "tdel"
ACTION_CANCEL = "cxl"

#: Actions that ask the administrator for a value instead of changing one.
_PROMPT_ACTIONS = frozenset({ACTION_SECTION_ADD, ACTION_TOPIC_ADD, ACTION_INTERVAL_INPUT})

#: Kinds of step-by-step input owned by this panel.
INPUT_SECTION = "section"
INPUT_TOPIC = "topic"
INPUT_INTERVAL = "interval"
SOURCE_INPUT_KINDS: frozenset[str] = frozenset(
    {INPUT_SECTION, INPUT_TOPIC, INPUT_INTERVAL}
)

_KIND_CODES: dict[str, str] = {"in": "inclusion", "ex": "exclusion"}
_KIND_BY_NAME: dict[str, str] = {"inclusion": "in", "exclusion": "ex"}
_KIND_TITLES: dict[str, str] = {
    "inclusion": "Темы включения",
    "exclusion": "Темы исключения",
}
_KIND_SINGULAR: dict[str, str] = {
    "inclusion": "тему включения",
    "exclusion": "тему исключения",
}

RESTART_MESSAGE = "Сохранено. Изменения применятся после перезапуска сервиса."
LIST_HINT = "Выберите источник, чтобы посмотреть и изменить его настройки."
EMPTY_LIST_TEXT = "Источники не настроены."

USAGE = (
    "<b>Управление источниками</b>\n"
    "/sources — панель источников\n"
    "/source &lt;id&gt; — карточка источника\n"
    "/source_set &lt;id&gt; enabled on|off\n"
    "/source_set &lt;id&gt; interval &lt;минуты&gt;\n"
    "/source_add &lt;id&gt; section &lt;название&gt; | &lt;url&gt;\n"
    "/source_add &lt;id&gt; topic_in|topic_ex &lt;тема&gt;\n"
    "/source_del &lt;id&gt; section|topic_in|topic_ex &lt;номер&gt;"
)

COMMANDS: tuple[str, ...] = (
    "sources",
    "source",
    "source_set",
    "source_add",
    "source_del",
)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """What the running process could do with the saved configuration."""

    running: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()
    error: str = ""


#: Applies the saved file to the running process and reports what happened.
SourcesApplier = Callable[[], ApplyResult]


def build_callback_data(action: str, *parts: object) -> str:
    """Build a callback payload of this panel, refusing an oversized one."""
    return encode_callback(CALLBACK_PREFIX, action, *parts)


def parse_callback_payload(data: str | None) -> tuple[str, tuple[str, ...]] | None:
    """Parse our callback payload, returning None when it is not ours."""
    return decode_callback(CALLBACK_PREFIX, data)


def _button(text: str, action: str, *parts: object) -> InlineKeyboardButton | None:
    return button(CALLBACK_PREFIX, text, action, *parts)


def _settings_button() -> InlineKeyboardButton | None:
    return button(SETTINGS_PREFIX, "⚙️ Настройки", SETTINGS_OPEN_ACTION)


def _status_icon(source: SourceConfig) -> str:
    return "\U0001f7e2" if source.enabled else "⚪"


def _status_word(source: SourceConfig) -> str:
    return "включён" if source.enabled else "выключен"


def _is_ready(source: SourceConfig) -> bool:
    return source.adapter_type in available_adapters()


def _site_of(source: SourceConfig) -> str:
    """The readable host of a source, without the technical adapter name."""
    text = str(source.base_url)
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.rstrip("/")


def _topics(source: SourceConfig, kind: str) -> list[str]:
    rules = source.inclusion_rules if kind == "inclusion" else source.exclusion_rules
    return list(rules.topics)


def _numbered(values: list[str]) -> str:
    if not values:
        return "—"
    return "\n".join(
        f"{number}. {escape(value)}" for number, value in enumerate(values, start=1)
    )


def _header(source: SourceConfig) -> str:
    return f"{_status_icon(source)} <b>{escape(source.name)}</b>"


# ------------------------------------------------------------------ rendering


def render_sources_list(sources: list[SourceConfig]) -> str:
    """Render the short introduction shown above the list of source buttons."""
    if not sources:
        return EMPTY_LIST_TEXT
    return "\n".join(["<b>Источники</b>", "", LIST_HINT])


def render_source(source: SourceConfig) -> str:
    """Render one source card with every editable setting and no identifiers."""
    lines = [
        _header(source),
        f"Состояние: <b>{_status_word(source)}</b>",
        f"Интервал опроса: <b>{source.min_interval_minutes}</b> мин "
        f"({MIN_INTERVAL_MINUTES}–{MAX_INTERVAL_MINUTES})",
        f"Сайт: {escape(_site_of(source))}",
    ]
    if not _is_ready(source):
        lines.append("⚠️ Источник пока не поддерживается и не может быть включён.")
    lines.extend(
        [
            "",
            f"<b>Разделы ({len(source.sections)})</b>",
            _numbered(
                [f"{section.name} — {str(section.url)}" for section in source.sections]
            ),
        ]
    )
    for kind in ("inclusion", "exclusion"):
        topics = _topics(source, kind)
        lines.extend(
            ["", f"<b>{_KIND_TITLES[kind]} ({len(topics)})</b>", _numbered(topics)]
        )
    return "\n".join(lines)


def render_topics(source: SourceConfig, kind: str) -> str:
    """Render one topic list of a source."""
    topics = _topics(source, kind)
    return "\n".join(
        [
            f"{_header(source)} · {_KIND_TITLES[kind]} ({len(topics)})",
            "",
            _numbered(topics),
        ]
    )


def render_sections(source: SourceConfig) -> str:
    """Render the listing pages of a source."""
    return "\n".join(
        [
            f"{_header(source)} · Разделы ({len(source.sections)})",
            "",
            _numbered(
                [f"{section.name} — {str(section.url)}" for section in source.sections]
            ),
        ]
    )


def _source_button_text(source: SourceConfig) -> str:
    return (
        f"{_status_icon(source)} {source.name} · "
        f"{_status_word(source)} · {source.min_interval_minutes} мин"
    )


def sources_keyboard(sources: list[SourceConfig]) -> InlineKeyboardMarkup:
    """One button per source plus the entry to the settings screen."""
    rows: list[list[InlineKeyboardButton | None]] = [
        [_button(_source_button_text(source), ACTION_OPEN, source.id)]
        for source in sources
    ]
    rows.append([_settings_button()])
    return keyboard_of(*rows)


def source_keyboard(source: SourceConfig) -> InlineKeyboardMarkup:
    """Buttons for every runtime-editable setting of one source."""
    current = source.min_interval_minutes
    slower = min(MAX_INTERVAL_MINUTES, current + INTERVAL_STEP_MINUTES)
    faster = max(MIN_INTERVAL_MINUTES, current - INTERVAL_STEP_MINUTES)
    toggle = (
        _button("⚪ Выключить", ACTION_DISABLE, source.id)
        if source.enabled
        else _button("\U0001f7e2 Включить", ACTION_ENABLE, source.id)
    )
    return keyboard_of(
        [toggle, _button("\U0001f504 Обновить", ACTION_OPEN, source.id)],
        [
            _button(f"➖ {faster} мин", ACTION_INTERVAL, source.id, faster),
            _button(f"➕ {slower} мин", ACTION_INTERVAL, source.id, slower),
            _button("✏️ Интервал", ACTION_INTERVAL_INPUT, source.id),
        ],
        [_button("\U0001f5c2 Разделы", ACTION_SECTIONS, source.id)],
        [
            _button("✅ Темы включения", ACTION_TOPICS_IN, source.id),
            _button("\U0001f6ab Темы исключения", ACTION_TOPICS_EX, source.id),
        ],
        [_button("⬅ К списку", ACTION_LIST), _settings_button()],
    )


def _delete_rows(
    source_id: str, count: int, action: str, *prefix: object
) -> list[list[InlineKeyboardButton | None]]:
    buttons = [
        _button(f"❌ {number}", action, source_id, *prefix, number - 1)
        for number in range(1, count + 1)
    ]
    return [buttons[start : start + 4] for start in range(0, len(buttons), 4)]


def topics_keyboard(source: SourceConfig, kind: str) -> InlineKeyboardMarkup:
    """Add and delete buttons for one topic list."""
    code = _KIND_BY_NAME[kind]
    rows = _delete_rows(source.id, len(_topics(source, kind)), ACTION_TOPIC_DELETE, code)
    rows.append([_button("➕ Добавить тему", ACTION_TOPIC_ADD, source.id, code)])
    rows.append([_button("⬅ К источнику", ACTION_OPEN, source.id)])
    return keyboard_of(*rows)


def sections_keyboard(source: SourceConfig) -> InlineKeyboardMarkup:
    """Add and delete buttons for the listing pages."""
    rows = _delete_rows(source.id, len(source.sections), ACTION_SECTION_DELETE)
    rows.append([_button("➕ Добавить раздел", ACTION_SECTION_ADD, source.id)])
    rows.append([_button("⬅ К источнику", ACTION_OPEN, source.id)])
    return keyboard_of(*rows)


def cancel_keyboard(source_id: str) -> InlineKeyboardMarkup:
    """The single button that abandons a step-by-step input."""
    return keyboard_of([cancel_button(CALLBACK_PREFIX, ACTION_CANCEL, source_id)])


class SourceAdminService:
    """Authorizes, validates and applies every source setting change."""

    def __init__(
        self,
        *,
        store: SourcesStore,
        settings: Settings,
        applier: SourcesApplier | None = None,
        pending: PendingInputStore | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._applier = applier
        self._pending = pending if pending is not None else PendingInputStore()

    @property
    def pending_inputs(self) -> PendingInputStore:
        """The shared slot of awaited step-by-step answers."""
        return self._pending

    # --------------------------------------------------------- authorization

    def is_authorized(self, user_id: int | None) -> bool:
        """Only configured numeric administrator ids may manage sources."""
        return self._settings.is_admin(user_id)

    # ------------------------------------------------------------- entry points

    def execute_command(
        self, *, command: str, args: str, user_id: int | None
    ) -> AdminReply:
        """Handle one administrator command."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized sources command ignored")
            return denied_reply()
        self._pending.clear(user_id)
        try:
            return self._run_command(command, args or "")
        except SourcesStoreError as error:
            return _error_reply(error)

    def execute_callback(self, *, data: str | None, user_id: int | None) -> AdminReply:
        """Handle one administrator button press."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized sources callback ignored")
            return denied_reply(view_only=True)
        parsed = parse_callback_payload(data)
        if parsed is None:
            return invalid_button_reply()
        action, parts = parsed
        if action not in _PROMPT_ACTIONS:
            # Any other button abandons an unfinished prompt, so a stale
            # question can never consume a later message.
            self._pending.clear(user_id)
        try:
            return self._run_callback(action, parts, user_id)
        except SourcesStoreError as error:
            return _error_reply(error, view_only=True)

    def execute_input(self, *, text: str, user_id: int | None) -> AdminReply:
        """Handle the message answering a step-by-step prompt."""
        if not self.is_authorized(user_id):
            logger.warning("unauthorized sources input ignored")
            self._pending.clear(user_id)
            return denied_reply()
        pending = self._pending.get(user_id)
        if pending is None or pending.kind not in SOURCE_INPUT_KINDS:
            return AdminReply()

        value = " ".join(str(text or "").split())
        if len(value) > MAX_INPUT_LENGTH:
            return self._prompt_again(pending, TOO_LONG_MESSAGE)
        try:
            result = self._apply_input(pending, value)
        except SourcesStoreError as error:
            return self._prompt_again(pending, str(error)[:NOTICE_LIMIT])
        self._pending.clear(user_id)
        return self._saved(result)

    def has_pending_input(self, user_id: int | None) -> bool:
        """True while this panel is waiting for a value from ``user_id``."""
        pending = self._pending.get(user_id)
        return pending is not None and pending.kind in SOURCE_INPUT_KINDS

    # ------------------------------------------------------------- commands

    def _run_command(self, command: str, args: str) -> AdminReply:
        parts = args.split()
        if command == "sources":
            return self.list_sources()
        if command == "source":
            if not parts:
                return AdminReply(text=USAGE)
            return self.show_source(parts[0])
        if command not in {"source_set", "source_add", "source_del"}:
            return AdminReply(text=USAGE)
        if len(parts) < 2:
            return AdminReply(text=USAGE)

        source_id, field = parts[0], parts[1].casefold()
        value = args.split(maxsplit=2)[2] if len(parts) > 2 else ""

        if command == "source_set":
            if field == "enabled":
                return self.set_enabled(source_id, parse_flag(value))
            if field == "interval":
                return self.set_interval(source_id, parse_interval(value))
            return AdminReply(text=USAGE)

        if command == "source_add":
            if field == "section":
                name, separator, url = value.partition("|")
                if not separator:
                    return AdminReply(text=USAGE)
                return self.add_section(source_id, name, url)
            if field in {"topic_in", "topic_ex"}:
                return self.add_topic(source_id, _kind_of(field), value)
            return AdminReply(text=USAGE)

        if field == "section":
            source = self._store.get(source_id)
            index = parse_position(value, total=len(source.sections))
            return self.remove_section(source_id, index)
        if field in {"topic_in", "topic_ex"}:
            kind = _kind_of(field)
            source = self._store.get(source_id)
            index = parse_position(value, total=len(_topics(source, kind)))
            return self.remove_topic(source_id, kind, index)
        return AdminReply(text=USAGE)

    # ------------------------------------------------------------ callbacks

    def _run_callback(
        self, action: str, parts: tuple[str, ...], user_id: int | None
    ) -> AdminReply:
        if action == ACTION_LIST:
            return self.list_sources()
        if not parts:
            return invalid_button_reply()
        source_id = parts[0]

        if action == ACTION_OPEN:
            return self.show_source(source_id)
        if action == ACTION_CANCEL:
            return self.cancel_input(source_id, notice=CANCELLED_MESSAGE)
        if action in {ACTION_ENABLE, ACTION_DISABLE}:
            return self.set_enabled(source_id, action == ACTION_ENABLE)
        if action == ACTION_INTERVAL and len(parts) > 1:
            return self.set_interval(source_id, parse_interval(parts[1]))
        if action == ACTION_INTERVAL_INPUT:
            return self.ask_for_interval(source_id, user_id)
        if action == ACTION_SECTIONS:
            return self.show_sections(source_id)
        if action == ACTION_SECTION_ADD:
            return self.ask_for_section(source_id, user_id)
        if action in {ACTION_TOPICS_IN, ACTION_TOPICS_EX}:
            kind = "inclusion" if action == ACTION_TOPICS_IN else "exclusion"
            return self.show_topics(source_id, kind)
        if action == ACTION_TOPIC_ADD and len(parts) > 1:
            kind = _KIND_CODES.get(parts[1])
            if kind is None:
                return invalid_button_reply()
            return self.ask_for_topic(source_id, kind, user_id)
        if action == ACTION_SECTION_DELETE and len(parts) > 1:
            return self.remove_section(source_id, _index_of(parts[1]))
        if action == ACTION_TOPIC_DELETE and len(parts) > 2:
            kind = _KIND_CODES.get(parts[1])
            if kind is None:
                return invalid_button_reply()
            return self.remove_topic(source_id, kind, _index_of(parts[2]))
        return invalid_button_reply()

    # -------------------------------------------------------------- actions

    def list_sources(self) -> AdminReply:
        """Show every configured source as a button."""
        sources = self._store.load()
        return AdminReply(
            text=render_sources_list(sources), keyboard=sources_keyboard(sources)
        )

    def show_source(self, source_id: str) -> AdminReply:
        """Show one source card."""
        source = self._store.get(source_id)
        return AdminReply(text=render_source(source), keyboard=source_keyboard(source))

    def show_sections(self, source_id: str) -> AdminReply:
        """Show the listing pages of a source."""
        source = self._store.get(source_id)
        return AdminReply(
            text=render_sections(source), keyboard=sections_keyboard(source)
        )

    def show_topics(self, source_id: str, kind: str) -> AdminReply:
        """Show one topic list of a source."""
        source = self._store.get(source_id)
        return AdminReply(
            text=render_topics(source, kind), keyboard=topics_keyboard(source, kind)
        )

    def set_enabled(self, source_id: str, enabled: bool) -> AdminReply:
        """Enable or disable a source.

        A source can be enabled only when its adapter is registered: otherwise
        the saved configuration could never be executed by the running process.
        """
        source = self._store.get(source_id)
        if enabled and not _is_ready(source):
            raise SourcesStoreError(
                "Источник пока не поддерживается, включение невозможно."
            )
        return self._saved(self._store.set_enabled(source_id, enabled))

    def set_interval(self, source_id: str, minutes: int) -> AdminReply:
        """Change the polling interval of a source."""
        return self._saved(self._store.set_interval(source_id, minutes))

    def add_section(self, source_id: str, name: str, url: str) -> AdminReply:
        """Add a listing page to a source."""
        return self._saved(self._store.add_section(source_id, name, url))

    def remove_section(self, source_id: str, index: int) -> AdminReply:
        """Remove a listing page from a source."""
        return self._saved(self._store.remove_section(source_id, index))

    def add_topic(self, source_id: str, kind: str, topic: str) -> AdminReply:
        """Add an inclusion or exclusion topic."""
        return self._saved(self._store.add_topic(source_id, kind, topic))

    def remove_topic(self, source_id: str, kind: str, index: int) -> AdminReply:
        """Remove an inclusion or exclusion topic."""
        return self._saved(self._store.remove_topic(source_id, kind, index))

    # -------------------------------------------------------- step by step

    def ask_for_section(self, source_id: str, user_id: int | None) -> AdminReply:
        """Ask for a new listing page instead of expecting a command."""
        source = self._store.get(source_id)
        self._pending.start(user_id, INPUT_SECTION, target=source.id)
        return self._prompt(source, _section_prompt(source))

    def ask_for_topic(
        self, source_id: str, kind: str, user_id: int | None
    ) -> AdminReply:
        """Ask for a new inclusion or exclusion topic."""
        source = self._store.get(source_id)
        self._pending.start(
            user_id, INPUT_TOPIC, target=source.id, extra=_KIND_BY_NAME[kind]
        )
        return self._prompt(source, _topic_prompt(kind))

    def ask_for_interval(self, source_id: str, user_id: int | None) -> AdminReply:
        """Ask for an exact polling interval."""
        source = self._store.get(source_id)
        self._pending.start(user_id, INPUT_INTERVAL, target=source.id)
        return self._prompt(source, _interval_prompt())

    def cancel_input(self, source_id: str, *, notice: str = "") -> AdminReply:
        """Abandon the awaited value and show the source card again."""
        reply = self.show_source(source_id)
        return AdminReply(
            text=reply.text, keyboard=reply.keyboard, notice=notice or CANCELLED_MESSAGE
        )

    def _apply_input(self, pending, value: str) -> EditResult:
        source_id = pending.target
        if pending.kind == INPUT_INTERVAL:
            return self._store.set_interval(source_id, parse_interval(value))
        if pending.kind == INPUT_TOPIC:
            kind = _KIND_CODES.get(pending.extra)
            if kind is None:  # pragma: no cover - only a corrupted pending slot
                raise SourcesStoreError("Неизвестный тип тем.")
            return self._store.add_topic(source_id, kind, value)
        name, separator, url = value.partition("|")
        if not separator:
            raise SourcesStoreError(
                "Ожидается «Название | https://…» одним сообщением."
            )
        return self._store.add_section(source_id, name, url)

    def _prompt(self, source: SourceConfig, question: str) -> AdminReply:
        text = "\n".join([_header(source), "", question])
        return AdminReply(
            text=text, keyboard=cancel_keyboard(source.id), notice="Жду сообщение."
        )

    def _prompt_again(self, pending, problem: str) -> AdminReply:
        """Repeat the question with the reason the last answer was refused."""
        try:
            source = self._store.get(pending.target)
        except SourcesStoreError as error:  # pragma: no cover - file changed meanwhile
            return _error_reply(error)
        question = _question_of(pending)
        text = "\n".join([_header(source), "", f"⚠️ {escape(problem)}", "", question])
        return AdminReply(
            text=text, keyboard=cancel_keyboard(source.id), notice=problem, alert=True
        )

    # -------------------------------------------------------------- private

    def _saved(self, result: EditResult) -> AdminReply:
        """Render the saved source together with the runtime status."""
        source = result.source
        note = self._apply(source.id)
        text = f"{render_source(source)}\n\n{result.description}\n{note}"
        return AdminReply(
            text=text, keyboard=source_keyboard(source), notice=result.description
        )

    def _apply(self, source_id: str) -> str:
        """Apply the saved file to the running process and describe the result."""
        if self._applier is None:
            return RESTART_MESSAGE
        try:
            outcome = self._applier()
        except Exception:  # noqa: BLE001 - a failed reload must not lose the save
            logger.exception("cannot apply the saved sources configuration")
            return "Сохранено, но применить без перезапуска не удалось."
        if outcome.error:
            return f"Сохранено. Применить сейчас не удалось: {outcome.error}"
        if source_id in outcome.restart_required:
            return "Сохранено. Для применения требуется перезапуск сервиса."
        if source_id in outcome.stopped:
            return "Сохранено и применено: опрос источника остановлен."
        if source_id in outcome.running:
            return "Сохранено и применено: источник опрашивается по новым настройкам."
        return RESTART_MESSAGE


def _section_prompt(source: SourceConfig) -> str:
    return (
        "Отправьте раздел одним сообщением:\n"
        "<code>Название | https://…</code>\n\n"
        f"Адрес должен быть на домене {escape(_site_of(source))}."
    )


def _topic_prompt(kind: str) -> str:
    return f"Отправьте {_KIND_SINGULAR[kind]} одним сообщением."


def _interval_prompt() -> str:
    return (
        "Отправьте интервал опроса в минутах "
        f"({MIN_INTERVAL_MINUTES}–{MAX_INTERVAL_MINUTES})."
    )


def _question_of(pending) -> str:
    if pending.kind == INPUT_INTERVAL:
        return _interval_prompt()
    if pending.kind == INPUT_TOPIC:
        return _topic_prompt(_KIND_CODES.get(pending.extra, "inclusion"))
    return "Отправьте раздел одним сообщением:\n<code>Название | https://…</code>"


def _kind_of(field: str) -> str:
    return "inclusion" if field == "topic_in" else "exclusion"


def _index_of(raw: str) -> int:
    try:
        index = int(raw)
    except (TypeError, ValueError):
        raise SourcesStoreError("Некорректный номер элемента.") from None
    if index < 0:
        raise SourcesStoreError("Некорректный номер элемента.")
    return index


def _error_reply(error: SourcesStoreError, *, view_only: bool = False) -> AdminReply:
    message = str(error)[:NOTICE_LIMIT]
    return AdminReply(text="" if view_only else message, notice=message, alert=True)


async def handle_admin_command(
    message: Message, command: str, args: str, service: SourceAdminService
) -> None:
    """Handle one source administration command."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    reply = service.execute_command(command=command, args=args, user_id=user_id)
    await send_reply(message, reply)


async def handle_admin_input(message: Message, service: SourceAdminService) -> None:
    """Handle the message answering a step-by-step prompt."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    reply = service.execute_input(text=getattr(message, "text", "") or "", user_id=user_id)
    if not reply.text and not reply.notice:
        return
    await send_reply(message, reply)


async def handle_admin_callback(
    callback: CallbackQuery, service: SourceAdminService
) -> None:
    """Handle one source administration button press."""
    user_id = getattr(getattr(callback, "from_user", None), "id", None)
    reply = service.execute_callback(data=callback.data, user_id=user_id)
    await apply_callback_reply(callback, reply)


def build_sources_router(service: SourceAdminService, settings: Settings) -> Router:
    """Create the router carrying the source administration handlers."""
    router = Router(name="sources-admin")
    admin_only = AdminOnly(settings)
    ours = F.data.startswith(f"{CALLBACK_PREFIX}:")
    waiting = PendingInputFilter(service.pending_inputs, SOURCE_INPUT_KINDS)

    @router.message(Command(*COMMANDS), admin_only)
    async def _on_command(message: Message, command: CommandObject) -> None:
        await handle_admin_command(
            message, command.command, command.args or "", service
        )

    @router.message(Command(*COMMANDS))
    async def _on_unauthorized_command(message: Message) -> None:
        logger.warning("unauthorized sources command ignored")
        await message.answer(DENIED_MESSAGE)

    @router.message(F.text, ~F.text.startswith("/"), admin_only, waiting)
    async def _on_input(message: Message) -> None:
        await handle_admin_input(message, service)

    @router.callback_query(ours, admin_only)
    async def _on_callback(callback: CallbackQuery) -> None:
        await handle_admin_callback(callback, service)

    @router.callback_query(ours)
    async def _on_unauthorized_callback(callback: CallbackQuery) -> None:
        logger.warning("unauthorized sources callback ignored")
        await callback.answer(DENIED_MESSAGE, show_alert=True)

    return router


__all__ = [
    "ACTION_CANCEL",
    "ACTION_DISABLE",
    "ACTION_ENABLE",
    "ACTION_INTERVAL",
    "ACTION_INTERVAL_INPUT",
    "ACTION_LIST",
    "ACTION_OPEN",
    "ACTION_SECTIONS",
    "ACTION_SECTION_ADD",
    "ACTION_SECTION_DELETE",
    "ACTION_TOPICS_EX",
    "ACTION_TOPICS_IN",
    "ACTION_TOPIC_ADD",
    "ACTION_TOPIC_DELETE",
    "COMMANDS",
    "SOURCE_INPUT_KINDS",
    "TEXT_LIMIT",
    "AdminReply",
    "ApplyResult",
    "SourceAdminService",
    "build_callback_data",
    "build_sources_router",
    "handle_admin_callback",
    "handle_admin_command",
    "handle_admin_input",
    "parse_callback_payload",
    "render_source",
    "render_sources_list",
]
