"""Configuration package: environment settings and source definitions."""

from news_monitor.config.settings import Settings, get_settings
from news_monitor.config.sources import (
    DeterministicRules,
    SourceConfig,
    SourceSection,
    get_source,
    load_enabled_sources,
    load_sources,
)

__all__ = [
    "DeterministicRules",
    "Settings",
    "SourceConfig",
    "SourceSection",
    "get_settings",
    "get_source",
    "load_enabled_sources",
    "load_sources",
]
