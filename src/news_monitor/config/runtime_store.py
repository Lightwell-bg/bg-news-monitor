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

MIN_PUBLISH_DELAY_MINUTES = 5
MAX_PUBLISH_DELAY_MINUTES = 1440

#: The only keys this store reads or writes.
IMPORTANCE_THRESHOLD_KEY = "importance_threshold"
PUBLISH_DELAY_MAX_KEY = "publish_delay_max_minutes"
KNOWN_KEYS: tuple[str, ...] = (IMPORTANCE_THRESHOLD_KEY, PUBLISH_DELAY_MAX_KEY)
#: Administrators who receive the daily report (a list of ids, not an int
#: override, so it is kept out of KNOWN_KEYS and handled by its own methods).
REPORT_RECIPIENTS_KEY = "report_recipient_ids"

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


def parse_publish_delay_max(value: str | int) -> int:
    """Parse and range-check the longest publication delay, in minutes."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise RuntimeSettingsError("Задержка должна быть целым числом.") from None
    if not MIN_PUBLISH_DELAY_MINUTES <= number <= MAX_PUBLISH_DELAY_MINUTES:
        raise RuntimeSettingsError(
            f"Задержка должна быть от {MIN_PUBLISH_DELAY_MINUTES} "
            f"до {MAX_PUBLISH_DELAY_MINUTES} минут."
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
        if PUBLISH_DELAY_MAX_KEY in data:
            try:
                overrides[PUBLISH_DELAY_MAX_KEY] = parse_publish_delay_max(
                    data[PUBLISH_DELAY_MAX_KEY]
                )
            except RuntimeSettingsError:
                logger.warning("saved publish delay is invalid, using default")
        return overrides

    def _load_recipients(self) -> list[int] | None:
        """Saved report recipient ids, or None when nothing valid is saved.

        An explicit empty list is a real value ("send to nobody"), distinct
        from an absent key.
        """
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(data, dict) or REPORT_RECIPIENTS_KEY not in data:
            return None
        raw = data[REPORT_RECIPIENTS_KEY]
        if not isinstance(raw, list):
            logger.warning("saved report recipients are invalid, using default")
            return None
        ids: list[int] = []
        for entry in raw:
            if isinstance(entry, bool) or not isinstance(entry, int):
                logger.warning("saved report recipients are invalid, using default")
                return None
            if entry not in ids:
                ids.append(entry)
        return ids

    def get_report_recipients(self, settings: Settings) -> tuple[int, ...]:
        """Administrators who receive the daily report.

        Default (nothing saved) is the owner, the first administrator. Saved ids
        that are no longer administrators are dropped silently.
        """
        saved = self._load_recipients()
        if saved is None:
            return settings.admin_ids[:1]
        return tuple(user_id for user_id in saved if settings.is_admin(user_id))

    def set_report_recipients(self, settings: Settings, ids: list[int] | tuple[int, ...]) -> tuple[int, ...]:
        """Validate and persist the report recipients; an empty list means nobody."""
        cleaned: list[int] = []
        for user_id in ids:
            if isinstance(user_id, bool) or not isinstance(user_id, int):
                raise RuntimeSettingsError("Получатель должен быть числовым ID.")
            if not settings.is_admin(user_id):
                raise RuntimeSettingsError("Отчёт можно отправлять только администраторам.")
            if user_id not in cleaned:
                cleaned.append(user_id)
        self._write(self.load(), recipients=cleaned)
        return tuple(cleaned)

    def save_importance_threshold(self, value: str | int) -> int:
        """Validate and persist the importance threshold, returning it."""
        number = parse_importance_threshold(value)
        overrides = self.load()
        overrides[IMPORTANCE_THRESHOLD_KEY] = number
        self._write(overrides)
        return number

    def save_publish_delay_max(self, value: str | int) -> int:
        """Validate and persist the longest publication delay, returning it."""
        number = parse_publish_delay_max(value)
        overrides = self.load()
        overrides[PUBLISH_DELAY_MAX_KEY] = number
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

    def _write(
        self, overrides: dict[str, int], recipients: list[int] | None = None
    ) -> None:
        """Write the whole file; saved report recipients survive other changes."""
        document: dict[str, object] = {
            key: overrides[key] for key in KNOWN_KEYS if key in overrides
        }
        if recipients is None:
            recipients = self._load_recipients()
        if recipients is not None:
            document[REPORT_RECIPIENTS_KEY] = list(recipients)
        body = yaml.safe_dump(
            document, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        try:
            atomic_write_text(self._path, _HEADER + body)
        except OSError:
            logger.exception("cannot save the runtime settings file")
            raise RuntimeSettingsError("Не удалось сохранить настройку.") from None
