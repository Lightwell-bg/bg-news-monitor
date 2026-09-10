"""Shared fixtures and fake clients.

No test performs a network call, sends a Telegram message or contacts an AI
provider: every external boundary is replaced by an in-memory fake.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).parent / "fixtures"

from news_monitor.ai.schema import AIAssessment, AIAssessmentError  # noqa: E402
from news_monitor.config.settings import Settings  # noqa: E402
from news_monitor.config.sources import SourceConfig  # noqa: E402
from news_monitor.sources.base import ArticleContent  # noqa: E402
from news_monitor.sources.fetcher import FetchError  # noqa: E402
from news_monitor.storage.db import (  # noqa: E402
    create_engine,
    create_session_factory,
    init_db,
)
from news_monitor.storage.repository import NewsRepository  # noqa: E402

ADMIN_ID = 111222333
SECOND_ADMIN_ID = 444555666
STRANGER_ID = 999888777
CHANNEL_ID = "@test_channel"


def fixture_html(name: str) -> str:
    """Read a local HTML fixture."""
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHtmlFetcher:
    """Serves HTML from a url to fixture mapping and records every call."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = dict(pages or {})
        self.calls: list[str] = []
        self.failures: set[str] = set()

    def add(self, url: str, html: str) -> None:
        self.pages[url] = html

    async def fetch(self, url: str) -> str:
        self.calls.append(url)
        if url in self.failures:
            raise FetchError("fake fetch failure")
        try:
            return self.pages[url]
        except KeyError as exc:
            raise FetchError(f"no fixture registered for {url}") from exc


@dataclass
class SentMessage:
    """One captured Telegram message."""

    chat_id: object
    text: str
    kwargs: dict = field(default_factory=dict)
    message_id: int = 0


class FakeMessage:
    """Return value of the fake send_message call."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """Captures messages instead of contacting Telegram."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.fail_next = False
        self._next_id = 1000

    async def send_message(self, chat_id: object, text: str, **kwargs: object):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("fake telegram failure")
        self._next_id += 1
        self.sent.append(
            SentMessage(
                chat_id=chat_id, text=text, kwargs=dict(kwargs), message_id=self._next_id
            )
        )
        return FakeMessage(self._next_id)

    def messages_to(self, chat_id: object) -> list[SentMessage]:
        return [message for message in self.sent if message.chat_id == chat_id]


class FakeAssessor:
    """Returns a prepared assessment or raises a prepared error."""

    def __init__(
        self,
        assessment: AIAssessment | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.assessment = assessment
        self.error = error
        self.calls: list[str] = []

    async def assess(self, article: ArticleContent, source: SourceConfig) -> AIAssessment:
        self.calls.append(article.url)
        if self.error is not None:
            raise self.error
        if self.assessment is None:
            raise AIAssessmentError("no assessment configured")
        return self.assessment


@pytest.fixture
def settings() -> Settings:
    """Settings built from explicit test values, never from a real .env file."""
    return Settings(
        _env_file=None,
        telegram_bot_token="test-token-value",
        telegram_admin_ids=f"{ADMIN_ID}, {SECOND_ADMIN_ID}",
        telegram_admin_id="",
        telegram_channel_id=CHANNEL_ID,
        openrouter_api_key="test-openrouter-key",
        openrouter_model="test/model",
        importance_threshold=70,
        check_interval_minutes=20,
        dry_run=False,
        max_articles_per_run=5,
    )


@pytest.fixture
def source() -> SourceConfig:
    """Flagman source configuration used by the tests."""
    return SourceConfig(
        id="flagman",
        name="Flagman",
        base_url="https://www.flagman.bg/",
        sections=[{"name": "Главная страница", "url": "https://www.flagman.bg/"}],
        adapter_type="flagman_homepage",
        language="bg",
        enabled=True,
        min_interval_minutes=20,
    )


@pytest_asyncio.fixture
async def repository() -> NewsRepository:
    """A repository backed by a fresh in-memory SQLite database."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    yield NewsRepository(create_session_factory(engine))
    await engine.dispose()


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def valid_assessment() -> AIAssessment:
    """An assessment that passes every deterministic guard."""
    return AIAssessment(
        importance=85,
        reason="Изменение затрагивает документы иностранцев в Болгарии.",
        title_ru="Болгария меняет правила пребывания иностранцев",
        draft_ru=(
            "С 15 октября 2026 года заявления на длительное пребывание принимают "
            "только через электронный портал. Срок рассмотрения сокращается с 30 "
            "до 14 дней. Пошлина остаётся 500 лева за первое разрешение."
        ),
        topics=["legislation and administration"],
    )


_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "", None})


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a real outbound connection."""


def _is_blocked(host: object) -> bool:
    """Return True for any host that is not the local loopback."""
    if host is None:
        return False
    name = str(host)
    return name not in _ALLOWED_HOSTS


def _host_of(address: object) -> object:
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return None


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to reach a real host.

    On Windows the Proactor event loop connects through overlapped I/O and never
    calls socket.connect, so the asyncio entry points have to be guarded as well.
    Loopback stays available because the event loop itself needs it.
    """
    import asyncio
    import socket

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address, *args, **kwargs):
        if _is_blocked(_host_of(address)):
            raise NetworkAccessAttempted(f"blocked connection to {_host_of(address)}")
        return original_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if _is_blocked(_host_of(address)):
            raise NetworkAccessAttempted(f"blocked connection to {_host_of(address)}")
        return original_connect_ex(self, address, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        raise NetworkAccessAttempted(f"blocked connection to {_host_of(address)}")

    def guarded_getaddrinfo(host, *args, **kwargs):
        if _is_blocked(host):
            raise NetworkAccessAttempted(f"blocked dns lookup for {host}")
        return _original_getaddrinfo(host, *args, **kwargs)

    _original_getaddrinfo = socket.getaddrinfo
    _original_loop_create_connection = asyncio.base_events.BaseEventLoop.create_connection
    _original_loop_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def guarded_loop_create_connection(self, protocol_factory, host=None, port=None, **kwargs):
        if _is_blocked(host):
            raise NetworkAccessAttempted(f"blocked connection to {host}")
        return await _original_loop_create_connection(
            self, protocol_factory, host, port, **kwargs
        )

    async def guarded_loop_getaddrinfo(self, host, port, **kwargs):
        if _is_blocked(host):
            raise NetworkAccessAttempted(f"blocked dns lookup for {host}")
        return await _original_loop_getaddrinfo(self, host, port, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop,
        "create_connection",
        guarded_loop_create_connection,
    )
    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop,
        "getaddrinfo",
        guarded_loop_getaddrinfo,
    )
