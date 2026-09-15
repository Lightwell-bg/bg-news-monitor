"""Tests for image-upgrade seeding of an explicitly approved source."""

from __future__ import annotations

from pathlib import Path

from news_monitor.config.seed_sources import seed_source_if_missing
from news_monitor.config.sources import load_sources


def test_seed_adds_burgas24_without_changing_existing_source(
    sources_file: Path, tmp_path: Path
) -> None:
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(
        """sources:
  - id: burgas24
    name: Burgas24
    base_url: https://www.burgas24.bg/
    sections:
      - name: Главная страница
        url: https://www.burgas24.bg/
    adapter_type: burgas24_homepage
    language: bg
    enabled: true
    min_interval_minutes: 30
""",
        encoding="utf-8",
    )

    assert seed_source_if_missing(str(sources_file), str(defaults), "burgas24") is True
    sources = {source.id: source for source in load_sources(sources_file)}
    assert sources["flagman"].min_interval_minutes == 20
    assert sources["burgas24"].enabled is True


def test_seed_is_idempotent(sources_file: Path, tmp_path: Path) -> None:
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(sources_file.read_text(encoding="utf-8"), encoding="utf-8")

    assert seed_source_if_missing(str(sources_file), str(defaults), "flagman") is False
