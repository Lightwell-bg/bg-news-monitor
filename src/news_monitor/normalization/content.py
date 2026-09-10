"""Normalization of article text and content hashing."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

CONTENT_HASH_BODY_CHARS = 2000


def normalize_text(text: str | None) -> str:
    """Collapse whitespace and unify Unicode composition."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\u00a0", " ")
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def normalize_for_hash(text: str | None) -> str:
    """Aggressive normalization: case folded, punctuation free, whitespace collapsed."""
    normalized = normalize_text(text).casefold()
    normalized = _PUNCT_RE.sub(" ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def content_hash(title: str | None, body: str | None) -> str:
    """Return the SHA-256 hash of the normalized title and the body prefix.

    Only the first :data:`CONTENT_HASH_BODY_CHARS` characters of the body take
    part so that later editorial additions to a long article do not create a
    second candidate for the same news item.
    """
    normalized_title = normalize_for_hash(title)
    normalized_body = normalize_for_hash(body)[:CONTENT_HASH_BODY_CHARS]
    if not normalized_title and not normalized_body:
        raise ValueError("cannot hash empty content")
    payload = f"{normalized_title}\n{normalized_body}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
