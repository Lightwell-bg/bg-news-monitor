"""Flagman adapter parsing, driven only by local HTML fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from news_monitor.sources.flagman import FlagmanHomepageAdapter, looks_like_article_path
from tests.conftest import fixture_html

HOMEPAGE_URL = "https://www.flagman.bg/"


def adapter() -> FlagmanHomepageAdapter:
    return FlagmanHomepageAdapter()


def test_listing_returns_only_article_links() -> None:
    items = adapter().parse_listing(fixture_html("flagman_homepage.html"), HOMEPAGE_URL)
    urls = [item.url for item in items]

    assert "https://flagman.bg/statia/307722-nov-zakon-za-chuzhdentsite-vliza-v-sila" in urls
    assert "https://flagman.bg/statia/307801-burgas-nova-bolnica-otvarya-vrati" in urls
    assert "https://flagman.bg/statia/307815-danaci-imoti-2026" in urls


def test_listing_drops_foreign_hosts_assets_and_anchors() -> None:
    urls = [
        item.url
        for item in adapter().parse_listing(
            fixture_html("flagman_homepage.html"), HOMEPAGE_URL
        )
    ]
    assert not any("example.com" in url for url in urls)
    assert not any(url.endswith(".jpg") for url in urls)
    assert not any("#" in url for url in urls)


def test_listing_deduplicates_normalized_links() -> None:
    urls = [
        item.url
        for item in adapter().parse_listing(
            fixture_html("flagman_homepage.html"), HOMEPAGE_URL
        )
    ]
    assert len(urls) == len(set(urls))


def test_listing_strips_tracking_parameters() -> None:
    urls = [
        item.url
        for item in adapter().parse_listing(
            fixture_html("flagman_homepage.html"), HOMEPAGE_URL
        )
    ]
    assert not any("utm_" in url for url in urls)


def test_article_extraction() -> None:
    article = adapter().parse_article(
        fixture_html("flagman_article.html"),
        "https://flagman.bg/statia/307722-nov-zakon-za-chuzhdentsite-vliza-v-sila",
    )
    assert article is not None
    assert article.title == "Нов закон за чужденците влиза в сила от октомври"
    assert article.published_at == datetime(2026, 9, 10, 5, 30, tzinfo=UTC)
    assert "15 октомври 2026" in article.body
    assert "var counter" not in article.body
    assert len(article.body) > 400


def test_article_without_title_is_unusable() -> None:
    assert (
        adapter().parse_article(
            fixture_html("flagman_article_no_title.html"), "https://flagman.bg/x"
        )
        is None
    )


def test_looks_like_article_path() -> None:
    assert looks_like_article_path("/statia/307722-nov-zakon-za-chuzhdentsite")
    assert looks_like_article_path("/2026/09/10/dulga-statia-za-neshto")
    assert not looks_like_article_path("/")
    assert not looks_like_article_path("/novini")
    assert not looks_like_article_path("/uploads/2026/photo-of-the-day.jpg")
