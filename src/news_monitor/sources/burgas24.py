"""Adapter for the Burgas24 homepage and its article pages.

Burgas24 publishes every news article under ``/novini/<rubric>/<Slug>-<id>``.
The homepage also links rubric landing pages, daily archives
(``/novini/15.09.2026/showall.html``), topic collections, videos, sport pages,
classified ads (``/obiavi/``), the events guide and sister sites of the network;
none of them is an article, so the listing keeps only the article path shape.

The adapter never performs a network call: the pipeline supplies the HTML, so
every test runs on local fixtures.
"""

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
from news_monitor.sources.links import is_listing_path, is_skipped_link

#: ``/novini/<rubric>/<slug>-<numeric id>``; the slug must contain a word before the id.
ARTICLE_PATH_RE = re.compile(
    r"^/novini/(?P<rubric>[A-Za-z][A-Za-z0-9_-]*)/"
    r"(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z][A-Za-z0-9-]*)-(?P<id>\d{4,})$"
)

#: Rubric-like path segments that hold listings or media, never an article.
NON_ARTICLE_RUBRICS: frozenset[str] = frozenset({"videos", "topics", "galleries"})

#: Below this length the extracted text is treated as boilerplate, not an article.
MIN_USABLE_BODY_CHARS = 40
MIN_BODY_CHARS_FOR_SELECTOR = 200

LISTING_CONTAINER_SELECTORS: tuple[str, ...] = ("main", "body")

ARTICLE_BODY_SELECTORS: tuple[str, ...] = (
    '[itemprop="articleBody"]',
    "article .article-text",
    ".article-text",
    ".news-lead-text",
)

TITLE_SELECTORS: tuple[str, ...] = (
    "h1.zaglavie",
    "article h1",
    "h1",
)

DATE_SELECTORS: tuple[str, ...] = (
    ".published-time time[datetime]",
    "article header time[datetime]",
    ".published-time",
)


def is_article_path(path: str) -> bool:
    """Return True for a Burgas24 article path such as ``/novini/burgas/Slug-3025321``."""
    match = ARTICLE_PATH_RE.fullmatch(path or "")
    if match is None:
        return False
    return match.group("rubric").lower() not in NON_ARTICLE_RUBRICS


class Burgas24HomepageAdapter:
    """Parses the Burgas24 homepage listing and Burgas24 article pages."""

    adapter_type = "burgas24_homepage"

    def __init__(self, base_url: str = "https://www.burgas24.bg/") -> None:
        self.base_url = str(base_url)

    def parse_listing(self, html: str, section_url: str) -> list[ListingItem]:
        """Return unique Burgas24 article links in document order.

        A homepage tile links the same article twice: an image link that has no
        text and a headline link. The first readable caption wins, falling back
        to the ``title`` attribute and the image ``alt`` text.
        """
        tree = parse_html(html)
        base = section_url or self.base_url

        for selector in LISTING_CONTAINER_SELECTORS:
            container = tree.css_first(selector)
            if container is None:
                continue

            titles: dict[str, str] = {}
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
                if is_listing_path(path) or not is_article_path(path):
                    continue
                if urlsplit(absolute).query:
                    # Articles never need a query; anything left after tracking
                    # parameters are stripped points at a service view.
                    continue
                caption = _anchor_caption(anchor)
                if absolute not in titles:
                    titles[absolute] = caption
                elif not titles[absolute] and caption:
                    titles[absolute] = caption
            if titles:
                return [ListingItem(url=url, title=title) for url, title in titles.items()]
        return []

    def parse_article(self, html: str, article_url: str) -> ArticleContent | None:
        """Extract title, publication time and body text from an article page.

        Returns ``None`` when the page carries no title or no usable body text.
        The page description is a truncated lead, so it is never used as a
        substitute body: a page without article text is skipped, not sent to AI.
        """
        tree = parse_html(html)

        title = normalize_text(
            meta_content(tree, 'meta[property="og:title"]')
            or json_ld_value(tree, "headline")
            or first_text(tree, *TITLE_SELECTORS)
        )
        if not title:
            return None

        body = ""
        for selector in ARTICLE_BODY_SELECTORS:
            candidate = node_text(tree.css_first(selector))
            if len(candidate) > len(body):
                body = candidate
            if len(body) >= MIN_BODY_CHARS_FOR_SELECTOR:
                break
        body = normalize_text(body)
        if len(body) < MIN_USABLE_BODY_CHARS:
            return None

        published_at = parse_datetime(
            json_ld_value(tree, "datePublished", "dateCreated")
            or meta_content(tree, 'meta[property="article:published_time"]')
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

        return ArticleContent(
            url=article_url,
            title=title,
            body=body,
            published_at=published_at,
            summary=meta_content(
                tree,
                'meta[property="og:description"]',
                'meta[name="description"]',
            ),
            section=meta_content(tree, 'meta[property="article:section"]'),
        )


def _anchor_caption(anchor) -> str:
    """Return the readable caption of a listing link."""
    caption = normalize_text(anchor.text())
    if caption:
        return caption
    caption = normalize_text(anchor.attributes.get("title"))
    if caption:
        return caption
    image = anchor.css_first("img[alt]")
    if image is not None:
        return normalize_text(image.attributes.get("alt"))
    return ""
