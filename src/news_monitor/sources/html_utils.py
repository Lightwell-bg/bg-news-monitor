"""Shared HTML extraction helpers built on selectolax."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from selectolax.parser import HTMLParser, Node

from news_monitor.normalization.content import normalize_text

_BG_MONTHS: dict[str, int] = {
    "\u044f\u043d\u0443\u0430\u0440\u0438": 1,
    "\u0444\u0435\u0432\u0440\u0443\u0430\u0440\u0438": 2,
    "\u043c\u0430\u0440\u0442": 3,
    "\u0430\u043f\u0440\u0438\u043b": 4,
    "\u043c\u0430\u0439": 5,
    "\u044e\u043d\u0438": 6,
    "\u044e\u043b\u0438": 7,
    "\u0430\u0432\u0433\u0443\u0441\u0442": 8,
    "\u0441\u0435\u043f\u0442\u0435\u043c\u0432\u0440\u0438": 9,
    "\u043e\u043a\u0442\u043e\u043c\u0432\u0440\u0438": 10,
    "\u043d\u043e\u0435\u043c\u0432\u0440\u0438": 11,
    "\u0434\u0435\u043a\u0435\u043c\u0432\u0440\u0438": 12,
}
_BG_DATE_RE = re.compile(
    r"(\d{1,2})\s+([\u0430-\u044f]+)\s+(\d{4})(?:\D+(\d{1,2}):(\d{2}))?",
    re.IGNORECASE | re.UNICODE,
)
_NUMERIC_DATE_RE = re.compile(
    r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?:\D+(\d{1,2}):(\d{2}))?"
)

BOILERPLATE_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "nav",
    "aside",
    "figure",
    "figcaption",
)


def parse_html(html: str) -> HTMLParser:
    """Parse an HTML document."""
    return HTMLParser(html or "")


def meta_content(tree: HTMLParser, *selectors: str) -> str:
    """Return the first non-empty ``content`` attribute among ``selectors``."""
    for selector in selectors:
        node = tree.css_first(selector)
        if node is None:
            continue
        value = normalize_text(node.attributes.get("content"))
        if value:
            return value
    return ""


def json_ld_objects(tree: HTMLParser) -> list[dict]:
    """Return every JSON-LD object embedded in the document."""
    found: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        found.extend(_flatten_json_ld(data))
    return found


def _flatten_json_ld(data: object) -> list[dict]:
    if isinstance(data, dict):
        items = [data]
        graph = data.get("@graph")
        if isinstance(graph, list):
            for entry in graph:
                items.extend(_flatten_json_ld(entry))
        return items
    if isinstance(data, list):
        items: list[dict] = []
        for entry in data:
            items.extend(_flatten_json_ld(entry))
        return items
    return []


def json_ld_value(tree: HTMLParser, *keys: str) -> str:
    """Return the first string value found for ``keys`` in any JSON-LD object."""
    for obj in json_ld_objects(tree):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_text(value)
    return ""


def parse_datetime(raw: str | None) -> datetime | None:
    """Parse ISO-8601, numeric and Bulgarian textual dates into aware UTC values."""
    if not raw:
        return None
    text = normalize_text(raw)
    if not text:
        return None

    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        return _as_utc(parsed)

    numeric = _NUMERIC_DATE_RE.search(text)
    if numeric:
        day, month, year, hour, minute = numeric.groups()
        try:
            return _as_utc(
                datetime(
                    int(year), int(month), int(day), int(hour or 0), int(minute or 0)
                )
            )
        except ValueError:
            return None

    bulgarian = _BG_DATE_RE.search(text.casefold())
    if bulgarian:
        day, month_name, year, hour, minute = bulgarian.groups()
        month = _BG_MONTHS.get(month_name)
        if month:
            try:
                return _as_utc(
                    datetime(
                        int(year), month, int(day), int(hour or 0), int(minute or 0)
                    )
                )
            except ValueError:
                return None
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def node_text(node: Node | None) -> str:
    """Return readable text of a node with boilerplate elements removed."""
    if node is None:
        return ""
    for tag in BOILERPLATE_TAGS:
        for junk in node.css(tag):
            junk.decompose()
    paragraphs = [normalize_text(p.text()) for p in node.css("p")]
    paragraphs = [p for p in paragraphs if len(p) >= 2]
    if paragraphs:
        return "\n".join(paragraphs)
    return normalize_text(node.text())


def first_text(tree: HTMLParser, *selectors: str) -> str:
    """Return the text of the first matching selector."""
    for selector in selectors:
        node = tree.css_first(selector)
        if node is None:
            continue
        value = normalize_text(node.text())
        if value:
            return value
    return ""
