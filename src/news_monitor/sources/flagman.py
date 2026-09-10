"""Adapter for the Flagman homepage and its article pages."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from news_monitor.normalization.content import normalize_text
from news_monitor.normalization.url import normalize_url, same_host
from news_monitor.sources.base import ArticleContent, ListingItem
from news_monitor.sources.html_utils import (
    first_text,
    json_ld_value,
    meta_content,
    node_text,
    parse_datetime,
    parse_html,
)

_NUMERIC_ID_RE = re.compile(r"/\d{4,}")
_SLUG_RE = re.compile(r"/[^/]*-[^/]*-[^/]*")

MIN_BODY_CHARS_FOR_SELECTOR = 200

LISTING_CONTAINER_SELECTORS: tuple[str, ...] = (
    "main",
    "#content",
    ".content",
    ".news-list",
    ".articles",
    "article",
    "body",
)

ARTICLE_BODY_SELECTORS: tuple[str, ...] = (
    '[itemprop="articleBody"]',
    "article .article-content",
    "article .article-text",
    ".article-content",
    ".article-text",
    ".news-content",
    ".entry-content",
    ".post-content",
    ".single-content",
    "article",
)

TITLE_SELECTORS: tuple[str, ...] = (
    "h1.article-title",
    "article h1",
    "h1",
)

DATE_SELECTORS: tuple[str, ...] = (
    "time[datetime]",
    ".article-date",
    ".news-date",
    ".date",
)

SKIP_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".pdf",
    ".mp4",
    ".mp3",
    ".xml",
)


def looks_like_article_path(path: str) -> bool:
    """Return True for paths that look like an article page.

    An article path either contains a numeric identifier of at least four
    digits or a slug segment with at least two hyphens.
    """
    if not path or path == "/":
        return False
    if path.lower().endswith(SKIP_EXTENSIONS):
        return False
    if _NUMERIC_ID_RE.search(path):
        return True
    return bool(_SLUG_RE.search(path))


class FlagmanHomepageAdapter:
    """Parses the Flagman homepage listing and Flagman article pages."""

    adapter_type = "flagman_homepage"

    def __init__(self, base_url: str = "https://www.flagman.bg/") -> None:
        self.base_url = str(base_url)

    def parse_listing(self, html: str, section_url: str) -> list[ListingItem]:
        """Return unique article links in document order."""
        tree = parse_html(html)
        base = section_url or self.base_url
        seen: set[str] = set()
        items: list[ListingItem] = []

        container = None
        for selector in LISTING_CONTAINER_SELECTORS:
            container = tree.css_first(selector)
            if container is not None:
                break
        if container is None:
            return []

        for anchor in container.css("a[href]"):
            href = (anchor.attributes.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            try:
                absolute = normalize_url(href, base_url=base)
            except ValueError:
                continue
            if not same_host(absolute, self.base_url):
                continue
            if not looks_like_article_path(urlsplit(absolute).path):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            items.append(ListingItem(url=absolute, title=normalize_text(anchor.text())))
        return items

    def parse_article(self, html: str, article_url: str) -> ArticleContent | None:
        """Extract title, publication time and body text from an article page."""
        tree = parse_html(html)

        title = (
            json_ld_value(tree, "headline", "name")
            or meta_content(tree, 'meta[property="og:title"]', 'meta[name="title"]')
            or first_text(tree, *TITLE_SELECTORS)
        )
        title = normalize_text(title)
        if not title:
            return None

        summary = meta_content(
            tree,
            'meta[property="og:description"]',
            'meta[name="description"]',
        )

        raw_date = json_ld_value(tree, "datePublished", "dateCreated") or meta_content(
            tree,
            'meta[property="article:published_time"]',
            'meta[itemprop="datePublished"]',
        )
        published_at = parse_datetime(raw_date)
        if published_at is None:
            for selector in DATE_SELECTORS:
                node = tree.css_first(selector)
                if node is None:
                    continue
                published_at = parse_datetime(
                    node.attributes.get("datetime") or node.text()
                )
                if published_at is not None:
                    break

        body = ""
        for selector in ARTICLE_BODY_SELECTORS:
            candidate = node_text(tree.css_first(selector))
            if len(candidate) > len(body):
                body = candidate
            if len(body) >= MIN_BODY_CHARS_FOR_SELECTOR:
                break
        if not body:
            body = summary

        return ArticleContent(
            url=article_url,
            title=title,
            body=normalize_text(body),
            published_at=published_at,
            summary=summary,
            section=meta_content(tree, 'meta[property="article:section"]'),
        )
