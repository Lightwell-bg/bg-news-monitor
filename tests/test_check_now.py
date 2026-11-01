"""Manual «check now»: scheduler trigger, cooldown, access and wiring."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_monitor.config.runtime_store import RUNTIME_FILE_NAME, RuntimeSettingsStore
from news_monitor.config.settings import Settings
from news_monitor.scheduler import PipelineScheduler
from news_monitor.telegram.admin_ui import DENIED_MESSAGE
from news_monitor.telegram.bot import register_admin_commands
from news_monitor.telegram.settings_admin import (
    ACTION_CHECK,
    CHECK_COOLDOWN_SECONDS,
    NO_SOURCES_MESSAGE,
    SettingsAdminService,
    build_callback_data,
    handle_check_command,
    handle_settings_callback,
)
from tests.conftest import ADMIN_ID, STRANGER_ID
from tests.test_sources_admin import FakeCallbackQuery, FakeMessage, FakeUser


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Checker:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.count


def _service(
    settings: Settings, tmp_path: Path, checker: Checker, clock: Clock
) -> SettingsAdminService:
    return SettingsAdminService(
        settings=settings,
        store=RuntimeSettingsStore(tmp_path / RUNTIME_FILE_NAME),
        checker=checker,
        clock=clock,
    )


async def _noop() -> None:
    return None


def test_run_sources_now_touches_only_source_jobs(settings: Settings) -> None:
    async def scenario() -> None:
        scheduler = PipelineScheduler(settings)
        far = datetime.now(UTC) + timedelta(days=1)
        scheduler.scheduler.start()
        try:
            for job_id in ("pipeline:a", "pipeline:b", "publisher:due", "reports:daily"):
                scheduler.scheduler.add_job(
                    _noop, "interval", hours=1, id=job_id, next_run_time=far
                )
            assert scheduler.run_sources_now() == 2
            now = datetime.now(UTC)
            for job_id in ("pipeline:a", "pipeline:b"):
                job = scheduler.scheduler.get_job(job_id)
                assert job.next_run_time <= now + timedelta(seconds=5)
            for job_id in ("publisher:due", "reports:daily"):
                job = scheduler.scheduler.get_job(job_id)
                assert job.next_run_time == far
        finally:
            scheduler.shutdown()

    asyncio.run(scenario())


def test_run_sources_now_returns_zero_when_not_running(settings: Settings) -> None:
    async def scenario() -> None:
        scheduler = PipelineScheduler(settings)
        assert scheduler.run_sources_now() == 0

    asyncio.run(scenario())


def test_cooldown_blocks_second_press_and_allows_after_120s(
    settings: Settings, tmp_path: Path
) -> None:
    checker, clock = Checker(3), Clock()
    service = _service(settings, tmp_path, checker, clock)

    first = service.execute_callback(
        data=build_callback_data(ACTION_CHECK), user_id=ADMIN_ID
    )
    assert "Проверка запущена по 3 источникам" in first.notice
    assert first.text and first.keyboard is not None

    clock.now += 30
    second = service.execute_callback(
        data=build_callback_data(ACTION_CHECK), user_id=ADMIN_ID
    )
    assert "попробуйте через 90 с." in second.notice
    assert checker.calls == 1

    clock.now += CHECK_COOLDOWN_SECONDS
    third = service.execute_callback(
        data=build_callback_data(ACTION_CHECK), user_id=ADMIN_ID
    )
    assert "Проверка запущена" in third.notice
    assert checker.calls == 2


def test_zero_sources_message_and_no_cooldown(settings: Settings, tmp_path: Path) -> None:
    checker, clock = Checker(0), Clock()
    service = _service(settings, tmp_path, checker, clock)
    assert service.execute_check_command(user_id=ADMIN_ID).text == NO_SOURCES_MESSAGE
    assert service.execute_check_command(user_id=ADMIN_ID).text == NO_SOURCES_MESSAGE
    assert checker.calls == 2


def test_check_command_replies_with_message(settings: Settings, tmp_path: Path) -> None:
    service = _service(settings, tmp_path, Checker(2), Clock())
    message = FakeMessage(from_user=FakeUser(ADMIN_ID))
    asyncio.run(handle_check_command(message, service))
    assert "Проверка запущена по 2 источникам" in message.answers[0][0]


@pytest.mark.parametrize("user_id", [STRANGER_ID, 555000111, None])
def test_check_denied_for_non_admins(
    settings: Settings, tmp_path: Path, user_id: int | None
) -> None:
    # a moderator is not an administrator, so is_admin() rejects them like a stranger
    checker = Checker(2)
    service = _service(settings, tmp_path, checker, Clock())
    command = service.execute_check_command(user_id=user_id)
    assert command.text == DENIED_MESSAGE
    callback = service.execute_callback(
        data=build_callback_data(ACTION_CHECK), user_id=user_id
    )
    assert callback.notice == DENIED_MESSAGE and callback.alert
    assert checker.calls == 0


def test_denied_button_press_is_answered(settings: Settings, tmp_path: Path) -> None:
    service = _service(settings, tmp_path, Checker(2), Clock())
    query = FakeCallbackQuery(
        data=build_callback_data(ACTION_CHECK), from_user=FakeUser(STRANGER_ID)
    )
    asyncio.run(handle_settings_callback(query, service))
    assert query.answers[0] == (DENIED_MESSAGE, True)


def test_button_in_settings_keyboard_within_limit(
    settings: Settings, tmp_path: Path
) -> None:
    service = _service(settings, tmp_path, Checker(1), Clock())
    keyboard = service.show_settings().keyboard
    first_row = keyboard.inline_keyboard[0]
    assert first_row[0].text == "🔄 Проверить новости сейчас"
    assert first_row[0].callback_data == "cfg:chk"
    assert len(first_row[0].callback_data.encode()) <= 64


def test_check_command_is_registered(settings: Settings) -> None:
    class Bot:
        def __init__(self) -> None:
            self.commands: list = []

        async def set_my_commands(self, commands, scope=None) -> None:
            self.commands.extend(commands)

    bot = Bot()
    asyncio.run(register_admin_commands(bot, settings))
    check = [c for c in bot.commands if c.command == "check"]
    assert check and check[0].description == "Проверить новости сейчас"
