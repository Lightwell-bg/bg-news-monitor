"""Logging setup with mandatory redaction of credentials.

Tokens, API keys and authorization headers must never reach a log file, so a
filter rewrites them even if a future call site passes them by mistake.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Telegram bot token: numeric id, colon, secret part.
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),
    # OpenRouter and OpenAI style keys.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bor-[A-Za-z0-9_\-]{16,}"),
    # Authorization headers and bearer tokens.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\bAuthorization\s*[:=]\s*\S+"),
    # Explicit key or token assignments.
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)


def redact(text: str) -> str:
    """Replace every credential like fragment with a placeholder."""
    if not text:
        return text
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


class RedactingFilter(logging.Filter):
    """Applies redaction to the formatted message and to its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure the root logger with the redacting filter attached."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
