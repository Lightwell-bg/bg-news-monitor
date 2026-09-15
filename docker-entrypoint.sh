#!/bin/sh
set -eu

# Sources and Telegram-managed runtime settings belong to the persistent data
# volume, not to the checked-out repository. Seed only a new installation.
sources_file="${SOURCES_FILE:-/app/data/sources.yaml}"
if [ ! -f "$sources_file" ]; then
    mkdir -p "$(dirname "$sources_file")"
    cp /app/defaults/sources.yaml "$sources_file"
else
    # A persistent file deliberately survives image upgrades.  Add the new
    # Burgas24 source once, while preserving every existing admin setting.
    python -m news_monitor.config.seed_sources \
        "$sources_file" /app/defaults/sources.yaml burgas24
fi

exec "$@"
