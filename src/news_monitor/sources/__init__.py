"""Source adapters and page fetching."""

from news_monitor.sources.base import (
    AdapterError,
    ArticleContent,
    HtmlFetcher,
    ListingItem,
    SourceAdapter,
    adapter_for,
)
from news_monitor.sources.fetcher import FetchError, HttpHtmlFetcher
from news_monitor.sources.flagman import FlagmanHomepageAdapter
from news_monitor.sources.registry import available_adapters, get_adapter

__all__ = [
    "AdapterError",
    "ArticleContent",
    "FetchError",
    "FlagmanHomepageAdapter",
    "HtmlFetcher",
    "HttpHtmlFetcher",
    "ListingItem",
    "SourceAdapter",
    "adapter_for",
    "available_adapters",
    "get_adapter",
]
