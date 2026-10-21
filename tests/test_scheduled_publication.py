"""Delayed publication: scheduling, the due job, publish-now and cancel."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect

from news_monitor.config.runtime_store import (
    PUBLISH_DELAY_MAX_KEY,
    RuntimeSettingsError,
    RuntimeSettingsStore,
)
from news_monitor.config.settings import Settings
from news_monitor.scheduler import PUBLICATION_JOB_ID, PipelineScheduler
from news_monitor.storage.db import create_engine, init_db
from news_monitor.storage.models import (
    NewsStatus,
    as_utc,
    can_transition,
)
from news_monitor.telegram.card import (
    ACTION_APPROVE,
    ACTION_CANCEL_SCHEDULE,
    ACTION_PUBLISH_NOW,
    build_callback_data,
    parse_callback_data,
)
from news_monitor.telegram.moderation import ActionOutcome, ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.timeutil import format_local_time
from tests.conftest import ADMIN_ID, CHANNEL_ID, STRANGER_ID

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class Clock:
    """A settable clock."""

    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def _awaiting(repository, n: int = 1):
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url=f"https://flagman.bg/statia/{n}-novina",
        title=f"Новина {n}",
    )
    await repository.mark_fetched(item.id, title=f"Новина {n}", body_hash=f"{n:064d}")
    await repository.mark_candidate(
        item.id,
        importance=85,
        ai_reason="Важно",
        title_ru=f"Заголовок {n}",
        draft_ru="Русский черновик новости.",
    )
    return await repository.mark_awaiting_approval(
        item.id, chat_id=ADMIN_ID, message_id=500 + n
    )


def _service(repository, fake_bot, settings, clock=None, seed=1) -> ModerationService:
    return ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, CHANNEL_ID, dry_run=settings.dry_run),
        settings=settings,
        clock=clock or Clock(),
        rng=random.Random(seed),
    )


async def _act(service, action, item_id, user_id=ADMIN_ID):
    return await service.handle_action(action=action, item_id=item_id, user_id=user_id)


# --- state machine -----------------------------------------------------------


def test_scheduled_transitions() -> None:
    assert can_transition(NewsStatus.AWAITING_APPROVAL, NewsStatus.SCHEDULED)
    assert can_transition(NewsStatus.SCHEDULED, NewsStatus.APPROVED)
    assert can_transition(NewsStatus.SCHEDULED, NewsStatus.AWAITING_APPROVAL)
    assert not can_transition(NewsStatus.SCHEDULED, NewsStatus.PUBLISHED)
    assert not can_transition(NewsStatus.SCHEDULED, NewsStatus.REJECTED_BY_ADMIN)
    assert not can_transition(NewsStatus.CANDIDATE, NewsStatus.SCHEDULED)


def test_as_utc_reads_naive_values_as_utc() -> None:
    assert as_utc(None) is None
    naive = datetime(2026, 10, 21, 9, 0)
    assert as_utc(naive) == NOW
    assert as_utc(naive).tzinfo is UTC
    sofia = datetime(2026, 10, 21, 12, 0, tzinfo=timezone_plus_3())
    assert as_utc(sofia) == NOW


def timezone_plus_3():
    from datetime import timezone

    return timezone(timedelta(hours=3))


def test_format_local_time_uses_sofia() -> None:
    # Sofia is UTC+3 on 2026-10-21 (DST ends on the last Sunday of October).
    assert format_local_time(NOW) == "12:00"
    assert format_local_time(datetime(2026, 10, 21, 9, 0)) == "12:00"


# --- migration ---------------------------------------------------------------


async def test_init_db_adds_scheduled_at_to_an_old_database(tmp_path) -> None:
    db_file = tmp_path / "old.db"
    connection = sqlite3.connect(db_file)
    connection.execute(
        "CREATE TABLE news_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source_id VARCHAR(64) NOT NULL, "
        "url TEXT NOT NULL, normalized_url VARCHAR(1024) NOT NULL)"
    )
    connection.execute(
        "INSERT INTO news_items (source_id, url, normalized_url) VALUES ('s', 'u', 'n')"
    )
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    try:
        await init_db(engine)
        await init_db(engine)  # idempotent

        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("news_items")}
            )
        assert "scheduled_at" in columns
    finally:
        await engine.dispose()

    kept = sqlite3.connect(db_file).execute("SELECT COUNT(*) FROM news_items").fetchone()
    assert kept[0] == 1


# --- repository --------------------------------------------------------------


async def test_schedule_and_cancel(repository) -> None:
    item = await _awaiting(repository)
    when = NOW + timedelta(minutes=10)

    assert await repository.schedule(item.id, admin_id=ADMIN_ID, scheduled_at=when)
    assert not await repository.schedule(item.id, admin_id=ADMIN_ID, scheduled_at=when)

    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.SCHEDULED
    assert stored.decided_by == ADMIN_ID
    assert as_utc(stored.scheduled_at) == when
    assert [i.id for i in await repository.list_scheduled()] == [item.id]

    assert await repository.cancel_schedule(item.id)
    assert not await repository.cancel_schedule(item.id)
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL
    assert stored.scheduled_at is None
    assert stored.decided_by is None


async def test_approve_now_from_both_states(repository) -> None:
    first = await _awaiting(repository, 1)
    second = await _awaiting(repository, 2)
    await repository.schedule(second.id, admin_id=ADMIN_ID, scheduled_at=NOW)

    assert await repository.approve_now(first.id, admin_id=ADMIN_ID)
    assert await repository.approve_now(second.id, admin_id=ADMIN_ID)
    assert not await repository.approve_now(first.id, admin_id=ADMIN_ID)
    for item in (first, second):
        assert (await repository.get(item.id)).status_enum is NewsStatus.APPROVED


async def test_claim_due_returns_only_due_items_once(repository) -> None:
    due = await _awaiting(repository, 1)
    later = await _awaiting(repository, 2)
    await repository.schedule(due.id, admin_id=ADMIN_ID, scheduled_at=NOW)
    await repository.schedule(
        later.id, admin_id=ADMIN_ID, scheduled_at=NOW + timedelta(hours=1)
    )

    assert await repository.claim_due(NOW) == [due.id]
    assert await repository.claim_due(NOW) == []
    assert (await repository.get(later.id)).status_enum is NewsStatus.SCHEDULED
    assert await repository.claim_due(NOW + timedelta(hours=2)) == [later.id]


async def test_claim_due_accepts_naive_now_and_orders_by_time(repository) -> None:
    a = await _awaiting(repository, 1)
    b = await _awaiting(repository, 2)
    await repository.schedule(a.id, admin_id=ADMIN_ID, scheduled_at=NOW)
    await repository.schedule(
        b.id, admin_id=ADMIN_ID, scheduled_at=NOW - timedelta(minutes=5)
    )
    assert await repository.claim_due(NOW.replace(tzinfo=None)) == [b.id, a.id]


async def test_claim_due_races_with_approve_now(repository) -> None:
    item = await _awaiting(repository)
    await repository.schedule(item.id, admin_id=ADMIN_ID, scheduled_at=NOW)

    claimed, approved = await asyncio.gather(
        repository.claim_due(NOW),
        repository.approve_now(item.id, admin_id=ADMIN_ID),
    )

    assert (len(claimed) == 1) != approved  # exactly one winner


async def test_concurrent_claimers_never_both_win(repository) -> None:
    item = await _awaiting(repository)
    await repository.schedule(item.id, admin_id=ADMIN_ID, scheduled_at=NOW)

    results = await asyncio.gather(*(repository.claim_due(NOW) for _ in range(4)))

    assert sum(len(r) for r in results) == 1


# --- moderation --------------------------------------------------------------


async def test_approve_schedules_within_the_window(repository, fake_bot, settings) -> None:
    settings.publish_delay_max_minutes = 30
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)

    result = await _act(service, ACTION_APPROVE, item.id)

    assert result.outcome is ActionOutcome.SCHEDULED
    assert result.changed
    assert result.message.startswith("Публикация запланирована на ")
    assert fake_bot.sent == []
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.SCHEDULED
    scheduled_at = as_utc(stored.scheduled_at)
    assert NOW + timedelta(minutes=5) <= scheduled_at <= NOW + timedelta(minutes=30)
    assert format_local_time(scheduled_at) in result.message


async def test_delay_collapses_to_minimum_when_max_is_smaller(
    repository, fake_bot, settings
) -> None:
    settings.publish_delay_max_minutes = 5
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)

    await _act(service, ACTION_APPROVE, item.id)

    stored = await repository.get(item.id)
    assert as_utc(stored.scheduled_at) == NOW + timedelta(minutes=5)


async def test_second_approve_is_already_handled(repository, fake_bot, settings) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)

    await _act(service, ACTION_APPROVE, item.id)
    second = await _act(service, ACTION_APPROVE, item.id)

    assert second.outcome is ActionOutcome.ALREADY_HANDLED


async def test_publish_due_waits_for_the_time(repository, fake_bot, settings) -> None:
    clock = Clock()
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, clock=clock)
    await _act(service, ACTION_APPROVE, item.id)
    scheduled_at = as_utc((await repository.get(item.id)).scheduled_at)

    clock.value = scheduled_at - timedelta(seconds=1)
    assert await service.publish_due() == []
    assert fake_bot.messages_to(CHANNEL_ID) == []

    clock.value = scheduled_at
    results = await service.publish_due()
    assert [r.outcome for r in results] == [ActionOutcome.PUBLISHED]
    assert await service.publish_due() == []
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1
    assert (await repository.get(item.id)).status_enum is NewsStatus.PUBLISHED


async def test_publish_due_drains_a_backlog_in_one_call(
    repository, fake_bot, settings
) -> None:
    clock = Clock()
    service = _service(repository, fake_bot, settings, clock=clock)
    for n in range(1, 26):
        item = await _awaiting(repository, n)
        await _act(service, ACTION_APPROVE, item.id)

    clock.value = NOW + timedelta(days=1)
    results = await service.publish_due()

    assert len(results) == 25
    assert all(r.outcome is ActionOutcome.PUBLISHED for r in results)
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 25
    assert await service.publish_due() == []


async def test_publish_now_from_awaiting_and_scheduled(
    repository, fake_bot, settings
) -> None:
    first = await _awaiting(repository, 1)
    second = await _awaiting(repository, 2)
    service = _service(repository, fake_bot, settings)
    await _act(service, ACTION_APPROVE, second.id)

    r1 = await _act(service, ACTION_PUBLISH_NOW, first.id)
    r2 = await _act(service, ACTION_PUBLISH_NOW, second.id)

    assert r1.outcome is ActionOutcome.PUBLISHED
    assert r2.outcome is ActionOutcome.PUBLISHED
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 2


async def test_second_publish_now_is_already_handled(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)

    await _act(service, ACTION_PUBLISH_NOW, item.id)
    second = await _act(service, ACTION_PUBLISH_NOW, item.id)

    assert second.outcome is ActionOutcome.ALREADY_HANDLED
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1


async def test_due_job_after_publish_now_does_not_republish(
    repository, fake_bot, settings
) -> None:
    clock = Clock()
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, clock=clock)
    await _act(service, ACTION_APPROVE, item.id)
    await _act(service, ACTION_PUBLISH_NOW, item.id)

    clock.value = NOW + timedelta(days=1)
    assert await service.publish_due() == []
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1


async def test_cancel_returns_to_awaiting(repository, fake_bot, settings) -> None:
    clock = Clock()
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, clock=clock)
    await _act(service, ACTION_APPROVE, item.id)

    result = await _act(service, ACTION_CANCEL_SCHEDULE, item.id)

    assert result.outcome is ActionOutcome.UNSCHEDULED
    assert result.changed
    assert result.message == "Публикация отменена, новость снова ждёт решения."
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL
    clock.value = NOW + timedelta(days=1)
    assert await service.publish_due() == []

    again = await _act(service, ACTION_CANCEL_SCHEDULE, item.id)
    assert again.outcome is ActionOutcome.ALREADY_HANDLED


async def test_publication_failure_in_due_job_does_not_stop_the_others(
    repository, fake_bot, settings
) -> None:
    clock = Clock()
    first = await _awaiting(repository, 1)
    second = await _awaiting(repository, 2)
    service = _service(repository, fake_bot, settings, clock=clock)
    await _act(service, ACTION_APPROVE, first.id)
    await _act(service, ACTION_APPROVE, second.id)
    clock.value = NOW + timedelta(days=1)
    fake_bot.fail_next = True

    results = await service.publish_due()

    outcomes = sorted(r.outcome.value for r in results)
    assert outcomes == sorted(
        [ActionOutcome.PUBLICATION_FAILED.value, ActionOutcome.PUBLISHED.value]
    )
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1


@pytest.mark.parametrize("action", [ACTION_APPROVE, ACTION_PUBLISH_NOW])
async def test_stranger_cannot_use_new_actions(
    repository, fake_bot, settings, action
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)

    result = await _act(service, action, item.id, user_id=STRANGER_ID)

    assert result.outcome is ActionOutcome.DENIED
    assert (await repository.get(item.id)).status_enum is NewsStatus.AWAITING_APPROVAL


async def test_stranger_cannot_cancel(repository, fake_bot, settings) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)
    await _act(service, ACTION_APPROVE, item.id)

    result = await _act(service, ACTION_CANCEL_SCHEDULE, item.id, user_id=STRANGER_ID)

    assert result.outcome is ActionOutcome.DENIED
    assert (await repository.get(item.id)).status_enum is NewsStatus.SCHEDULED


def test_new_actions_round_trip_in_callback_data() -> None:
    for action in (ACTION_PUBLISH_NOW, ACTION_CANCEL_SCHEDULE):
        assert parse_callback_data(build_callback_data(action, 7)) == (action, 7)


# --- runtime store and settings ---------------------------------------------


def test_publish_delay_save_and_load(tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")
    assert store.save_publish_delay_max("45") == 45
    assert store.save_importance_threshold(55) == 55
    assert store.load() == {PUBLISH_DELAY_MAX_KEY: 45, "importance_threshold": 55}


@pytest.mark.parametrize("value", ["abc", "", "4", "1441", "-1", "5.5"])
def test_publish_delay_rejects_invalid_values(tmp_path, value) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")
    with pytest.raises(RuntimeSettingsError):
        store.save_publish_delay_max(value)
    assert not (tmp_path / "runtime.yaml").exists()


def test_publish_delay_error_messages(tmp_path) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")
    with pytest.raises(RuntimeSettingsError, match="целым числом"):
        store.save_publish_delay_max("x")
    with pytest.raises(RuntimeSettingsError, match="от 5 до 1440 минут"):
        store.save_publish_delay_max(2)


def test_invalid_saved_delay_is_ignored(tmp_path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text("publish_delay_max_minutes: 2\nimportance_threshold: 40\n", "utf-8")
    assert RuntimeSettingsStore(path).load() == {"importance_threshold": 40}


def test_saved_delay_is_applied_to_settings(tmp_path, settings) -> None:
    store = RuntimeSettingsStore(tmp_path / "runtime.yaml")
    store.save_publish_delay_max(120)
    store.apply_to(settings)
    assert settings.publish_delay_max_minutes == 120


def test_delay_setting_default_and_bounds() -> None:
    base = {"_env_file": None}
    assert Settings(**base).publish_delay_max_minutes == 60
    assert Settings(**base, publish_delay_max_minutes=5).publish_delay_max_minutes == 5
    assert Settings(**base, publish_delay_max_minutes=1440).publish_delay_max_minutes == 1440
    for bad in (4, 1441):
        with pytest.raises(ValueError):
            Settings(**base, publish_delay_max_minutes=bad)


# --- scheduler ---------------------------------------------------------------


async def test_publication_job_is_registered_once(settings) -> None:
    scheduler = PipelineScheduler(settings)

    async def job() -> None:
        return None

    scheduler.add_publication_job(job)
    scheduler.add_publication_job(job, seconds=10)
    jobs = scheduler.scheduler.get_jobs()

    assert [j.id for j in jobs] == [PUBLICATION_JOB_ID] == ["publisher:due"]
    assert jobs[0].max_instances == 1
    assert jobs[0].trigger.interval.total_seconds() == 10
    scheduler.shutdown()
