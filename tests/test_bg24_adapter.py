"""BG24 adapter parsing, driven only by local HTML fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from news_monitor.sources.bg24 import Bg24HomepageAdapter
from news_monitor.sources.links import is_listing_path, looks_like_article_path
from news_monitor.sources.registry import available_adapters, get_adapter
from tests.conftest import fixture_html

HOMEPAGE_URL = "https://bg-24.com/"
ARTICLE_URL = "https://bg-24.com/novini/125478-nova-naredba-za-chuzhdentsite"


def adapter() -> Bg24HomepageAdapter:
    return Bg24HomepageAdapter()


def _listing_urls() -> list[str]:
    return [
        item.url
        for item in adapter().parse_listing(
            fixture_html("bg24_homepage.html"), HOMEPAGE_URL
        )
    ]


def test_adapter_is_registered() -> None:
    assert "bg24_homepage" in available_adapters()
    assert isinstance(get_adapter("bg24_homepage"), Bg24HomepageAdapter)


def test_listing_returns_only_article_links() -> None:
    urls = _listing_urls()

    assert ARTICLE_URL in urls
    assert "https://bg-24.com/2026/09/10/burgas-startira-nova-avtobusna-liniya" in urls
    assert "https://bg-24.com/novini/125501-tokut-poskupva-ot-oktomvri" in urls
    assert len(urls) == 3


def test_listing_keeps_the_titles_of_the_links() -> None:
    items = adapter().parse_listing(fixture_html("bg24_homepage.html"), HOMEPAGE_URL)
    titles = {item.url: item.title for item in items}

    assert titles[ARTICLE_URL] == "Нова наредба за чужденците влиза в сила"


def test_listing_drops_categories_tags_authors_and_pagination() -> None:
    urls = _listing_urls()

    assert not any("/kategoriya/" in url for url in urls)
    assert not any("/tag/" in url for url in urls)
    assert not any("/avtor/" in url for url in urls)
    assert not any("/page/" in url for url in urls)


def test_listing_drops_foreign_hosts_assets_and_anchors() -> None:
    urls = _listing_urls()

    assert not any("example.com" in url for url in urls)
    assert not any(url.endswith(".jpg") for url in urls)
    assert not any("#" in url for url in urls)


def test_listing_deduplicates_and_strips_tracking_parameters() -> None:
    urls = _listing_urls()

    assert len(urls) == len(set(urls))
    assert not any("utm_" in url for url in urls)


def test_listing_of_an_empty_document_is_empty() -> None:
    assert adapter().parse_listing("", HOMEPAGE_URL) == []
    assert adapter().parse_listing("<html><body></body></html>", HOMEPAGE_URL) == []


def test_article_extraction() -> None:
    article = adapter().parse_article(fixture_html("bg24_article.html"), ARTICLE_URL)

    assert article is not None
    assert article.url == ARTICLE_URL
    assert article.title == "Нова наредба за чужденците влиза в сила"
    assert article.published_at == datetime(2026, 9, 10, 6, 15, tzinfo=UTC)
    assert "1 ноември 2026" in article.body
    assert "pageCounter" not in article.body
    assert len(article.body) > 400
    assert article.section == "Общество"


def test_incomplete_article_is_unusable() -> None:
    assert (
        adapter().parse_article(
            fixture_html("bg24_article_incomplete.html"),
            "https://bg-24.com/novini/1-v-podgotovka",
        )
        is None
    )


def test_article_without_a_title_is_unusable() -> None:
    assert (
        adapter().parse_article(
            fixture_html("bg24_article_no_title.html"),
            "https://bg-24.com/novini/2-bez-zaglavie",
        )
        is None
    )


def test_article_of_an_empty_document_is_unusable() -> None:
    assert adapter().parse_article("", ARTICLE_URL) is None


def test_link_heuristics() -> None:
    assert looks_like_article_path("/novini/125478-nova-naredba")
    assert looks_like_article_path("/2026/09/10/burgas-nova-liniya-dnes")
    assert not looks_like_article_path("/")
    assert not looks_like_article_path("/kontakti")
    assert is_listing_path("/kategoriya/biznes-i-ikonomika/")
    assert is_listing_path("/page/2/")
    assert is_listing_path("/2026/09/")
    assert not is_listing_path("/novini/125478-nova-naredba")
