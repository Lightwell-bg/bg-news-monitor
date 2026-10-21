"""Database engine and session management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from news_monitor.storage.models import Base

logger = logging.getLogger(__name__)


def _ensure_sqlite_directory(database_url: str) -> None:
    """Create the parent directory of a local SQLite file when needed."""
    if not database_url.startswith("sqlite"):
        return
    path_part = urlsplit(database_url).path.lstrip("/")
    if not path_part or path_part == ":memory:":
        return
    target = Path(path_part)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine for the configured database."""
    _ensure_sqlite_directory(database_url)
    return create_async_engine(database_url, echo=echo, future=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the engine."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


#: Columns added after the first release: (table, column, DDL type).
#: create_all never alters an existing table, so they are added here.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("news_items", "scheduled_at", "DATETIME"),
)


def _add_missing_columns(connection: Connection) -> None:
    """Add every column of _ADDITIVE_COLUMNS that an older database lacks."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if table not in tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            continue
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        logger.info("added column %s.%s to the existing database", table, column)


async def init_db(engine: AsyncEngine) -> None:
    """Create every missing table and add columns introduced later."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_add_missing_columns)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Provide a transactional session scope."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
