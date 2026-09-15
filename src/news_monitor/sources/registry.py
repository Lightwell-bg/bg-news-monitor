"""Registry mapping adapter_type values to adapter implementations."""

from __future__ import annotations

from news_monitor.sources.base import SourceAdapter
from news_monitor.sources.bg24 import Bg24HomepageAdapter
from news_monitor.sources.burgas24 import Burgas24HomepageAdapter
from news_monitor.sources.flagman import FlagmanHomepageAdapter

_ADAPTERS: dict[str, type] = {
    Bg24HomepageAdapter.adapter_type: Bg24HomepageAdapter,
    Burgas24HomepageAdapter.adapter_type: Burgas24HomepageAdapter,
    FlagmanHomepageAdapter.adapter_type: FlagmanHomepageAdapter,
}


def get_adapter(adapter_type: str) -> SourceAdapter:
    """Instantiate the adapter registered for adapter_type."""
    try:
        factory = _ADAPTERS[adapter_type]
    except KeyError as exc:
        raise KeyError(f"no adapter registered for {adapter_type!r}") from exc
    return factory()


def available_adapters() -> tuple[str, ...]:
    """Return every registered adapter type."""
    return tuple(sorted(_ADAPTERS))
