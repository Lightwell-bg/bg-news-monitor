"""Operational settings the administrator may change from Telegram.

Only non-secret runtime values live here. The file is written next to
``sources.yaml``, holds a fixed set of known keys and is never used to read,
write or reveal anything from ``.env``: tokens, keys and chat identifiers stay
in the environment alone.

The environment still provides the defaults. A value saved here overrides the
environment default when the process starts, which is what makes a change made
from Telegram survive a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from news_monitor.config.files import atomic_write_text
from news_monitor.config.settings import Settings

logger = logging.getLogger(__name__)

#: The name of the file, always next to the configured sources file.
RUNTIME_FILE_NAME = "runtime.yaml"

MIN_IMPORTANCE_THRESHOLD = 0
MAX_IMPORTANCE_THRESHOLD = 100

#: The only keys this store reads or writes.
IMPORTANCE_THRESHOLD_KEY = "importance_threshold"
KNOWN_KEYS: tuple[str, ...] = (IMPORTANCE_THRESHOLD_KEY,)

_HEADER = (
    "# Настройки, изменяемые администратором из Telegram.\n"
    "# Файл не содержит токенов и ключей; секреты остаются только в окружении.\n"
)


class RuntimeSettingsError(ValueError):
    """A rejected value. The message is safe to show to the administrator."""


def runtime_settings_path(settings: Settings) -> Path:
    """The runtime settings file belonging to the configured sources file."""
    return Path(settings.sources_file).with_name(RUNTIME_FILE_NAME)


def parse_importance_threshold(value: str | int) -> int:
    """Parse and range-check an importance threshold."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise RuntimeSettingsError("Порог должен быть целым числом.") from None
    if not MIN_IMPORTANCE_THRESHOLD <= number <= MAX_IMPORTANCE_THRESHOLD:
        raise RuntimeSettingsError(
            f"Порог должен быть от {MIN_IMPORTANCE_THRESHOLD} "
            f"до {MAX_IMPORTANCE_THRESHOLD}."
        )
    return number


class RuntimeSettingsStore:
    """Reads and writes the single runtime settings file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The only file this store may write."""
        return self._path

    def load(self) -> dict[str, int]:
        """Return the saved overrides, ignoring anything unknown or broken.

        A damaged file must not stop the service: it is reported and treated as
        "no override", so the environment defaults are used instead.
        """
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            logger.warning("runtime settings file is not readable, using defaults")
            return {}
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            logger.warning("runtime settings file is not valid YAML, using defaults")
            return {}
        if not isinstance(data, dict):
            if data is not None:
                logger.warning("runtime settings file has an unexpected structure")
            return {}

        overrides: dict[str, int] = {}
        if IMPORTANCE_THRESHOLD_KEY in data:
            try:
                overrides[IMPORTANCE_THRESHOLD_KEY] = parse_importance_threshold(
                    data[IMPORTANCE_THRESHOLD_KEY]
                )
            except RuntimeSettingsError:
                logger.warning("saved importance threshold is invalid, using default")
        return overrides

    def save_importance_threshold(self, value: str | int) -> int:
        """Validate and persist the importance threshold, returning it."""
        number = parse_importance_threshold(value)
        overrides = self.load()
        overrides[IMPORTANCE_THRESHOLD_KEY] = number
        self._write(overrides)
        return number

    def apply_to(self, settings: Settings) -> dict[str, int]:
        """Apply every saved override to ``settings`` and report what changed."""
        applied: dict[str, int] = {}
        for key, value in self.load().items():
            if key not in KNOWN_KEYS:  # pragma: no cover - load filters already
                continue
            if getattr(settings, key, None) == value:
                applied[key] = value
                continue
            setattr(settings, key, value)
            applied[key] = value
        if applied:
            logger.info("runtime settings applied: %s", ", ".join(sorted(applied)))
        return applied

    def _write(self, overrides: dict[str, int]) -> None:
        document = {key: overrides[key] for key in KNOWN_KEYS if key in overrides}
        body = yaml.safe_dump(
            document, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        try:
            atomic_write_text(self._path, _HEADER + body)
        except OSError:
            logger.exception("cannot save the runtime settings file")
            raise RuntimeSettingsError("Не удалось сохранить настройку.") from None
