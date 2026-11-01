"""Review fixes: auth re-check, pending slots, fan-out refresh, atomic event, paging, UTF-16."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from news_monitor.config.runtime_store import RuntimeSettingsStore
from news_monitor.reports import REPORT_LIMIT, build_daily_report
from news_monitor.storage.models import EVENT_APPROVE, NewsStatus
from news_monitor.storage.repository import DecisionEvent, NewsRepository
from news_monitor.telegram.access import MAX_MODERATORS, AccessControl
from news_monitor.telegram.admin_ui import PendingInputStore
from news_monitor.telegram.card import ACTION_APPROVE, ACTION_REJECT
from news_monitor.telegram.moderation import ActionOutcome, ModerationService
from news_monitor.telegram.moderators_admin import (
    ACTION_PAGE,
    INPUT_ADD_MODERATOR,
    MODERATORS_PER_PAGE,
    IncomingInput,
    ModeratorsAdminService,
    ModeratorView,
    build_callback_data,
    moderators_keyboard,
    render_moderators,
)
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.telegram.sender import AdminCardSender
from tests.conftest import ADMIN_ID, CHANNEL_ID, SECOND_ADMIN_ID, FakeBot
from tests.test_daily_report import DAY, MORNING, _item
from tests.test_moderators import MODERATOR_ID, NOW, EditBot, _awaiting, _candidate

NEW_ID = 700100300


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.fixture
def access(settings, repository) -> AccessControl:
    control = AccessControl(settings, repository)
    control._moderators.add(MODERATOR_ID)
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


# ------------------------------------------------ 1. authorization re-check


async def test_moderator_removed_during_lookup_is_denied(
    repository, fake_bot, settings, access, monkeypatch
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)
    original_get = repository.get

    async def get_and_remove(item_id):
        found = await original_get(item_id)
        await access.remove_moderator(MODERATOR_ID)  # removed mid-lookup
        return found

    monkeypatch.setattr(repository, "get", get_and_remove)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=MODERATOR_ID, user_name="Анна"
    )

    monkeypatch.undo()
    assert result.outcome is ActionOutcome.DENIED
    assert (await repository.get(item.id)).status_enum is NewsStatus.AWAITING_APPROVAL
    assert await repository.latest_event(item.id) is None


# ------------------------------------------------ 2. pending add slot


def _admin_service(settings, repository, access, tmp_path, pending, bot) -> ModeratorsAdminService:
    return ModeratorsAdminService(
        access=access,
        store=RuntimeSettingsStore(tmp_path / "runtime.yaml"),
        repository=repository,
        bot=bot,
        settings=settings,
        pending=pending,
        today=lambda: date(2026, 10, 21),
    )


async def test_two_concurrent_answers_add_only_one_moderator(
    settings, repository, access, tmp_path, monkeypatch
) -> None:
    pending = PendingInputStore()
    service = _admin_service(settings, repository, access, tmp_path, pending, FakeBot())
    pending.start(ADMIN_ID, INPUT_ADD_MODERATOR)
    calls: list[int] = []
    original = access.add_moderator

    async def slow_add(user_id, name="", added_by=None):
        calls.append(user_id)
        await asyncio.sleep(0.01)
        return await original(user_id, name, added_by)

    monkeypatch.setattr(access, "add_moderator", slow_add)

    first, second = await asyncio.gather(
        service.execute_input(incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID),
        service.execute_input(incoming=IncomingInput(text=str(NEW_ID + 1)), user_id=ADMIN_ID),
    )

    assert calls == [NEW_ID]
    assert sorted([first.done, second.done]) == [False, True]
    assert NEW_ID in access.moderator_ids
    assert NEW_ID + 1 not in access.moderator_ids
    assert pending.get(ADMIN_ID) is None


async def test_slot_opened_by_another_panel_during_the_add_survives(
    settings, repository, access, tmp_path, monkeypatch
) -> None:
    pending = PendingInputStore()
    service = _admin_service(settings, repository, access, tmp_path, pending, FakeBot())
    pending.start(ADMIN_ID, INPUT_ADD_MODERATOR)
    original = access.add_moderator

    async def add_while_other_panel_opens(user_id, name="", added_by=None):
        pending.start(ADMIN_ID, "src_url")  # another panel asks for its own value
        return await original(user_id, name, added_by)

    monkeypatch.setattr(access, "add_moderator", add_while_other_panel_opens)

    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID
    )

    assert outcome.done
    survivor = pending.get(ADMIN_ID)
    assert survivor is not None and survivor.kind == "src_url"


async def test_a_failing_add_reopens_the_slot(
    settings, repository, access, tmp_path, monkeypatch
) -> None:
    pending = PendingInputStore()
    service = _admin_service(settings, repository, access, tmp_path, pending, FakeBot())
    pending.start(ADMIN_ID, INPUT_ADD_MODERATOR)

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(access, "add_moderator", boom)

    with pytest.raises(RuntimeError):
        await service.execute_input(
            incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID
        )

    assert service.has_pending_input(ADMIN_ID)


def test_pending_take_and_reopen() -> None:
    store = PendingInputStore()
    store.start(1, "a")
    assert store.take(1, "b") is None  # other kind is left alone
    taken = store.take(1, "a")
    assert taken is not None and store.take(1, "a") is None
    assert store.reopen(1, taken) is True
    store.take(1, "a")
    store.start(1, "c")
    assert store.reopen(1, taken) is False  # never clobbers a newer slot
    assert store.get(1).kind == "c"


# ------------------------------------------------ 3. fan-out decision


class DecidingBot(EditBot):
    """The second send triggers a rejection, as a fast press on the first copy would."""

    def __init__(self, service: ModerationService) -> None:
        super().__init__()
        self.service = service
        self.triggered = False

    async def send_message(self, chat_id, text, **kwargs):
        if len(self.sent) == 1 and not self.triggered:
            self.triggered = True
            await self.service.handle_action(
                action=ACTION_REJECT, item_id=self.item_id, user_id=ADMIN_ID, user_name="Влад"
            )
        return await super().send_message(chat_id, text, **kwargs)


async def test_decision_during_fan_out_refreshes_later_copies(
    repository, settings, access
) -> None:
    service = _service(repository, FakeBot(), settings, access)
    bot = DecidingBot(service)
    item = await _candidate(repository)
    bot.item_id = item.id
    sender = AdminCardSender(bot, repository, settings.admin_ids, settings, access=access)

    delivered = await sender.send_card(item)

    assert delivered is not None
    assert (await repository.get(item.id)).status_enum is NewsStatus.REJECTED_BY_ADMIN
    copies = await repository.list_card_messages(item.id)
    assert sorted(c for c, _ in copies) == sorted([ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID])
    last_edit: dict[int, dict] = {}
    for edit in bot.edits:
        last_edit[edit["chat_id"]] = edit
    assert set(last_edit) == {ADMIN_ID, SECOND_ADMIN_ID, MODERATOR_ID}
    for edit in last_edit.values():
        assert edit["reply_markup"] is None
        assert "Влад" in edit["text"]


async def test_no_refresh_when_nothing_was_decided(repository, settings, access) -> None:
    bot = EditBot()
    item = await _candidate(repository)
    sender = AdminCardSender(bot, repository, settings.admin_ids, settings, access=access)

    await sender.send_card(item)

    assert bot.edits == []


# ------------------------------------------------ 4. atomic transition + event


async def _events(repository):
    return await repository.list_events(
        datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    )


async def test_event_exists_only_when_the_transition_won(repository) -> None:
    item = await _awaiting(repository)
    event = DecisionEvent(user_id=5, user_name="Анна", action=EVENT_APPROVE, at=NOW)

    assert await repository.schedule(
        item.id, admin_id=5, scheduled_at=NOW + timedelta(minutes=9), event=event
    )
    assert not await repository.schedule(
        item.id, admin_id=6, scheduled_at=NOW + timedelta(minutes=9), event=event
    )
    assert not await repository.reject_by_admin(
        item.id, admin_id=6, event=DecisionEvent(6, "Б", "reject", NOW)
    )
    assert not await repository.approve_now(
        (await _awaiting(repository, 2)).id + 99,
        admin_id=6,
        event=DecisionEvent(6, "Б", "publish_now", NOW),
    )
    assert [e.user_id for e in await _events(repository)] == [5]


async def test_cancel_and_approve_now_write_their_events(repository) -> None:
    item = await _awaiting(repository)
    await repository.schedule(item.id, admin_id=5, scheduled_at=NOW + timedelta(minutes=9))
    assert await repository.cancel_schedule(
        item.id, event=DecisionEvent(5, "А", "cancel", NOW)
    )
    assert await repository.approve_now(
        item.id, admin_id=5, event=DecisionEvent(5, "А", "publish_now", NOW)
    )
    assert [e.action for e in await _events(repository)] == ["cancel", "publish_now"]


async def test_failing_event_insert_rolls_back_the_transition(
    repository, fake_bot, settings, access, monkeypatch
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings, access)

    def boom(session, item_id, event):
        raise RuntimeError("disk full")

    monkeypatch.setattr(NewsRepository, "_add_event", staticmethod(boom))

    with pytest.raises(RuntimeError):
        await service.handle_action(
            action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID, user_name="Влад"
        )

    monkeypatch.undo()
    assert (await repository.get(item.id)).status_enum is NewsStatus.AWAITING_APPROVAL
    assert await _events(repository) == []


async def test_latest_event_follows_commit_order_not_timestamps(repository) -> None:
    item = await _awaiting(repository)
    await repository.record_event(item.id, 1, "A", "approve", NOW + timedelta(hours=1))
    await repository.record_event(item.id, 2, "B", "cancel", NOW)  # committed later

    assert (await repository.latest_event(item.id)).user_id == 2


# ------------------------------------------------ 5. pagination


def _views(count: int) -> list[ModeratorView]:
    names = ["😀&<" + "Ж" * 61, "&" * 64, "😀" * 32]
    return [
        ModeratorView(user_id=10**12 + n, name=names[n % len(names)]) for n in range(count)
    ]


def test_every_page_of_fifty_long_names_fits_telegram() -> None:
    views = _views(MAX_MODERATORS)
    pages = -(-len(views) // MODERATORS_PER_PAGE)
    seen = 0
    for page in range(pages):
        text = render_moderators(views, page)
        keyboard = moderators_keyboard(views, page)
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        assert u16(text) < 3900
        assert len(buttons) <= 100
        assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
        seen += sum(1 for b in buttons if b.text.startswith("🗑"))
    assert seen == len(views)


def test_stale_page_is_clamped_and_navigation_is_consistent() -> None:
    views = _views(25)
    last = render_moderators(views, 999)
    assert "Страница 3 из 3" in last
    assert "21. " in last and "11. " not in last
    first = moderators_keyboard(views, -5)
    data = [b.callback_data for row in first.inline_keyboard for b in row]
    assert build_callback_data(ACTION_PAGE, 1) in data
    assert build_callback_data(ACTION_PAGE, -1) not in data


async def test_page_callback_through_the_service(
    settings, repository, access, tmp_path
) -> None:
    for n in range(15):
        await access.add_moderator(NEW_ID + n, f"Имя {n}", ADMIN_ID)
    service = _admin_service(settings, repository, access, tmp_path, PendingInputStore(), FakeBot())

    reply = await service.execute_callback(
        data=build_callback_data(ACTION_PAGE, 50), user_id=ADMIN_ID
    )

    assert "Страница 2 из 2" in reply.text


# ------------------------------------------------ 6. UTF-16 budget


async def test_report_with_emoji_stays_under_the_utf16_limit(repository) -> None:
    item = await _item(repository, 1, "😀" * 200)
    for minute in range(60):
        await repository.record_event(
            item.id,
            10 + minute,
            "😀" * 60,
            EVENT_APPROVE,
            MORNING + timedelta(minutes=minute),
        )

    text = await build_daily_report(repository, DAY)

    assert u16(text) < REPORT_LIMIT + 100
    assert u16(text) < 4096
    assert "…и ещё" in text
    assert text.splitlines()[-1].startswith("Ожидают решения сейчас:")
