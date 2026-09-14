"""Source administration from Telegram: authorization, validation, apply.

Nothing here contacts Telegram: the message and callback objects are fakes and
every edit goes to a temporary sources.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from news_monitor.app import build_application
from news_monitor.config.settings import Settings
from news_monitor.config.sources import load_sources
from news_monitor.config.sources_store import SourcesStore
from news_monitor.telegram.filters import AdminOnly
from news_monitor.telegram.sources_admin import (
    ACTION_CANCEL,
    ACTION_ENABLE,
    ACTION_INTERVAL,
    ACTION_INTERVAL_INPUT,
    ACTION_LIST,
    ACTION_OPEN,
    ACTION_SECTION_ADD,
    ACTION_SECTIONS,
    ACTION_TOPIC_ADD,
    ACTION_TOPIC_DELETE,
    ACTION_TOPICS_IN,
    ApplyResult,
    SourceAdminService,
    build_callback_data,
    build_sources_router,
    handle_admin_callback,
    handle_admin_command,
    handle_admin_input,
)
from tests.conftest import ADMIN_ID, SECOND_ADMIN_ID, STRANGER_ID

FAKE_TOKEN = "42:fake"


# --------------------------------------------------------------------- fakes


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeMessage:
    from_user: FakeUser
    answers: list = field(default_factory=list)
    edits: list = field(default_factory=list)

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append((text, kwargs))


@dataclass
class FakeCallbackQuery:
    data: str | None
    from_user: FakeUser
    message: FakeMessage | None = None
    answers: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.message is None:
            self.message = FakeMessage(from_user=self.from_user)

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


# ------------------------------------------------------------------ helpers


@pytest.fixture
def service(sources_file: Path, settings: Settings) -> SourceAdminService:
    return SourceAdminService(store=SourcesStore(sources_file), settings=settings)


def _run(service: SourceAdminService, command: str, args: str = "", user=ADMIN_ID):
    return service.execute_command(command=command, args=args, user_id=user)


def _press(service: SourceAdminService, action: str, *parts: object, user=ADMIN_ID):
    return service.execute_callback(
        data=build_callback_data(action, *parts), user_id=user
    )


def _labels(reply) -> list[str]:
    """Every button caption of a rendered answer."""
    if reply.keyboard is None:
        return []
    return [item.text for row in reply.keyboard.inline_keyboard for item in row]


def _payloads(reply) -> list[str]:
    """Every callback payload of a rendered answer."""
    if reply.keyboard is None:
        return []
    return [item.callback_data for row in reply.keyboard.inline_keyboard for item in row]


def _source(sources_file: Path, source_id: str):
    for source in load_sources(sources_file):
        if source.id == source_id:
            return source
    raise AssertionError(f"source {source_id} disappeared")


# ------------------------------------------------------------ authorization


@pytest.mark.parametrize("user", [STRANGER_ID, None, 0])
def test_a_stranger_sees_nothing(service: SourceAdminService, user) -> None:
    reply = _run(service, "sources", user=user)

    assert "Недостаточно прав" in reply.text
    assert "flagman" not in reply.text


@pytest.mark.parametrize("user", [STRANGER_ID, None])
def test_a_stranger_changes_nothing(
    service: SourceAdminService, sources_file: Path, user
) -> None:
    before = sources_file.read_bytes()

    reply = _run(service, "source_set", "flagman interval 60", user=user)

    assert reply.alert is True
    assert sources_file.read_bytes() == before


def test_a_stranger_cannot_press_a_button(
    service: SourceAdminService, sources_file: Path
) -> None:
    before = sources_file.read_bytes()

    reply = _press(service, ACTION_ENABLE, "bg24", user=STRANGER_ID)

    assert reply.alert is True
    assert reply.text == ""
    assert sources_file.read_bytes() == before


def test_every_configured_administrator_is_accepted(
    service: SourceAdminService,
) -> None:
    assert service.is_authorized(ADMIN_ID) is True
    assert service.is_authorized(SECOND_ADMIN_ID) is True
    assert service.is_authorized(STRANGER_ID) is False
    assert service.is_authorized(None) is False


# ------------------------------------------------------------------ reading


def test_the_list_shows_every_source(service: SourceAdminService) -> None:
    reply = _run(service, "sources")

    labels = _labels(reply)
    assert any("Flagman" in label for label in labels)
    assert any("BG24" in label for label in labels)
    assert any("Настройки" in label for label in labels)
    # One row per source plus the settings row, and no identifier in the text.
    assert len(reply.keyboard.inline_keyboard) == 4


def test_the_panel_never_shows_a_source_id(service: SourceAdminService) -> None:
    """No identifier, adapter name or command syntax reaches the panel text."""
    listing = _run(service, "sources")
    card = _press(service, ACTION_OPEN, "flagman")
    sections = _press(service, ACTION_SECTIONS, "flagman")

    for reply in (listing, card, sections):
        for technical in ("<code>", "/source", "flagman_homepage", "adapter"):
            assert technical not in reply.text
    # The identifiers appear nowhere in the list, and the card of one source
    # never mentions another one.
    for identifier in ("flagman", "bg24", "legacy"):
        assert identifier not in listing.text
    for identifier in ("bg24", "legacy"):
        assert identifier not in card.text


def test_the_card_shows_every_setting(service: SourceAdminService) -> None:
    reply = _run(service, "source", "flagman")

    assert "Flagman" in reply.text
    assert "20" in reply.text
    assert "Главная страница" in reply.text
    assert "migration and residency" in reply.text
    assert "advertising" in reply.text


def test_an_unknown_source_is_reported(service: SourceAdminService) -> None:
    reply = _run(service, "source", "nope")

    assert "не найден" in reply.text
    assert reply.alert is True


def test_a_command_without_arguments_shows_the_usage(
    service: SourceAdminService,
) -> None:
    assert "/source_set" in _run(service, "source").text
    assert "/source_set" in _run(service, "source_set", "flagman").text
    assert "/source_set" in _run(service, "source_set", "flagman unknown 5").text


# ------------------------------------------------------------------ editing


def test_interval_is_changed_by_command(
    service: SourceAdminService, sources_file: Path
) -> None:
    reply = _run(service, "source_set", "flagman interval 45")

    assert "45" in reply.text
    assert _source(sources_file, "flagman").min_interval_minutes == 45


@pytest.mark.parametrize("value", ["0", "-1", "5000", "abc", ""])
def test_an_invalid_interval_is_refused(
    service: SourceAdminService, sources_file: Path, value: str
) -> None:
    before = sources_file.read_bytes()

    reply = _run(service, "source_set", f"flagman interval {value}".strip())

    assert reply.alert is True or "/source_set" in reply.text
    assert sources_file.read_bytes() == before


def test_enabled_is_changed_by_command(
    service: SourceAdminService, sources_file: Path
) -> None:
    _run(service, "source_set", "bg24 enabled on")
    assert _source(sources_file, "bg24").enabled is True

    _run(service, "source_set", "bg24 enabled off")
    assert _source(sources_file, "bg24").enabled is False


def test_a_source_without_an_adapter_cannot_be_enabled(
    service: SourceAdminService, sources_file: Path
) -> None:
    reply = _run(service, "source_set", "legacy enabled on")

    assert "не поддерживается" in reply.text
    assert _source(sources_file, "legacy").enabled is False


def test_a_section_is_added_and_removed(
    service: SourceAdminService, sources_file: Path
) -> None:
    _run(
        service,
        "source_add",
        "flagman section Новини | https://www.flagman.bg/kategoriya/novini/",
    )
    assert len(_source(sources_file, "flagman").sections) == 2

    _run(service, "source_del", "flagman section 2")
    assert [
        section.name for section in _source(sources_file, "flagman").sections
    ] == ["Главная страница"]


def test_a_section_of_a_foreign_host_is_refused(
    service: SourceAdminService, sources_file: Path
) -> None:
    reply = _run(
        service, "source_add", "flagman section Чужой | https://evil.example.com/news/"
    )

    assert reply.alert is True
    assert len(_source(sources_file, "flagman").sections) == 1


def test_the_last_section_is_kept(
    service: SourceAdminService, sources_file: Path
) -> None:
    reply = _run(service, "source_del", "flagman section 1")

    assert "последний" in reply.text
    assert len(_source(sources_file, "flagman").sections) == 1


def test_a_section_without_a_url_shows_the_usage(
    service: SourceAdminService, sources_file: Path
) -> None:
    before = sources_file.read_bytes()

    assert "/source_add" in _run(service, "source_add", "flagman section Новини").text
    assert sources_file.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "attribute"),
    [("topic_in", "inclusion_rules"), ("topic_ex", "exclusion_rules")],
)
def test_a_topic_is_added_and_removed(
    service: SourceAdminService, sources_file: Path, field: str, attribute: str
) -> None:
    _run(service, "source_add", f"flagman {field} новая тема")
    topics = getattr(_source(sources_file, "flagman"), attribute).topics
    assert topics[-1] == "новая тема"

    _run(service, "source_del", f"flagman {field} {len(topics)}")
    assert "новая тема" not in getattr(
        _source(sources_file, "flagman"), attribute
    ).topics


def test_an_empty_topic_is_refused(
    service: SourceAdminService, sources_file: Path
) -> None:
    before = sources_file.read_bytes()

    _run(service, "source_add", "flagman topic_in    ")

    assert sources_file.read_bytes() == before


def test_a_topic_number_out_of_range_is_refused(
    service: SourceAdminService, sources_file: Path
) -> None:
    before = sources_file.read_bytes()

    reply = _run(service, "source_del", "flagman topic_in 99")

    assert reply.alert is True
    assert sources_file.read_bytes() == before


# ---------------------------------------------------------------- callbacks


def test_buttons_open_the_list_and_the_card(service: SourceAdminService) -> None:
    assert "Источники" in _press(service, ACTION_LIST).text
    assert "Flagman" in _press(service, ACTION_OPEN, "flagman").text


def test_the_enable_button_saves_the_change(
    service: SourceAdminService, sources_file: Path
) -> None:
    reply = _press(service, ACTION_ENABLE, "bg24")

    assert _source(sources_file, "bg24").enabled is True
    assert reply.keyboard is not None


def test_the_interval_button_saves_the_change(
    service: SourceAdminService, sources_file: Path
) -> None:
    _press(service, ACTION_INTERVAL, "flagman", 25)

    assert _source(sources_file, "flagman").min_interval_minutes == 25


def test_the_topic_button_deletes_one_topic(
    service: SourceAdminService, sources_file: Path
) -> None:
    view = _press(service, ACTION_TOPICS_IN, "flagman")
    assert "migration and residency" in view.text

    _press(service, ACTION_TOPIC_DELETE, "flagman", "in", 0)

    assert _source(sources_file, "flagman").inclusion_rules.topics == [
        "legislation and administration"
    ]


@pytest.mark.parametrize(
    "data", ["", None, "garbage", "src", "src:open", "src:unknown:flagman", "news:approve:1"]
)
def test_a_malformed_callback_changes_nothing(
    service: SourceAdminService, sources_file: Path, data
) -> None:
    before = sources_file.read_bytes()

    reply = service.execute_callback(data=data, user_id=ADMIN_ID)

    assert reply.text == ""
    assert reply.alert is True
    assert sources_file.read_bytes() == before


def test_a_callback_index_outside_the_list_changes_nothing(
    service: SourceAdminService, sources_file: Path
) -> None:
    before = sources_file.read_bytes()

    reply = _press(service, ACTION_TOPIC_DELETE, "flagman", "in", 42)

    assert reply.alert is True
    assert sources_file.read_bytes() == before


def test_a_long_card_stays_inside_the_telegram_limit(
    service: SourceAdminService,
) -> None:
    for number in range(38):
        _run(service, "source_add", f"flagman topic_in {'тема ' * 20}{number}")

    reply = _run(service, "source", "flagman")

    assert len(reply.text) <= 3901
    assert reply.text.endswith("…")
    assert reply.text.count("<b>") == reply.text.count("</b>")


def test_the_callback_payload_stays_inside_the_telegram_limit() -> None:
    data = build_callback_data(ACTION_TOPIC_DELETE, "flagman", "in", 39)
    assert len(data.encode("utf-8")) <= 64

    with pytest.raises(ValueError):
        build_callback_data(ACTION_OPEN, "x" * 100)


# ------------------------------------------------------------------ handlers


async def test_the_command_handler_answers_the_administrator(
    service: SourceAdminService,
) -> None:
    message = FakeMessage(from_user=FakeUser(ADMIN_ID))

    await handle_admin_command(message, "sources", "", service)

    assert message.answers
    assert "Источники" in message.answers[0][0]


async def test_the_command_handler_refuses_a_stranger(
    service: SourceAdminService, sources_file: Path
) -> None:
    message = FakeMessage(from_user=FakeUser(STRANGER_ID))
    before = sources_file.read_bytes()

    await handle_admin_command(message, "source_set", "flagman interval 99", service)

    assert "Недостаточно прав" in message.answers[0][0]
    assert sources_file.read_bytes() == before


async def test_the_callback_handler_refreshes_the_card(
    service: SourceAdminService, sources_file: Path
) -> None:
    callback = FakeCallbackQuery(
        build_callback_data(ACTION_ENABLE, "bg24"), FakeUser(ADMIN_ID)
    )

    await handle_admin_callback(callback, service)

    assert callback.answers
    assert callback.message.edits
    assert _source(sources_file, "bg24").enabled is True


async def test_the_callback_handler_refuses_a_stranger(
    service: SourceAdminService, sources_file: Path
) -> None:
    callback = FakeCallbackQuery(
        build_callback_data(ACTION_ENABLE, "bg24"), FakeUser(STRANGER_ID)
    )
    before = sources_file.read_bytes()

    await handle_admin_callback(callback, service)

    assert callback.answers[0][1] is True
    assert callback.message.edits == []
    assert sources_file.read_bytes() == before


def test_the_router_is_built_with_both_handlers(
    service: SourceAdminService, settings: Settings
) -> None:
    router = build_sources_router(service, settings)

    assert router.name == "sources-admin"
    # two command handlers plus the step-by-step input handler
    assert len(router.message.handlers) == 3
    assert len(router.callback_query.handlers) == 2


async def _filters_pass(handler, event) -> bool:
    """True when every filter of an aiogram handler accepts the event."""
    for one in handler.filters:
        if not bool(await one.call(event)):
            return False
    return True


async def test_the_router_only_reacts_to_its_own_callbacks(
    service: SourceAdminService, settings: Settings
) -> None:
    """A moderation callback must fall through to the moderation router."""
    router = build_sources_router(service, settings)
    guarded, fallback = router.callback_query.handlers

    assert await _filters_pass(
        guarded, FakeCallbackQuery("src:list", FakeUser(ADMIN_ID))
    )
    assert not await _filters_pass(
        guarded, FakeCallbackQuery("src:list", FakeUser(STRANGER_ID))
    )
    assert await _filters_pass(
        fallback, FakeCallbackQuery("src:list", FakeUser(STRANGER_ID))
    )
    for handler in (guarded, fallback):
        assert not await _filters_pass(
            handler, FakeCallbackQuery("news:approve:1", FakeUser(ADMIN_ID))
        )


def test_only_the_guarded_command_handler_carries_the_admin_filter(
    service: SourceAdminService, settings: Settings
) -> None:
    router = build_sources_router(service, settings)
    guarded, fallback, waiting = router.message.handlers

    assert any(isinstance(one.callback, AdminOnly) for one in guarded.filters)
    assert not any(isinstance(one.callback, AdminOnly) for one in fallback.filters)
    assert any(isinstance(one.callback, AdminOnly) for one in waiting.filters)


def test_the_dispatcher_keeps_the_moderation_router(
    service: SourceAdminService, settings: Settings, repository, fake_bot
) -> None:
    from news_monitor.telegram.bot import create_dispatcher
    from news_monitor.telegram.moderation import ModerationService
    from news_monitor.telegram.publisher import ChannelPublisher

    moderation = ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, "@channel", dry_run=True),
        settings=settings,
    )

    dispatcher = create_dispatcher(moderation, settings, service)

    assert [router.name for router in dispatcher.sub_routers] == [
        "sources-admin",
        "moderation",
    ]


# --------------------------------------------------------------- applying


def test_without_a_runtime_the_administrator_is_told_to_restart(
    sources_file: Path, settings: Settings
) -> None:
    service = SourceAdminService(store=SourcesStore(sources_file), settings=settings)

    reply = service.execute_command(
        command="source_set", args="flagman interval 30", user_id=ADMIN_ID
    )

    assert "перезапуск" in reply.text


def test_a_failing_runtime_does_not_lose_the_saved_change(
    sources_file: Path, settings: Settings
) -> None:
    def broken_applier() -> ApplyResult:
        raise RuntimeError("boom")

    service = SourceAdminService(
        store=SourcesStore(sources_file), settings=settings, applier=broken_applier
    )

    reply = service.execute_command(
        command="source_set", args="flagman interval 30", user_id=ADMIN_ID
    )

    assert "Сохранено" in reply.text
    assert _source(sources_file, "flagman").min_interval_minutes == 30


def test_a_source_needing_a_restart_is_reported(
    sources_file: Path, settings: Settings
) -> None:
    service = SourceAdminService(
        store=SourcesStore(sources_file),
        settings=settings,
        applier=lambda: ApplyResult(restart_required=("flagman",)),
    )

    reply = service.execute_command(
        command="source_set", args="flagman interval 30", user_id=ADMIN_ID
    )

    assert "перезапуск" in reply.text


# --------------------------------------------- applying inside the process


def _app_settings(tmp_path: Path, sources_file: Path) -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token=FAKE_TOKEN,
        telegram_admin_ids=str(ADMIN_ID),
        telegram_channel_id="@channel",
        openrouter_api_key="fake-key",
        openrouter_model="test/model",
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/admin.db",
        sources_file=str(sources_file),
        check_interval_minutes=5,
        dry_run=True,
    )


async def _close(application) -> None:
    application.scheduler.shutdown()
    await application.fetcher.aclose()
    await application.assessor.aclose()
    await application.bot.session.close()


def _job_ids(application) -> list[str]:
    return sorted(job.id for job in application.scheduler.scheduler.get_jobs())


async def test_a_saved_change_is_applied_without_duplicating_jobs(
    tmp_path: Path, sources_file: Path
) -> None:
    application = await build_application(_app_settings(tmp_path, sources_file))
    try:
        assert _job_ids(application) == ["pipeline:flagman"]

        reply = application.source_admin.execute_command(
            command="source_set", args="flagman interval 45", user_id=ADMIN_ID
        )

        assert "применено" in reply.text
        assert _job_ids(application) == ["pipeline:flagman"]
        assert application.pipelines["flagman"].source.min_interval_minutes == 45
        job = application.scheduler.scheduler.get_jobs()[0]
        assert job.trigger.interval == timedelta(minutes=45)
    finally:
        await _close(application)


async def test_enabling_and_disabling_a_source_starts_and_stops_one_job(
    tmp_path: Path, sources_file: Path
) -> None:
    application = await build_application(_app_settings(tmp_path, sources_file))
    try:
        enabled = application.source_admin.execute_command(
            command="source_set", args="bg24 enabled on", user_id=ADMIN_ID
        )
        assert "применено" in enabled.text
        assert _job_ids(application) == ["pipeline:bg24", "pipeline:flagman"]

        application.source_admin.execute_command(
            command="source_set", args="bg24 enabled on", user_id=ADMIN_ID
        )
        assert _job_ids(application) == ["pipeline:bg24", "pipeline:flagman"]

        disabled = application.source_admin.execute_command(
            command="source_set", args="bg24 enabled off", user_id=ADMIN_ID
        )
        assert "остановлен" in disabled.text
        assert _job_ids(application) == ["pipeline:flagman"]
        assert "bg24" not in application.pipelines
    finally:
        await _close(application)


async def test_startup_fails_on_a_missing_sources_file(tmp_path: Path) -> None:
    settings = _app_settings(tmp_path, tmp_path / "absent.yaml")

    with pytest.raises(RuntimeError) as excinfo:
        await build_application(settings)

    assert "sources file" in str(excinfo.value)
    assert FAKE_TOKEN not in str(excinfo.value)


async def test_a_source_without_an_adapter_is_never_scheduled(
    tmp_path: Path, sources_file: Path
) -> None:
    """Even a hand-edited file cannot schedule a source without an adapter."""
    text = sources_file.read_text(encoding="utf-8").replace(
        "    adapter_type: legacy_homepage\n    language: bg\n    enabled: false",
        "    adapter_type: legacy_homepage\n    language: bg\n    enabled: true",
    )
    sources_file.write_text(text, encoding="utf-8")

    application = await build_application(_app_settings(tmp_path, sources_file))
    try:
        assert _job_ids(application) == ["pipeline:flagman"]

        outcome = application.reload_sources()

        assert outcome.restart_required == ("legacy",)
        assert _job_ids(application) == ["pipeline:flagman"]
    finally:
        await _close(application)
