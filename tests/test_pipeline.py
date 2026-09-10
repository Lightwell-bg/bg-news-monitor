"""The vertical scenario end to end, on fixtures and fakes only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from news_monitor.ai.client import AIRequestError
from news_monitor.ai.schema import AIAssessment, AIAssessmentError
from news_monitor.pipeline import NewsPipeline
from news_monitor.sources.flagman import FlagmanHomepageAdapter
from news_monitor.storage.models import NewsStatus
from news_monitor.telegram.sender import AdminCardSender
from tests.conftest import ADMIN_ID, FakeAssessor, FakeHtmlFetcher, fixture_html

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

HOMEPAGE = "https://www.flagman.bg/"
ARTICLE_1 = "https://flagman.bg/statia/307722-nov-zakon-za-chuzhdentsite-vliza-v-sila"
ARTICLE_2 = "https://flagman.bg/statia/307801-burgas-nova-bolnica-otvarya-vrati"
ARTICLE_3 = "https://flagman.bg/statia/307815-danaci-imoti-2026"


def _fetcher(**extra: str) -> FakeHtmlFetcher:
    pages = {
        HOMEPAGE: fixture_html("flagman_homepage.html"),
        ARTICLE_1: fixture_html("flagman_article.html"),
        ARTICLE_2: fixture_html("flagman_article_second.html"),
        ARTICLE_3: fixture_html("flagman_article_short.html"),
    }
    pages.update(extra)
    return FakeHtmlFetcher(pages)


def _pipeline(repository, settings, source, fetcher, assessor, fake_bot) -> NewsPipeline:
    return NewsPipeline(
        source=source,
        adapter=FlagmanHomepageAdapter(),
        fetcher=fetcher,
        repository=repository,
        assessor=assessor,
        card_sender=AdminCardSender(fake_bot, repository, settings.admin_ids),
        settings=settings,
    )


async def test_new_article_becomes_a_card(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    fetcher = _fetcher()
    assessor = FakeAssessor(valid_assessment)
    pipeline = _pipeline(repository, settings, source, fetcher, assessor, fake_bot)

    report = await pipeline.run_once(now=NOW)

    assert report.discovered == 3
    assert report.cards_sent >= 1
    assert fake_bot.messages_to(ADMIN_ID)

    stored = await repository.get_by_normalized_url(ARTICLE_1)
    assert stored is not None
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL
    assert stored.title_ru == valid_assessment.title_ru


async def test_nothing_is_published_by_the_pipeline(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    pipeline = _pipeline(
        repository, settings, source, _fetcher(), FakeAssessor(valid_assessment), fake_bot
    )
    await pipeline.run_once(now=NOW)

    channel_messages = [
        message for message in fake_bot.sent if message.chat_id == settings.telegram_channel_id
    ]
    assert channel_messages == []

    published = await repository.list_by_status(NewsStatus.PUBLISHED)
    assert published == []


async def test_second_run_does_not_duplicate_work(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    fetcher = _fetcher()
    assessor = FakeAssessor(valid_assessment)
    pipeline = _pipeline(repository, settings, source, fetcher, assessor, fake_bot)

    first = await pipeline.run_once(now=NOW)
    cards_after_first = len(fake_bot.sent)
    ai_calls_after_first = len(assessor.calls)

    second = await pipeline.run_once(now=NOW)

    assert first.discovered == 3
    assert second.discovered == 0
    assert second.already_known == 3
    assert len(fake_bot.sent) == cards_after_first
    assert len(assessor.calls) == ai_calls_after_first


async def test_short_article_is_rejected_before_the_ai_call(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    assessor = FakeAssessor(valid_assessment)
    pipeline = _pipeline(repository, settings, source, _fetcher(), assessor, fake_bot)

    await pipeline.run_once(now=NOW)

    stored = await repository.get_by_normalized_url(ARTICLE_3)
    assert stored is not None
    assert stored.status_enum is NewsStatus.REJECTED_BY_FILTER
    assert ARTICLE_3 not in assessor.calls


async def test_reprinted_article_is_marked_duplicate(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    fetcher = _fetcher(**{ARTICLE_2: fixture_html("flagman_article.html")})
    pipeline = _pipeline(
        repository, settings, source, fetcher, FakeAssessor(valid_assessment), fake_bot
    )

    report = await pipeline.run_once(now=NOW)

    assert report.duplicates == 1
    statuses = {
        (await repository.get_by_normalized_url(url)).status_enum
        for url in (ARTICLE_1, ARTICLE_2)
    }
    assert NewsStatus.DUPLICATE in statuses


@pytest.mark.parametrize(
    "error",
    [
        AIAssessmentError("AI answer is not valid JSON"),
        AIRequestError("OpenRouter returned HTTP 500"),
    ],
)
async def test_invalid_ai_answer_creates_no_card(
    repository, settings, source, fake_bot, error
) -> None:
    assessor = FakeAssessor(error=error)
    pipeline = _pipeline(repository, settings, source, _fetcher(), assessor, fake_bot)

    report = await pipeline.run_once(now=NOW)

    assert report.ai_failures >= 1
    assert report.cards_sent == 0
    assert fake_bot.sent == []

    stored = await repository.get_by_normalized_url(ARTICLE_1)
    assert stored is not None
    assert stored.status_enum is NewsStatus.FETCHED
    assert stored.title_ru == ""


async def test_low_importance_creates_no_card(
    repository, settings, source, fake_bot
) -> None:
    weak = AIAssessment(
        importance=20,
        reason="Местное событие без значения для аудитории.",
        title_ru="Незначительная новость",
        draft_ru="Событие касается узкого круга людей и не меняет правил.",
    )
    pipeline = _pipeline(repository, settings, source, _fetcher(), FakeAssessor(weak), fake_bot)

    report = await pipeline.run_once(now=NOW)

    assert report.below_threshold >= 1
    assert report.cards_sent == 0
    stored = await repository.get_by_normalized_url(ARTICLE_1)
    assert stored.status_enum is NewsStatus.REJECTED_BY_FILTER


async def test_draft_copying_the_article_creates_no_card(
    repository, settings, source, fake_bot
) -> None:
    article_body = FlagmanHomepageAdapter().parse_article(
        fixture_html("flagman_article.html"), ARTICLE_1
    ).body
    copied = AIAssessment(
        importance=95,
        reason="Полное копирование текста статьи.",
        title_ru="Скопированная новость",
        draft_ru=article_body[:1400],
    )
    pipeline = _pipeline(
        repository, settings, source, _fetcher(), FakeAssessor(copied), fake_bot
    )

    report = await pipeline.run_once(now=NOW)

    assert report.guard_failures >= 1
    assert report.cards_sent == 0
    assert fake_bot.sent == []


async def test_draft_with_invented_numbers_creates_no_card(
    repository, settings, source, fake_bot
) -> None:
    invented = AIAssessment(
        importance=95,
        reason="Черновик содержит выдуманные цифры.",
        title_ru="Новость с выдуманными цифрами",
        draft_ru=(
            "Пошлина вырастет до 9999 лева, а срок рассмотрения составит 77 дней "
            "для всех заявителей начиная с 3333 года."
        ),
    )
    pipeline = _pipeline(
        repository, settings, source, _fetcher(), FakeAssessor(invented), fake_bot
    )

    report = await pipeline.run_once(now=NOW)

    assert report.guard_failures >= 1
    assert report.cards_sent == 0

    stored = await repository.get_by_normalized_url(ARTICLE_1)
    assert stored.status_enum is NewsStatus.REJECTED_BY_FILTER
    assert "draft guard" in stored.status_reason


async def test_title_with_invented_number_creates_no_card(
    repository, settings, source, fake_bot
) -> None:
    """A fabricated figure in the Russian headline is as invented as one in
    the draft body, so it must block the card even when draft_ru is clean."""
    invented_title = AIAssessment(
        importance=95,
        reason="Заголовок содержит выдуманную цифру.",
        title_ru="Штраф вырастет до 500000 лева для нарушителей",
        draft_ru="Правила меняются в этом году для всех заинтересованных сторон.",
    )
    pipeline = _pipeline(
        repository, settings, source, _fetcher(), FakeAssessor(invented_title), fake_bot
    )

    report = await pipeline.run_once(now=NOW)

    assert report.guard_failures >= 1
    assert report.cards_sent == 0
    assert fake_bot.sent == []

    stored = await repository.get_by_normalized_url(ARTICLE_1)
    assert stored.status_enum is NewsStatus.REJECTED_BY_FILTER
    assert "draft guard" in stored.status_reason


async def test_unreachable_listing_is_reported(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    fetcher = _fetcher()
    fetcher.failures.add(HOMEPAGE)
    pipeline = _pipeline(
        repository, settings, source, fetcher, FakeAssessor(valid_assessment), fake_bot
    )

    report = await pipeline.run_once(now=NOW)

    assert report.fetch_failures == 1
    assert report.discovered == 0
    assert fake_bot.sent == []


async def test_max_articles_per_run_is_respected(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    settings.max_articles_per_run = 1
    pipeline = _pipeline(
        repository, settings, source, _fetcher(), FakeAssessor(valid_assessment), fake_bot
    )

    report = await pipeline.run_once(now=NOW)
    assert report.discovered == 1


async def test_regenerate_reruns_the_guards(
    repository, settings, source, fake_bot, valid_assessment
) -> None:
    fetcher = _fetcher()
    assessor = FakeAssessor(valid_assessment)
    pipeline = _pipeline(repository, settings, source, fetcher, assessor, fake_bot)
    await pipeline.run_once(now=NOW)

    item = await repository.get_by_normalized_url(ARTICLE_1)
    again = await pipeline.regenerate(item)
    assert again is not None
    assert again.title_ru == valid_assessment.title_ru

    assessor.error = AIAssessmentError("broken answer")
    assessor.assessment = None
    assert await pipeline.regenerate(item) is None
