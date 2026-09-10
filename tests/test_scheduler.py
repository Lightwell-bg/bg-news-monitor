"""Scheduling uses the interval of the source."""

from __future__ import annotations

from news_monitor.pipeline import NewsPipeline
from news_monitor.scheduler import PipelineScheduler, effective_interval_minutes
from news_monitor.sources.flagman import FlagmanHomepageAdapter
from news_monitor.telegram.sender import AdminCardSender
from tests.conftest import FakeAssessor, FakeHtmlFetcher


def _pipeline(repository, settings, source, fake_bot) -> NewsPipeline:
    return NewsPipeline(
        source=source,
        adapter=FlagmanHomepageAdapter(),
        fetcher=FakeHtmlFetcher(),
        repository=repository,
        assessor=FakeAssessor(),
        card_sender=AdminCardSender(fake_bot, repository, settings.admin_ids),
        settings=settings,
    )


async def test_source_interval_is_never_exceeded(
    repository, settings, source, fake_bot
) -> None:
    source.min_interval_minutes = 45
    settings.check_interval_minutes = 5
    pipeline = _pipeline(repository, settings, source, fake_bot)

    assert effective_interval_minutes(pipeline, settings) == 45


async def test_global_interval_applies_when_it_is_slower(
    repository, settings, source, fake_bot
) -> None:
    source.min_interval_minutes = 10
    settings.check_interval_minutes = 30
    pipeline = _pipeline(repository, settings, source, fake_bot)

    assert effective_interval_minutes(pipeline, settings) == 30


async def test_pipeline_is_registered_as_a_single_job(
    repository, settings, source, fake_bot
) -> None:
    scheduler = PipelineScheduler(settings)
    pipeline = _pipeline(repository, settings, source, fake_bot)

    minutes = scheduler.add_pipeline(pipeline)
    jobs = scheduler.scheduler.get_jobs()

    assert minutes == max(source.min_interval_minutes, settings.check_interval_minutes)
    assert [job.id for job in jobs] == ["pipeline:flagman"]
    assert jobs[0].max_instances == 1

    scheduler.add_pipeline(pipeline)
    assert len(scheduler.scheduler.get_jobs()) == 1
    scheduler.shutdown()
