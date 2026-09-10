"""Contracts shared by every source adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from news_monitor.config.sources import SourceConfig


@dataclass(frozen=True, slots=True)
class ListingItem:
    """A link discovered on a listing page."""

    url: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class ArticleContent:
    """Metadata and context extracted from an article page."""

    url: str
    title: str
    body: str
    published_at: datetime | None = None
    summary: str = ""
    section: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """Parses listing and article HTML for one source type.

    Adapters never perform network calls themselves; the pipeline supplies HTML
    so that tests can run entirely on local fixtures.
    """

    adapter_type: str

    def parse_listing(self, html: str, section_url: str) -> list[ListingItem]:
        """Return article links found on a listing page, newest first."""

    def parse_article(self, html: str, article_url: str) -> ArticleContent | None:
        """Return article content, or ``None`` when the page is unusable."""


@runtime_checkable
class HtmlFetcher(Protocol):
    """Retrieves raw HTML. Replaced by a fake in tests."""

    async def fetch(self, url: str) -> str: ...


class AdapterError(RuntimeError):
    """Raised when a source adapter cannot process a page."""


def adapter_for(source: SourceConfig) -> SourceAdapter:
    """Return the adapter registered for ``source.adapter_type``."""
    from news_monitor.sources.registry import get_adapter

    return get_adapter(source.adapter_type)
