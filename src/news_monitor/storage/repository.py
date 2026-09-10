"""Persistence operations with deterministic deduplication and atomic transitions.

Every moderation decision is written with a conditional UPDATE that also matches
the expected current status. The number of affected rows tells the caller whether
it won the race, which makes a repeated button press a no-op instead of a second
publication.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from news_monitor.normalization.url import normalize_url
from news_monitor.storage.models import (
    NewsItem,
    NewsStatus,
    ensure_transition,
    utcnow,
)

logger = logging.getLogger(__name__)


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
    ) -> bool:
        """Apply a status change only if the row is still in the expected status.

        Returns True when this call performed the change and False when another
        call already did, which keeps repeated callbacks idempotent.
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
            await session.commit()
            return bool(result.rowcount)

    async def approve(self, item_id: int, *, admin_id: int) -> bool:
        """Move AWAITING_APPROVAL to APPROVED exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.AWAITING_APPROVAL,
            target=NewsStatus.APPROVED,
            values={"decided_by": admin_id, "status_reason": ""},
        )

    async def reject_by_admin(self, item_id: int, *, admin_id: int) -> bool:
        """Move AWAITING_APPROVAL to REJECTED_BY_ADMIN exactly once."""
        return await self._conditional_update(
            item_id,
            expected=NewsStatus.AWAITING_APPROVAL,
            target=NewsStatus.REJECTED_BY_ADMIN,
            values={"decided_by": admin_id, "status_reason": "rejected by admin"},
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
