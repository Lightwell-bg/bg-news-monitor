"""Atomic replacement of a configuration file.

A half-written configuration file is worse than an unchanged one, so every
writer in :mod:`news_monitor.config` goes through the same routine: write a
temporary file next to the target, flush it to disk and replace the target in
one step.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically.

    Raises :class:`OSError` when the file could not be written; the caller
    translates it into a message that is safe to show to the administrator.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():  # pragma: no cover - only after a failed replace
            try:
                temporary.unlink()
            except OSError:
                logger.warning("temporary file left behind: %s", temporary.name)
