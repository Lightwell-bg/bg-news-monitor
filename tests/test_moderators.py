"""Moderator role: storage, access control, restricted actions, card fan-out."""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect

from news_monitor.storage.db import create_engine, init_db
from news_monitor.storage.models import (
    EVENT_APPROVE,
    EVENT_CANCEL,
    EVENT_PUBLISH_NOW,
    EVENT_REJECT,
    ModerationEvent,
    NewsStatus,
)
from news_monitor.telegram.access import MAX_MODERATORS, AccessControl, AddModeratorResult
from news_monitor.telegram.bot import (
    handle_moderation_callback,
    refresh_cards_after_publication,
    refresh_item_cards,
)
from news_monitor.telegram.card import (
    ACTION_APPROVE,
    ACTION_CANCEL_SCHEDULE,
    ACTION_PUBLISH_NOW,
    ACTION_REGENERATE,
    ACTION_REJECT,
    build_callback_data,
    build_keyboard,
    build_moderator_keyboard,
    build_scheduled_keyboard,
    render_decision_line,
)
from news_monitor.telegram.filters import CanModerate
from news_monitor.telegram.moderation import ActionOutcome, ActionResult, ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.telegram.sender import AdminCardSender
from tests.conftest import (
    ADMIN_ID,
    CHANNEL_ID,
    SECOND_ADMIN_ID,
    STRANGER_ID,
    FakeBot,
)
from tests.test_bot_handlers import FakeCallbackQuery, FakeUser

MODERATOR_ID = 700100200
SECOND_MODERATOR_ID = 700100300
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


async def _candidate(repository, n: int = 1):
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url=f"https://flagman.bg/statia/{n}-novina",
        title=f"Новина {n}",
    )
    await repository.mark_fetched(item.id, title=f"Новина {n}", body_hash=f"{n:064d}")
    return await repository.mark_candidate(
        item.id,
        importance=90,
        ai_reason="Важно",
        title_ru=f"Заголовок {n}",
        draft_ru="Русский черновик новости.",
    )


async def _awaiting(repository, n: int = 1):
    item = await _candidate(repository, n)
    return await repository.mark_awaiting_approval(item.id, chat_id=ADMIN_ID, message_id=n)


@pytest.fixture
def access(settings, repository) -> AccessControl:
    control = AccessControl(settings, repository)
    control._moderators.add(MODERATOR_ID)  # as if loaded from the database
    return control


def _service(repository, fake_bot, settings, access) -> ModerationService:
    return ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, CHANNEL_ID, dry_run=False),
        settings=settings,
        access=access,
        clock=lambda: NOW,
        rng=random.Random(1),
    )


class EditBot(FakeBot):
    """FakeBot that also records card edits and can fail for chosen chats."""

    def __init__(self, fail_chats: set[int] | None = None, fail_send: set[int] | None = None):
        super().__init__()
        self.edits: list[dict] = []
        self.fail_chats = fail_chats or set()
        self.fail_send = fail_send or set()

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_send:
            raise RuntimeError("chat unreachable")
        return await super().send_message(chat_id, text, **kwargs)

    async def edit_message_text(self, **kwargs) -> None:
        if kwargs["chat_id"] in self.fail_chats:
            raise RuntimeError("message is not modified")
        self.edits.append(kwargs)


def _callbacks(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ----------------------------------------------------------------- storage


async def test_moderator_crud(repository) -> None:
    assert await repository.add_moderator(MODERATOR_ID, "Анна", ADMIN_ID) is True
    assert await repository.add_moderator(MODERATOR_ID, "Другая", ADMIN_ID) is False

    rows = await repository.list_moderators()
    assert [(r.user_id, r.display_name, r.added_by) for r in rows] == [
        (MODERATOR_ID, "Анна", ADMIN_ID)
    ]

    await repository.update_moderator_name(MODERATOR_ID, "Анна К.")
    assert (await repository.list_moderators())[0].display_name == "Анна К."

    assert await repository.remove_moderator(MODERATOR_ID) is True
    assert await repository.remove_moderator(MODERATOR_ID) is False
    assert await repository.list_moderators() == []


async def test_telegram_ids_beyond_32_bits_are_stored(repository) -> None:
    big = 7_000_000_000_123
    await repository.add_moderator(big, "Big")
    await repository.add_card_message(1, big, big + 1)

    assert (await repository.list_moderators())[0].user_id == big
    assert await repository.list_card_messages(1) == [(big, big + 1)]


async def test_card_messages_are_deduped_and_include_the_legacy_card(repository) -> None:
    item = await _awaiting(repository)  # legacy pair (ADMIN_ID, 1) on the item

    await repository.add_card_message(item.id, ADMIN_ID, 1)
    await repository.add_card_message(item.id, SECOND_ADMIN_ID, 77)
    await repository.add_card_message(item.id, SECOND_ADMIN_ID, 78)  # duplicate chat

    assert await repository.list_card_messages(item.id) == [
        (ADMIN_ID, 1),
        (SECOND_ADMIN_ID, 77),
    ]


async def test_legacy_item_without_card_rows_still_lists_its_admin_card(repository) -> None:
    item = await _awaiting(repository)

    assert await repository.list_card_messages(item.id) == [(ADMIN_ID, 1)]
    other = await _candidate(repository, 2)
    assert await repository.list_card_messages(other.id) == []


async def test_events_are_listed_by_window_and_latest_wins(repository) -> None:
    t1 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    t3 = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    await repository.record_event(5, ADMIN_ID, "Админ", EVENT_APPROVE, t1)
    await repository.record_event(5, ADMIN_ID, "Админ", EVENT_CANCEL, t2)
    await repository.record_event(6, MODERATOR_ID, "Анна", EVENT_REJECT, t3)

    window = await repository.list_events(
        datetime(2026, 10, 20, 0, 0, tzinfo=UTC), datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
    )
    assert [e.action for e in window] == [EVENT_APPROVE, EVENT_CANCEL]
    latest = await repository.latest_event(5)
    assert latest is not None and latest.action == EVENT_CANCEL
    assert await repository.latest_event(999) is None


async def test_count_by_status(repository) -> None:
    await _awaiting(repository, 1)
    await _awaiting(repository, 2)
    await _candidate(repository, 3)

    assert await repository.count_by_status(NewsStatus.AWAITING_APPROVAL) == 2
    assert await repository.count_by_status(NewsStatus.CANDIDATE) == 1
    assert await repository.count_by_status(NewsStatus.PUBLISHED) == 0


async def test_init_db_adds_the_new_tables_to_an_old_database(tmp_path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE news_items (id INTEGER PRIMARY KEY, source_id VARCHAR(64) NOT NULL,"
            " source_name VARCHAR(128) NOT NULL DEFAULT '', section VARCHAR(128) NOT NULL DEFAULT '',"
            " url TEXT NOT NULL, normalized_url VARCHAR(1024) NOT NULL, content_hash VARCHAR(64),"
            " original_title TEXT NOT NULL DEFAULT '', published_at DATETIME,"
            " status VARCHAR(32) NOT NULL DEFAULT 'DISCOVERED', status_reason TEXT NOT NULL DEFAULT '',"
            " importance INTEGER, ai_reason TEXT NOT NULL DEFAULT '', title_ru TEXT NOT NULL DEFAULT '',"
            " draft_ru TEXT NOT NULL DEFAULT '', admin_chat_id INTEGER, admin_message_id INTEGER,"
            " channel_message_id INTEGER, decided_by INTEGER,"
            " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    engine = create_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await init_db(engine)
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()

    assert {"news_items", "moderators", "card_messages", "moderation_events"} <= tables


# ------------------------------------------------------------ access control


async def test_access_loads_moderators_from_the_database(settings, repository) -> None:
    await repository.add_moderator(MODERATOR_ID, "Анна", ADMIN_ID)
    control = AccessControl(settings, repository)
    assert control.is_moderator(MODERATOR_ID) is False  # not loaded yet

    await control.load()

    assert control.is_moderator(MODERATOR_ID) is True
    assert control.can_moderate(MODERATOR_ID) is True
    assert control.is_admin(MODERATOR_ID) is False
    assert control.can_moderate(STRANGER_ID) is False
    assert control.can_moderate(None) is False
    assert control.owner_id == ADMIN_ID


async def test_removed_moderator_loses_rights_at_once(settings, repository) -> None:
    control = AccessControl(settings, repository)
    assert await control.add_moderator(MODERATOR_ID, "Анна", ADMIN_ID) is AddModeratorResult.ADDED
    assert control.can_moderate(MODERATOR_ID)

    assert await control.remove_moderator(MODERATOR_ID) is True

    assert not control.can_moderate(MODERATOR_ID)
    assert await repository.list_moderators() == []
    assert await control.remove_moderator(MODERATOR_ID) is False


async def test_an_administrator_cannot_become_a_moderator(settings, repository) -> None:
    control = AccessControl(settings, repository)

    assert await control.add_moderator(ADMIN_ID) is AddModeratorResult.IS_ADMIN
    assert await repository.list_moderators() == []
    assert control.is_moderator(ADMIN_ID) is False
    assert control.can_moderate(ADMIN_ID) is True


async def test_duplicate_moderator_is_refused(settings, repository) -> None:
    control = AccessControl(settings, repository)
    await control.add_moderator(MODERATOR_ID)

    assert await control.add_moderator(MODERATOR_ID) is AddModeratorResult.ALREADY_MODERATOR
    assert len(await repository.list_moderators()) == 1


async def test_moderators_are_capped(settings, repository) -> None:
    control = AccessControl(settings, repository)
    for offset in range(MAX_MODERATORS):
        assert await control.add_moderator(1000 + offset) is AddModeratorResult.ADDED

    assert await control.add_moderator(5000) is AddModeratorResult.LIMIT_REACHED
    assert len(await repository.list_moderators()) == MAX_MODERATORS


async def test_can_moderate_filter(access) -> None:
    flt = CanModerate(access)

    assert await flt(FakeCallbackQuery("x", FakeUser(ADMIN_ID))) is True
    assert await flt(FakeCallbackQuery("x", FakeUser(MODERATOR_ID))) is True
    assert await flt(FakeCallbackQuery("x", FakeUser(STRANGER_ID))) is False


# -------------------------------------------------------------- the service


async def test_moderator_approve_schedules_and_logs_an_event(
    repository, fake_bot, settings, access
) -> None:
    await repository.add_moderator(MODERATOR_ID, "")
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=MODERATOR_ID, user_name="Анна"
    )

    assert result.outcome is ActionOutcome.SCHEDULED
    assert (await repository.get(item.id)).status_enum is NewsStatus.SCHEDULED
    event = await repository.latest_event(item.id)
    assert (event.action, event.user_id, event.user_name) == (
        EVENT_APPROVE,
        MODERATOR_ID,
        "Анна",
    )
    assert (await repository.list_moderators())[0].display_name == "Анна"
    assert fake_bot.messages_to(CHANNEL_ID) == []


async def test_moderator_reject_logs_an_event(repository, fake_bot, settings, access) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)

    result = await service.handle_action(
        action=ACTION_REJECT, item_id=item.id, user_id=MODERATOR_ID, user_name="Анна"
    )

    assert result.outcome is ActionOutcome.REJECTED
    assert (await repository.latest_event(item.id)).action == EVENT_REJECT


@pytest.mark.parametrize(
    "action", [ACTION_PUBLISH_NOW, ACTION_CANCEL_SCHEDULE, ACTION_REGENERATE]
)
async def test_moderator_cannot_use_admin_only_actions(
    repository, fake_bot, settings, access, action
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)
    await service.handle_action(action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID)

    result = await service.handle_action(
        action=action, item_id=item.id, user_id=MODERATOR_ID, user_name="Анна"
    )

    assert result.outcome is ActionOutcome.DENIED
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.SCHEDULED
    assert fake_bot.messages_to(CHANNEL_ID) == []
    assert [e.action for e in await _all_events(repository)] == [EVENT_APPROVE]


async def _all_events(repository) -> list[ModerationEvent]:
    return await repository.list_events(
        datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    )


async def test_stranger_is_denied_every_action(repository, fake_bot, settings, access) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)

    for action in (ACTION_APPROVE, ACTION_REJECT, ACTION_PUBLISH_NOW, ACTION_REGENERATE):
        result = await service.handle_action(
            action=action, item_id=item.id, user_id=STRANGER_ID
        )
        assert result.outcome is ActionOutcome.DENIED

    assert (await repository.get(item.id)).status_enum is NewsStatus.AWAITING_APPROVAL
    assert await _all_events(repository) == []


async def test_admin_keeps_every_action_and_each_win_is_logged(
    repository, fake_bot, settings, access
) -> None:
    first = await _awaiting(repository, 1)
    second = await _awaiting(repository, 2)
    third = await _awaiting(repository, 3)
    service = _service(repository, fake_bot, settings, access)

    assert (
        await service.handle_action(
            action=ACTION_APPROVE, item_id=first.id, user_id=ADMIN_ID, user_name="Влад"
        )
    ).outcome is ActionOutcome.SCHEDULED
    assert (
        await service.handle_action(
            action=ACTION_CANCEL_SCHEDULE, item_id=first.id, user_id=ADMIN_ID, user_name="Влад"
        )
    ).outcome is ActionOutcome.UNSCHEDULED
    assert (
        await service.handle_action(
            action=ACTION_PUBLISH_NOW, item_id=second.id, user_id=ADMIN_ID, user_name="Влад"
        )
    ).outcome is ActionOutcome.PUBLISHED
    assert (
        await service.handle_action(
            action=ACTION_REJECT, item_id=third.id, user_id=ADMIN_ID, user_name="Влад"
        )
    ).outcome is ActionOutcome.REJECTED

    assert [e.action for e in await _all_events(repository)] == [
        EVENT_APPROVE,
        EVENT_CANCEL,
        EVENT_PUBLISH_NOW,
        EVENT_REJECT,
    ]


async def test_two_presses_record_one_event_and_one_decision(
    repository, fake_bot, settings, access
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)

    first = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=MODERATOR_ID, user_name="Анна"
    )
    second = await service.handle_action(
        action=ACTION_PUBLISH_NOW, item_id=item.id, user_id=ADMIN_ID, user_name="Влад"
    )
    third = await service.handle_action(
        action=ACTION_REJECT, item_id=item.id, user_id=ADMIN_ID, user_name="Влад"
    )

    assert first.outcome is ActionOutcome.SCHEDULED
    # An administrator may still publish a scheduled item now: that is a second,
    # separate decision. The reject after it loses.
    assert second.outcome is ActionOutcome.PUBLISHED
    assert third.outcome is ActionOutcome.ALREADY_HANDLED
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1
    assert [e.action for e in await _all_events(repository)] == [
        EVENT_APPROVE,
        EVENT_PUBLISH_NOW,
    ]


async def test_moderator_then_admin_same_action_second_loses(
    repository, fake_bot, settings, access
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)

    first = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=MODERATOR_ID, user_name="Анна"
    )
    second = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID, user_name="Влад"
    )

    assert first.outcome is ActionOutcome.SCHEDULED
    assert second.outcome is ActionOutcome.ALREADY_HANDLED
    assert len(await _all_events(repository)) == 1


async def test_a_removed_moderator_is_denied_by_the_service(
    repository, fake_bot, settings, access
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)
    await access.remove_moderator(MODERATOR_ID)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=MODERATOR_ID
    )

    assert result.outcome is ActionOutcome.DENIED


# ------------------------------------------------------------- card fan-out


async def test_card_goes_to_admins_then_moderators_with_role_keyboards(
    repository, settings, access
) -> None:
    access._moderators.add(SECOND_MODERATOR_ID)
    bot = EditBot()
    item = await _candidate(repository)
    sender = AdminCardSender(bot, repository, settings.admin_ids, settings, access=access)

    delivered = await sender.send_card(item)

    assert delivered is not None
    assert delivered.status_enum is NewsStatus.AWAITING_APPROVAL
    assert [m.chat_id for m in bot.sent] == [
        ADMIN_ID,
        SECOND_ADMIN_ID,
        MODERATOR_ID,
        SECOND_MODERATOR_ID,
    ]
    admin_actions = _callbacks(bot.sent[0].kwargs["reply_markup"])
    moderator_actions = _callbacks(bot.sent[2].kwargs["reply_markup"])
    assert any(":now:" in data for data in admin_actions)
    assert any(":regenerate:" in data for data in admin_actions)
    assert [data.split(":")[1] for data in moderator_actions] == ["approve", "reject"]
    rows = await repository.list_card_messages(item.id)
    assert sorted(chat for chat, _ in rows) == sorted(
        [ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID, SECOND_MODERATOR_ID]
    )
    assert (delivered.admin_chat_id, delivered.admin_message_id) == (
        ADMIN_ID,
        bot.sent[0].message_id,
    )


async def test_a_failing_recipient_is_skipped_and_awaiting_is_marked_once(
    repository, settings, access, monkeypatch
) -> None:
    bot = EditBot(fail_send={ADMIN_ID})
    item = await _candidate(repository)
    calls: list[int] = []
    original = repository.mark_awaiting_approval

    async def spy(item_id, *, chat_id, message_id):
        calls.append(chat_id)
        return await original(item_id, chat_id=chat_id, message_id=message_id)

    monkeypatch.setattr(repository, "mark_awaiting_approval", spy)
    sender = AdminCardSender(bot, repository, settings.admin_ids, settings, access=access)

    delivered = await sender.send_card(item)

    assert delivered is not None
    assert calls == [SECOND_ADMIN_ID]  # once, for the first success
    assert delivered.admin_chat_id == SECOND_ADMIN_ID
    assert [m.chat_id for m in bot.sent] == [SECOND_ADMIN_ID, MODERATOR_ID]
    assert sorted(c for c, _ in await repository.list_card_messages(item.id)) == sorted(
        [SECOND_ADMIN_ID, MODERATOR_ID]
    )


async def test_no_successful_send_returns_none(repository, settings, access) -> None:
    bot = EditBot(fail_send={ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID})
    item = await _candidate(repository)
    sender = AdminCardSender(bot, repository, settings.admin_ids, settings, access=access)

    assert await sender.send_card(item) is None
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.CANDIDATE
    assert await repository.list_card_messages(item.id) == []


async def test_sender_without_access_keeps_sending_to_admins_only(
    repository, settings
) -> None:
    bot = EditBot()
    item = await _candidate(repository)

    await AdminCardSender(bot, repository, settings.admin_ids).send_card(item)

    assert [m.chat_id for m in bot.sent] == [ADMIN_ID, SECOND_ADMIN_ID]


# ----------------------------------------------------------- card refreshing


async def _delivered(repository, settings, access, bot):
    item = await _candidate(repository)
    sender = AdminCardSender(bot, repository, settings.admin_ids, settings, access=access)
    delivered = await sender.send_card(item)
    assert delivered is not None
    return delivered


async def test_refresh_edits_every_copy_with_the_keyboard_of_its_role(
    repository, settings, access
) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    bot.edits.clear()
    stored = await repository.get(item.id)

    await refresh_item_cards(
        bot, access, repository, stored, status_line="LINE", delay_max=60
    )

    by_chat = {edit["chat_id"]: edit for edit in bot.edits}
    assert set(by_chat) == {ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID}
    assert all(edit["text"].endswith("\n\nLINE") for edit in bot.edits)
    assert _callbacks(by_chat[ADMIN_ID]["reply_markup"]) == _callbacks(
        build_keyboard(stored, delay_max_minutes=60)
    )
    assert _callbacks(by_chat[MODERATOR_ID]["reply_markup"]) == _callbacks(
        build_moderator_keyboard(stored, delay_max_minutes=60)
    )


async def test_refresh_of_a_scheduled_item_leaves_buttons_for_admins_only(
    repository, settings, access
) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    bot.edits.clear()
    await repository.schedule(item.id, admin_id=ADMIN_ID, scheduled_at=NOW)
    stored = await repository.get(item.id)

    await refresh_item_cards(bot, access, repository, stored, delay_max=60)

    by_chat = {edit["chat_id"]: edit for edit in bot.edits}
    assert _callbacks(by_chat[ADMIN_ID]["reply_markup"]) == _callbacks(
        build_scheduled_keyboard(stored)
    )
    assert by_chat[MODERATOR_ID]["reply_markup"] is None
    assert "⏰ Публикация в" in by_chat[MODERATOR_ID]["text"]


async def test_refresh_of_a_decided_item_removes_every_button(
    repository, settings, access
) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    bot.edits.clear()
    await repository.reject_by_admin(item.id, admin_id=ADMIN_ID)
    stored = await repository.get(item.id)

    await refresh_item_cards(bot, access, repository, stored, status_line="X", delay_max=60)

    assert len(bot.edits) == 3
    assert all(edit["reply_markup"] is None for edit in bot.edits)


async def test_a_failing_edit_does_not_stop_the_others(repository, settings, access) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    bot.edits.clear()
    bot.fail_chats = {SECOND_ADMIN_ID}
    stored = await repository.get(item.id)

    await refresh_item_cards(bot, access, repository, stored, delay_max=60)

    assert {edit["chat_id"] for edit in bot.edits} == {ADMIN_ID, MODERATOR_ID}


async def test_legacy_item_with_only_the_admin_card_is_refreshed(
    repository, settings, access
) -> None:
    item = await _awaiting(repository)  # no card_messages rows at all
    bot = EditBot()

    await refresh_item_cards(bot, access, repository, item, status_line="L", delay_max=60)

    assert [(e["chat_id"], e["message_id"]) for e in bot.edits] == [(ADMIN_ID, 1)]
    assert _callbacks(bot.edits[0]["reply_markup"]) == _callbacks(
        build_keyboard(item, delay_max_minutes=60)
    )


async def test_publication_refresh_updates_all_copies(repository, settings, access) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    bot.edits.clear()
    stored = await repository.get(item.id)

    await refresh_cards_after_publication(
        bot, [ActionResult(ActionOutcome.PUBLISHED, "", stored)], repository=repository
    )

    assert {edit["chat_id"] for edit in bot.edits} == {ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID}
    assert all(edit["reply_markup"] is None for edit in bot.edits)
    assert all(edit["text"].endswith("✅ Опубликовано по расписанию") for edit in bot.edits)


# --------------------------------------------------------- callback handling


@dataclass
class _Chat:
    id: int


class _Message:
    def __init__(self, chat_id: int, message_id: int) -> None:
        self.chat = _Chat(chat_id)
        self.message_id = message_id
        self.edits: list = []
        self.cleared = 0

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append((text, kwargs))

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.cleared += 1


@dataclass
class _User:
    id: int
    full_name: str = ""


def _press(action: str, item_id: int, user: _User, message: _Message) -> FakeCallbackQuery:
    callback = FakeCallbackQuery(build_callback_data(action, item_id), user)
    callback.message = message
    return callback


async def test_moderator_press_refreshes_all_copies_with_the_decision_line(
    repository, fake_bot, settings, access
) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    bot.edits.clear()
    rows = dict(await repository.list_card_messages(item.id))
    message = _Message(MODERATOR_ID, rows[MODERATOR_ID])
    callback = _press(ACTION_APPROVE, item.id, _User(MODERATOR_ID, "Анна <b>"), message)

    await handle_moderation_callback(
        callback, _service(repository, fake_bot, settings, access), bot
    )

    assert "запланирована" in callback.answers[0][0]
    by_chat = {edit["chat_id"]: edit for edit in bot.edits}
    assert set(by_chat) == {ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID}
    assert "✅ Одобрил(а): Анна &lt;b&gt;, выйдет в" in by_chat[ADMIN_ID]["text"]
    assert by_chat[MODERATOR_ID]["reply_markup"] is None
    assert _callbacks(by_chat[ADMIN_ID]["reply_markup"])  # scheduled keyboard
    assert message.edits == [] and message.cleared == 0  # it was among the copies


async def test_a_stale_press_clears_buttons_on_every_copy(
    repository, fake_bot, settings, access
) -> None:
    bot = EditBot()
    item = await _delivered(repository, settings, access, bot)
    service = _service(repository, fake_bot, settings, access)
    await service.handle_action(
        action=ACTION_REJECT, item_id=item.id, user_id=ADMIN_ID, user_name="Влад"
    )
    bot.edits.clear()
    rows = dict(await repository.list_card_messages(item.id))
    callback = _press(
        ACTION_APPROVE, item.id, _User(MODERATOR_ID, "Анна"), _Message(MODERATOR_ID, rows[MODERATOR_ID])
    )

    await handle_moderation_callback(callback, service, bot)

    assert callback.answers[0][0] == "Новость уже обработана."
    assert len(bot.edits) == 3
    assert all(edit["reply_markup"] is None for edit in bot.edits)
    assert all("❌ Отклонил(а): Влад" in edit["text"] for edit in bot.edits)
    assert (await repository.get(item.id)).status_enum is NewsStatus.REJECTED_BY_ADMIN


async def test_a_card_outside_the_recorded_copies_is_edited_through_the_message(
    repository, fake_bot, settings, access
) -> None:
    item = await _awaiting(repository)
    bot = EditBot()
    message = _Message(424242, 9)  # unknown to card_messages
    callback = _press(ACTION_APPROVE, item.id, _User(ADMIN_ID, "Влад"), message)

    await handle_moderation_callback(
        callback, _service(repository, fake_bot, settings, access), bot
    )

    assert {e["chat_id"] for e in bot.edits} == {ADMIN_ID}  # legacy copy
    assert len(message.edits) == 1
    assert "✅ Одобрил(а): Влад" in message.edits[0][0]


async def test_a_denied_press_edits_nothing(repository, fake_bot, settings, access) -> None:
    item = await _awaiting(repository)
    bot = EditBot()
    message = _Message(MODERATOR_ID, 5)
    callback = _press(ACTION_PUBLISH_NOW, item.id, _User(MODERATOR_ID, "Анна"), message)

    await handle_moderation_callback(
        callback, _service(repository, fake_bot, settings, access), bot
    )

    assert callback.answers[0][1] is True
    assert bot.edits == [] and message.edits == [] and message.cleared == 0
    assert (await repository.get(item.id)).status_enum is NewsStatus.AWAITING_APPROVAL


# ------------------------------------------------------------- card helpers


async def test_moderator_keyboard_has_approve_reject_and_source(repository) -> None:
    item = await _awaiting(repository)
    markup = build_moderator_keyboard(item, delay_max_minutes=90)

    assert [[b.text for b in row] for row in markup.inline_keyboard] == [
        ["✅ Одобрить (5–90 мин)", "❌ Отклонить"],
        ["🔗 Открыть источник"],
    ]
    assert _callbacks(markup) == [
        build_callback_data(ACTION_APPROVE, item.id),
        build_callback_data(ACTION_REJECT, item.id),
    ]
    assert markup.inline_keyboard[1][0].url == item.url


async def test_decision_lines(repository) -> None:
    item = await _awaiting(repository)
    await repository.schedule(
        item.id, admin_id=ADMIN_ID, scheduled_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    )
    stored = await repository.get(item.id)

    def event(action: str, name: str = "Анна") -> ModerationEvent:
        return ModerationEvent(item_id=item.id, user_id=42, user_name=name, action=action)

    assert render_decision_line(event(EVENT_APPROVE), stored) == "✅ Одобрил(а): Анна, выйдет в 12:00"
    assert render_decision_line(event(EVENT_PUBLISH_NOW), stored) == "⚡ Опубликовал(а) сразу: Анна"
    assert render_decision_line(event(EVENT_REJECT), stored) == "❌ Отклонил(а): Анна"
    assert render_decision_line(event(EVENT_CANCEL), stored) == "↩️ Публикацию отменил(а): Анна"
    assert render_decision_line(event(EVENT_REJECT, ""), stored) == "❌ Отклонил(а): ID 42"
    assert render_decision_line(event(EVENT_REJECT, "<i>&"), stored) == "❌ Отклонил(а): &lt;i&gt;&amp;"
