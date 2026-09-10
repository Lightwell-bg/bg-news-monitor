"""URL normalization and content hashing."""

from __future__ import annotations

import pytest

from news_monitor.normalization.content import content_hash, normalize_text
from news_monitor.normalization.url import normalize_url, same_host


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.flagman.bg/statia/1", "https://flagman.bg/statia/1"),
        ("HTTPS://WWW.Flagman.BG/statia/1/", "https://flagman.bg/statia/1"),
        ("https://flagman.bg/statia/1#comments", "https://flagman.bg/statia/1"),
        ("https://flagman.bg:443/statia/1", "https://flagman.bg/statia/1"),
        (
            "https://flagman.bg/statia/1?utm_source=mail&utm_medium=email",
            "https://flagman.bg/statia/1",
        ),
        (
            "https://flagman.bg/statia/1?fbclid=abc&id=7",
            "https://flagman.bg/statia/1?id=7",
        ),
        (
            "https://flagman.bg/statia/1?b=2&a=1",
            "https://flagman.bg/statia/1?a=1&b=2",
        ),
        ("https://flagman.bg", "https://flagman.bg/"),
    ],
)
def test_normalize_url_variants(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_is_idempotent() -> None:
    once = normalize_url("https://www.flagman.bg/statia/1/?utm_source=x#top")
    assert normalize_url(once) == once


def test_normalize_url_resolves_relative_links() -> None:
    assert (
        normalize_url("/statia/5-novina", base_url="https://www.flagman.bg/")
        == "https://flagman.bg/statia/5-novina"
    )


@pytest.mark.parametrize("raw", ["", "   ", "ftp://flagman.bg/x", "javascript:void(0)"])
def test_normalize_url_rejects_unusable_values(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_same_host_ignores_www_prefix() -> None:
    assert same_host("https://www.flagman.bg/statia/1", "https://flagman.bg/")
    assert not same_host("https://example.com/statia/1", "https://flagman.bg/")


def test_normalize_text_collapses_whitespace() -> None:
    assert normalize_text("  a\n\n b\u00a0c  ") == "a b c"


def test_content_hash_is_stable_for_formatting_differences() -> None:
    first = content_hash("Заглавие", "Текст   на   статията.")
    second = content_hash(" заглавие ", "Текст на статията!")
    assert first == second


def test_content_hash_differs_for_different_articles() -> None:
    assert content_hash("Заглавие A", "Текст A") != content_hash("Заглавие B", "Текст B")


def test_content_hash_rejects_empty_content() -> None:
    with pytest.raises(ValueError):
        content_hash("", "")
