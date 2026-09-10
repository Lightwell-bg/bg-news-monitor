"""Deterministic pre-AI rules and the code-enforced draft guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from news_monitor.filters.guards import (
    check_no_invented_numbers,
    check_not_verbatim_copy,
    check_source_link,
    longest_verbatim_run,
    unsupported_numbers,
)
from news_monitor.filters.rules import check_article, check_listing_url
from news_monitor.sources.base import ArticleContent

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BODY = (
    "От 15 октомври 2026 година влизат в сила промени в Закона за чужденците. "
    "Документите се подават електронно през портала на Миграция. Срокът за "
    "разглеждане се съкращава от 30 на 14 дни. Държавната такса остава 500 лева."
) * 2


def _article(**overrides) -> ArticleContent:
    data = {
        "url": "https://flagman.bg/statia/1",
        "title": "Нов закон за чужденците влиза в сила",
        "body": BODY,
        "published_at": NOW - timedelta(hours=2),
    }
    data.update(overrides)
    return ArticleContent(**data)


def test_listing_url_from_the_source_is_accepted(source) -> None:
    assert check_listing_url("https://flagman.bg/statia/307722-novina", source)


def test_listing_url_from_another_host_is_refused(source) -> None:
    verdict = check_listing_url("https://example.com/statia/1", source)
    assert not verdict.accepted
    assert "host" in verdict.reason


@pytest.mark.parametrize(
    "url",
    [
        "https://flagman.bg/sport/futbol-mach-1234",
        "https://flagman.bg/tag/burgas-novini",
        "https://flagman.bg/galeria/307900-snimki-ot-plazha",
        "https://flagman.bg/video/307901-reportazh-ot-myastoto",
    ],
)
def test_service_sections_are_refused(url: str, source) -> None:
    assert not check_listing_url(url, source).accepted


def test_complete_article_is_accepted(source) -> None:
    assert check_article(_article(), source, now=NOW)


def test_short_title_is_refused(source) -> None:
    verdict = check_article(_article(title="Кратко"), source, now=NOW)
    assert not verdict.accepted
    assert "title" in verdict.reason


def test_short_body_is_refused(source) -> None:
    verdict = check_article(_article(body="Само едно изречение."), source, now=NOW)
    assert not verdict.accepted
    assert "body" in verdict.reason


def test_old_article_is_refused(source) -> None:
    verdict = check_article(
        _article(published_at=NOW - timedelta(hours=100)), source, now=NOW
    )
    assert not verdict.accepted
    assert "older" in verdict.reason


def test_article_without_a_date_is_still_accepted(source) -> None:
    assert check_article(_article(published_at=None), source, now=NOW)


def test_blocked_title_keyword_is_refused(source) -> None:
    source.filters.blocked_title_keywords = ["хороскоп"]
    verdict = check_article(
        _article(title="Хороскоп за днес и утре за всички зодии"), source, now=NOW
    )
    assert not verdict.accepted
    assert "keyword" in verdict.reason


SOURCE_TEXT = (
    "От 15 октомври 2026 година влизат в сила промени в Закона за чужденците в "
    "Република България. Документите за продължително пребиваване ще се подават "
    "само по електронен път през портала на Миграция. Срокът за разглеждане на "
    "заявленията се съкращава от 30 на 14 дни. Държавната такса остава 500 лева "
    "за първоначално разрешение и 250 лева за подновяване."
)


def test_own_words_summary_passes_the_copy_guard() -> None:
    draft = (
        "С 15 октября 2026 года документы на длительное пребывание принимают "
        "только через электронный портал. Рассмотрение сокращается до 14 дней."
    )
    assert check_not_verbatim_copy(draft, SOURCE_TEXT)


def test_full_copy_of_the_article_is_refused() -> None:
    verdict = check_not_verbatim_copy(SOURCE_TEXT, SOURCE_TEXT)
    assert not verdict.passed
    assert "verbatim" in verdict.reason or "length" in verdict.reason


def test_long_verbatim_fragment_is_refused() -> None:
    fragment = " ".join(SOURCE_TEXT.split()[:30])
    long_source = SOURCE_TEXT * 4
    verdict = check_not_verbatim_copy(fragment + " Допълнителен текст.", long_source)
    assert not verdict.passed
    assert "verbatim" in verdict.reason


def test_longest_verbatim_run_is_measured_in_words() -> None:
    assert longest_verbatim_run("съвсем друг текст", SOURCE_TEXT) <= 2
    assert longest_verbatim_run(SOURCE_TEXT, SOURCE_TEXT) > 20


def test_numbers_present_in_the_source_are_supported() -> None:
    draft = "Срок сокращается до 14 дней, пошлина остаётся 500 лева с 15 октября 2026 года."
    assert unsupported_numbers(draft, SOURCE_TEXT) == []
    assert check_no_invented_numbers(draft, SOURCE_TEXT)


def test_invented_numbers_are_detected() -> None:
    draft = "Пошлина вырастет до 900 лева, а срок составит 99 дней."
    missing = unsupported_numbers(draft, SOURCE_TEXT)
    assert "900" in missing
    assert "99" in missing

    verdict = check_no_invented_numbers(draft, SOURCE_TEXT)
    assert not verdict.passed
    assert "900" in verdict.reason


def test_thousands_separators_do_not_create_false_positives() -> None:
    source_text = "Инвестицията е 75 000 лева за проекта в Бургас."
    draft = "Инвестиции составят 75 000 лева."
    assert unsupported_numbers(draft, source_text) == []


def test_draft_without_numbers_is_supported() -> None:
    assert check_no_invented_numbers("Правила меняются осенью.", SOURCE_TEXT)


def test_source_link_must_be_present() -> None:
    url = "https://flagman.bg/statia/1"
    assert check_source_link(f"Текст\nИсточник: {url}", url)

    verdict = check_source_link("Текст без ссылки", url)
    assert not verdict.passed
    assert "link" in verdict.reason

    assert not check_source_link("Текст", "").passed
