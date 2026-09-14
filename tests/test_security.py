"""Static configuration checks and log redaction."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from news_monitor.logging_config import REDACTED, RedactingFilter, redact

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"

SCANNED_DIRS = (ROOT / "src", ROOT / "tests", ROOT / "config")

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("telegram bot token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}")),
    ("openai style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("openrouter key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


# This module intentionally contains synthetic credential shapes to prove that
# the scanner and the log redaction really match them, so it is not scanned.
SELF = Path(__file__).name


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        for pattern in ("*.py", "*.yaml", "*.yml", "*.html"):
            files.extend(directory.rglob(pattern))
    return [
        path
        for path in files
        if "__pycache__" not in path.parts and path.name != SELF
    ]


def test_no_hardcoded_secret_appears_in_the_repository() -> None:
    problems: list[str] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{path.relative_to(ROOT)}: {label}")
    assert problems == []


def test_env_example_contains_no_values_for_secrets() -> None:
    secret_keys = {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADMIN_ID",
        "TELEGRAM_ADMIN_IDS",
        "TELEGRAM_CHANNEL_ID",
        "OPENROUTER_API_KEY",
    }
    seen: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        seen.add(key)
        if key in secret_keys:
            assert value.strip() == "", f"{key} must stay empty in .env.example"
    assert secret_keys <= seen


def test_env_files_are_ignored_by_git() -> None:
    ignored = GITIGNORE.read_text(encoding="utf-8")
    assert ".env" in ignored
    assert "!.env.example" in ignored


def test_settings_module_reads_secrets_only_from_the_environment() -> None:
    source = (ROOT / "src" / "news_monitor" / "config" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert "SecretStr" in source
    assert "env_file" in source


TELEGRAM_TEST_TOKEN = "1234567890:" + "AAHrandomsecretvaluewithenoughlength12"
OPENROUTER_TEST_TOKEN = "sk-or-v1-" + "abcdefghijklmnopqrstuvwxyz012345"


@pytest.mark.parametrize(
    "message",
    [
        f"bot token {TELEGRAM_TEST_TOKEN}",
        f"key {OPENROUTER_TEST_TOKEN}",
        "Authorization: Bearer abcdefghijklmnop",
        "api_key=supersecretvalue",
        "password: hunter2hunter2",
    ],
)
def test_redaction_removes_credentials(message: str) -> None:
    cleaned = redact(message)
    assert REDACTED in cleaned
    for token in (TELEGRAM_TEST_TOKEN, "supersecretvalue", "hunter2hunter2"):
        assert token not in cleaned


def test_redaction_keeps_ordinary_text() -> None:
    assert redact("published item 42 to the channel") == "published item 42 to the channel"


def test_logging_filter_redacts_message_and_arguments(caplog) -> None:
    logger = logging.getLogger("news_monitor.test.redaction")
    logger.addFilter(RedactingFilter())
    test_token = "1234567890:" + "AAHsecretsecretsecretsecret1234567"
    with caplog.at_level(logging.INFO, logger=logger.name):
        for handler_filter in (RedactingFilter(),):
            caplog.handler.addFilter(handler_filter)
        logger.info("calling with token %s", test_token)

    combined = " ".join(record.getMessage() for record in caplog.records)
    assert test_token not in combined
    assert REDACTED in combined


def test_no_source_line_logs_a_credential() -> None:
    """No logging or print call may receive a token, key or password value."""
    accessors = ("bot_token", "openrouter_key", "get_secret_value", "api_key")
    emitters = ("logger.", "logging.", "print(")
    problems: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(emitter in line for emitter in emitters) and any(
                accessor in line for accessor in accessors
            ):
                problems.append(f"{path.relative_to(ROOT)}:{number}")
    assert problems == []


def test_outbound_network_is_blocked_during_tests() -> None:
    """The autouse guard must really refuse a connection to a real host."""
    import socket

    from tests.conftest import NetworkAccessAttempted

    with pytest.raises(NetworkAccessAttempted):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            probe.connect(("openrouter.ai", 443))


async def test_http_fetcher_cannot_reach_a_real_host() -> None:
    """A real fetcher must be stopped by the guard, never leave the machine.

    This is the regression test for a guard that only patched socket.connect:
    on Windows the Proactor event loop bypassed it and the request really went
    out to the live site.
    """
    from news_monitor.sources.fetcher import HttpHtmlFetcher

    from tests.conftest import NetworkAccessAttempted

    fetcher = HttpHtmlFetcher(user_agent="test-agent", timeout_seconds=1)
    try:
        with pytest.raises(NetworkAccessAttempted):
            await fetcher.fetch("https://www.flagman.bg/")
    finally:
        await fetcher.aclose()


async def test_openrouter_client_cannot_reach_a_real_host(source) -> None:
    """The AI client without a fake transport must also be blocked."""
    from news_monitor.ai.client import OpenRouterAssessor
    from news_monitor.sources.base import ArticleContent

    from tests.conftest import NetworkAccessAttempted

    assessor = OpenRouterAssessor(api_key="fake-key", model="test/model", timeout_seconds=1)
    article = ArticleContent(url="https://flagman.bg/statia/1", title="T", body="B")
    try:
        with pytest.raises(NetworkAccessAttempted):
            await assessor.assess(article, source)
    finally:
        await assessor.aclose()
