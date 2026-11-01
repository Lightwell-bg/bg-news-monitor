"""aiogram wiring: the access filters and the moderation callback handler."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardMarkup,
)

from news_monitor.config.settings import Settings
from news_monitor.storage.models import (
    EVENT_APPROVE,
    EVENT_CANCEL,
    EVENT_PUBLISH_NOW,
    EVENT_REJECT,
    ModerationEvent,
    NewsItem,
    NewsStatus,
)
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.access import AccessControl
from news_monitor.telegram.card import (
    build_keyboard,
    build_moderator_keyboard,
    build_scheduled_keyboard,
    parse_callback_data,
    render_admin_card,
    render_decision_line,
)
from news_monitor.telegram.filters import AdminOnly, CanModerate
from news_monitor.telegram.moderation import ActionOutcome, ActionResult, ModerationService
from news_monitor.telegram.moderators_admin import (
    ModeratorsAdminService,
    build_moderators_router,
)
from news_monitor.telegram.settings_admin import SettingsAdminService, build_settings_router
from news_monitor.telegram.sources_admin import SourceAdminService, build_sources_router

logger = logging.getLogger(__name__)

__all__ = [
    "AdminOnly",
    "CanModerate",
    "build_router",
    "create_bot",
    "create_dispatcher",
    "handle_moderation_callback",
    "refresh_cards_after_publication",
    "refresh_item_cards",
]

#: Item statuses in which a logged decision is still the current truth.
_EVENT_STATUSES: dict[str, frozenset[NewsStatus]] = {
    EVENT_APPROVE: frozenset(
        {
            NewsStatus.SCHEDULED,
            NewsStatus.APPROVED,
            NewsStatus.PUBLISHED,
            NewsStatus.PUBLICATION_FAILED,
        }
    ),
    EVENT_PUBLISH_NOW: frozenset(
        {NewsStatus.APPROVED, NewsStatus.PUBLISHED, NewsStatus.PUBLICATION_FAILED}
    ),
    EVENT_REJECT: frozenset({NewsStatus.REJECTED_BY_ADMIN}),
    EVENT_CANCEL: frozenset({NewsStatus.AWAITING_APPROVAL}),
}


def _keyboard_for_chat(
    chat_id: int,
    item: NewsItem,
    access: AccessControl | None,
    delay_max: int | None,
) -> InlineKeyboardMarkup | None:
    """Buttons a chat gets under a card, by its role and the item status."""
    if access is None:
        return None
    status = item.status_enum
    if status is NewsStatus.AWAITING_APPROVAL:
        if access.is_admin(chat_id):
            return build_keyboard(item, delay_max_minutes=delay_max)
        if access.is_moderator(chat_id):
            return build_moderator_keyboard(item, delay_max_minutes=delay_max)
        return None
    if status is NewsStatus.SCHEDULED and access.is_admin(chat_id):
        return build_scheduled_keyboard(item)
    return None


def _decision_status_line(item: NewsItem, event: ModerationEvent | None) -> str | None:
    """The "who decided" line, or None when the last event no longer applies."""
    if event is None:
        return None
    if item.status_enum not in _EVENT_STATUSES.get(event.action, frozenset()):
        return None
    return render_decision_line(event, item) or None


async def refresh_item_cards(
    bot: Bot,
    access: AccessControl | None,
    repository: NewsRepository | None,
    item: NewsItem,
    *,
    status_line: str | None = None,
    delay_max: int | None = None,
) -> list[tuple[int, int]]:
    """Edit every delivered copy of an item's card and return the copies.

    The text is the card plus the optional status line; the buttons depend on
    the role of each chat and on the item status. A failed edit is logged and
    swallowed, so one unreachable chat never stops the others.
    """
    if repository is not None:
        copies = await repository.list_card_messages(item.id)
    elif item.admin_chat_id is not None and item.admin_message_id is not None:
        copies = [(int(item.admin_chat_id), int(item.admin_message_id))]
    else:
        copies = []

    text = render_admin_card(item)
    if status_line:
        text = f"{text}\n\n{status_line}"
    for chat_id, message_id in copies:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
                reply_markup=_keyboard_for_chat(chat_id, item, access, delay_max),
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 - one failed copy must not stop the rest
            logger.warning("cannot refresh card %s: %s", item.id, type(exc).__name__)
    return copies


async def handle_moderation_callback(
    callback: CallbackQuery, service: ModerationService, bot: Bot | None = None
) -> None:
    """Handle one moderation button press.

    Authorization is verified twice: by the router filter and again inside the
    service, so a handler registered without the filter still cannot publish.
    After a decision every delivered copy of the card is refreshed, so the
    other recipients see who decided and lose the buttons that no longer apply.
    """
    parsed = parse_callback_data(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    action, item_id = parsed
    user = callback.from_user
    user_id = getattr(user, "id", None)
    user_name = getattr(user, "full_name", "") or ""
    result = await service.handle_action(
        action=action, item_id=item_id, user_id=user_id, user_name=user_name
    )

    await callback.answer(result.message, show_alert=result.outcome is ActionOutcome.DENIED)

    if result.outcome is ActionOutcome.DENIED:
        return
    if not (result.changed or result.outcome is ActionOutcome.ALREADY_HANDLED):
        return

    item = result.item
    message = getattr(callback, "message", None)
    copies: list[tuple[int, int]] = []
    status_line: str | None = None
    keyboard: InlineKeyboardMarkup | None = None

    if item is not None:
        status_line = _decision_status_line(
            item, await service.repository.latest_event(item.id)
        )
        delay_max = service.publish_delay_max_minutes
        bot = bot or getattr(callback, "bot", None)
        if bot is not None:
            copies = await refresh_item_cards(
                bot,
                service.access,
                service.repository,
                item,
                status_line=status_line,
                delay_max=delay_max,
            )
        keyboard = _keyboard_for_chat(user_id, item, service.access, delay_max)

    if message is None:
        return

    # A card that is not among the recorded copies (or no bot to edit with) is
    # refreshed through the callback message itself.
    chat = getattr(message, "chat", None)
    pressed = (getattr(chat, "id", None), getattr(message, "message_id", None))
    if pressed in copies:
        return

    if keyboard is not None and item is not None:
        text = render_admin_card(item)
        if status_line:
            text = f"{text}\n\n{status_line}"
        try:
            await message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 - a failed edit must not crash the bot
            logger.warning("cannot refresh card %s: %s", item_id, type(exc).__name__)
        return

    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 - a failed edit must not crash the bot
        logger.debug("cannot clear buttons for %s: %s", item_id, type(exc).__name__)


_PUBLICATION_STATUS_LINES: dict[ActionOutcome, str] = {
    ActionOutcome.PUBLISHED: "✅ Опубликовано по расписанию",
    ActionOutcome.PUBLISHED_DRY_RUN: "Черновой режим: не отправлено",
    ActionOutcome.PUBLICATION_FAILED: "⚠️ Публикация не удалась",
}


async def refresh_cards_after_publication(
    bot: Bot,
    results: list[ActionResult],
    *,
    repository: NewsRepository | None = None,
) -> None:
    """Update every copy of the card of each item the scheduler has just processed.

    Buttons are removed and a status line is appended. A failed edit is logged
    and never raised: the publication itself is already done. Without a
    repository only the legacy single admin card is updated.
    """
    for result in results:
        item = result.item
        status_line = _PUBLICATION_STATUS_LINES.get(result.outcome)
        if item is None or status_line is None:
            continue
        try:
            await refresh_item_cards(
                bot, None, repository, item, status_line=status_line
            )
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not stop the job
            logger.warning("cannot refresh card %s: %s", item.id, type(exc).__name__)


def build_router(
    service: ModerationService,
    settings: Settings,
    access: AccessControl | None = None,
) -> Router:
    """Create the router carrying the moderation handler.

    Administrators and moderators reach the handler; the service still limits
    a moderator to approve and reject.
    """
    access = access if access is not None else service.access
    router = Router(name="moderation")

    @router.callback_query(CanModerate(access))
    async def _on_callback(callback: CallbackQuery, bot: Bot) -> None:
        await handle_moderation_callback(callback, service, bot)

    @router.callback_query()
    async def _on_unauthorized(callback: CallbackQuery) -> None:
        logger.warning("unauthorized callback ignored")
        await callback.answer("Недостаточно прав для этого действия.", show_alert=True)

    return router


def create_bot(settings: Settings) -> Bot:
    """Create the aiogram bot from the configured token."""
    token = settings.bot_token
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
    return Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def register_admin_commands(bot: Bot, settings: Settings) -> None:
    """Expose the source panel commands only in configured admin chats."""
    commands = [
        BotCommand(command="sources", description="Источники"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="moderators", description="Модераторы"),
        BotCommand(command="check", description="Проверить новости сейчас"),
    ]
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:  # noqa: BLE001 - command menu must not stop polling
            logger.warning("cannot register admin commands: %s", type(exc).__name__)


def create_dispatcher(
    service: ModerationService,
    settings: Settings,
    source_admin: SourceAdminService | None = None,
    settings_admin: SettingsAdminService | None = None,
    access: AccessControl | None = None,
    moderators_admin: ModeratorsAdminService | None = None,
) -> Dispatcher:
    """Create a dispatcher with the admin and moderation routers registered.

    The administration routers are included first: each one reacts only to its
    own callback prefix and commands, so every other update still reaches the
    moderation router below them.
    """
    dispatcher = Dispatcher()
    if source_admin is not None:
        dispatcher.include_router(build_sources_router(source_admin, settings))
    if settings_admin is not None:
        dispatcher.include_router(build_settings_router(settings_admin, settings))
    if moderators_admin is not None:
        dispatcher.include_router(build_moderators_router(moderators_admin, settings))
    dispatcher.include_router(build_router(service, settings, access))
    return dispatcher
