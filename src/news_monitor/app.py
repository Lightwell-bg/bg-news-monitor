"""Application composition root and entrypoint."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot

from news_monitor.ai.client import OpenRouterAssessor
from news_monitor.ai.schema import AIAssessment
from news_monitor.config.settings import Settings, get_settings
from news_monitor.config.sources import load_enabled_sources
from news_monitor.logging_config import setup_logging
from news_monitor.pipeline import NewsPipeline
from news_monitor.scheduler import PipelineScheduler
from news_monitor.sources.fetcher import HttpHtmlFetcher
from news_monitor.sources.registry import get_adapter
from news_monitor.storage.db import create_engine, create_session_factory, init_db
from news_monitor.storage.models import NewsItem
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.bot import create_bot, create_dispatcher
from news_monitor.telegram.moderation import ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.telegram.sender import AdminCardSender

logger = logging.getLogger(__name__)


@dataclass
class Application:
    """Wired application components."""

    settings: Settings
    repository: NewsRepository
    pipelines: dict[str, NewsPipeline]
    scheduler: PipelineScheduler
    moderation: ModerationService
    bot: Bot
    fetcher: HttpHtmlFetcher
    assessor: OpenRouterAssessor


def _validate_settings(settings: Settings) -> None:
    """Fail fast on a configuration that cannot moderate anything."""
    missing: list[str] = []
    if not settings.bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.admin_ids:
        missing.append("TELEGRAM_ADMIN_IDS")
    if not settings.openrouter_key:
        missing.append("OPENROUTER_API_KEY")
    if not settings.dry_run and not settings.telegram_channel_id:
        missing.append("TELEGRAM_CHANNEL_ID")
    if missing:
        raise RuntimeError(
            "missing required configuration: " + ", ".join(sorted(missing))
        )


async def build_application(settings: Settings | None = None) -> Application:
    """Create every component and register the enabled sources."""
    settings = settings or get_settings()
    _validate_settings(settings)

    engine = create_engine(settings.database_url)
    await init_db(engine)
    repository = NewsRepository(create_session_factory(engine))

    bot = create_bot(settings)
    publisher = ChannelPublisher(
        bot, settings.telegram_channel_id, dry_run=settings.dry_run
    )
    card_sender = AdminCardSender(bot, repository, settings.admin_ids)

    fetcher = HttpHtmlFetcher(
        user_agent=settings.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
    )
    assessor = OpenRouterAssessor(
        api_key=settings.openrouter_key,
        model=settings.openrouter_model,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.openrouter_timeout_seconds,
    )

    pipelines: dict[str, NewsPipeline] = {}
    for source in load_enabled_sources(settings.sources_file):
        pipelines[source.id] = NewsPipeline(
            source=source,
            adapter=get_adapter(source.adapter_type),
            fetcher=fetcher,
            repository=repository,
            assessor=assessor,
            card_sender=card_sender,
            settings=settings,
        )
    if not pipelines:
        logger.warning("no enabled source found in %s", settings.sources_file)

    async def regenerate(item: NewsItem) -> AIAssessment | None:
        pipeline = pipelines.get(item.source_id)
        if pipeline is None:
            return None
        return await pipeline.regenerate(item)

    moderation = ModerationService(
        repository=repository,
        publisher=publisher,
        settings=settings,
        regenerator=regenerate,
    )

    scheduler = PipelineScheduler(settings)
    for pipeline in pipelines.values():
        scheduler.add_pipeline(pipeline)

    return Application(
        settings=settings,
        repository=repository,
        pipelines=pipelines,
        scheduler=scheduler,
        moderation=moderation,
        bot=bot,
        fetcher=fetcher,
        assessor=assessor,
    )


async def run() -> None:
    """Start the scheduler and the Telegram long polling loop."""
    settings = get_settings()
    setup_logging()
    application = await build_application(settings)

    bot = application.bot
    dispatcher = create_dispatcher(application.moderation, settings)

    application.scheduler.start()
    logger.info(
        "started with %s source(s), dry_run=%s",
        len(application.pipelines),
        settings.dry_run,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await bot.session.close()


def main() -> None:
    """Console entrypoint."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        logging.getLogger(__name__).info("stopped by the operator")


if __name__ == "__main__":  # pragma: no cover
    main()
