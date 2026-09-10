"""Publication of an approved item to the Telegram channel."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from news_monitor.filters.guards import check_source_link
from news_monitor.storage.models import NewsItem
from news_monitor.telegram.card import render_channel_post

logger = logging.getLogger(__name__)


class PublicationError(RuntimeError):
    """Raised when a publication cannot be produced or delivered."""


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of one publication attempt."""

    message_id: int | None
    dry_run: bool = False


@runtime_checkable
class ChannelSender(Protocol):
    """Minimal Telegram sending surface used by the publisher."""

    async def send_message(self, chat_id: object, text: str, **kwargs: object) -> object:
        ...


class ChannelPublisher:
    """Sends the rendered post to the configured channel.

    Publication is only ever called by the moderation service after the item
    reached APPROVED, so the AI has no path of its own to the channel.
    """

    def __init__(
        self,
        sender: ChannelSender,
        channel_id: str | int,
        *,
        dry_run: bool = False,
    ) -> None:
        self._sender = sender
        self._channel_id = channel_id
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    async def publish(self, item: NewsItem) -> PublishResult:
        """Render and send the post, enforcing the mandatory source link."""
        text = render_channel_post(item)
        link_check = check_source_link(text, item.url)
        if not link_check.passed:
            raise PublicationError(link_check.reason)

        if self._dry_run:
            logger.info(
                "dry run: publication for item %s prepared but not sent", item.id
            )
            return PublishResult(message_id=None, dry_run=True)

        if not self._channel_id:
            raise PublicationError("telegram channel is not configured")

        try:
            message = await self._sender.send_message(
                self._channel_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
            raise PublicationError(f"telegram send failed: {type(exc).__name__}") from exc

        message_id = getattr(message, "message_id", None)
        if message_id is None:
            raise PublicationError("telegram did not return a message id")
        return PublishResult(message_id=int(message_id), dry_run=False)
