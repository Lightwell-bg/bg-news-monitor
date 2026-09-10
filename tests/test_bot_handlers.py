"""Callback routing: only administrators can reach the moderation service."""

from __future__ import annotations

from dataclasses import dataclass, field

from news_monitor.storage.models import NewsStatus
from news_monitor.telegram.bot import AdminOnly, handle_moderation_callback
from news_monitor.telegram.card import ACTION_APPROVE, build_callback_data
from news_monitor.telegram.moderation import ActionOutcome, ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from tests.conftest import ADMIN_ID, CHANNEL_ID, STRANGER_ID


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeCallbackMessage:
    edits: list = field(default_factory=list)
    markup_cleared: int = 0

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.markup_cleared += 1


@dataclass
class FakeCallbackQuery:
    data: str | None
    from_user: FakeUser
    message: FakeCallbackMessage = field(default_factory=FakeCallbackMessage)
    answers: list = field(default_factory=list)

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


async def _awaiting(repository):
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url="https://flagman.bg/statia/1-novina",
        title="Заглавие",
    )
    await repository.mark_fetched(item.id, title="Заглавие", body_hash="c" * 64)
    await repository.mark_candidate(
        item.id,
        importance=90,
        ai_reason="Важно",
        title_ru="Русский заголовок",
        draft_ru="Русский черновик новости.",
    )
    return await repository.mark_awaiting_approval(item.id, chat_id=ADMIN_ID, message_id=1)


def _service(repository, fake_bot, settings) -> ModerationService:
    return ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, CHANNEL_ID, dry_run=False),
        settings=settings,
    )


async def test_admin_filter_accepts_only_configured_ids(settings) -> None:
    admin_only = AdminOnly(settings)
    assert await admin_only(FakeCallbackQuery("x", FakeUser(ADMIN_ID))) is True
    assert await admin_only(FakeCallbackQuery("x", FakeUser(STRANGER_ID))) is False


async def test_admin_callback_publishes_and_clears_buttons(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting(repository)
    callback = FakeCallbackQuery(
        build_callback_data(ACTION_APPROVE, item.id), FakeUser(ADMIN_ID)
    )

    await handle_moderation_callback(callback, _service(repository, fake_bot, settings))

    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1
    assert callback.message.markup_cleared == 1
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.PUBLISHED


async def test_stranger_callback_changes_nothing(repository, fake_bot, settings) -> None:
    item = await _awaiting(repository)
    callback = FakeCallbackQuery(
        build_callback_data(ACTION_APPROVE, item.id), FakeUser(STRANGER_ID)
    )

    await handle_moderation_callback(callback, _service(repository, fake_bot, settings))

    assert fake_bot.sent == []
    assert callback.message.markup_cleared == 0
    assert callback.answers[0][1] is True
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL


async def test_repeated_callback_publishes_once(repository, fake_bot, settings) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)
    data = build_callback_data(ACTION_APPROVE, item.id)

    first = FakeCallbackQuery(data, FakeUser(ADMIN_ID))
    second = FakeCallbackQuery(data, FakeUser(ADMIN_ID))
    await handle_moderation_callback(first, service)
    await handle_moderation_callback(second, service)

    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1


async def test_malformed_callback_is_answered_without_side_effects(
    repository, fake_bot, settings
) -> None:
    callback = FakeCallbackQuery("garbage", FakeUser(ADMIN_ID))
    await handle_moderation_callback(callback, _service(repository, fake_bot, settings))

    assert fake_bot.sent == []
    assert callback.answers


async def test_service_denies_even_without_the_router_filter(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting(repository)
    service = _service(repository, fake_bot, settings)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=STRANGER_ID
    )
    assert result.outcome is ActionOutcome.DENIED
    assert fake_bot.sent == []
