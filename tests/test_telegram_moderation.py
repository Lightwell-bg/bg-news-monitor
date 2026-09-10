"""Authorization, repeated callbacks and channel publication."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from news_monitor.storage.models import NewsStatus
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.card import (
    ACTION_APPROVE,
    ACTION_REGENERATE,
    ACTION_REJECT,
    build_callback_data,
    parse_callback_data,
    render_admin_card,
    render_channel_post,
)
from news_monitor.telegram.moderation import ActionOutcome, ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.telegram.sender import AdminCardSender
from tests.conftest import ADMIN_ID, CHANNEL_ID, SECOND_ADMIN_ID, STRANGER_ID, FakeBot

ARTICLE_URL = "https://flagman.bg/statia/307722-nov-zakon"


async def _awaiting_item(repository: NewsRepository):
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url=ARTICLE_URL,
        title="Нов закон за чужденците",
    )
    await repository.mark_fetched(
        item.id,
        title="Нов закон за чужденците",
        body_hash="a" * 64,
        published_at=datetime(2026, 9, 10, 5, 30, tzinfo=UTC),
    )
    await repository.mark_candidate(
        item.id,
        importance=85,
        ai_reason="Затрагивает документы иностранцев.",
        title_ru="Болгария меняет правила пребывания",
        draft_ru="С 15 октября заявления принимают только онлайн.",
    )
    return await repository.mark_awaiting_approval(
        item.id, chat_id=ADMIN_ID, message_id=500
    )


def _service(repository, fake_bot, settings, *, regenerator=None) -> ModerationService:
    return ModerationService(
        repository=repository,
        publisher=ChannelPublisher(fake_bot, CHANNEL_ID, dry_run=settings.dry_run),
        settings=settings,
        regenerator=regenerator,
    )


async def test_admin_can_publish(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
    )

    assert result.outcome is ActionOutcome.PUBLISHED
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1

    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.PUBLISHED
    assert stored.decided_by == ADMIN_ID


async def test_second_admin_from_the_list_is_allowed(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=SECOND_ADMIN_ID
    )
    assert result.outcome is ActionOutcome.PUBLISHED


async def test_stranger_cannot_publish(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=STRANGER_ID
    )

    assert result.outcome is ActionOutcome.DENIED
    assert fake_bot.sent == []

    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("user_id", [None, 0, -1, STRANGER_ID])
async def test_unknown_users_cannot_reject_either(
    repository, fake_bot, settings, user_id
) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    result = await service.handle_action(
        action=ACTION_REJECT, item_id=item.id, user_id=user_id
    )

    assert result.outcome is ActionOutcome.DENIED
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL


async def test_second_press_does_not_create_a_second_post(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    first = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
    )
    second = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
    )

    assert first.outcome is ActionOutcome.PUBLISHED
    assert second.outcome is ActionOutcome.ALREADY_HANDLED
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1

    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.PUBLISHED


async def test_concurrent_presses_publish_once(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    results = await asyncio.gather(
        *(
            service.handle_action(
                action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
            )
            for _ in range(4)
        )
    )

    published = [r for r in results if r.outcome is ActionOutcome.PUBLISHED]
    assert len(published) == 1
    assert len(fake_bot.messages_to(CHANNEL_ID)) == 1


async def test_reject_prevents_publication(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    rejected = await service.handle_action(
        action=ACTION_REJECT, item_id=item.id, user_id=ADMIN_ID
    )
    later = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
    )

    assert rejected.outcome is ActionOutcome.REJECTED
    assert later.outcome is ActionOutcome.ALREADY_HANDLED
    assert fake_bot.messages_to(CHANNEL_ID) == []

    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.REJECTED_BY_ADMIN


async def test_double_reject_is_idempotent(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    await service.handle_action(action=ACTION_REJECT, item_id=item.id, user_id=ADMIN_ID)
    second = await service.handle_action(
        action=ACTION_REJECT, item_id=item.id, user_id=ADMIN_ID
    )
    assert second.outcome is ActionOutcome.ALREADY_HANDLED


async def test_failed_publication_is_recorded(repository, fake_bot, settings) -> None:
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)
    fake_bot.fail_next = True

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
    )

    assert result.outcome is ActionOutcome.PUBLICATION_FAILED
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.PUBLICATION_FAILED
    assert fake_bot.messages_to(CHANNEL_ID) == []


async def test_dry_run_does_not_send_to_the_channel(repository, fake_bot, settings) -> None:
    settings.dry_run = True
    item = await _awaiting_item(repository)
    service = _service(repository, fake_bot, settings)

    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID
    )

    assert result.outcome is ActionOutcome.PUBLISHED_DRY_RUN
    assert fake_bot.sent == []
    stored = await repository.get(item.id)
    assert stored.status_enum is NewsStatus.PUBLISHED


async def test_unknown_item_is_reported(repository, fake_bot, settings) -> None:
    service = _service(repository, fake_bot, settings)
    result = await service.handle_action(
        action=ACTION_APPROVE, item_id=4242, user_id=ADMIN_ID
    )
    assert result.outcome is ActionOutcome.NOT_FOUND
    assert fake_bot.sent == []


async def test_regeneration_updates_the_draft(
    repository, fake_bot, settings, valid_assessment
) -> None:
    item = await _awaiting_item(repository)

    async def regenerator(_item):
        return valid_assessment

    service = _service(repository, fake_bot, settings, regenerator=regenerator)
    result = await service.handle_action(
        action=ACTION_REGENERATE, item_id=item.id, user_id=ADMIN_ID
    )

    assert result.outcome is ActionOutcome.REGENERATED
    stored = await repository.get(item.id)
    assert stored.title_ru == valid_assessment.title_ru
    assert stored.status_enum is NewsStatus.AWAITING_APPROVAL


async def test_regeneration_after_publication_is_refused(
    repository, fake_bot, settings, valid_assessment
) -> None:
    item = await _awaiting_item(repository)

    async def regenerator(_item):
        return valid_assessment

    service = _service(repository, fake_bot, settings, regenerator=regenerator)
    await service.handle_action(action=ACTION_APPROVE, item_id=item.id, user_id=ADMIN_ID)

    result = await service.handle_action(
        action=ACTION_REGENERATE, item_id=item.id, user_id=ADMIN_ID
    )
    assert result.outcome is ActionOutcome.ALREADY_HANDLED

    stored = await repository.get(item.id)
    assert stored.title_ru == "Болгария меняет правила пребывания"


async def test_regeneration_failure_keeps_the_previous_draft(
    repository, fake_bot, settings
) -> None:
    item = await _awaiting_item(repository)

    async def regenerator(_item):
        raise RuntimeError("model unavailable")

    service = _service(repository, fake_bot, settings, regenerator=regenerator)
    result = await service.handle_action(
        action=ACTION_REGENERATE, item_id=item.id, user_id=ADMIN_ID
    )

    assert result.outcome is ActionOutcome.REGENERATION_FAILED
    stored = await repository.get(item.id)
    assert stored.title_ru == "Болгария меняет правила пребывания"


async def test_publication_always_contains_the_source_link(repository) -> None:
    item = await _awaiting_item(repository)
    post = render_channel_post(item)
    assert ARTICLE_URL in post


async def test_publication_without_a_url_is_impossible(repository) -> None:
    item = await _awaiting_item(repository)
    item.url = ""
    with pytest.raises(ValueError):
        render_channel_post(item)


async def test_admin_card_contains_the_required_fields(repository) -> None:
    item = await _awaiting_item(repository)
    card = render_admin_card(item)

    assert "Flagman" in card
    assert "85" in card
    assert "Болгария меняет правила пребывания" in card
    assert "Нов закон за чужденците" in card
    assert ARTICLE_URL in card


async def test_card_is_sent_only_to_configured_admins(
    repository, fake_bot, settings
) -> None:
    item, _ = await repository.register_discovered(
        source_id="flagman", source_name="Flagman", url=ARTICLE_URL, title="Заглавие"
    )
    await repository.mark_fetched(item.id, title="Заглавие", body_hash="b" * 64)
    candidate = await repository.mark_candidate(
        item.id,
        importance=90,
        ai_reason="Важно",
        title_ru="Русский заголовок",
        draft_ru="Русский черновик новости.",
    )

    sender = AdminCardSender(fake_bot, repository, settings.admin_ids)
    delivered = await sender.send_card(candidate)

    assert delivered is not None
    assert delivered.status_enum is NewsStatus.AWAITING_APPROVAL
    assert [message.chat_id for message in fake_bot.sent] == [ADMIN_ID]
    assert STRANGER_ID not in [message.chat_id for message in fake_bot.sent]


@pytest.mark.parametrize(
    "data", ["", None, "news:approve", "other:approve:1", "news:publish:1", "news:approve:x", "news:approve:0"]
)
def test_malformed_callback_data_is_refused(data) -> None:
    assert parse_callback_data(data) is None


def test_callback_data_round_trip() -> None:
    assert parse_callback_data(build_callback_data(ACTION_APPROVE, 12)) == (
        ACTION_APPROVE,
        12,
    )


async def test_post_with_query_parameters_is_valid_telegram_html(repository) -> None:
    """A url with query parameters must not leak a raw ampersand into HTML mode."""
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url="https://flagman.bg/statia/1-novina?page=2&ref_id=7",
        title="Заглавие",
    )
    item.title_ru = "Русский заголовок"
    item.draft_ru = "Русский черновик новости."

    post = render_channel_post(item)

    assert "&amp;" in post
    assert "&" not in post.replace("&amp;", "")
    from news_monitor.filters.guards import check_source_link

    assert check_source_link(post, item.url).passed


async def test_post_escapes_html_in_the_draft(repository) -> None:
    """Angle brackets from a draft must never become live markup."""
    item, _ = await repository.register_discovered(
        source_id="flagman",
        source_name="Flagman",
        url="https://flagman.bg/statia/2-novina",
        title="Заглавие",
    )
    item.title_ru = "Заголовок <script>"
    item.draft_ru = "Черновик с <b>тегом</b> внутри."

    post = render_channel_post(item)

    assert "<script>" not in post
    assert "&lt;script&gt;" in post
    assert "<b>тегом</b>" not in post
