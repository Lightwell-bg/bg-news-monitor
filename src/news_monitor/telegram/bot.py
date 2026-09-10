"""aiogram wiring: the admin only filter and the moderation callback handler."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery

from news_monitor.config.settings import Settings
from news_monitor.telegram.card import build_keyboard, parse_callback_data, render_admin_card
from news_monitor.telegram.moderation import ActionOutcome, ModerationService

logger = logging.getLogger(__name__)


class AdminOnly(BaseFilter):
    """Passes only for numeric ids listed in the administrator settings."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self, event: CallbackQuery) -> bool:
        user = getattr(event, "from_user", None)
        return self.settings.is_admin(getattr(user, "id", None))


async def handle_moderation_callback(
    callback: CallbackQuery, service: ModerationService
) -> None:
    """Handle one moderation button press.

    Authorization is verified twice: by the router filter and again inside the
    service, so a handler registered without the filter still cannot publish.
    """
    parsed = parse_callback_data(callback.data)
    if parsed is None:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    action, item_id = parsed
    user_id = getattr(callback.from_user, "id", None)
    result = await service.handle_action(action=action, item_id=item_id, user_id=user_id)

    await callback.answer(result.message, show_alert=result.outcome is ActionOutcome.DENIED)

    if result.outcome is ActionOutcome.DENIED:
        return

    message = getattr(callback, "message", None)
    if message is None:
        return

    if result.outcome is ActionOutcome.REGENERATED and result.item is not None:
        try:
            await message.edit_text(
                render_admin_card(result.item),
                parse_mode="HTML",
                reply_markup=build_keyboard(result.item),
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 - a failed edit must not crash the bot
            logger.warning("cannot refresh card %s: %s", item_id, type(exc).__name__)
        return

    if result.changed or result.outcome is ActionOutcome.ALREADY_HANDLED:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception as exc:  # noqa: BLE001 - a failed edit must not crash the bot
            logger.debug("cannot clear buttons for %s: %s", item_id, type(exc).__name__)


def build_router(service: ModerationService, settings: Settings) -> Router:
    """Create the router carrying the moderation handler."""
    router = Router(name="moderation")

    @router.callback_query(AdminOnly(settings))
    async def _on_callback(callback: CallbackQuery) -> None:
        await handle_moderation_callback(callback, service)

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


def create_dispatcher(service: ModerationService, settings: Settings) -> Dispatcher:
    """Create a dispatcher with the moderation router registered."""
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service, settings))
    return dispatcher
