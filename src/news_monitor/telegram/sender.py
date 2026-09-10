"""Delivery of moderation cards to the configured administrators."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from news_monitor.storage.models import NewsItem
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.card import build_keyboard, render_admin_card

logger = logging.getLogger(__name__)


@runtime_checkable
class MessageSender(Protocol):
    """Minimal Telegram sending surface used by the card sender."""

    async def send_message(self, chat_id: object, text: str, **kwargs: object) -> object:
        ...


class AdminCardSender:
    """Sends the private card and records the delivered message."""

    def __init__(
        self,
        sender: MessageSender,
        repository: NewsRepository,
        admin_ids: tuple[int, ...],
    ) -> None:
        self._sender = sender
        self._repository = repository
        self._admin_ids = tuple(admin_ids)

    async def send_card(self, item: NewsItem) -> NewsItem | None:
        """Deliver the card to the first reachable administrator.

        The item moves to AWAITING_APPROVAL only after a card was really sent,
        so an undelivered candidate can never be approved by a stale button.
        """
        if not self._admin_ids:
            logger.error("no administrator configured, card for item %s not sent", item.id)
            return None

        text = render_admin_card(item)
        keyboard = build_keyboard(item)

        for admin_id in self._admin_ids:
            try:
                message = await self._sender.send_message(
                    admin_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception as exc:  # noqa: BLE001 - try the next administrator
                logger.warning(
                    "cannot send card for item %s to admin: %s",
                    item.id,
                    type(exc).__name__,
                )
                continue
            message_id = getattr(message, "message_id", None)
            if message_id is None:  # pragma: no cover - defensive
                continue
            return await self._repository.mark_awaiting_approval(
                item.id, chat_id=admin_id, message_id=int(message_id)
            )

        logger.error("card for item %s could not be delivered", item.id)
        return None
