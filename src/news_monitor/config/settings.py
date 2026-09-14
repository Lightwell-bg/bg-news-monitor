"""Application settings loaded from environment variables.

Secrets never appear in code or logs: values come from the environment only and
``repr``/``str`` of :class:`Settings` masks every sensitive field.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ID_SPLIT_RE = re.compile(r"[,;\s]+")


def _parse_ids(raw: Any) -> tuple[int, ...]:
    """Parse a comma/space separated list of numeric Telegram identifiers."""
    if raw is None:
        return ()
    if isinstance(raw, int):
        return (raw,)
    if isinstance(raw, (list, tuple, set)):
        parts: list[str] = [str(item) for item in raw]
    else:
        parts = [part for part in _ID_SPLIT_RE.split(str(raw)) if part]
    ids: list[int] = []
    for part in parts:
        part = part.strip().strip("\"'")
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:  # pragma: no cover - guarded by validator tests
            raise ValueError("Telegram admin id must be numeric") from exc
        if value not in ids:
            ids.append(value)
    return tuple(ids)


class Settings(BaseSettings):
    """Runtime configuration.

    ``TELEGRAM_ADMIN_IDS`` is the canonical list of administrators.
    ``TELEGRAM_ADMIN_ID`` is kept for single-administrator backwards compatibility.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # A value changed at runtime (the importance threshold) must pass the
        # same field constraints as one loaded from the environment.
        validate_assignment=True,
    )

    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_admin_id: str = Field(default="")
    telegram_admin_ids: str = Field(default="")
    telegram_channel_id: str = Field(default="")

    openrouter_api_key: SecretStr = Field(default=SecretStr(""))
    openrouter_model: str = Field(default="openai/gpt-4o-mini")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    openrouter_timeout_seconds: float = Field(default=60.0, gt=0)

    check_interval_minutes: int = Field(default=20, ge=1)
    importance_threshold: int = Field(default=70, ge=0, le=100)
    database_url: str = Field(default="sqlite+aiosqlite:///./data/news.db")
    dry_run: bool = Field(default=True)

    sources_file: str = Field(default="config/sources.yaml")
    max_articles_per_run: int = Field(default=5, ge=1, le=50)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    user_agent: str = Field(
        default="BGNewsMonitor/0.1 (+moderated news digest; contact via Telegram)"
    )

    @field_validator("telegram_admin_id", "telegram_admin_ids", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @property
    def admin_ids(self) -> tuple[int, ...]:
        """Numeric identifiers allowed to press the moderation buttons."""
        combined = list(_parse_ids(self.telegram_admin_ids))
        for extra in _parse_ids(self.telegram_admin_id):
            if extra not in combined:
                combined.append(extra)
        return tuple(combined)

    def is_admin(self, user_id: int | None) -> bool:
        """Return ``True`` only for configured numeric administrator ids."""
        if user_id is None:
            return False
        return user_id in self.admin_ids

    @property
    def bot_token(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def openrouter_key(self) -> str:
        return self.openrouter_api_key.get_secret_value()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(model={self.openrouter_model!r}, dry_run={self.dry_run}, "
            f"admins={len(self.admin_ids)}, secrets=***)"
        )

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
