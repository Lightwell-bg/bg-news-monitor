"""Daily moderation report: day bounds, text, recipients, scheduling, wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from news_monitor.app import build_application, register_daily_report_job
from news_monitor.config.runtime_store import (
    REPORT_RECIPIENTS_KEY,
    RuntimeSettingsError,
    RuntimeSettingsStore,
)
from news_monitor.config.settings import Settings
from news_monitor.reports import build_daily_report, send_daily_report
from news_monitor.scheduler import DAILY_REPORT_JOB_ID, PipelineScheduler
from news_monitor.storage.models import (
    EVENT_APPROVE,
    EVENT_CANCEL,
    EVENT_PUBLISH_NOW,
    EVENT_REJECT,
)
from news_monitor.telegram.access import AccessControl
from news_monitor.timeutil import LOCAL_TZ, local_day_bounds
from tests.conftest import ADMIN_ID, SECOND_ADMIN_ID, STRANGER_ID, FakeBot
from tests.test_app import _settings as app_settings

DAY = date(2026, 10, 21)
#: 10:15 local (UTC+3 in October) on DAY.
MORNING = datetime(2026, 10, 21, 7, 15, tzinfo=UTC)


async def _item(repository, n: int, title: str = ""):
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url=f"https://flagman.bg/statia/{n}-novina",
        title=f"Original {n}",
    )
    await repository.mark_fetched(item.id, title=f"Original {n}", body_hash=f"{n:064d}")
    return await repository.mark_candidate(
        item.id,
        importance=90,
        ai_reason="Важно",
        title_ru=title or f"Заголовок {n}",
        draft_ru="Черновик.",
    )


# --------------------------------------------------------------- day bounds


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (date(2026, 3, 29), 23),  # spring forward in Europe/Sofia
        (date(2026, 10, 25), 25),  # fall back
        (date(2026, 10, 21), 24),
    ],
)
def test_local_day_bounds_follow_dst(day, hours) -> None:
    start, end = local_day_bounds(day)

    assert end - start == timedelta(hours=hours)
    assert start.tzinfo is UTC or start.utcoffset() == timedelta(0)
    assert start.astimezone(LOCAL_TZ).strftime("%H:%M") == "00:00"
    assert end.astimezone(LOCAL_TZ).strftime("%H:%M") == "00:00"


def test_local_day_bounds_are_utc_instants() -> None:
    start, end = local_day_bounds(date(2026, 3, 29))

    assert start == datetime(2026, 3, 28, 22, 0, tzinfo=UTC)  # UTC+2
    assert end == datetime(2026, 3, 29, 21, 0, tzinfo=UTC)  # UTC+3 after the switch


# ------------------------------------------------------------------- report


async def test_empty_day_reports_no_decisions_and_the_pending_count(repository) -> None:
    item = await _item(repository, 1)
    await repository.mark_awaiting_approval(item.id, chat_id=ADMIN_ID, message_id=1)

    text = await build_daily_report(repository, DAY)

    assert "Отчёт модерации за 21.10.2026" in text
    assert "За день решений не было." in text
    assert text.splitlines()[-1] == "Ожидают решения сейчас: 1"


async def test_report_counts_per_decider_and_lists_items(repository) -> None:
    first = await _item(repository, 1, "Первая новость")
    second = await _item(repository, 2, "Вторая новость")
    third = await _item(repository, 3, "Третья новость")
    await repository.record_event(first.id, 10, "Анна", EVENT_APPROVE, MORNING)
    await repository.record_event(second.id, 10, "Анна", EVENT_REJECT, MORNING + timedelta(minutes=5))
    await repository.record_event(third.id, 10, "Анна", EVENT_CANCEL, MORNING + timedelta(minutes=6))
    await repository.record_event(first.id, ADMIN_ID, "Влад", EVENT_PUBLISH_NOW, MORNING + timedelta(minutes=9))
    # Outside the reported day: must not be counted.
    await repository.record_event(third.id, 11, "Иван", EVENT_APPROVE, MORNING + timedelta(days=1))
    await repository.record_event(third.id, 11, "Иван", EVENT_APPROVE, MORNING - timedelta(days=1))

    text = await build_daily_report(repository, DAY)
    lines = text.splitlines()

    assert "Анна — одобрено 1, отклонено 1, отменено 1" in lines
    assert "Влад — одобрено 1, отклонено 0" in lines
    assert lines.index("Анна — одобрено 1, отклонено 1, отменено 1") < lines.index(
        "Влад — одобрено 1, отклонено 0"
    )
    assert "Иван" not in text
    assert "✅ 10:15 Первая новость — Анна" in lines
    assert "❌ 10:20 Вторая новость — Анна" in lines
    assert "↩️ 10:21 Третья новость — Анна" in lines
    assert "⚡ 10:24 Первая новость — Влад" in lines
    assert lines[-1] == "Ожидают решения сейчас: 0"


async def test_report_without_a_name_falls_back_to_the_id(repository) -> None:
    item = await _item(repository, 1)
    await repository.record_event(item.id, 555, "", EVENT_REJECT, MORNING)

    text = await build_daily_report(repository, DAY)

    assert "ID 555 — одобрено 0, отклонено 1" in text
    assert "— ID 555" in text


async def test_report_escapes_titles_and_names(repository) -> None:
    item = await _item(repository, 1, "<b>&")
    await repository.record_event(item.id, 10, "<i>Анна</i>", EVENT_APPROVE, MORNING)

    text = await build_daily_report(repository, DAY)

    assert "&lt;b&gt;&amp;" in text
    assert "<b>&" not in text
    assert "&lt;i&gt;Анна&lt;/i&gt;" in text
    assert "<i>" not in text


async def test_report_shortens_long_titles(repository) -> None:
    item = await _item(repository, 1, "Д" * 300)
    await repository.record_event(item.id, 10, "Анна", EVENT_APPROVE, MORNING)

    text = await build_daily_report(repository, DAY)

    assert "Д" * 300 not in text
    assert "Д" * 79 + "…" in text


async def test_report_is_clipped_by_whole_lines_and_counts_the_rest(repository) -> None:
    item = await _item(repository, 1, "Д" * 200)
    for minute in range(45):
        await repository.record_event(
            item.id, 10 + minute, f"Участник {minute}", EVENT_APPROVE, MORNING + timedelta(minutes=minute)
        )

    text = await build_daily_report(repository, DAY)

    assert len(text) < 3900
    assert "…и ещё" in text
    assert text.splitlines()[-1].startswith("Ожидают решения сейчас:")


async def test_report_lists_at_most_thirty_items(repository) -> None:
    item = await _item(repository, 1, "Новость")
    for minute in range(40):
        await repository.record_event(
            item.id, 10, "Анна", EVENT_APPROVE, MORNING + timedelta(minutes=minute)
        )

    text = await build_daily_report(repository, DAY)

    assert sum(1 for line in text.splitlines() if line.startswith("✅ ")) == 30
    assert "…и ещё 10" in text.splitlines()


# ---------------------------------------------------------------- delivery


async def test_send_daily_report_survives_a_failing_recipient(repository) -> None:
    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if chat_id == ADMIN_ID:
                raise RuntimeError("blocked")
            return await super().send_message(chat_id, text, **kwargs)

    bot = FlakyBot()

    delivered = await send_daily_report(bot, repository, [ADMIN_ID, SECOND_ADMIN_ID], DAY)

    assert delivered == 1
    assert [m.chat_id for m in bot.sent] == [SECOND_ADMIN_ID]
    assert bot.sent[0].kwargs["parse_mode"] == "HTML"
    assert "Отчёт модерации" in bot.sent[0].text


async def test_send_daily_report_never_raises_on_a_build_error(repository, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(repository, "list_events", boom)
    bot = FakeBot()

    assert await send_daily_report(bot, repository, [ADMIN_ID], DAY) == 0
    assert bot.sent == []


# --------------------------------------------------------------- recipients


def test_recipients_default_to_the_owner(settings, tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")

    assert store.get_report_recipients(settings) == (ADMIN_ID,)


def test_recipients_are_saved_and_filtered_by_current_admins(settings, tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")

    assert store.set_report_recipients(settings, [SECOND_ADMIN_ID, ADMIN_ID]) == (
        SECOND_ADMIN_ID,
        ADMIN_ID,
    )
    assert store.get_report_recipients(settings) == (SECOND_ADMIN_ID, ADMIN_ID)

    only_first = Settings(
        _env_file=None, telegram_admin_ids=str(ADMIN_ID), telegram_bot_token="t"
    )
    assert store.get_report_recipients(only_first) == (ADMIN_ID,)


def test_an_explicit_empty_list_means_nobody(settings, tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")

    store.set_report_recipients(settings, [])

    assert store.get_report_recipients(settings) == ()


def test_only_administrators_can_be_recipients(settings, tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")

    with pytest.raises(RuntimeSettingsError):
        store.set_report_recipients(settings, [ADMIN_ID, STRANGER_ID])
    assert not (tmp_path / "runtime.yaml").exists()


def test_recipients_and_other_settings_survive_each_others_writes(settings, tmp_path) -> None:
    path = tmp_path / "runtime.yaml"
    store = RuntimeSettingsStore(path)

    store.set_report_recipients(settings, [SECOND_ADMIN_ID])
    store.save_importance_threshold(55)
    assert store.load() == {"importance_threshold": 55}
    assert store.get_report_recipients(settings) == (SECOND_ADMIN_ID,)

    store.set_report_recipients(settings, [ADMIN_ID])
    assert store.load() == {"importance_threshold": 55}
    assert REPORT_RECIPIENTS_KEY in path.read_text(encoding="utf-8")


def test_apply_to_ignores_the_recipients_key(settings, tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")
    store.set_report_recipients(settings, [ADMIN_ID])
    store.save_publish_delay_max(30)

    assert store.apply_to(settings) == {"publish_delay_max_minutes": 30}


def test_runtime_file_holds_no_secrets(settings, tmp_path) -> None:
    path = tmp_path / "runtime.yaml"
    RuntimeSettingsStore(path).set_report_recipients(settings, [ADMIN_ID])

    body = path.read_text(encoding="utf-8")
    assert "test-token-value" not in body and "test-openrouter-key" not in body


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(("raw", "expected"), [("09:00", "09:00"), ("7:05", "07:05"), (" 23:59 ", "23:59")])
def test_report_time_is_normalised(raw, expected) -> None:
    assert Settings(_env_file=None, daily_report_time=raw).daily_report_time == expected


@pytest.mark.parametrize("raw", ["", "9", "24:00", "12:60", "ab:cd", "09:00:00"])
def test_invalid_report_time_is_rejected(raw) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, daily_report_time=raw)


def test_report_time_defaults_to_nine() -> None:
    assert Settings(_env_file=None).daily_report_time == "09:00"


# ----------------------------------------------------- scheduler and wiring


def test_scheduler_registers_a_cron_job_in_sofia_time(settings) -> None:
    scheduler = PipelineScheduler(settings)

    async def noop() -> None:
        return None

    scheduler.add_daily_report_job(noop, "09:30")
    scheduler.add_daily_report_job(noop, "10:45")  # re-registering replaces

    jobs = [j for j in scheduler.scheduler.get_jobs() if j.id == DAILY_REPORT_JOB_ID]
    assert len(jobs) == 1
    job = jobs[0]
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "10" and fields["minute"] == "45"
    assert str(job.trigger.timezone) == "Europe/Sofia"
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time == 3600


async def test_application_wires_access_and_the_daily_report_job(tmp_path, monkeypatch) -> None:
    application = await build_application(app_settings(tmp_path, daily_report_time="08:15"))
    try:
        assert isinstance(application.access, AccessControl)
        assert application.moderation.access is application.access
        assert application.access.owner_id == ADMIN_ID

        register_daily_report_job(application)
        job = application.scheduler.scheduler.get_job(DAILY_REPORT_JOB_ID)
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("8", "15")

        sent: list[tuple[object, list[int], date]] = []

        async def fake_send(bot, repository, recipients, day):
            sent.append((bot, list(recipients), day))
            return 1

        monkeypatch.setattr("news_monitor.app.send_daily_report", fake_send)
        await job.func()

        assert len(sent) == 1
        assert sent[0][1] == [ADMIN_ID]
        assert sent[0][2] == datetime.now(LOCAL_TZ).date() - timedelta(days=1)
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await application.bot.session.close()


async def test_daily_report_job_sends_nothing_when_nobody_is_selected(
    tmp_path, monkeypatch
) -> None:
    # A private copy of the sources file keeps runtime.yaml inside tmp_path.
    sources = tmp_path / "sources.yaml"
    sources.write_text(Path("config/sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    settings = app_settings(tmp_path, sources_file=str(sources))
    application = await build_application(settings)
    try:
        application.runtime_store.set_report_recipients(settings, [])
        assert application.runtime_store.path.parent == tmp_path
        called: list[object] = []

        async def fake_send(*args, **kwargs):
            called.append(args)
            return 0

        monkeypatch.setattr("news_monitor.app.send_daily_report", fake_send)
        register_daily_report_job(application)
        await application.scheduler.scheduler.get_job(DAILY_REPORT_JOB_ID).func()

        assert called == []
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await application.bot.session.close()
