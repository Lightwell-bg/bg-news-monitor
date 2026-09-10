"""The vertical scenario: listing, article, deduplication, filter, AI, card.

The pipeline never publishes. Its last step is a private card for the
administrator; the channel is reachable only through ModerationService.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from news_monitor.ai.client import AIAssessor, AIRequestError
from news_monitor.ai.schema import AIAssessment, AIAssessmentError
from news_monitor.config.settings import Settings
from news_monitor.config.sources import SourceConfig
from news_monitor.filters.guards import check_draft
from news_monitor.filters.rules import check_article, check_listing_url
from news_monitor.normalization.content import content_hash
from news_monitor.sources.base import ArticleContent, HtmlFetcher, SourceAdapter
from news_monitor.sources.fetcher import FetchError
from news_monitor.storage.models import NewsItem, NewsStatus
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.sender import AdminCardSender

logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    """Counters describing one scheduled run of a source."""

    source_id: str
    discovered: int = 0
    already_known: int = 0
    fetched: int = 0
    duplicates: int = 0
    rejected_by_filter: int = 0
    candidates: int = 0
    cards_sent: int = 0
    ai_failures: int = 0
    fetch_failures: int = 0
    guard_failures: int = 0
    below_threshold: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return the counters as a plain dictionary for logging."""
        return {
            "source_id": self.source_id,
            "discovered": self.discovered,
            "already_known": self.already_known,
            "fetched": self.fetched,
            "duplicates": self.duplicates,
            "rejected_by_filter": self.rejected_by_filter,
            "candidates": self.candidates,
            "cards_sent": self.cards_sent,
            "ai_failures": self.ai_failures,
            "fetch_failures": self.fetch_failures,
            "guard_failures": self.guard_failures,
            "below_threshold": self.below_threshold,
        }


class NewsPipeline:
    """Runs one source end to end, up to the moderation card."""

    def __init__(
        self,
        *,
        source: SourceConfig,
        adapter: SourceAdapter,
        fetcher: HtmlFetcher,
        repository: NewsRepository,
        assessor: AIAssessor,
        card_sender: AdminCardSender,
        settings: Settings,
    ) -> None:
        self._source = source
        self._adapter = adapter
        self._fetcher = fetcher
        self._repository = repository
        self._assessor = assessor
        self._card_sender = card_sender
        self._settings = settings

    @property
    def source(self) -> SourceConfig:
        return self._source

    async def run_once(self, *, now: datetime | None = None) -> RunReport:
        """Process every configured section of the source."""
        report = RunReport(source_id=self._source.id)
        reference = now or datetime.now(UTC)
        for section in self._source.sections:
            await self._process_section(
                section_name=section.name,
                section_url=str(section.url),
                report=report,
                now=reference,
            )
        logger.info("run finished: %s", report.as_dict())
        return report

    async def _process_section(
        self, *, section_name: str, section_url: str, report: RunReport, now: datetime
    ) -> None:
        try:
            html = await self._fetcher.fetch(section_url)
        except FetchError as exc:
            report.fetch_failures += 1
            report.errors.append(f"listing {section_name}: {exc}")
            logger.warning("cannot fetch listing %s: %s", section_name, exc)
            return

        processed = 0
        for listing_item in self._adapter.parse_listing(html, section_url):
            if processed >= self._settings.max_articles_per_run:
                break

            url_verdict = check_listing_url(listing_item.url, self._source)
            if not url_verdict.accepted:
                continue

            item, created = await self._repository.register_discovered(
                source_id=self._source.id,
                source_name=self._source.name,
                url=listing_item.url,
                title=listing_item.title,
                section=section_name,
                base_url=str(self._source.base_url),
            )
            if not created:
                report.already_known += 1
                continue

            report.discovered += 1
            processed += 1
            await self._process_article(item, report=report, now=now)

    async def _process_article(
        self, item: NewsItem, *, report: RunReport, now: datetime
    ) -> None:
        """Fetch, deduplicate, filter, assess and finally build a card."""
        article = await self.fetch_article(item.url)
        if article is None:
            report.fetch_failures += 1
            await self._repository.mark_rejected_by_filter(
                item.id, "article page could not be parsed"
            )
            report.rejected_by_filter += 1
            return

        try:
            digest = content_hash(article.title, article.body)
        except ValueError:
            await self._repository.mark_rejected_by_filter(item.id, "empty content")
            report.rejected_by_filter += 1
            return

        stored = await self._repository.mark_fetched(
            item.id,
            title=article.title,
            body_hash=digest,
            published_at=article.published_at,
            section=article.section,
        )
        if stored.status_enum is NewsStatus.DUPLICATE:
            report.duplicates += 1
            return
        report.fetched += 1

        article_verdict = check_article(article, self._source, now=now)
        if not article_verdict.accepted:
            await self._repository.mark_rejected_by_filter(
                item.id, article_verdict.reason
            )
            report.rejected_by_filter += 1
            return

        assessment = await self.assess(article)
        if assessment is None:
            report.ai_failures += 1
            return

        guard = check_draft(assessment.draft_ru, article.body, title=assessment.title_ru)
        if not guard.passed:
            report.guard_failures += 1
            await self._repository.mark_rejected_by_filter(
                item.id, f"draft guard: {guard.reason}"
            )
            report.rejected_by_filter += 1
            logger.warning("draft rejected for item %s: %s", item.id, guard.reason)
            return

        if assessment.importance < self._settings.importance_threshold:
            report.below_threshold += 1
            await self._repository.mark_rejected_by_filter(
                item.id,
                f"importance {assessment.importance} below threshold "
                f"{self._settings.importance_threshold}",
            )
            report.rejected_by_filter += 1
            return

        candidate = await self._repository.mark_candidate(
            item.id,
            importance=assessment.importance,
            ai_reason=assessment.reason,
            title_ru=assessment.title_ru,
            draft_ru=assessment.draft_ru,
        )
        report.candidates += 1

        delivered = await self._card_sender.send_card(candidate)
        if delivered is not None:
            report.cards_sent += 1

    async def fetch_article(self, url: str) -> ArticleContent | None:
        """Fetch and parse one article page."""
        try:
            html = await self._fetcher.fetch(url)
        except FetchError as exc:
            logger.warning("cannot fetch article: %s", exc)
            return None
        return self._adapter.parse_article(html, url)

    async def assess(self, article: ArticleContent) -> AIAssessment | None:
        """Return a validated assessment, or None when the answer is unusable.

        An invalid AI answer never becomes a card: the item simply stays in
        FETCHED and is picked up again on a later run.
        """
        try:
            return await self._assessor.assess(article, self._source)
        except AIAssessmentError as exc:
            logger.warning("invalid AI answer, no card created: %s", exc)
            return None
        except AIRequestError as exc:
            logger.warning("AI request failed, no card created: %s", exc)
            return None

    async def regenerate(self, item: NewsItem) -> AIAssessment | None:
        """Re-run the assessment for an item still awaiting a decision.

        The draft guards apply again, so a regenerated draft that copies the
        article or invents figures is discarded instead of replacing the old one.
        """
        article = await self.fetch_article(item.url)
        if article is None:
            return None
        assessment = await self.assess(article)
        if assessment is None:
            return None
        guard = check_draft(assessment.draft_ru, article.body, title=assessment.title_ru)
        if not guard.passed:
            logger.warning(
                "regenerated draft rejected for item %s: %s", item.id, guard.reason
            )
            return None
        return assessment
