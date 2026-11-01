"""Delivery of moderation cards to the administrators and moderators."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from news_monitor.config.settings import Settings
from news_monitor.storage.models import NewsItem, NewsStatus
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.access import AccessControl
from news_monitor.telegram.card import (
    build_keyboard,
    build_moderator_keyboard,
    render_admin_card,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class MessageSender(Protocol):
    """Minimal Telegram sending surface used by the card sender."""

    async def send_message(self, chat_id: object, text: str, **kwargs: object) -> object:
        ...


class AdminCardSender:
    """Sends the private card to every recipient and records each copy."""

    def __init__(
        self,
        sender: MessageSender,
        repository: NewsRepository,
        admin_ids: tuple[int, ...],
        settings: Settings | None = None,
        access: AccessControl | None = None,
    ) -> None:
        self._settings = settings
        self._sender = sender
        self._repository = repository
        self._admin_ids = tuple(admin_ids)
        self._access = access

    def _recipients(self) -> list[int]:
        """Administrators in configured order, then moderators, without repeats."""
        recipients: list[int] = list(dict.fromkeys(self._admin_ids))
        if self._access is not None:
            for moderator_id in self._access.moderator_ids:
                if moderator_id not in recipients:
                    recipients.append(moderator_id)
        return recipients

    async def send_card(self, item: NewsItem) -> NewsItem | None:
        """Deliver the card to every administrator and moderator.

        The item moves to AWAITING_APPROVAL right after the first card was
        really sent, so an undelivered candidate can never be approved by a
        stale button, while a press on an early copy is already valid. A
        recipient that cannot be reached is skipped. Returns None only when no
        copy was delivered.
        """
        if not self._admin_ids:
            logger.error("no administrator configured, card for item %s not sent", item.id)
            return None

        text = render_admin_card(item)
        delay_max = (
            int(self._settings.publish_delay_max_minutes)
            if self._settings is not None
            else None
        )
        admin_keyboard = build_keyboard(item, delay_max_minutes=delay_max)
        moderator_keyboard = build_moderator_keyboard(item, delay_max_minutes=delay_max)

        delivered: NewsItem | None = None
        for chat_id in self._recipients():
            keyboard = admin_keyboard if chat_id in self._admin_ids else moderator_keyboard
            try:
                message = await self._sender.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception as exc:  # noqa: BLE001 - try the next recipient
                logger.warning(
                    "cannot send card for item %s to a recipient: %s",
                    item.id,
                    type(exc).__name__,
                )
                continue
            message_id = getattr(message, "message_id", None)
            if message_id is None:  # pragma: no cover - defensive
                continue
            if delivered is None:
                delivered = await self._repository.mark_awaiting_approval(
                    item.id, chat_id=chat_id, message_id=int(message_id)
                )
            await self._repository.add_card_message(item.id, chat_id, int(message_id))

        if delivered is None:
            logger.error("card for item %s could not be delivered", item.id)
        else:
            await self._refresh_if_decided(item.id, delay_max)
        return delivered

    async def _refresh_if_decided(self, item_id: int, delay_max: int | None) -> None:
        """Fix copies that went out with buttons after a decision was already made.

        A press on an early copy can decide the item while later copies are
        still being sent; those later copies carry stale buttons.
        """
        try:
            latest = await self._repository.get(item_id)
            if latest is None or latest.status_enum is NewsStatus.AWAITING_APPROVAL:
                return
            if not hasattr(self._sender, "edit_message_text"):
                return
            # Imported here: bot.py depends on this package's other modules.
            from news_monitor.telegram.bot import (
                _decision_status_line,
                refresh_item_cards,
            )

            status_line = _decision_status_line(
                latest, await self._repository.latest_event(item_id)
            )
            await refresh_item_cards(
                self._sender,  # type: ignore[arg-type]
                self._access,
                self._repository,
                latest,
                status_line=status_line,
                delay_max=delay_max,
            )
        except Exception as exc:  # noqa: BLE001 - the cards are delivered either way
            logger.warning(
                "cannot refresh cards of item %s after fan-out: %s",
                item_id,
                type(exc).__name__,
            )
