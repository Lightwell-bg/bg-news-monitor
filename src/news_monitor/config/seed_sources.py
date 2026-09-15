"""Seed explicitly approved bundled sources into a persistent runtime file."""

from __future__ import annotations

import argparse

from news_monitor.config.sources import get_source
from news_monitor.config.sources_store import SourcesStore


def seed_source_if_missing(
    runtime_path: str, defaults_path: str, source_id: str
) -> bool:
    """Copy one bundled source only when its runtime configuration lacks it."""
    source = get_source(defaults_path, source_id)
    return SourcesStore(runtime_path).add_if_missing(source)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_path")
    parser.add_argument("defaults_path")
    parser.add_argument("source_id")
    args = parser.parse_args()
    if seed_source_if_missing(args.runtime_path, args.defaults_path, args.source_id):
        print(f"Seeded source: {args.source_id}")


if __name__ == "__main__":
    main()
