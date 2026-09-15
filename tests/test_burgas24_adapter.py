"""Burgas24 adapter parsing, driven only by local HTML fixtures."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from news_monitor.config.settings import Settings
from news_monitor.config.sources import get_source
from news_monitor.config.sources_store import SourcesStore
from news_monitor.sources.burgas24 import Burgas24HomepageAdapter, is_article_path
from news_monitor.sources.registry import available_adapters, get_adapter
from news_monitor.telegram.sources_admin import SourceAdminService
from tests.conftest import ADMIN_ID, ROOT, fixture_html

HOMEPAGE_URL = "https://www.burgas24.bg/"
ARTICLE_URL = (
    "https://www.burgas24.bg/novini/Bylgaria/"
    "NAP-proveri-turgovci-na-cvetya-predi-15-septemvri-3025405"
)
PROJECT_SOURCES_FILE = ROOT / "config" / "sources.yaml"

LEAD_URL = (
    "https://burgas24.bg/novini/burgas/"
    "Obshtina-Burgas-otkri-novo-uchilishte-v-Kraimorie-3025321"
)
NAP_URL = (
    "https://burgas24.bg/novini/Bylgaria/"
    "NAP-proveri-turgovci-na-cvetya-predi-15-septemvri-3025405"
)
POMORIE_URL = (
    "https://burgas24.bg/novini/regionalni/"
    "Novi-avtobusni-linii-v-Pomorie-ot-oktomvri-3025339"
)
EU_URL = (
    "https://burgas24.bg/novini/mejdunarodni/"
    "ES-promenya-pravilata-za-prebivavane-3025410"
)


def adapter() -> Burgas24HomepageAdapter:
    return Burgas24HomepageAdapter()


def _listing():
    return adapter().parse_listing(fixture_html("burgas24_homepage.html"), HOMEPAGE_URL)


def _listing_urls() -> list[str]:
    return [item.url for item in _listing()]


# ----------------------------------------------------------------- registry


def test_adapter_is_registered() -> None:
    assert "burgas24_homepage" in available_adapters()
    assert isinstance(get_adapter("burgas24_homepage"), Burgas24HomepageAdapter)


# ------------------------------------------------------------------ listing


def test_listing_returns_only_unique_article_links_in_document_order() -> None:
    assert _listing_urls() == [LEAD_URL, NAP_URL, POMORIE_URL, EU_URL]


def test_listing_titles_fall_back_to_the_title_attribute_and_image_alt() -> None:
    titles = {item.url: item.title for item in _listing()}

    # The first link of a tile is an image with a title attribute.
    assert titles[LEAD_URL] == "Община Бургас откри ново училище в Крайморие"
    # This image link has neither text nor title: the alt text is used.
    assert titles[NAP_URL] == "НАП провери търговци на цветя преди 15 септември"
    assert titles[POMORIE_URL] == "Нови автобусни линии в Поморие от октомври"
    assert all(titles.values())


@pytest.mark.parametrize(
    "fragment",
    [
        "/showall.html",
        "/novini/topics/",
        "/novini/videos",
        "/sport",
        "/obiavi/",
        "/spravochnik/",
        "/team",
        "/kontakti",
        "/reklama",
        "/potrebiteli/",
        "weather.html",
        "rss.html",
        "op=",
        ".webp",
    ],
)
def test_listing_drops_service_pages_ads_media_and_sport(fragment: str) -> None:
    assert not any(fragment in url for url in _listing_urls())


def test_listing_drops_rubric_landing_pages() -> None:
    urls = _listing_urls()

    for rubric in ("/novini", "/novini/burgas", "/novini/Bylgaria", "/sport"):
        assert "https://burgas24.bg" + rubric not in urls


def test_listing_drops_other_hosts_sister_sites_and_pseudo_links() -> None:
    urls = _listing_urls()

    assert all(url.startswith("https://burgas24.bg/novini/") for url in urls)
    assert not any("sofia24.bg" in url for url in urls)
    assert not any("facebook.com" in url for url in urls)
    assert not any("javascript" in url or "#" in url for url in urls)


def test_listing_strips_tracking_and_fragments_before_deduplication() -> None:
    urls = _listing_urls()

    assert len(urls) == len(set(urls))
    assert not any("utm_" in url for url in urls)
    assert urls.count(NAP_URL) == 1


def test_listing_ignores_links_outside_the_main_content() -> None:
    assert not any("Arhivna-statiya" in url for url in _listing_urls())


def test_listing_without_main_uses_the_whole_document() -> None:
    html = (
        "<html><body><div>"
        '<a href="/novini/burgas/Nov-park-v-kvartal-Meden-rudnik-3026001">Нов парк</a>'
        '<a href="/novini/burgas/">Бургас</a>'
        "</div></body></html>"
    )

    assert [item.url for item in adapter().parse_listing(html, HOMEPAGE_URL)] == [
        "https://burgas24.bg/novini/burgas/Nov-park-v-kvartal-Meden-rudnik-3026001"
    ]


def test_listing_of_an_empty_document_is_empty() -> None:
    assert adapter().parse_listing("", HOMEPAGE_URL) == []
    assert adapter().parse_listing("<html><body></body></html>", HOMEPAGE_URL) == []


def test_listing_without_article_links_is_empty() -> None:
    html = (
        "<html><body><main>"
        '<a href="https://www.burgas24.bg/obiavi/imoti/">Имоти</a>'
        '<a href="https://www.burgas24.bg/sport/3025429.html">Спорт</a>'
        '<a href="https://www.burgas24.bg/novini/14.09.2026/showall.html">Вчера</a>'
        "</main></body></html>"
    )

    assert adapter().parse_listing(html, HOMEPAGE_URL) == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/novini/burgas/Obshtina-Burgas-otkri-novo-uchilishte-3025321", True),
        ("/novini/Bylgaria/NAP-proveri-turgovci-3025405", True),
        ("/novini/regionalni/2-dni-bez-voda-v-Aitos-3025111", True),
        ("/novini/burgas", False),
        ("/novini/burgas/", False),
        ("/novini/15.09.2026/showall.html", False),
        ("/novini/topics/1979486.html", False),
        ("/novini/videos/169685.html", False),
        ("/novini/videos/Video-ot-burgas-169685", False),
        ("/novini/burgas/3025321", False),
        ("/sport/3025429.html", False),
        ("/obiavi/2412345.html", False),
        ("/obiavi/rabota/Tursi-se-shofor-kategoriya-C-2412399", False),
        ("/novini/burgas/Kratko-123", False),
        ("", False),
    ],
)
def test_article_path_shape(path: str, expected: bool) -> None:
    assert is_article_path(path) is expected


# ------------------------------------------------------------------ article


def test_article_extraction() -> None:
    article = adapter().parse_article(fixture_html("burgas24_article.html"), ARTICLE_URL)

    assert article is not None
    assert article.url == ARTICLE_URL
    assert article.title == "НАП провери търговци на цветя преди 15 септември"
    assert article.published_at == datetime(2026, 9, 15, 11, 51, 25, tzinfo=UTC)
    assert article.section == "Национални новини"
    assert article.summary.startswith("Инспектори на приходната агенция")
    assert article.body.startswith("Инспектори от Главна дирекция")
    assert "1 октомври 2026" in article.body
    assert len(article.body) > 400


def test_article_body_excludes_scripts_comments_and_page_chrome() -> None:
    article = adapter().parse_article(fixture_html("burgas24_article.html"), ARTICLE_URL)

    assert article is not None
    for noise in (
        "pageCounter",
        "Публикувай коментар",
        "Коментарите са на публикуващите",
        "бюлетин",
        "Тестов Автор",
        "Burgas24.bg",
    ):
        assert noise not in article.body


def test_article_date_falls_back_to_the_visible_time_element() -> None:
    html = fixture_html("burgas24_article.html")
    html = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", html, flags=re.S
    )
    html = re.sub(r'<meta property="article:published_time"[^>]*>', "", html)

    article = adapter().parse_article(html, ARTICLE_URL)

    assert article is not None
    assert article.published_at == datetime(2026, 9, 15, 11, 51, 25, tzinfo=UTC)


def test_incomplete_article_is_unusable() -> None:
    """A title with a stub body and a long description is still not an article."""
    assert (
        adapter().parse_article(
            fixture_html("burgas24_article_incomplete.html"),
            "https://www.burgas24.bg/novini/burgas/Katastrofa-na-putya-3025500",
        )
        is None
    )


def test_article_without_a_title_is_unusable() -> None:
    assert (
        adapter().parse_article(
            fixture_html("burgas24_article_no_title.html"),
            "https://www.burgas24.bg/novini/burgas/Bez-zaglavie-3025501",
        )
        is None
    )


def test_article_of_an_empty_document_is_unusable() -> None:
    assert adapter().parse_article("", ARTICLE_URL) is None
    assert adapter().parse_article("<html><body></body></html>", ARTICLE_URL) is None


# ----------------------------------------------------------- configuration


def test_burgas24_source_is_enabled_and_valid() -> None:
    source = get_source(PROJECT_SOURCES_FILE, "burgas24")

    assert source.enabled is True
    assert source.name == "Burgas24"
    assert source.adapter_type == "burgas24_homepage"
    assert source.adapter_type in available_adapters()
    assert str(source.base_url) == HOMEPAGE_URL
    assert [str(section.url) for section in source.sections] == [HOMEPAGE_URL]
    assert source.min_interval_minutes >= 1
    assert source.inclusion_rules.topics
    assert "sport" in source.exclusion_rules.topics
    assert "advertising" in source.exclusion_rules.topics


def test_burgas24_is_listed_in_the_panel_without_its_id(
    tmp_path: Path, settings: Settings
) -> None:
    copy = tmp_path / "sources.yaml"
    shutil.copyfile(PROJECT_SOURCES_FILE, copy)
    service = SourceAdminService(store=SourcesStore(copy), settings=settings)

    listing = service.execute_command(command="sources", args="", user_id=ADMIN_ID)
    labels = [
        button.text for row in listing.keyboard.inline_keyboard for button in row
    ]
    burgas_labels = [label for label in labels if "Burgas24" in label]

    assert len(burgas_labels) == 1
    assert "включён" in burgas_labels[0]
    for technical in ("burgas24_homepage", "burgas24", "adapter"):
        assert technical not in listing.text
    assert "burgas24_homepage" not in burgas_labels[0]
    assert "burgas24" not in burgas_labels[0]
