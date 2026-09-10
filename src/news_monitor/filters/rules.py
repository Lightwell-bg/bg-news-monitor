"""Deterministic rules applied before any AI call.

These checks are intentionally cheap and fully reproducible: they protect the
AI budget and guarantee that obviously unusable pages never reach the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from news_monitor.config.sources import DeterministicRules, SourceConfig
from news_monitor.normalization.content import normalize_text
from news_monitor.normalization.url import same_host
from news_monitor.sources.base import ArticleContent


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    """Outcome of the deterministic pre-AI filter."""

    accepted: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.accepted


ACCEPTED = FilterVerdict(accepted=True)


def check_listing_url(url: str, source: SourceConfig) -> FilterVerdict:
    """Reject links that do not belong to the source or lead to service pages."""
    rules = source.filters
    if not same_host(url, str(source.base_url)):
        return FilterVerdict(False, "url outside the configured source host")
    path = urlsplit(url).path.lower()
    for fragment in rules.excluded_path_parts:
        if fragment.lower() in path:
            return FilterVerdict(False, f"excluded path fragment: {fragment}")
    return ACCEPTED


def check_article(
    article: ArticleContent,
    source: SourceConfig,
    *,
    now: datetime | None = None,
) -> FilterVerdict:
    """Reject articles that are too short, too old or explicitly blocked."""
    rules: DeterministicRules = source.filters
    title = normalize_text(article.title)
    body = normalize_text(article.body)

    if len(title) < rules.min_title_length:
        return FilterVerdict(
            False, f"title shorter than {rules.min_title_length} characters"
        )
    if len(body) < rules.min_body_length:
        return FilterVerdict(
            False, f"body shorter than {rules.min_body_length} characters"
        )

    lowered_title = title.casefold()
    for keyword in rules.blocked_title_keywords:
        if keyword.strip() and keyword.strip().casefold() in lowered_title:
            return FilterVerdict(False, f"blocked title keyword: {keyword}")

    if article.published_at is not None:
        reference = now or datetime.now(UTC)
        published = article.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_hours = (reference - published).total_seconds() / 3600
        if age_hours > rules.max_age_hours:
            return FilterVerdict(
                False, f"older than {rules.max_age_hours} hours"
            )
        if age_hours < -1:
            return FilterVerdict(False, "publication time is in the future")

    return ACCEPTED
