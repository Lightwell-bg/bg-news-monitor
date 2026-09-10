"""Loading and validation of ``config/sources.yaml``."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

DEFAULT_EXCLUDED_PATH_PARTS: tuple[str, ...] = (
    "/tag/",
    "/tags/",
    "/avtor/",
    "/author/",
    "/video/",
    "/galeria/",
    "/gallery/",
    "/sport/",
    "/sports/",
    "/horoscope/",
    "/horoskop/",
    "/reklama/",
    "/advertorial/",
    "/search",
)


class SourceSection(BaseModel):
    """A single listing page of a source."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: HttpUrl


class TopicRules(BaseModel):
    """Topic hints passed to the AI prompt (not a deterministic filter)."""

    model_config = ConfigDict(extra="forbid")

    topics: list[str] = Field(default_factory=list)


class DeterministicRules(BaseModel):
    """Code-enforced rules applied before any AI call."""

    model_config = ConfigDict(extra="forbid")

    min_title_length: int = Field(default=15, ge=1)
    min_body_length: int = Field(default=200, ge=1)
    max_age_hours: int = Field(default=48, ge=1)
    excluded_path_parts: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_PATH_PARTS)
    )
    blocked_title_keywords: list[str] = Field(default_factory=list)


class SourceConfig(BaseModel):
    """Configuration of one news source."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    base_url: HttpUrl
    sections: list[SourceSection] = Field(min_length=1)
    adapter_type: str
    language: str = "bg"
    enabled: bool = False
    min_interval_minutes: int = Field(default=20, ge=1)
    inclusion_rules: TopicRules = Field(default_factory=TopicRules)
    exclusion_rules: TopicRules = Field(default_factory=TopicRules)
    filters: DeterministicRules = Field(default_factory=DeterministicRules)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source id must not be empty")
        return value.strip()


class SourcesFile(BaseModel):
    """Root document of ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceConfig] = Field(default_factory=list)


def load_sources(path: str | Path) -> list[SourceConfig]:
    """Load and validate every configured source."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"sources file not found: {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    return SourcesFile.model_validate(raw).sources


def load_enabled_sources(path: str | Path) -> list[SourceConfig]:
    """Load only sources explicitly marked as ``enabled: true``."""
    return [source for source in load_sources(path) if source.enabled]


def get_source(path: str | Path, source_id: str) -> SourceConfig:
    """Return a single source by id."""
    for source in load_sources(path):
        if source.id == source_id:
            return source
    raise KeyError(f"source not configured: {source_id}")
