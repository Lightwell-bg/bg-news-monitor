"""Validated and atomic editing of sources.yaml.

Every test works on a temporary copy: the real config/sources.yaml is never
written by the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from news_monitor.config.sources import load_sources
from news_monitor.config.sources_store import (
    MAX_INTERVAL_MINUTES,
    MAX_SECTIONS,
    MAX_TEXT_LENGTH,
    MAX_TOPICS_PER_KIND,
    SourcesStore,
    SourcesStoreError,
    parse_flag,
    parse_interval,
    parse_position,
)


@pytest.fixture
def store(sources_file: Path) -> SourcesStore:
    return SourcesStore(sources_file)


def _reloaded(sources_file: Path, source_id: str):
    """Read the file again, exactly as the process would after a restart."""
    for source in load_sources(sources_file):
        if source.id == source_id:
            return source
    raise AssertionError(f"source {source_id} disappeared")


def _rejected(sources_file: Path, action) -> str:
    """Run an action that must fail and prove the file did not change."""
    before = sources_file.read_bytes()
    with pytest.raises(SourcesStoreError) as excinfo:
        action()
    assert sources_file.read_bytes() == before
    return str(excinfo.value)


# --------------------------------------------------------------------- read


def test_load_returns_every_source(store: SourcesStore) -> None:
    assert [source.id for source in store.load()] == ["flagman", "bg24", "legacy"]


def test_get_returns_one_source(store: SourcesStore) -> None:
    assert store.get("bg24").min_interval_minutes == 30


def test_unknown_source_is_refused(store: SourcesStore, sources_file: Path) -> None:
    assert "не найден" in _rejected(sources_file, lambda: store.get("nope"))


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SourcesStoreError):
        SourcesStore(tmp_path / "absent.yaml").load()


def test_a_broken_file_is_reported_and_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("sources: [", encoding="utf-8")

    assert "YAML" in _rejected(path, lambda: SourcesStore(path).set_interval("x", 10))


def test_an_invalid_file_is_never_edited(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n  - id: broken\n", encoding="utf-8")

    _rejected(path, lambda: SourcesStore(path).set_enabled("broken", True))


# ------------------------------------------------------------------ enabled


def test_enabled_is_saved_and_survives_a_reload(
    store: SourcesStore, sources_file: Path
) -> None:
    result = store.set_enabled("bg24", True)

    assert result.source.enabled is True
    assert _reloaded(sources_file, "bg24").enabled is True

    store.set_enabled("bg24", False)
    assert _reloaded(sources_file, "bg24").enabled is False


def test_other_sources_are_not_touched(store: SourcesStore, sources_file: Path) -> None:
    store.set_enabled("bg24", True)

    flagman = _reloaded(sources_file, "flagman")
    assert flagman.enabled is True
    assert flagman.min_interval_minutes == 20
    assert [topic for topic in flagman.inclusion_rules.topics] == [
        "migration and residency",
        "legislation and administration",
    ]


# ----------------------------------------------------------------- interval


def test_interval_is_saved(store: SourcesStore, sources_file: Path) -> None:
    store.set_interval("flagman", 45)

    assert _reloaded(sources_file, "flagman").min_interval_minutes == 45


@pytest.mark.parametrize("value", [0, -5, MAX_INTERVAL_MINUTES + 1, "abc", "", "1.5"])
def test_invalid_interval_changes_nothing(
    store: SourcesStore, sources_file: Path, value
) -> None:
    _rejected(sources_file, lambda: store.set_interval("flagman", value))
    assert _reloaded(sources_file, "flagman").min_interval_minutes == 20


# ----------------------------------------------------------------- sections


def test_section_is_added(store: SourcesStore, sources_file: Path) -> None:
    store.add_section("flagman", "Новини", "https://www.flagman.bg/kategoriya/novini/")

    sections = _reloaded(sources_file, "flagman").sections
    assert [section.name for section in sections] == ["Главная страница", "Новини"]
    assert str(sections[1].url).startswith("https://flagman.bg/kategoriya/novini")


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("", "https://www.flagman.bg/novini/"),
        ("   ", "https://www.flagman.bg/novini/"),
        ("x" * (MAX_TEXT_LENGTH + 1), "https://www.flagman.bg/novini/"),
        ("Новини", ""),
        ("Новини", "not-a-url"),
        ("Новини", "ftp://www.flagman.bg/novini/"),
        ("Новини", "javascript:alert(1)"),
        ("Новини", "https://evil.example.com/novini/"),
    ],
)
def test_invalid_section_changes_nothing(
    store: SourcesStore, sources_file: Path, name: str, url: str
) -> None:
    _rejected(sources_file, lambda: store.add_section("flagman", name, url))
    assert len(_reloaded(sources_file, "flagman").sections) == 1


def test_duplicate_section_is_refused(store: SourcesStore, sources_file: Path) -> None:
    store.add_section("flagman", "Новини", "https://www.flagman.bg/kategoriya/novini/")

    _rejected(
        sources_file,
        lambda: store.add_section(
            "flagman", "Другое", "https://www.flagman.bg/kategoriya/novini"
        ),
    )


def test_section_is_removed(store: SourcesStore, sources_file: Path) -> None:
    store.add_section("flagman", "Новини", "https://www.flagman.bg/kategoriya/novini/")
    store.remove_section("flagman", 1)

    assert [section.name for section in _reloaded(sources_file, "flagman").sections] == [
        "Главная страница"
    ]


def test_the_last_section_cannot_be_removed(
    store: SourcesStore, sources_file: Path
) -> None:
    assert "последний" in _rejected(
        sources_file, lambda: store.remove_section("flagman", 0)
    )


def test_section_index_is_checked(store: SourcesStore, sources_file: Path) -> None:
    _rejected(sources_file, lambda: store.remove_section("flagman", 7))


def test_section_limit_is_enforced(store: SourcesStore, sources_file: Path) -> None:
    for number in range(MAX_SECTIONS - 1):
        store.add_section(
            "flagman", f"Раздел {number}", f"https://www.flagman.bg/r{number}/"
        )

    _rejected(
        sources_file,
        lambda: store.add_section("flagman", "Лишний", "https://www.flagman.bg/extra/"),
    )
    assert len(_reloaded(sources_file, "flagman").sections) == MAX_SECTIONS


# ------------------------------------------------------------------- topics


@pytest.mark.parametrize(
    ("kind", "attribute"),
    [("inclusion", "inclusion_rules"), ("exclusion", "exclusion_rules")],
)
def test_topic_is_added_and_removed(
    store: SourcesStore, sources_file: Path, kind: str, attribute: str
) -> None:
    store.add_topic("flagman", kind, "  новая   тема  ")

    topics = getattr(_reloaded(sources_file, "flagman"), attribute).topics
    assert topics[-1] == "новая тема"

    store.remove_topic("flagman", kind, len(topics) - 1)
    assert "новая тема" not in getattr(
        _reloaded(sources_file, "flagman"), attribute
    ).topics


def test_duplicate_topic_is_refused(store: SourcesStore, sources_file: Path) -> None:
    assert "уже есть" in _rejected(
        sources_file,
        lambda: store.add_topic("flagman", "inclusion", "Migration And Residency"),
    )


@pytest.mark.parametrize("topic", ["", "   ", "\n\t", "x" * (MAX_TEXT_LENGTH + 1)])
def test_invalid_topic_changes_nothing(
    store: SourcesStore, sources_file: Path, topic: str
) -> None:
    _rejected(sources_file, lambda: store.add_topic("flagman", "inclusion", topic))
    assert len(_reloaded(sources_file, "flagman").inclusion_rules.topics) == 2


def test_unknown_topic_kind_is_refused(store: SourcesStore, sources_file: Path) -> None:
    _rejected(sources_file, lambda: store.add_topic("flagman", "other", "тема"))


def test_topic_index_is_checked(store: SourcesStore, sources_file: Path) -> None:
    _rejected(sources_file, lambda: store.remove_topic("flagman", "inclusion", 99))


def test_topic_limit_is_enforced(store: SourcesStore, sources_file: Path) -> None:
    existing = len(store.get("flagman").inclusion_rules.topics)
    for number in range(MAX_TOPICS_PER_KIND - existing):
        store.add_topic("flagman", "inclusion", f"тема {number}")

    _rejected(sources_file, lambda: store.add_topic("flagman", "inclusion", "ещё одна"))
    assert (
        len(_reloaded(sources_file, "flagman").inclusion_rules.topics)
        == MAX_TOPICS_PER_KIND
    )


# -------------------------------------------------------------------- write


def test_the_file_stays_readable_and_keeps_its_header(
    store: SourcesStore, sources_file: Path
) -> None:
    store.set_interval("flagman", 25)

    text = sources_file.read_text(encoding="utf-8")
    assert text.startswith("# Тестовый файл источников.")
    assert "# Комментарий должен пережить перезапись." in text
    assert [source.id for source in load_sources(sources_file)] == [
        "flagman",
        "bg24",
        "legacy",
    ]


def test_no_temporary_file_is_left_behind(
    store: SourcesStore, sources_file: Path
) -> None:
    store.set_interval("flagman", 33)

    assert [path.name for path in sources_file.parent.iterdir()] == ["sources.yaml"]


def test_the_store_writes_only_its_own_path(
    store: SourcesStore, sources_file: Path
) -> None:
    other = sources_file.parent / "other.yaml"
    other.write_text("sources: []\n", encoding="utf-8")

    store.set_enabled("flagman", False)

    assert store.path == sources_file
    assert other.read_text(encoding="utf-8") == "sources: []\n"


# ------------------------------------------------------------------ parsers


@pytest.mark.parametrize(("raw", "expected"), [("on", True), ("OFF", False), ("да", True)])
def test_parse_flag(raw: str, expected: bool) -> None:
    assert parse_flag(raw) is expected


@pytest.mark.parametrize("raw", ["maybe", "", "1 0"])
def test_parse_flag_refuses_anything_else(raw: str) -> None:
    with pytest.raises(SourcesStoreError):
        parse_flag(raw)


def test_parse_interval_accepts_the_documented_range() -> None:
    assert parse_interval(" 15 ") == 15
    assert parse_interval(MAX_INTERVAL_MINUTES) == MAX_INTERVAL_MINUTES


def test_parse_position_is_one_based() -> None:
    assert parse_position("1", total=3) == 0
    assert parse_position(3, total=3) == 2
    for raw in ("0", "4", "x", ""):
        with pytest.raises(SourcesStoreError):
            parse_position(raw, total=3)
    with pytest.raises(SourcesStoreError):
        parse_position("1", total=0)
