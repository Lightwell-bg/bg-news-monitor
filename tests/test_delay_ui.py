"""Delayed publication UI: keyboards, cards, handler edits, settings, refresh, wiring."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from news_monitor.app import build_application, register_publication_job
from news_monitor.config.runtime_store import (
    RUNTIME_FILE_NAME,
    RuntimeSettingsStore,
)
from news_monitor.config.settings import Settings
from news_monitor.scheduler import PUBLICATION_JOB_ID
from news_monitor.storage.models import NewsStatus
from news_monitor.telegram.admin_ui import PendingInputStore
from news_monitor.telegram.bot import (
    handle_moderation_callback,
    refresh_cards_after_publication,
)
from news_monitor.telegram.card import (
    ACTION_APPROVE,
    ACTION_CANCEL_SCHEDULE,
    ACTION_PUBLISH_NOW,
    build_callback_data,
    build_keyboard,
    build_scheduled_keyboard,
    parse_callback_data,
    render_admin_card,
)
from news_monitor.telegram.moderation import ActionOutcome, ActionResult, ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.telegram.settings_admin import (
    ACTION_DELAY,
    ACTION_DELAY_INPUT,
    SettingsAdminService,
)
from news_monitor.telegram.settings_admin import build_callback_data as settings_callback
from tests.conftest import ADMIN_ID, CHANNEL_ID
from tests.test_app import _settings as app_settings
from tests.test_bot_handlers import FakeCallbackQuery, FakeUser, _awaiting

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _labels_of(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


def _callbacks_of(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------- keyboards


async def test_awaiting_keyboard_names_the_delay_window(repository) -> None:
    item = await _awaiting(repository)
    markup = build_keyboard(item, delay_max_minutes=90)

    labels = _labels_of(markup)
    assert labels[0] == ["✅ Одобрить (5–90 мин)", "⚡ Сразу"]
    assert labels[1] == ["❌ Отклонить", "🔄 Перегенерировать"]
    assert labels[2] == ["🔗 Открыть источник"]
    assert markup.inline_keyboard[0][0].callback_data == build_callback_data(
        ACTION_APPROVE, item.id
    )
    assert markup.inline_keyboard[0][1].callback_data == build_callback_data(
        ACTION_PUBLISH_NOW, item.id
    )
    assert markup.inline_keyboard[2][0].url == item.url


async def test_awaiting_keyboard_without_a_known_delay_is_generic(repository) -> None:
    item = await _awaiting(repository)
    assert _labels_of(build_keyboard(item))[0][0] == "✅ Одобрить с задержкой"


async def test_scheduled_keyboard_offers_publish_now_and_cancel(repository) -> None:
    item = await _awaiting(repository)
    markup = build_scheduled_keyboard(item)

    assert _labels_of(markup) == [
        ["⚡ Опубликовать сейчас", "↩️ Отменить"],
        ["🔗 Открыть источник"],
    ]
    assert _callbacks_of(markup) == [
        build_callback_data(ACTION_PUBLISH_NOW, item.id),
        build_callback_data(ACTION_CANCEL_SCHEDULE, item.id),
    ]


async def test_callback_data_stays_under_telegram_limit(repository) -> None:
    item = await _awaiting(repository)
    item.id = 2_000_000_000
    for markup in (build_keyboard(item, delay_max_minutes=1440), build_scheduled_keyboard(item)):
        for data in _callbacks_of(markup):
            assert len(data.encode("utf-8")) < 64


def test_new_callbacks_are_parsed() -> None:
    assert parse_callback_data("news:now:7") == (ACTION_PUBLISH_NOW, 7)
    assert parse_callback_data("news:unsched:7") == (ACTION_CANCEL_SCHEDULE, 7)
    assert parse_callback_data("news:unsched:x") is None


# -------------------------------------------------------------- admin card


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 21, 9, 0, tzinfo=UTC), "12:00"),  # summer time, UTC+3
        (datetime(2027, 1, 15, 9, 0, tzinfo=UTC), "11:00"),  # winter time, UTC+2
        (datetime(2026, 3, 29, 0, 30), "02:30"),  # before the spring switch, naive UTC
        (datetime(2026, 3, 29, 1, 30, tzinfo=UTC), "04:30"),  # after the spring switch
    ],
)
async def test_scheduled_card_shows_sofia_time(repository, moment, expected) -> None:
    item = await _awaiting(repository)
    assert await repository.schedule(item.id, admin_id=ADMIN_ID, scheduled_at=moment)
    stored = await repository.get(item.id)

    assert f"⏰ Публикация в {expected} (София)" in render_admin_card(stored)


async def test_awaiting_card_has_no_schedule_line(repository) -> None:
    item = await _awaiting(repository)
    assert "⏰" not in render_admin_card(item)


# ------------------------------------------------------------- bot handler


@dataclass
class EditMessage:
    edits: list = field(default_factory=list)
    markup_cleared: int = 0

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append((text, kwargs))

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.markup_cleared += 1


def _service(repository, fake_bot, settings) -> ModerationService:
    return ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, CHANNEL_ID, dry_run=False),
        settings=settings,
        clock=lambda: NOW,
        rng=random.Random(1),
    )


def _press(action: str, item_id: int) -> FakeCallbackQuery:
    callback = FakeCallbackQuery(build_callback_data(action, item_id), FakeUser(ADMIN_ID))
    callback.message = EditMessage()
    return callback


async def test_approve_edits_the_card_into_a_scheduled_one(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting(repository)
    callback = _press(ACTION_APPROVE, item.id)

    await handle_moderation_callback(callback, _service(repository, fake_bot, settings))

    assert fake_bot.messages_to(CHANNEL_ID) == []
    assert callback.answers and "запланирована" in callback.answers[0][0]
    text, kwargs = callback.message.edits[0]
    assert "⏰ Публикация в" in text
    assert _callbacks_of(kwargs["reply_markup"]) == _callbacks_of(
        build_scheduled_keyboard(item)
    )
    assert callback.message.markup_cleared == 0


async def test_cancel_restores_the_awaiting_card_with_the_current_delay(
    repository, fake_bot, settings
) -> None:
    settings.publish_delay_max_minutes = 120
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)
    await handle_moderation_callback(_press(ACTION_APPROVE, item.id), service)

    callback = _press(ACTION_CANCEL_SCHEDULE, item.id)
    await handle_moderation_callback(callback, service)

    text, kwargs = callback.message.edits[0]
    assert "⏰" not in text
    assert _labels_of(kwargs["reply_markup"])[0] == ["✅ Одобрить (5–120 мин)", "⚡ Сразу"]
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL


async def test_failed_card_edit_does_not_crash_the_handler(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting(repository)
    callback = _press(ACTION_APPROVE, item.id)

    async def boom(text: str, **kwargs: object) -> None:
        raise RuntimeError("telegram is down")

    callback.message.edit_text = boom
    await handle_moderation_callback(callback, _service(repository, fake_bot, settings))

    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.SCHEDULED


# ---------------------------------------------------------- settings screen


@pytest.fixture
def runtime_file(tmp_path: Path) -> Path:
    return tmp_path / RUNTIME_FILE_NAME


@pytest.fixture
def panel(settings: Settings, runtime_file: Path) -> SettingsAdminService:
    return SettingsAdminService(
        settings=settings,
        store=RuntimeSettingsStore(runtime_file),
        pending=PendingInputStore(),
    )


def _delay_press(panel, action: str, *parts: object):
    return panel.execute_callback(data=settings_callback(action, *parts), user_id=ADMIN_ID)


def _reply_labels(reply) -> list[str]:
    return [b.text for row in reply.keyboard.inline_keyboard for b in row]


def test_settings_screen_shows_the_delay(panel) -> None:
    reply = panel.execute_command(user_id=ADMIN_ID)

    assert "Задержка публикации: случайно от 5 до <b>60</b> мин" in reply.text
    assert "«Сразу» публикует без задержки" in reply.text
    labels = _reply_labels(reply)
    assert "⏱ ➖ 30 мин" in labels
    assert "⏱ ➖ 50 мин" in labels
    assert "⏱ ➕ 70 мин" in labels
    assert "⏱ ➕ 90 мин" in labels
    assert "✏️ Ввести задержку" in labels


def test_delay_button_saves_and_applies(panel, settings, runtime_file) -> None:
    reply = _delay_press(panel, ACTION_DELAY, 90)

    assert settings.publish_delay_max_minutes == 90
    assert RuntimeSettingsStore(runtime_file).load()["publish_delay_max_minutes"] == 90
    assert "<b>90</b>" in reply.text


def test_delay_steps_are_clamped(panel, settings) -> None:
    settings.publish_delay_max_minutes = 5
    labels = _reply_labels(panel.show_settings())
    assert not any(label.startswith("⏱ ➖") for label in labels)
    assert "⏱ ➕ 15 мин" in labels

    settings.publish_delay_max_minutes = 1440
    labels = _reply_labels(panel.show_settings())
    assert not any(label.startswith("⏱ ➕") for label in labels)
    assert "⏱ ➖ 1410 мин" in labels

    settings.publish_delay_max_minutes = 20
    labels = _reply_labels(panel.show_settings())
    assert "⏱ ➖ 5 мин" in labels  # 20 - 30 clamps to the minimum


def test_delay_is_changed_by_a_message(panel, settings, runtime_file) -> None:
    prompt = _delay_press(panel, ACTION_DELAY_INPUT)
    assert "Текущая максимальная задержка" in prompt.text
    assert panel.has_pending_input(ADMIN_ID) is True

    panel.execute_input(text="45", user_id=ADMIN_ID)

    assert settings.publish_delay_max_minutes == 45
    assert panel.has_pending_input(ADMIN_ID) is False
    assert settings.importance_threshold == 70


@pytest.mark.parametrize("answer", ["4", "1441", "abc", "", "10.5"])
def test_invalid_delay_is_refused_and_prompt_repeats(
    panel, settings, runtime_file, answer
) -> None:
    _delay_press(panel, ACTION_DELAY_INPUT)

    reply = panel.execute_input(text=answer, user_id=ADMIN_ID)

    assert reply.alert is True
    assert "от 5 до 1440 минут" in reply.text
    assert settings.publish_delay_max_minutes == 60
    assert not runtime_file.exists()
    assert panel.has_pending_input(ADMIN_ID) is True


def test_invalid_delay_button_is_refused(panel, settings, runtime_file) -> None:
    reply = _delay_press(panel, ACTION_DELAY, 5000)

    assert reply.alert is True
    assert settings.publish_delay_max_minutes == 60
    assert not runtime_file.exists()


def test_stranger_cannot_change_the_delay(panel, settings) -> None:
    reply = panel.execute_callback(data=settings_callback(ACTION_DELAY, 90), user_id=1)

    assert reply.alert is True
    assert settings.publish_delay_max_minutes == 60


# ----------------------------------------------------- card refresh after job


class EditBot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def edit_message_text(self, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("message is gone")
        self.calls.append(kwargs)


async def test_refresh_edits_the_admin_cards(repository) -> None:
    item = await _awaiting(repository)
    bot = EditBot()
    results = [
        ActionResult(ActionOutcome.PUBLISHED, "", item),
        ActionResult(ActionOutcome.PUBLISHED_DRY_RUN, "", item),
        ActionResult(ActionOutcome.PUBLICATION_FAILED, "", item),
    ]

    await refresh_cards_after_publication(bot, results)

    assert len(bot.calls) == 3
    first = bot.calls[0]
    assert first["chat_id"] == ADMIN_ID
    assert first["message_id"] == item.admin_message_id
    assert first["reply_markup"] is None
    assert first["disable_web_page_preview"] is True
    assert first["text"].endswith("✅ Опубликовано по расписанию")
    assert bot.calls[1]["text"].endswith("Черновой режим: не отправлено")
    assert bot.calls[2]["text"].endswith("⚠️ Публикация не удалась")


async def test_refresh_skips_items_without_a_card_and_other_outcomes(repository) -> None:
    item = await _awaiting(repository)
    bot = EditBot()
    no_card = await repository.register_discovered(
        source_id="flagman", source_name="Flagman", url="https://flagman.bg/statia/9-x"
    )
    results = [
        ActionResult(ActionOutcome.PUBLISHED, "", no_card[0]),
        ActionResult(ActionOutcome.ALREADY_HANDLED, "", item),
        ActionResult(ActionOutcome.PUBLISHED, "", None),
    ]

    await refresh_cards_after_publication(bot, results)

    assert bot.calls == []


async def test_refresh_swallows_edit_errors(repository) -> None:
    item = await _awaiting(repository)
    results = [ActionResult(ActionOutcome.PUBLISHED, "", item)] * 2

    await refresh_cards_after_publication(EditBot(fail=True), results)


# ------------------------------------------------------------------ wiring


async def test_application_registers_the_publication_job(tmp_path) -> None:
    application = await build_application(app_settings(tmp_path))
    try:
        register_publication_job(application)

        job_ids = [job.id for job in application.scheduler.scheduler.get_jobs()]
        assert PUBLICATION_JOB_ID in job_ids
        assert job_ids.count(PUBLICATION_JOB_ID) == 1
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await application.bot.session.close()


async def test_publication_job_publishes_due_items_and_refreshes_cards(
    tmp_path, monkeypatch
) -> None:
    application = await build_application(app_settings(tmp_path))
    try:
        calls: list[object] = []

        async def fake_publish_due():
            calls.append("due")
            return ["result"]

        async def fake_refresh(bot, results):
            calls.append((bot, results))

        monkeypatch.setattr(application.moderation, "publish_due", fake_publish_due)
        monkeypatch.setattr("news_monitor.app.refresh_cards_after_publication", fake_refresh)
        register_publication_job(application)

        job = application.scheduler.scheduler.get_job(PUBLICATION_JOB_ID)
        await job.func()

        assert calls == ["due", (application.bot, ["result"])]
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await application.bot.session.close()
