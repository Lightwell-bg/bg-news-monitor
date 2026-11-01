"""Persistence operations with deterministic deduplication and atomic transitions.

Every moderation decision is written with a conditional UPDATE that also matches
the expected current status. The number of affected rows tells the caller whether
it won the race, which makes a repeated button press a no-op instead of a second
publication.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from news_monitor.normalization.url import normalize_url
from news_monitor.storage.models import (
    CardMessage,
    ModerationEvent,
    Moderator,
    NewsItem,
    NewsStatus,
    as_utc,
    ensure_transition,
    utcnow,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    """A moderation_event written in the same transaction as a won transition."""

    user_id: int
    user_name: str
    action: str
    at: datetime | None = None


class DuplicateReason:
    """Human readable duplicate explanations stored in status_reason."""

    URL = "duplicate normalized url"
    CONTENT = "duplicate content hash"


class NewsRepository:
    """Data access for news items."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, item_id: int) -> NewsItem | None:
        """Return one item by primary key."""
        async with self._session_factory() as session:
            return await session.get(NewsItem, item_id)

    async def get_by_normalized_url(self, normalized: str) -> NewsItem | None:
        """Return one item by its normalized url."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(NewsItem).where(NewsItem.normalized_url == normalized)
            )
            return result.scalar_one_or_none()

    async def list_by_status(self, status: NewsStatus) -> list[NewsItem]:
        """Return every item currently in the given status."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(NewsItem).where(NewsItem.status == status.value)
            )
            return list(result.scalars().all())

    async def register_discovered(
        self,
        *,
        source_id: str,
        source_name: str,
        url: str,
        title: str = "",
        section: str = "",
        base_url: str | None = None,
    ) -> tuple[NewsItem, bool]:
        """Insert a newly discovered link.

        Returns the stored item and True when it was created. A link whose
        normalized url is already known is never inserted twice.
        """
        normalized = normalize_url(url, base_url=base_url)
        async with self._session_factory() as session:
            existing = await session.execute(
                select(NewsItem).where(NewsItem.normalized_url == normalized)
            )
            found = existing.scalar_one_or_none()
            if found is not None:
                return found, False

            item = NewsItem(
                source_id=source_id,
                source_name=source_name,
                section=section,
                url=url,
                normalized_url=normalized,
                original_title=title,
                status=NewsStatus.DISCOVERED.value,
            )
            session.add(item)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                retry = await session.execute(
                    select(NewsItem).where(NewsItem.normalized_url == normalized)
                )
                concurrent = retry.scalar_one_or_none()
                if concurrent is None:  # pragma: no cover - defensive
                    raise
                return concurrent, False
            await session.refresh(item)
            return item, True

    async def mark_fetched(
        self,
        item_id: int,
        *,
        title: str,
        body_hash: str,
        published_at: datetime | None = None,
        section: str = "",
    ) -> NewsItem:
        """Store fetched metadata, or mark the item as a content duplicate.

        The content hash is written only when no other item owns it, so the
        unique constraint stays intact and the copy is marked DUPLICATE.
        """
        async with self._session_factory() as session:
            item = await session.get(NewsItem, item_id)
            if item is None:
                raise KeyError(f"news item not found: {item_id}")

            duplicate = await session.execute(
                select(NewsItem.id).where(
                    NewsItem.content_hash == body_hash,
                    NewsItem.id != item_id,
                )
            )
            if duplicate.scalar_one_or_none() is not None:
                ensure_transition(item.status_enum, NewsStatus.DUPLICATE)
                item.original_title = title or item.original_title
                item.status = NewsStatus.DUPLICATE.value
                item.status_reason = DuplicateReason.CONTENT
                item.updated_at = utcnow()
                await session.commit()
                await session.refresh(item)
                return item

            ensure_transition(item.status_enum, NewsStatus.FETCHED)
            item.original_title = title
            item.content_hash = body_hash
            item.published_at = published_at
            item.section = section or item.section
            item.status = NewsStatus.FETCHED.value
            item.status_reason = ""
            item.updated_at = utcnow()
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return await self._force_duplicate(item_id, DuplicateReason.CONTENT)
            await session.refresh(item)
            return item

    async def _force_duplicate(self, item_id: int, reason: str) -> NewsItem:
        """Mark an item as a duplicate without touching its content hash."""
        async with self._session_factory() as session:
            item = await session.get(NewsItem, item_id)
            if item is None:  # pragma: no cover - defensive
                raise KeyError(f"news item not found: {item_id}")
            item.content_hash = None
            item.status = NewsStatus.DUPLICATE.value
            item.status_reason = reason
            item.updated_at = utcnow()
            await session.commit()
            await session.refresh(item)
            return item

    async def mark_rejected_by_filter(self, item_id: int, reason: str) -> NewsItem:
        """Reject an item with a deterministic rule."""
        return await self._set_status(
            item_id, NewsStatus.REJECTED_BY_FILTER, reason=reason
        )

    async def mark_duplicate(self, item_id: int, reason: str) -> NewsItem:
        """Mark an item as a duplicate."""
        return await self._set_status(item_id, NewsStatus.DUPLICATE, reason=reason)

    async def mark_candidate(
        self,
        item_id: int,
        *,
        importance: int,
        ai_reason: str,
        title_ru: str,
        draft_ru: str,
    ) -> NewsItem:
        """Store a validated AI result and promote the item to CANDIDATE."""
        async with self._session_factory() as session:
            item = await session.get(NewsItem, item_id)
            if item is None:
                raise KeyError(f"news item not found: {item_id}")
            ensure_transition(item.status_enum, NewsStatus.CANDIDATE)
            item.importance = importance
            item.ai_reason = ai_reason
            item.title_ru = title_ru
            item.draft_ru = draft_ru
            item.status = NewsStatus.CANDIDATE.value
            item.status_reason = ""
            item.updated_at = utcnow()
            await session.commit()
            await session.refresh(item)
            return item

    async def mark_awaiting_approval(
        self, item_id: int, *, chat_id: int, message_id: int
    ) -> NewsItem:
        """Record that the moderation card was delivered to the administrator."""
        async with self._session_factory() as session:
            item = await session.get(NewsItem, item_id)
            if item is None:
                raise KeyError(f"news item not found: {item_id}")
            ensure_transition(item.status_enum, NewsStatus.AWAITING_APPROVAL)
            item.admin_chat_id = chat_id
            item.admin_message_id = message_id
            item.status = NewsStatus.AWAITING_APPROVAL.value
            item.updated_at = utcnow()
            await session.commit()
            await session.refresh(item)
            return item

    async def _set_status(
        self, item_id: int, target: NewsStatus, *, reason: str = ""
    ) -> NewsItem:
        async with self._session_factory() as session:
            item = await session.get(NewsItem, item_id)
            if item is None:
                raise KeyError(f"news item not found: {item_id}")
            ensure_transition(item.status_enum, target)
            item.status = target.value
            item.status_reason = reason
            item.updated_at = utcnow()
            await session.commit()
            await session.refresh(item)
            return item

    async def _conditional_update(
        self,
        item_id: int,
        *,
        expected: NewsStatus,
        target: NewsStatus,
        values: dict[str, object] | None = None,
        event: DecisionEvent | None = None,
    ) -> bool:
        """Apply a status change only if the row is still in the expected status.

        Returns True when this call performed the change and False when another
        call already did, which keeps repeated callbacks idempotent. A given
        ``event`` is inserted in the same transaction, only for the winner.
        """
        payload: dict[str, object] = {
            "status": target.value,
            "updated_at": utcnow(),
        }
        payload.update(values or {})
        async with self._session_factory() as session:
            result = await session.execute(
                update(NewsItem)
                .where(NewsItem.id == item_id, NewsItem.status == expected.value)
                .values(**payload)
            )
            won = bool(result.rowcount)
            if won and event is not None:
                self._add_event(session, item_id, event)
            await session.commit()
            return won

    @staticmethod
    def _add_event(session: AsyncSession, item_id: int, event: DecisionEvent) -> None:
        """Queue a moderation_event row in the caller's transaction."""
        session.add(
            ModerationEvent(
                item_id=item_id,
                user_id=event.user_id,
                user_name=(event.user_name or "")[:256],
                action=event.action,
                created_at=as_utc(event.at) if event.at is not None else utcnow(),
            )
        )

    async def approve(self, item_id: int, *, admin_id: int) -> bool:
        """Move AWAITING_APPROVAL to APPROVED exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.AWAITING_APPROVAL,
            target=NewsStatus.APPROVED,
            values={"decided_by": admin_id, "status_reason": ""},
        )

    async def approve_now(
        self, item_id: int, *, admin_id: int, event: DecisionEvent | None = None
    ) -> bool:
        """Move AWAITING_APPROVAL or SCHEDULED to APPROVED in one atomic UPDATE."""
        async with self._session_factory() as session:
            result = await session.execute(
                update(NewsItem)
                .where(
                    NewsItem.id == item_id,
                    NewsItem.status.in_(
                        [
                            NewsStatus.AWAITING_APPROVAL.value,
                            NewsStatus.SCHEDULED.value,
                        ]
                    ),
                )
                .values(
                    status=NewsStatus.APPROVED.value,
                    decided_by=admin_id,
                    status_reason="",
                    updated_at=utcnow(),
                )
            )
            won = bool(result.rowcount)
            if won and event is not None:
                self._add_event(session, item_id, event)
            await session.commit()
            return won

    async def schedule(
        self,
        item_id: int,
        *,
        admin_id: int,
        scheduled_at: datetime,
        event: DecisionEvent | None = None,
    ) -> bool:
        """Move AWAITING_APPROVAL to SCHEDULED exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.AWAITING_APPROVAL,
            target=NewsStatus.SCHEDULED,
            values={
                "decided_by": admin_id,
                "scheduled_at": as_utc(scheduled_at),
                "status_reason": "",
            },
            event=event,
        )

    async def cancel_schedule(
        self, item_id: int, *, event: DecisionEvent | None = None
    ) -> bool:
        """Move SCHEDULED back to AWAITING_APPROVAL exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.SCHEDULED,
            target=NewsStatus.AWAITING_APPROVAL,
            values={"scheduled_at": None, "decided_by": None},
            event=event,
        )

    async def claim_due(self, now: datetime, *, limit: int = 10) -> list[int]:
        """Atomically claim scheduled items whose time has come.

        Each due item is moved SCHEDULED -> APPROVED by a conditional UPDATE;
        only the ids this call actually won are returned, so two claimers (or a
        claim and a publish-now press) can never both publish the same item.
        """
        moment = as_utc(now)
        async with self._session_factory() as session:
            result = await session.execute(
                select(NewsItem.id)
                .where(
                    NewsItem.status == NewsStatus.SCHEDULED.value,
                    NewsItem.scheduled_at <= moment,
                )
                .order_by(NewsItem.scheduled_at)
                .limit(limit)
            )
            candidates = list(result.scalars().all())

        claimed: list[int] = []
        for item_id in candidates:
            async with self._session_factory() as session:
                outcome = await session.execute(
                    update(NewsItem)
                    .where(
                        NewsItem.id == item_id,
                        NewsItem.status == NewsStatus.SCHEDULED.value,
                        NewsItem.scheduled_at <= moment,
                    )
                    .values(
                        status=NewsStatus.APPROVED.value,
                        status_reason="",
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
                if outcome.rowcount:
                    claimed.append(item_id)
        return claimed

    async def list_scheduled(self) -> list[NewsItem]:
        """Return every scheduled item, soonest first."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(NewsItem)
                .where(NewsItem.status == NewsStatus.SCHEDULED.value)
                .order_by(NewsItem.scheduled_at)
            )
            return list(result.scalars().all())

    async def reject_by_admin(
        self, item_id: int, *, admin_id: int, event: DecisionEvent | None = None
    ) -> bool:
        """Move AWAITING_APPROVAL to REJECTED_BY_ADMIN exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.AWAITING_APPROVAL,
            target=NewsStatus.REJECTED_BY_ADMIN,
            values={"decided_by": admin_id, "status_reason": "rejected by admin"},
            event=event,
        )

    async def mark_published(self, item_id: int, *, channel_message_id: int) -> bool:
        """Move APPROVED to PUBLISHED exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.APPROVED,
            target=NewsStatus.PUBLISHED,
            values={"channel_message_id": channel_message_id, "status_reason": ""},
        )

    async def mark_publication_failed(self, item_id: int, reason: str) -> bool:
        """Move APPROVED to PUBLICATION_FAILED."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.APPROVED,
            target=NewsStatus.PUBLICATION_FAILED,
            values={"status_reason": reason},
        )

    async def retry_publication(self, item_id: int) -> bool:
        """Move PUBLICATION_FAILED back to APPROVED for another attempt."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.PUBLICATION_FAILED,
            target=NewsStatus.APPROVED,
            values={"status_reason": ""},
        )

    async def count_by_status(self, status: NewsStatus) -> int:
        """Number of items currently in the given status."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(NewsItem).where(
                    NewsItem.status == status.value
                )
            )
            return int(result.scalar_one())

    # ------------------------------------------------------------ moderators

    async def add_moderator(
        self, user_id: int, display_name: str = "", added_by: int | None = None
    ) -> bool:
        """Store a moderator. Returns False when the user already is one."""
        async with self._session_factory() as session:
            if await session.get(Moderator, user_id) is not None:
                return False
            session.add(
                Moderator(
                    user_id=user_id,
                    display_name=display_name[:256],
                    added_by=added_by,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def remove_moderator(self, user_id: int) -> bool:
        """Delete a moderator. Returns False when there was none."""
        async with self._session_factory() as session:
            result = await session.execute(
                delete(Moderator).where(Moderator.user_id == user_id)
            )
            await session.commit()
            return bool(result.rowcount)

    async def list_moderators(self) -> list[Moderator]:
        """Every moderator, oldest first."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Moderator).order_by(Moderator.created_at, Moderator.user_id)
            )
            return list(result.scalars().all())

    async def update_moderator_name(self, user_id: int, name: str) -> None:
        """Refresh the stored display name of a moderator (no-op for strangers)."""
        async with self._session_factory() as session:
            await session.execute(
                update(Moderator)
                .where(Moderator.user_id == user_id)
                .values(display_name=name[:256])
            )
            await session.commit()

    # ---------------------------------------------------------- card copies

    async def add_card_message(self, item_id: int, chat_id: int, message_id: int) -> None:
        """Remember one delivered copy of a card; a duplicate chat is ignored."""
        async with self._session_factory() as session:
            session.add(
                CardMessage(item_id=item_id, chat_id=chat_id, message_id=message_id)
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def list_card_messages(self, item_id: int) -> list[tuple[int, int]]:
        """Every (chat_id, message_id) holding a card of the item.

        Includes the legacy single admin card stored on the item itself, so
        cards sent before the per-copy table existed are still refreshed.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(CardMessage.chat_id, CardMessage.message_id)
                .where(CardMessage.item_id == item_id)
                .order_by(CardMessage.id)
            )
            copies = [(int(chat), int(message)) for chat, message in result.all()]
            item = await session.get(NewsItem, item_id)
        if (
            item is not None
            and item.admin_chat_id is not None
            and item.admin_message_id is not None
        ):
            legacy = (int(item.admin_chat_id), int(item.admin_message_id))
            if legacy not in copies:
                copies.insert(0, legacy)
        return copies

    # ------------------------------------------------------ moderation log

    async def record_event(
        self,
        item_id: int,
        user_id: int,
        user_name: str,
        action: str,
        at: datetime | None = None,
    ) -> None:
        """Append one won decision to the moderation log."""
        moment = as_utc(at) if at is not None else utcnow()
        async with self._session_factory() as session:
            session.add(
                ModerationEvent(
                    item_id=item_id,
                    user_id=user_id,
                    user_name=(user_name or "")[:256],
                    action=action,
                    created_at=moment,
                )
            )
            await session.commit()

    async def list_events(
        self, start_utc: datetime, end_utc: datetime
    ) -> list[ModerationEvent]:
        """Events with start <= created_at < end, oldest first."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ModerationEvent)
                .where(
                    ModerationEvent.created_at >= as_utc(start_utc),
                    ModerationEvent.created_at < as_utc(end_utc),
                )
                .order_by(ModerationEvent.created_at, ModerationEvent.id)
            )
            return list(result.scalars().all())

    async def latest_event(self, item_id: int) -> ModerationEvent | None:
        """The most recent logged decision of an item."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(ModerationEvent)
                .where(ModerationEvent.item_id == item_id)
                .order_by(ModerationEvent.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def update_draft(
        self,
        item_id: int,
        *,
        importance: int,
        ai_reason: str,
        title_ru: str,
        draft_ru: str,
    ) -> bool:
        """Replace the stored draft while the item is still awaiting approval.

        Returns False once a decision has been taken, so a regenerate button
        pressed after approval can never change what was published.
        """
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.AWAITING_APPROVAL,
            target=NewsStatus.AWAITING_APPROVAL,
            values={
                "importance": importance,
                "ai_reason": ai_reason,
                "title_ru": title_ru,
                "draft_ru": draft_ru,
            },
        )
