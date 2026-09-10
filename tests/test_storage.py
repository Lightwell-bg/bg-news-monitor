"""Statuses, deduplication and atomic transitions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from news_monitor.normalization.content import content_hash
from news_monitor.storage.models import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    NewsStatus,
    can_transition,
)
from news_monitor.storage.repository import DuplicateReason, NewsRepository

SOURCE = {"source_id": "flagman", "source_name": "Flagman"}


async def _discover(repository: NewsRepository, url: str, title: str = "Заглавие"):
    return await repository.register_discovered(url=url, title=title, **SOURCE)


async def _to_candidate(repository: NewsRepository, url: str, body: str):
    item, _ = await _discover(repository, url)
    await repository.mark_fetched(
        item.id,
        title="Заглавие на статията",
        body_hash=content_hash("Заглавие на статията", body),
        published_at=datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
    )
    await repository.mark_candidate(
        item.id,
        importance=90,
        ai_reason="Важно",
        title_ru="Русский заголовок",
        draft_ru="Русский черновик новости.",
    )
    return await repository.mark_awaiting_approval(item.id, chat_id=111, message_id=222)


async def test_new_link_is_created_once(repository: NewsRepository) -> None:
    first, created_first = await _discover(repository, "https://flagman.bg/statia/1")
    assert created_first is True
    assert first.status_enum is NewsStatus.DISCOVERED

    second, created_second = await _discover(repository, "https://flagman.bg/statia/1")
    assert created_second is False
    assert second.id == first.id


async def test_url_variants_collapse_to_one_item(repository: NewsRepository) -> None:
    first, _ = await _discover(repository, "https://www.flagman.bg/statia/1/")
    second, created = await _discover(
        repository, "https://flagman.bg/statia/1?utm_source=mail#top"
    )
    assert created is False
    assert second.id == first.id


async def test_duplicate_content_hash_is_marked_duplicate(
    repository: NewsRepository,
) -> None:
    body = "Един и същ текст на новината с достатъчно съдържание за хеширане."
    digest = content_hash("Заглавие", body)

    original, _ = await _discover(repository, "https://flagman.bg/statia/1")
    await repository.mark_fetched(original.id, title="Заглавие", body_hash=digest)

    reprint, _ = await _discover(repository, "https://flagman.bg/statia/2")
    stored = await repository.mark_fetched(reprint.id, title="Заглавие", body_hash=digest)

    assert stored.status_enum is NewsStatus.DUPLICATE
    assert stored.status_reason == DuplicateReason.CONTENT
    assert stored.content_hash is None

    untouched = await repository.get(original.id)
    assert untouched is not None
    assert untouched.status_enum is NewsStatus.FETCHED


async def test_full_happy_path_statuses(repository: NewsRepository) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")
    assert item.status_enum is NewsStatus.AWAITING_APPROVAL

    assert await repository.approve(item.id, admin_id=42) is True
    approved = await repository.get(item.id)
    assert approved is not None
    assert approved.status_enum is NewsStatus.APPROVED
    assert approved.decided_by == 42

    assert await repository.mark_published(item.id, channel_message_id=555) is True
    published = await repository.get(item.id)
    assert published is not None
    assert published.status_enum is NewsStatus.PUBLISHED
    assert published.channel_message_id == 555


async def test_approve_is_won_by_exactly_one_caller(repository: NewsRepository) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")

    assert await repository.approve(item.id, admin_id=42) is True
    assert await repository.approve(item.id, admin_id=42) is False

    stored = await repository.get(item.id)
    assert stored is not None
    assert stored.status_enum is NewsStatus.APPROVED


async def test_concurrent_approve_produces_one_winner(repository: NewsRepository) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")

    results = await asyncio.gather(
        repository.approve(item.id, admin_id=42),
        repository.approve(item.id, admin_id=42),
        repository.approve(item.id, admin_id=42),
    )
    assert results.count(True) == 1


async def test_mark_published_runs_once(repository: NewsRepository) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")
    await repository.approve(item.id, admin_id=42)

    assert await repository.mark_published(item.id, channel_message_id=1) is True
    assert await repository.mark_published(item.id, channel_message_id=2) is False

    stored = await repository.get(item.id)
    assert stored is not None
    assert stored.channel_message_id == 1


async def test_reject_blocks_a_later_approve(repository: NewsRepository) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")

    assert await repository.reject_by_admin(item.id, admin_id=42) is True
    assert await repository.approve(item.id, admin_id=42) is False

    stored = await repository.get(item.id)
    assert stored is not None
    assert stored.status_enum is NewsStatus.REJECTED_BY_ADMIN


async def test_publication_failure_can_be_retried(repository: NewsRepository) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")
    await repository.approve(item.id, admin_id=42)

    assert await repository.mark_publication_failed(item.id, "telegram down") is True
    assert await repository.retry_publication(item.id) is True

    stored = await repository.get(item.id)
    assert stored is not None
    assert stored.status_enum is NewsStatus.APPROVED


async def test_update_draft_only_while_awaiting_approval(
    repository: NewsRepository,
) -> None:
    item = await _to_candidate(repository, "https://flagman.bg/statia/1", "Текст на новината.")

    assert (
        await repository.update_draft(
            item.id,
            importance=95,
            ai_reason="Новая причина",
            title_ru="Новый заголовок",
            draft_ru="Новый черновик новости.",
        )
        is True
    )
    refreshed = await repository.get(item.id)
    assert refreshed is not None
    assert refreshed.title_ru == "Новый заголовок"
    assert refreshed.status_enum is NewsStatus.AWAITING_APPROVAL

    await repository.approve(item.id, admin_id=42)
    assert (
        await repository.update_draft(
            item.id,
            importance=10,
            ai_reason="Поздно",
            title_ru="Поздний заголовок",
            draft_ru="Поздний черновик новости.",
        )
        is False
    )
    final = await repository.get(item.id)
    assert final is not None
    assert final.title_ru == "Новый заголовок"


async def test_invalid_transition_is_refused(repository: NewsRepository) -> None:
    item, _ = await _discover(repository, "https://flagman.bg/statia/1")
    with pytest.raises(InvalidTransitionError):
        await repository.mark_candidate(
            item.id,
            importance=90,
            ai_reason="Важно",
            title_ru="Заголовок",
            draft_ru="Черновик новости.",
        )


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    for status in (
        NewsStatus.DUPLICATE,
        NewsStatus.REJECTED_BY_FILTER,
        NewsStatus.REJECTED_BY_ADMIN,
        NewsStatus.PUBLISHED,
    ):
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_state_machine_matches_the_approved_path() -> None:
    assert can_transition(NewsStatus.DISCOVERED, NewsStatus.FETCHED)
    assert can_transition(NewsStatus.FETCHED, NewsStatus.CANDIDATE)
    assert can_transition(NewsStatus.CANDIDATE, NewsStatus.AWAITING_APPROVAL)
    assert can_transition(NewsStatus.AWAITING_APPROVAL, NewsStatus.APPROVED)
    assert can_transition(NewsStatus.APPROVED, NewsStatus.PUBLISHED)
    assert not can_transition(NewsStatus.CANDIDATE, NewsStatus.PUBLISHED)
    assert not can_transition(NewsStatus.AWAITING_APPROVAL, NewsStatus.PUBLISHED)
    assert not can_transition(NewsStatus.DISCOVERED, NewsStatus.APPROVED)
