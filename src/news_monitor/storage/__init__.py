"""Storage layer: models, engine helpers and the repository."""

from news_monitor.storage.db import (
    create_engine,
    create_session_factory,
    init_db,
    session_scope,
)
from news_monitor.storage.models import (
    ALLOWED_TRANSITIONS,
    Base,
    InvalidTransitionError,
    NewsItem,
    NewsStatus,
    can_transition,
    ensure_transition,
)
from news_monitor.storage.repository import DuplicateReason, NewsRepository

__all__ = [
    "ALLOWED_TRANSITIONS",
    "Base",
    "DuplicateReason",
    "InvalidTransitionError",
    "NewsItem",
    "NewsRepository",
    "NewsStatus",
    "can_transition",
    "create_engine",
    "create_session_factory",
    "ensure_transition",
    "init_db",
    "session_scope",
]
