"""Periodic execution of a pipeline with the interval of its source."""

from __future__ import annotations

import logging

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from news_monitor.config.settings import Settings
from news_monitor.pipeline import NewsPipeline

logger = logging.getLogger(__name__)


def effective_interval_minutes(pipeline: NewsPipeline, settings: Settings) -> int:
    """Return the polling interval, never faster than the source allows."""
    return max(pipeline.source.min_interval_minutes, settings.check_interval_minutes)


class PipelineScheduler:
    """Runs each pipeline on its own interval."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    def _remove_existing(self, job_id: str) -> None:
        """Drop a job with the same id, including one still pending a start.

        APScheduler only honours replace_existing for a persistent jobstore, so
        a pending job has to be removed explicitly. Without this a re-registered
        source would be polled twice per interval.
        """
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            return

    def add_pipeline(self, pipeline: NewsPipeline) -> int:
        """Register one pipeline and return the interval used, in minutes."""
        minutes = effective_interval_minutes(pipeline, self._settings)
        job_id = f"pipeline:{pipeline.source.id}"
        self._remove_existing(job_id)

        async def _job() -> None:
            try:
                await pipeline.run_once()
            except Exception:  # noqa: BLE001 - one failure must not stop the loop
                logger.exception("run failed for source %s", pipeline.source.id)

        self._scheduler.add_job(
            _job,
            trigger=IntervalTrigger(minutes=minutes),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "source %s scheduled every %s minutes", pipeline.source.id, minutes
        )
        return minutes

    def start(self) -> None:
        """Start the scheduler."""
        self._scheduler.start()

    def shutdown(self) -> None:
        """Stop the scheduler without waiting for running jobs."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
