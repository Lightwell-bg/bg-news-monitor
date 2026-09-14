"""Source-agnostic heuristics that decide whether a link is an article.

The rules are deterministic and shared by every adapter so that a listing page
never turns a category, tag or pagination link into a news candidate.
"""

from __future__ import annotations

import re

_NUMERIC_ID_RE = re.compile(r"/\d{4,}")
_SLUG_RE = re.compile(r"/[^/]*-[^/]*-[^/]*")
_ARCHIVE_PATH_RE = re.compile(r"^/\d{4}(?:/\d{1,2}){0,2}/?$")

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

#: Path fragments that belong to listing and service pages, never to an article.
LISTING_PATH_PARTS: tuple[str, ...] = (
    "/category/",
    "/kategoriya/",
    "/kategorii/",
    "/rubrika/",
    "/tema/",
    "/tag/",
    "/tags/",
    "/author/",
    "/avtor/",
    "/page/",
    "/stranica/",
    "/search",
    "/feed",
    "/wp-json/",
    "/wp-content/",
    "/wp-admin/",
)

NON_ARTICLE_SCHEMES: tuple[str, ...] = ("#", "mailto:", "tel:", "javascript:")


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


def is_listing_path(path: str) -> bool:
    """Return True for category, tag, author, pagination and service paths."""
    lowered = (path or "").lower()
    if _ARCHIVE_PATH_RE.fullmatch(lowered):
        return True
    if not lowered.endswith("/"):
        lowered += "/"
    return any(fragment in lowered for fragment in LISTING_PATH_PARTS)


def is_skipped_link(href: str) -> bool:
    """Return True for anchors, mail, phone and script pseudo links."""
    value = (href or "").strip()
    return not value or value.startswith(NON_ARTICLE_SCHEMES)
