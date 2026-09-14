"""Adapter for the BG24 homepage and its article pages.

The adapter never performs a network call: the pipeline supplies the HTML, so
every test runs on local fixtures.
"""

from __future__ import annotations

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
from news_monitor.sources.links import (
    is_listing_path,
    is_skipped_link,
    looks_like_article_path,
)

MIN_BODY_CHARS_FOR_SELECTOR = 200
#: Below this length the extracted text is treated as boilerplate, not an article.
MIN_USABLE_BODY_CHARS = 40

LISTING_CONTAINER_SELECTORS: tuple[str, ...] = (
    "main",
    "#content",
    "#main",
    ".site-main",
    ".posts",
    ".post-list",
    ".news-list",
    ".content",
    "body",
)

ARTICLE_BODY_SELECTORS: tuple[str, ...] = (
    '[itemprop="articleBody"]',
    "article .entry-content",
    "article .post-content",
    ".entry-content",
    ".post-content",
    ".single-content",
    ".article-content",
    ".news-content",
    "article",
)

TITLE_SELECTORS: tuple[str, ...] = (
    "h1.entry-title",
    "h1.post-title",
    "article h1",
    "h1",
)

DATE_SELECTORS: tuple[str, ...] = (
    "time[datetime]",
    ".entry-date",
    ".post-date",
    ".published",
    ".date",
)


class Bg24HomepageAdapter:
    """Parses the BG24 homepage listing and BG24 article pages."""

    adapter_type = "bg24_homepage"

    def __init__(self, base_url: str = "https://bg-24.com/") -> None:
        self.base_url = str(base_url)

    def parse_listing(self, html: str, section_url: str) -> list[ListingItem]:
        """Return unique article links of this host in document order.

        Category, tag, author and pagination links are dropped even when their
        slug looks like an article, so a listing page never becomes a candidate.
        """
        tree = parse_html(html)
        base = section_url or self.base_url

        for selector in LISTING_CONTAINER_SELECTORS:
            container = tree.css_first(selector)
            if container is None:
                continue

            seen: set[str] = set()
            items: list[ListingItem] = []
            for anchor in container.css("a[href]"):
                href = (anchor.attributes.get("href") or "").strip()
                if is_skipped_link(href):
                    continue
                try:
                    absolute = normalize_url(href, base_url=base)
                except ValueError:
                    continue
                if not same_host(absolute, self.base_url):
                    continue
                path = urlsplit(absolute).path
                if is_listing_path(path) or not looks_like_article_path(path):
                    continue
                if absolute in seen:
                    continue
                seen.add(absolute)
                items.append(
                    ListingItem(url=absolute, title=normalize_text(anchor.text()))
                )
            if items:
                return items
        return []

    def parse_article(self, html: str, article_url: str) -> ArticleContent | None:
        """Extract title, publication time and body text from an article page.

        Returns ``None`` when the page carries no title or no usable body, so an
        incomplete page is skipped instead of reaching the AI as a stub.
        """
        tree = parse_html(html)

        title = normalize_text(
            meta_content(tree, 'meta[property="og:title"]', 'meta[name="title"]')
            or json_ld_value(tree, "headline")
            or first_text(tree, *TITLE_SELECTORS)
        )
        if not title:
            return None

        summary = meta_content(
            tree,
            'meta[property="og:description"]',
            'meta[name="description"]',
        )

        published_at = parse_datetime(
            json_ld_value(tree, "datePublished", "dateCreated")
            or meta_content(
                tree,
                'meta[property="article:published_time"]',
                'meta[itemprop="datePublished"]',
            )
        )
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
        if len(body) < MIN_USABLE_BODY_CHARS:
            body = summary if len(summary) > len(body) else body
        body = normalize_text(body)
        if len(body) < MIN_USABLE_BODY_CHARS:
            return None

        return ArticleContent(
            url=article_url,
            title=title,
            body=body,
            published_at=published_at,
            summary=summary,
            section=meta_content(tree, 'meta[property="article:section"]'),
        )
