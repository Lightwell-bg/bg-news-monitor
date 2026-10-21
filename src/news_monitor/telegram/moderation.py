"""Moderation decisions: authorization, atomic transition and publication.

This module is the only path to the channel. Every call performs, in order:

1. an authorization check against the configured administrator ids;
2. an atomic status transition that succeeds for exactly one caller;
3. the publication itself, executed by code and never by the model.

Approving does not publish: it schedules publication at a random moment between
5 minutes and the configured maximum. Publication happens either from "publish
now" or from the due job (``publish_due``); both only publish an item they have
just claimed with an atomic SCHEDULED/AWAITING_APPROVAL -> APPROVED update.
"""

from __future__ import annotations

import enum
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from news_monitor.ai.schema import AIAssessment
from news_monitor.config.runtime_store import MIN_PUBLISH_DELAY_MINUTES
from news_monitor.config.settings import Settings
from news_monitor.filters.guards import check_draft
from news_monitor.storage.models import NewsItem, NewsStatus, utcnow
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.card import (
    ACTION_APPROVE,
    ACTION_CANCEL_SCHEDULE,
    ACTION_PUBLISH_NOW,
    ACTION_REGENERATE,
    ACTION_REJECT,
)
from news_monitor.telegram.publisher import ChannelPublisher, PublicationError
from news_monitor.timeutil import format_local_time

logger = logging.getLogger(__name__)

#: Safety cap on claim batches per publish_due() run (10 items each).
MAX_DUE_BATCHES = 20

Regenerator = Callable[[NewsItem], Awaitable[AIAssessment | None]]


class ActionOutcome(str, enum.Enum):
    """Result of one moderation button press."""

    DENIED = "denied"
    NOT_FOUND = "not_found"
    ALREADY_HANDLED = "already_handled"
    PUBLISHED = "published"
    PUBLISHED_DRY_RUN = "published_dry_run"
    REJECTED = "rejected"
    PUBLICATION_FAILED = "publication_failed"
    SCHEDULED = "scheduled"
    UNSCHEDULED = "unscheduled"
    REGENERATED = "regenerated"
    REGENERATION_FAILED = "regeneration_failed"
    UNKNOWN_ACTION = "unknown_action"


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome plus a message safe to show to the administrator."""

    outcome: ActionOutcome
    message: str
    item: NewsItem | None = None

    @property
    def changed(self) -> bool:
        """True when this call actually changed the stored state."""
        return self.outcome in {
            ActionOutcome.PUBLISHED,
            ActionOutcome.PUBLISHED_DRY_RUN,
            ActionOutcome.REJECTED,
            ActionOutcome.PUBLICATION_FAILED,
            ActionOutcome.SCHEDULED,
            ActionOutcome.UNSCHEDULED,
            ActionOutcome.REGENERATED,
        }


MESSAGES: dict[ActionOutcome, str] = {
    ActionOutcome.DENIED: "Недостаточно прав для этого действия.",
    ActionOutcome.NOT_FOUND: "Новость не найдена.",
    ActionOutcome.ALREADY_HANDLED: "Новость уже обработана.",
    ActionOutcome.PUBLISHED: "Опубликовано в канале.",
    ActionOutcome.PUBLISHED_DRY_RUN: "Черновой режим: публикация подготовлена, но не отправлена.",
    ActionOutcome.REJECTED: "Отклонено.",
    ActionOutcome.PUBLICATION_FAILED: "Публикация не удалась, статус сохранён.",
    # SCHEDULED has no fixed text: the message carries the computed time.
    ActionOutcome.UNSCHEDULED: "Публикация отменена, новость снова ждёт решения.",
    ActionOutcome.REGENERATED: "Черновик обновлён.",
    ActionOutcome.REGENERATION_FAILED: "Не удалось обновить черновик.",
    ActionOutcome.UNKNOWN_ACTION: "Неизвестное действие.",
}


def _result(outcome: ActionOutcome, item: NewsItem | None = None) -> ActionResult:
    return ActionResult(outcome=outcome, message=MESSAGES[outcome], item=item)


class ModerationService:
    """Applies an administrator decision to one news item."""

    def __init__(
        self,
        *,
        repository: NewsRepository,
        publisher: ChannelPublisher,
        settings: Settings,
        regenerator: Regenerator | None = None,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._settings = settings
        self._regenerator = regenerator
        self._clock = clock
        self._rng = rng if rng is not None else random.SystemRandom()

    @property
    def publish_delay_max_minutes(self) -> int:
        """The current longest approval delay, as the running settings hold it."""
        return int(self._settings.publish_delay_max_minutes)

    def is_authorized(self, user_id: int | None) -> bool:
        """Only numeric ids from the configured administrator list may decide."""
        return self._settings.is_admin(user_id)

    async def handle_action(
        self, *, action: str, item_id: int, user_id: int | None
    ) -> ActionResult:
        """Authorize and execute one moderation action."""
        if not self.is_authorized(user_id):
            logger.warning(
                "rejected moderation attempt for item %s from unauthorized user",
                item_id,
            )
            return _result(ActionOutcome.DENIED)

        item = await self._repository.get(item_id)
        if item is None:
            return _result(ActionOutcome.NOT_FOUND)

        if action == ACTION_APPROVE:
            return await self._schedule(item_id, user_id)
        if action == ACTION_PUBLISH_NOW:
            return await self._publish_now(item_id, user_id)
        if action == ACTION_CANCEL_SCHEDULE:
            return await self._unschedule(item_id)
        if action == ACTION_REJECT:
            return await self._reject(item_id, user_id)
        if action == ACTION_REGENERATE:
            return await self._regenerate(item)
        return _result(ActionOutcome.UNKNOWN_ACTION, item)

    async def _schedule(self, item_id: int, admin_id: int) -> ActionResult:
        """Schedule publication at a random moment within the allowed window."""
        low = MIN_PUBLISH_DELAY_MINUTES * 60
        high = max(self._settings.publish_delay_max_minutes * 60, low)
        delay_seconds = self._rng.randint(low, high)
        scheduled_at = self._clock() + timedelta(seconds=delay_seconds)

        won = await self._repository.schedule(
            item_id, admin_id=admin_id, scheduled_at=scheduled_at
        )
        item = await self._repository.get(item_id)
        if not won:
            return _result(ActionOutcome.ALREADY_HANDLED, item)

        minutes = max(1, round(delay_seconds / 60))
        message = (
            f"Публикация запланирована на {format_local_time(scheduled_at)} "
            f"(через {minutes} мин)."
        )
        return ActionResult(
            outcome=ActionOutcome.SCHEDULED, message=message, item=item
        )

    async def _unschedule(self, item_id: int) -> ActionResult:
        won = await self._repository.cancel_schedule(item_id)
        item = await self._repository.get(item_id)
        if not won:
            return _result(ActionOutcome.ALREADY_HANDLED, item)
        return _result(ActionOutcome.UNSCHEDULED, item)

    async def _publish_now(self, item_id: int, admin_id: int) -> ActionResult:
        """Publish immediately from AWAITING_APPROVAL or SCHEDULED, exactly once.

        The conditional update is the single point of serialization: a second
        press finds the item already APPROVED and never publishes.
        """
        won = await self._repository.approve_now(item_id, admin_id=admin_id)
        if not won:
            item = await self._repository.get(item_id)
            return _result(ActionOutcome.ALREADY_HANDLED, item)
        return await self._publish_claimed(item_id)

    async def publish_due(self) -> list[ActionResult]:
        """Publish every scheduled item whose time has come.

        Called by the scheduler, a system job, so no administrator check is
        made: the items were authorized when they were scheduled. A failure of
        one item is logged and never stops the others.
        """
        # One "now" for the whole run: items scheduled meanwhile do not extend it.
        now = self._clock()
        results: list[ActionResult] = []
        for _ in range(MAX_DUE_BATCHES):
            claimed = await self._repository.claim_due(now)
            if not claimed:
                break
            for item_id in claimed:
                try:
                    results.append(await self._publish_claimed(item_id))
                except Exception:  # noqa: BLE001 - keep publishing the remaining items
                    logger.exception(
                        "scheduled publication crashed for item %s", item_id
                    )
        return results

    async def _publish_claimed(self, item_id: int) -> ActionResult:
        """Publish an item this caller has already moved to APPROVED.

        An item that crashes after the claim is not resent: a timeout means
        "maybe done", never "send again".
        """
        item = await self._repository.get(item_id)
        if item is None:  # pragma: no cover - defensive
            return _result(ActionOutcome.NOT_FOUND)

        try:
            published = await self._publisher.publish(item)
        except PublicationError as exc:
            await self._repository.mark_publication_failed(item_id, str(exc))
            logger.error("publication failed for item %s: %s", item_id, exc)
            return _result(
                ActionOutcome.PUBLICATION_FAILED, await self._repository.get(item_id)
            )

        await self._repository.mark_published(
            item_id, channel_message_id=published.message_id
        )
        outcome = (
            ActionOutcome.PUBLISHED_DRY_RUN
            if published.dry_run
            else ActionOutcome.PUBLISHED
        )
        return _result(outcome, await self._repository.get(item_id))

    async def _reject(self, item_id: int, admin_id: int) -> ActionResult:
        won = await self._repository.reject_by_admin(item_id, admin_id=admin_id)
        if not won:
            return _result(
                ActionOutcome.ALREADY_HANDLED, await self._repository.get(item_id)
            )
        return _result(ActionOutcome.REJECTED, await self._repository.get(item_id))

    async def _regenerate(self, item: NewsItem) -> ActionResult:
        """Ask the model for a new draft while the item still awaits a decision."""
        if item.status_enum is not NewsStatus.AWAITING_APPROVAL:
            return _result(ActionOutcome.ALREADY_HANDLED, item)
        if self._regenerator is None:
            return _result(ActionOutcome.REGENERATION_FAILED, item)

        try:
            assessment = await self._regenerator(item)
        except Exception as exc:  # noqa: BLE001 - any failure keeps the old draft
            logger.warning(
                "regeneration failed for item %s: %s", item.id, type(exc).__name__
            )
            return _result(ActionOutcome.REGENERATION_FAILED, item)

        if assessment is None:
            return _result(ActionOutcome.REGENERATION_FAILED, item)

        updated = await self._repository.update_draft(
            item.id,
            importance=assessment.importance,
            ai_reason=assessment.reason,
            title_ru=assessment.title_ru,
            draft_ru=assessment.draft_ru,
        )
        if not updated:
            return _result(
                ActionOutcome.ALREADY_HANDLED, await self._repository.get(item.id)
            )
        return _result(ActionOutcome.REGENERATED, await self._repository.get(item.id))


def draft_is_publishable(assessment: AIAssessment, source_text: str) -> tuple[bool, str]:
    """Deterministic gate applied before a card is ever created."""
    verdict = check_draft(assessment.draft_ru, source_text)
    return verdict.passed, verdict.reason
