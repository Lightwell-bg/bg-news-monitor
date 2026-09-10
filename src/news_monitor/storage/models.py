"""SQLAlchemy models and the news lifecycle state machine."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone aware current time in UTC."""
    return datetime.now(UTC)


class NewsStatus(str, enum.Enum):
    """Lifecycle of a discovered news item."""

    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    DUPLICATE = "DUPLICATE"
    REJECTED_BY_FILTER = "REJECTED_BY_FILTER"
    CANDIDATE = "CANDIDATE"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED_BY_ADMIN = "REJECTED_BY_ADMIN"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"


TERMINAL_STATUSES: frozenset[NewsStatus] = frozenset(
    {
        NewsStatus.DUPLICATE,
        NewsStatus.REJECTED_BY_FILTER,
        NewsStatus.REJECTED_BY_ADMIN,
        NewsStatus.PUBLISHED,
    }
)

ALLOWED_TRANSITIONS: dict[NewsStatus, frozenset[NewsStatus]] = {
    NewsStatus.DISCOVERED: frozenset(
        {
            NewsStatus.FETCHED,
            NewsStatus.DUPLICATE,
            NewsStatus.REJECTED_BY_FILTER,
        }
    ),
    NewsStatus.FETCHED: frozenset(
        {
            NewsStatus.DUPLICATE,
            NewsStatus.REJECTED_BY_FILTER,
            NewsStatus.CANDIDATE,
        }
    ),
    NewsStatus.CANDIDATE: frozenset({NewsStatus.AWAITING_APPROVAL}),
    NewsStatus.AWAITING_APPROVAL: frozenset(
        {
            NewsStatus.REJECTED_BY_ADMIN,
            NewsStatus.APPROVED,
        }
    ),
    NewsStatus.APPROVED: frozenset(
        {
            NewsStatus.PUBLISHED,
            NewsStatus.PUBLICATION_FAILED,
        }
    ),
    NewsStatus.PUBLICATION_FAILED: frozenset({NewsStatus.APPROVED}),
    NewsStatus.DUPLICATE: frozenset(),
    NewsStatus.REJECTED_BY_FILTER: frozenset(),
    NewsStatus.REJECTED_BY_ADMIN: frozenset(),
    NewsStatus.PUBLISHED: frozenset(),
}


class InvalidTransitionError(RuntimeError):
    """Raised when a status change is not part of the approved state machine."""

    def __init__(self, current: NewsStatus, target: NewsStatus) -> None:
        super().__init__(f"transition {current.value} -> {target.value} is not allowed")
        self.current = current
        self.target = target


def can_transition(current: NewsStatus, target: NewsStatus) -> bool:
    """Return True when the state machine permits the transition."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def ensure_transition(current: NewsStatus, target: NewsStatus) -> None:
    """Raise InvalidTransitionError when the transition is not permitted."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


class Base(DeclarativeBase):
    """Declarative base for every table."""


class NewsItem(Base):
    """One discovered news item and its moderation state."""

    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("normalized_url", name="uq_news_items_normalized_url"),
        UniqueConstraint("content_hash", name="uq_news_items_content_hash"),
        Index("ix_news_items_status", "status"),
        Index("ix_news_items_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    section: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    original_title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(32), default=NewsStatus.DISCOVERED.value, nullable=False
    )
    status_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    importance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    title_ru: Mapped[str] = mapped_column(Text, default="", nullable=False)
    draft_ru: Mapped[str] = mapped_column(Text, default="", nullable=False)

    admin_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admin_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def status_enum(self) -> NewsStatus:
        """Status as an enum member."""
        return NewsStatus(self.status)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"NewsItem(id={self.id}, source={self.source_id!r}, status={self.status})"
