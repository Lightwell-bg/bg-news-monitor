#!/bin/sh
set -eu

# Sources and Telegram-managed runtime settings belong to the persistent data
# volume, not to the checked-out repository. Seed only a new installation.
sources_file="${SOURCES_FILE:-/app/data/sources.yaml}"
if [ ! -f "$sources_file" ]; then
    mkdir -p "$(dirname "$sources_file")"
    cp /app/defaults/sources.yaml "$sources_file"
fi

exec "$@"
