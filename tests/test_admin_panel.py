"""The inline administration panel: step-by-step input and the settings screen.

Nothing here contacts Telegram or the network: the message and callback objects
are fakes, the sources file is a temporary copy and the runtime settings file
lives in ``tmp_path``. No test reads or writes a real ``.env``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from news_monitor.app import build_application
from news_monitor.config.runtime_store import (
    RUNTIME_FILE_NAME,
    RuntimeSettingsError,
    RuntimeSettingsStore,
    runtime_settings_path,
)
from news_monitor.config.settings import Settings
from news_monitor.config.sources import load_sources
from news_monitor.config.sources_store import SourcesStore
from news_monitor.telegram.admin_ui import CALLBACK_LIMIT, PendingInputStore
from news_monitor.telegram.filters import AdminOnly
from news_monitor.telegram.settings_admin import (
    ACTION_CANCEL as SETTINGS_CANCEL,
)
from news_monitor.telegram.settings_admin import (
    ACTION_OPEN as SETTINGS_OPEN,
)
from news_monitor.telegram.settings_admin import (
    ACTION_THRESHOLD,
    ACTION_THRESHOLD_INPUT,
    SettingsAdminService,
    build_settings_router,
    handle_settings_callback,
    handle_settings_command,
    handle_settings_input,
)
from news_monitor.telegram.settings_admin import (
    build_callback_data as settings_callback,
)
from news_monitor.telegram.sources_admin import (
    ACTION_CANCEL,
    ACTION_INTERVAL_INPUT,
    ACTION_LIST,
    ACTION_OPEN,
    ACTION_SECTION_ADD,
    ACTION_SECTIONS,
    ACTION_TOPIC_ADD,
    ACTION_TOPICS_EX,
    ACTION_TOPICS_IN,
    SourceAdminService,
    build_callback_data,
    handle_admin_input,
)
from tests.conftest import ADMIN_ID, SECOND_ADMIN_ID, STRANGER_ID
from tests.test_sources_admin import FakeCallbackQuery, FakeMessage, FakeUser

FAKE_TOKEN = "42:fake"
NEW_SECTION = "Новини | https://www.flagman.bg/kategoriya/novini/"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def pending() -> PendingInputStore:
    """The single input slot shared by both panels, as wired in production."""
    return PendingInputStore()


@pytest.fixture
def sources(
    sources_file: Path, settings: Settings, pending: PendingInputStore
) -> SourceAdminService:
    return SourceAdminService(
        store=SourcesStore(sources_file), settings=settings, pending=pending
    )


@pytest.fixture
def runtime_file(tmp_path: Path) -> Path:
    return tmp_path / RUNTIME_FILE_NAME


@pytest.fixture
def panel(
    settings: Settings, runtime_file: Path, pending: PendingInputStore
) -> SettingsAdminService:
    return SettingsAdminService(
        settings=settings,
        store=RuntimeSettingsStore(runtime_file),
        pending=pending,
    )


def _press(service, action: str, *parts: object, user=ADMIN_ID):
    return service.execute_callback(
        data=build_callback_data(action, *parts), user_id=user
    )


def _press_settings(service, action: str, *parts: object, user=ADMIN_ID):
    return service.execute_callback(
        data=settings_callback(action, *parts), user_id=user
    )


def _say(service, text: str, user=ADMIN_ID):
    return service.execute_input(text=text, user_id=user)


def _source(sources_file: Path, source_id: str):
    for source in load_sources(sources_file):
        if source.id == source_id:
            return source
    raise AssertionError(f"source {source_id} disappeared")


def _payloads(reply) -> list[str]:
    if reply.keyboard is None:
        return []
    return [item.callback_data for row in reply.keyboard.inline_keyboard for item in row]


def _labels(reply) -> list[str]:
    if reply.keyboard is None:
        return []
    return [item.text for row in reply.keyboard.inline_keyboard for item in row]


# ------------------------------------------------------- step by step input


def test_a_section_is_added_without_typing_a_source_id(
    sources: SourceAdminService, sources_file: Path
) -> None:
    prompt = _press(sources, ACTION_SECTION_ADD, "flagman")
    assert "Название" in prompt.text
    assert sources.has_pending_input(ADMIN_ID) is True

    saved = _say(sources, NEW_SECTION)

    assert [section.name for section in _source(sources_file, "flagman").sections] == [
        "Главная страница",
        "Новини",
    ]
    assert "Новини" in saved.text
    assert sources.has_pending_input(ADMIN_ID) is False


@pytest.mark.parametrize(
    ("action", "attribute", "code"),
    [
        (ACTION_TOPICS_IN, "inclusion_rules", "in"),
        (ACTION_TOPICS_EX, "exclusion_rules", "ex"),
    ],
)
def test_a_topic_is_added_from_the_topic_screen(
    sources: SourceAdminService,
    sources_file: Path,
    action: str,
    attribute: str,
    code: str,
) -> None:
    listing = _press(sources, action, "flagman")
    assert build_callback_data(ACTION_TOPIC_ADD, "flagman", code) in _payloads(listing)

    _press(sources, ACTION_TOPIC_ADD, "flagman", code)
    _say(sources, "новая тема")

    topics = getattr(_source(sources_file, "flagman"), attribute).topics
    assert topics[-1] == "новая тема"


def test_an_exact_interval_is_accepted_as_a_message(
    sources: SourceAdminService, sources_file: Path
) -> None:
    _press(sources, ACTION_INTERVAL_INPUT, "flagman")

    _say(sources, "37")

    assert _source(sources_file, "flagman").min_interval_minutes == 37


@pytest.mark.parametrize("answer", ["0", "abc", "5000", "   "])
def test_an_invalid_interval_message_changes_nothing(
    sources: SourceAdminService, sources_file: Path, answer: str
) -> None:
    _press(sources, ACTION_INTERVAL_INPUT, "flagman")
    before = sources_file.read_bytes()

    reply = _say(sources, answer)

    assert reply.alert is True
    assert sources_file.read_bytes() == before
    # The question stays open so the administrator can simply answer again.
    assert sources.has_pending_input(ADMIN_ID) is True


def test_a_section_without_a_url_keeps_the_question_open(
    sources: SourceAdminService, sources_file: Path
) -> None:
    _press(sources, ACTION_SECTION_ADD, "flagman")
    before = sources_file.read_bytes()

    reply = _say(sources, "Просто название")

    assert reply.alert is True
    assert "Название" in reply.text
    assert sources_file.read_bytes() == before
    assert sources.has_pending_input(ADMIN_ID) is True


def test_a_section_of_a_foreign_host_is_refused_in_the_dialog(
    sources: SourceAdminService, sources_file: Path
) -> None:
    _press(sources, ACTION_SECTION_ADD, "flagman")
    before = sources_file.read_bytes()

    reply = _say(sources, "Чужой | https://evil.example.com/news/")

    assert reply.alert is True
    assert sources_file.read_bytes() == before


def test_an_overlong_message_is_refused(
    sources: SourceAdminService, sources_file: Path
) -> None:
    _press(sources, ACTION_TOPIC_ADD, "flagman", "in")
    before = sources_file.read_bytes()

    reply = _say(sources, "т" * 500)

    assert reply.alert is True
    assert sources_file.read_bytes() == before


def test_the_cancel_button_stops_waiting_for_input(
    sources: SourceAdminService, sources_file: Path
) -> None:
    prompt = _press(sources, ACTION_SECTION_ADD, "flagman")
    assert build_callback_data(ACTION_CANCEL, "flagman") in _payloads(prompt)

    cancelled = _press(sources, ACTION_CANCEL, "flagman")

    assert sources.has_pending_input(ADMIN_ID) is False
    assert "Flagman" in cancelled.text
    before = sources_file.read_bytes()
    assert _say(sources, NEW_SECTION).text == ""
    assert sources_file.read_bytes() == before


def test_another_button_abandons_an_unfinished_question(
    sources: SourceAdminService,
) -> None:
    _press(sources, ACTION_SECTION_ADD, "flagman")

    _press(sources, ACTION_LIST)

    assert sources.has_pending_input(ADMIN_ID) is False


def test_a_message_without_a_question_is_ignored(
    sources: SourceAdminService, sources_file: Path
) -> None:
    before = sources_file.read_bytes()

    reply = _say(sources, "привет")

    assert reply.text == ""
    assert reply.notice == ""
    assert sources_file.read_bytes() == before


def test_a_forgotten_question_expires(
    sources_file: Path, settings: Settings
) -> None:
    now = {"value": 0.0}
    store = PendingInputStore(ttl_seconds=60, clock=lambda: now["value"])
    service = SourceAdminService(
        store=SourcesStore(sources_file), settings=settings, pending=store
    )
    service.execute_callback(
        data=build_callback_data(ACTION_SECTION_ADD, "flagman"), user_id=ADMIN_ID
    )
    assert service.has_pending_input(ADMIN_ID) is True

    now["value"] = 61.0

    assert service.has_pending_input(ADMIN_ID) is False
    assert service.execute_input(text=NEW_SECTION, user_id=ADMIN_ID).text == ""


def test_each_administrator_has_an_own_question(
    sources: SourceAdminService, sources_file: Path
) -> None:
    _press(sources, ACTION_TOPIC_ADD, "flagman", "in")
    _press(sources, ACTION_INTERVAL_INPUT, "flagman", user=SECOND_ADMIN_ID)

    _say(sources, "тема первого администратора")
    _say(sources, "44", user=SECOND_ADMIN_ID)

    source = _source(sources_file, "flagman")
    assert source.inclusion_rules.topics[-1] == "тема первого администратора"
    assert source.min_interval_minutes == 44


@pytest.mark.parametrize("user", [STRANGER_ID, None])
def test_a_stranger_cannot_start_or_answer_a_question(
    sources: SourceAdminService, sources_file: Path, user
) -> None:
    before = sources_file.read_bytes()

    denied = _press(sources, ACTION_SECTION_ADD, "flagman", user=user)
    answered = _say(sources, NEW_SECTION, user=user)

    assert denied.alert is True
    assert sources.has_pending_input(user) is False
    assert "Недостаточно прав" in answered.notice
    assert sources_file.read_bytes() == before


def test_a_stranger_cannot_answer_a_question_opened_by_an_administrator(
    sources: SourceAdminService, sources_file: Path
) -> None:
    _press(sources, ACTION_SECTION_ADD, "flagman")
    before = sources_file.read_bytes()

    reply = _say(sources, NEW_SECTION, user=STRANGER_ID)

    assert reply.alert is True
    assert sources_file.read_bytes() == before
    assert sources.has_pending_input(ADMIN_ID) is True


async def test_the_input_handler_answers_only_when_asked(
    sources: SourceAdminService,
) -> None:
    message = FakeMessage(from_user=FakeUser(ADMIN_ID))

    await handle_admin_input(message, sources)
    assert message.answers == []

    _press(sources, ACTION_TOPIC_ADD, "flagman", "in")
    message.text = "новая тема"
    await handle_admin_input(message, sources)

    assert message.answers


# ------------------------------------------------------------- panel layout


def test_every_panel_button_fits_the_telegram_limit(
    sources: SourceAdminService, panel: SettingsAdminService
) -> None:
    screens = [
        _press(sources, ACTION_LIST),
        _press(sources, ACTION_OPEN, "flagman"),
        _press(sources, ACTION_SECTIONS, "flagman"),
        _press(sources, ACTION_TOPICS_IN, "flagman"),
        _press(sources, ACTION_TOPICS_EX, "flagman"),
        _press(sources, ACTION_SECTION_ADD, "flagman"),
        panel.show_settings(),
    ]

    payloads = [payload for screen in screens for payload in _payloads(screen)]
    assert payloads
    for payload in payloads:
        assert len(payload.encode("utf-8")) <= CALLBACK_LIMIT


def test_the_two_panels_link_to_each_other(
    sources: SourceAdminService, panel: SettingsAdminService
) -> None:
    listing = _press(sources, ACTION_LIST)
    settings_screen = panel.show_settings()

    assert settings_callback(SETTINGS_OPEN) in _payloads(listing)
    assert build_callback_data(ACTION_LIST) in _payloads(settings_screen)


# ---------------------------------------------------------- settings screen


def test_the_settings_screen_shows_the_current_threshold(
    panel: SettingsAdminService,
) -> None:
    reply = panel.execute_command(user_id=ADMIN_ID)

    assert "Порог важности" in reply.text
    assert "70" in reply.text


def test_the_settings_screen_hides_every_other_setting(
    panel: SettingsAdminService, settings: Settings
) -> None:
    reply = panel.execute_command(user_id=ADMIN_ID)

    assert settings.bot_token not in reply.text
    assert settings.openrouter_key not in reply.text
    assert settings.telegram_channel_id not in reply.text
    assert settings.openrouter_model not in reply.text
    assert str(ADMIN_ID) not in reply.text


@pytest.mark.parametrize("user", [STRANGER_ID, None, 0])
def test_a_stranger_sees_no_settings(panel: SettingsAdminService, user) -> None:
    command = panel.execute_command(user_id=user)
    button = _press_settings(panel, SETTINGS_OPEN, user=user)

    assert "Недостаточно прав" in command.text
    assert "70" not in command.text
    assert button.text == ""
    assert button.alert is True


@pytest.mark.parametrize("user", [STRANGER_ID, None])
def test_a_stranger_cannot_change_the_threshold(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path, user
) -> None:
    reply = _press_settings(panel, ACTION_THRESHOLD, 10, user=user)

    assert reply.alert is True
    assert settings.importance_threshold == 70
    assert not runtime_file.exists()


def test_a_button_changes_applies_and_saves_the_threshold(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path
) -> None:
    reply = _press_settings(panel, ACTION_THRESHOLD, 60)

    assert settings.importance_threshold == 60
    assert RuntimeSettingsStore(runtime_file).load() == {"importance_threshold": 60}
    assert "60" in reply.text


def test_the_threshold_is_changed_by_a_message(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path
) -> None:
    prompt = _press_settings(panel, ACTION_THRESHOLD_INPUT)
    assert panel.has_pending_input(ADMIN_ID) is True
    assert settings_callback(SETTINGS_CANCEL) in _payloads(prompt)

    panel.execute_input(text="85", user_id=ADMIN_ID)

    assert settings.importance_threshold == 85
    assert RuntimeSettingsStore(runtime_file).load() == {"importance_threshold": 85}
    assert panel.has_pending_input(ADMIN_ID) is False


@pytest.mark.parametrize("answer", ["101", "-1", "abc", "", "70.5"])
def test_an_invalid_threshold_is_neither_applied_nor_saved(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path, answer: str
) -> None:
    _press_settings(panel, ACTION_THRESHOLD_INPUT)

    reply = panel.execute_input(text=answer, user_id=ADMIN_ID)

    assert reply.alert is True
    assert settings.importance_threshold == 70
    assert not runtime_file.exists()
    assert panel.has_pending_input(ADMIN_ID) is True


def test_an_invalid_threshold_button_is_refused(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path
) -> None:
    reply = _press_settings(panel, ACTION_THRESHOLD, 500)

    assert reply.alert is True
    assert settings.importance_threshold == 70
    assert not runtime_file.exists()


def test_the_settings_cancel_button_stops_waiting(
    panel: SettingsAdminService, settings: Settings
) -> None:
    _press_settings(panel, ACTION_THRESHOLD_INPUT)

    cancelled = _press_settings(panel, SETTINGS_CANCEL)

    assert panel.has_pending_input(ADMIN_ID) is False
    assert "Порог важности" in cancelled.text
    assert panel.execute_input(text="85", user_id=ADMIN_ID).text == ""
    assert settings.importance_threshold == 70


def test_the_step_buttons_stay_inside_the_allowed_range(
    panel: SettingsAdminService, settings: Settings
) -> None:
    settings.importance_threshold = 100
    labels = _labels(panel.show_settings())

    assert "➕ 100" not in labels
    assert "➖ 90" in labels

    settings.importance_threshold = 0
    labels = _labels(panel.show_settings())

    assert "➖ 0" not in labels
    assert "➕ 10" in labels


@pytest.mark.parametrize("data", ["", None, "garbage", "cfg", "cfg:unknown", "src:list"])
def test_a_malformed_settings_callback_changes_nothing(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path, data
) -> None:
    reply = panel.execute_callback(data=data, user_id=ADMIN_ID)

    assert reply.text == ""
    assert reply.alert is True
    assert settings.importance_threshold == 70
    assert not runtime_file.exists()


def test_only_the_panels_pending_kind_is_consumed(
    sources: SourceAdminService, panel: SettingsAdminService, settings: Settings
) -> None:
    """One slot per administrator: the settings question replaces the source one."""
    _press(sources, ACTION_TOPIC_ADD, "flagman", "in")
    _press_settings(panel, ACTION_THRESHOLD_INPUT)

    assert sources.has_pending_input(ADMIN_ID) is False
    assert panel.has_pending_input(ADMIN_ID) is True
    assert _say(sources, "55").text == ""

    panel.execute_input(text="55", user_id=ADMIN_ID)
    assert settings.importance_threshold == 55


# ----------------------------------------------------------------- handlers


async def test_the_settings_handlers_answer_the_administrator(
    panel: SettingsAdminService, settings: Settings
) -> None:
    message = FakeMessage(from_user=FakeUser(ADMIN_ID))
    await handle_settings_command(message, panel)
    assert "Порог важности" in message.answers[0][0]

    callback = FakeCallbackQuery(
        settings_callback(ACTION_THRESHOLD, 40), FakeUser(ADMIN_ID)
    )
    await handle_settings_callback(callback, panel)
    assert callback.message.edits
    assert settings.importance_threshold == 40

    _press_settings(panel, ACTION_THRESHOLD_INPUT)
    typed = FakeMessage(from_user=FakeUser(ADMIN_ID))
    typed.text = "45"
    await handle_settings_input(typed, panel)
    assert settings.importance_threshold == 45


async def test_the_settings_handlers_refuse_a_stranger(
    panel: SettingsAdminService, settings: Settings, runtime_file: Path
) -> None:
    callback = FakeCallbackQuery(
        settings_callback(ACTION_THRESHOLD, 10), FakeUser(STRANGER_ID)
    )

    await handle_settings_callback(callback, panel)

    assert callback.answers[0][1] is True
    assert callback.message.edits == []
    assert settings.importance_threshold == 70
    assert not runtime_file.exists()


async def _filters_pass(handler, event) -> bool:
    for one in handler.filters:
        if not bool(await one.call(event)):
            return False
    return True


def test_the_settings_router_is_built_with_its_handlers(
    panel: SettingsAdminService, settings: Settings
) -> None:
    router = build_settings_router(panel, settings)

    assert router.name == "settings-admin"
    assert len(router.message.handlers) == 3
    assert len(router.callback_query.handlers) == 2
    guarded, fallback, waiting = router.message.handlers
    assert any(isinstance(one.callback, AdminOnly) for one in guarded.filters)
    assert not any(isinstance(one.callback, AdminOnly) for one in fallback.filters)
    assert any(isinstance(one.callback, AdminOnly) for one in waiting.filters)


async def test_the_settings_router_only_reacts_to_its_own_callbacks(
    panel: SettingsAdminService, settings: Settings
) -> None:
    router = build_settings_router(panel, settings)
    guarded, fallback = router.callback_query.handlers

    assert await _filters_pass(
        guarded, FakeCallbackQuery("cfg:open", FakeUser(ADMIN_ID))
    )
    assert not await _filters_pass(
        guarded, FakeCallbackQuery("cfg:open", FakeUser(STRANGER_ID))
    )
    assert await _filters_pass(
        fallback, FakeCallbackQuery("cfg:open", FakeUser(STRANGER_ID))
    )
    for handler in (guarded, fallback):
        for foreign in ("src:list", "news:approve:1"):
            assert not await _filters_pass(
                handler, FakeCallbackQuery(foreign, FakeUser(ADMIN_ID))
            )


def test_the_dispatcher_registers_both_panels_and_moderation(
    sources: SourceAdminService,
    panel: SettingsAdminService,
    settings: Settings,
    repository,
    fake_bot,
) -> None:
    from news_monitor.telegram.bot import create_dispatcher
    from news_monitor.telegram.moderation import ModerationService
    from news_monitor.telegram.publisher import ChannelPublisher

    moderation = ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, "@channel", dry_run=True),
        settings=settings,
    )

    dispatcher = create_dispatcher(moderation, settings, sources, panel)

    assert [router.name for router in dispatcher.sub_routers] == [
        "sources-admin",
        "settings-admin",
        "moderation",
    ]


# ------------------------------------------------------------ runtime store


def test_the_runtime_file_lives_next_to_the_sources_file(
    settings: Settings, tmp_path: Path
) -> None:
    settings.sources_file = str(tmp_path / "sources.yaml")

    assert runtime_settings_path(settings) == tmp_path / RUNTIME_FILE_NAME


def test_the_runtime_store_saves_and_reloads_the_threshold(
    runtime_file: Path,
) -> None:
    store = RuntimeSettingsStore(runtime_file)

    assert store.load() == {}
    assert store.save_importance_threshold("35") == 35
    assert RuntimeSettingsStore(runtime_file).load() == {"importance_threshold": 35}


@pytest.mark.parametrize("value", ["101", "-1", "", "abc", None])
def test_the_runtime_store_refuses_an_invalid_threshold(
    runtime_file: Path, value
) -> None:
    store = RuntimeSettingsStore(runtime_file)

    with pytest.raises(RuntimeSettingsError):
        store.save_importance_threshold(value)
    assert not runtime_file.exists()


def test_the_runtime_store_never_holds_a_secret(
    runtime_file: Path, settings: Settings
) -> None:
    RuntimeSettingsStore(runtime_file).save_importance_threshold(50)

    saved = runtime_file.read_text(encoding="utf-8")

    assert settings.bot_token not in saved
    assert settings.openrouter_key not in saved
    assert saved.count("importance_threshold") == 1
    assert "token" not in saved.casefold()


def test_a_damaged_runtime_file_falls_back_to_the_environment(
    runtime_file: Path, settings: Settings
) -> None:
    runtime_file.write_text("importance_threshold: [broken\n", encoding="utf-8")

    applied = RuntimeSettingsStore(runtime_file).apply_to(settings)

    assert applied == {}
    assert settings.importance_threshold == 70


def test_an_out_of_range_saved_value_is_ignored(
    runtime_file: Path, settings: Settings
) -> None:
    runtime_file.write_text("importance_threshold: 900\n", encoding="utf-8")

    applied = RuntimeSettingsStore(runtime_file).apply_to(settings)

    assert applied == {}
    assert settings.importance_threshold == 70


def test_an_unknown_key_is_never_applied_or_written(
    runtime_file: Path, settings: Settings
) -> None:
    runtime_file.write_text(
        "importance_threshold: 40\ntelegram_bot_token: leaked\n", encoding="utf-8"
    )
    store = RuntimeSettingsStore(runtime_file)

    assert store.apply_to(settings) == {"importance_threshold": 40}
    assert settings.telegram_bot_token.get_secret_value() == "test-token-value"

    store.save_importance_threshold(41)
    assert "telegram_bot_token" not in runtime_file.read_text(encoding="utf-8")


def test_the_runtime_store_leaves_no_temporary_file(
    runtime_file: Path, tmp_path: Path
) -> None:
    RuntimeSettingsStore(runtime_file).save_importance_threshold(42)

    assert [path.name for path in tmp_path.iterdir()] == [RUNTIME_FILE_NAME]


# ------------------------------------------------- applying inside the process


def _app_settings(tmp_path: Path, sources_file: Path) -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token=FAKE_TOKEN,
        telegram_admin_ids=str(ADMIN_ID),
        telegram_channel_id="@channel",
        openrouter_api_key="fake-key",
        openrouter_model="test/model",
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/panel.db",
        sources_file=str(sources_file),
        importance_threshold=70,
        dry_run=True,
    )


async def _close(application) -> None:
    application.scheduler.shutdown()
    await application.fetcher.aclose()
    await application.assessor.aclose()
    await application.bot.session.close()


async def test_a_saved_threshold_is_applied_on_startup(
    tmp_path: Path, sources_file: Path
) -> None:
    (tmp_path / RUNTIME_FILE_NAME).write_text(
        "importance_threshold: 33\n", encoding="utf-8"
    )
    settings = _app_settings(tmp_path, sources_file)

    application = await build_application(settings)
    try:
        assert application.settings.importance_threshold == 33
        assert application.pipelines["flagman"]._settings.importance_threshold == 33
    finally:
        await _close(application)


async def test_a_change_from_the_panel_reaches_the_running_pipeline(
    tmp_path: Path, sources_file: Path
) -> None:
    application = await build_application(_app_settings(tmp_path, sources_file))
    try:
        panel = application.settings_admin
        assert panel is not None

        panel.execute_callback(
            data=settings_callback(ACTION_THRESHOLD, 25), user_id=ADMIN_ID
        )

        assert application.settings.importance_threshold == 25
        assert application.pipelines["flagman"]._settings.importance_threshold == 25
        assert RuntimeSettingsStore(tmp_path / RUNTIME_FILE_NAME).load() == {
            "importance_threshold": 25
        }
    finally:
        await _close(application)
