"""Application composition root and entrypoint."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot

from news_monitor.ai.client import OpenRouterAssessor
from news_monitor.ai.schema import AIAssessment
from news_monitor.config.runtime_store import RuntimeSettingsStore, runtime_settings_path
from news_monitor.config.settings import Settings, get_settings
from news_monitor.config.sources import SourceConfig, load_sources
from news_monitor.config.sources_store import SourcesStore
from news_monitor.logging_config import setup_logging
from news_monitor.pipeline import NewsPipeline
from news_monitor.scheduler import PipelineScheduler
from news_monitor.sources.fetcher import HttpHtmlFetcher
from news_monitor.sources.registry import get_adapter
from news_monitor.storage.db import create_engine, create_session_factory, init_db
from news_monitor.storage.models import NewsItem
from news_monitor.storage.repository import NewsRepository
from news_monitor.telegram.bot import create_bot, create_dispatcher, register_admin_commands
from news_monitor.telegram.moderation import ModerationService
from news_monitor.telegram.publisher import ChannelPublisher
from news_monitor.telegram.admin_ui import PendingInputStore
from news_monitor.telegram.sender import AdminCardSender
from news_monitor.telegram.settings_admin import SettingsAdminService
from news_monitor.telegram.sources_admin import ApplyResult, SourceAdminService

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
    card_sender: AdminCardSender
    sources_store: SourcesStore
    runtime_store: RuntimeSettingsStore
    source_admin: SourceAdminService | None = None
    settings_admin: SettingsAdminService | None = None

    def build_pipeline(self, source: SourceConfig) -> NewsPipeline:
        """Create the pipeline of one source with the shared components."""
        return NewsPipeline(
            source=source,
            adapter=get_adapter(source.adapter_type),
            fetcher=self.fetcher,
            repository=self.repository,
            assessor=self.assessor,
            card_sender=self.card_sender,
            settings=self.settings,
        )

    def reload_sources(self) -> ApplyResult:
        """Apply the saved ``sources.yaml`` to the running process.

        Every enabled source is re-registered under its single job id, so an
        interval change replaces the job instead of adding a second one. A
        source that cannot be built (unknown adapter) keeps the previous state
        and is reported as needing a restart, while the file stays saved.
        """
        try:
            configured = load_sources(self.settings.sources_file)
        except Exception:  # noqa: BLE001 - the saved file is reported, not lost
            logger.exception("cannot reload the sources file")
            return ApplyResult(error="конфигурацию не удалось перечитать.")

        running: list[str] = []
        stopped: list[str] = []
        restart_required: list[str] = []

        for source in configured:
            if not source.enabled:
                self.pipelines.pop(source.id, None)
                if self.scheduler.remove_pipeline(source.id):
                    stopped.append(source.id)
                continue
            try:
                pipeline = self.build_pipeline(source)
            except KeyError:
                logger.warning(
                    "source %s has no registered adapter, not scheduled", source.id
                )
                restart_required.append(source.id)
                continue
            self.pipelines[source.id] = pipeline
            self.scheduler.add_pipeline(pipeline)
            running.append(source.id)

        known = {source.id for source in configured}
        for removed_id in [key for key in self.pipelines if key not in known]:
            self.pipelines.pop(removed_id, None)
            if self.scheduler.remove_pipeline(removed_id):
                stopped.append(removed_id)

        return ApplyResult(
            running=tuple(running),
            stopped=tuple(stopped),
            restart_required=tuple(restart_required),
        )


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

    # A value saved by the administrator overrides the environment default.
    runtime_store = RuntimeSettingsStore(runtime_settings_path(settings))
    runtime_store.apply_to(settings)

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

    application = Application(
        settings=settings,
        repository=repository,
        pipelines=pipelines,
        scheduler=PipelineScheduler(settings),
        moderation=moderation,
        bot=bot,
        fetcher=fetcher,
        assessor=assessor,
        card_sender=card_sender,
        sources_store=SourcesStore(settings.sources_file),
        runtime_store=runtime_store,
    )
    pending_inputs = PendingInputStore()
    application.source_admin = SourceAdminService(
        store=application.sources_store,
        settings=settings,
        applier=application.reload_sources,
        pending=pending_inputs,
    )
    application.settings_admin = SettingsAdminService(
        settings=settings,
        store=runtime_store,
        pending=pending_inputs,
    )

    applied = application.reload_sources()
    if applied.error:
        raise RuntimeError(f"cannot load the sources file: {settings.sources_file}")
    if applied.restart_required:
        logger.warning(
            "sources without a registered adapter are not scheduled: %s",
            ", ".join(applied.restart_required),
        )
    if not application.pipelines:
        logger.warning("no enabled source found in %s", settings.sources_file)
    return application


async def run() -> None:
    """Start the scheduler and the Telegram long polling loop."""
    settings = get_settings()
    setup_logging()
    application = await build_application(settings)

    bot = application.bot
    dispatcher = create_dispatcher(
        application.moderation,
        settings,
        application.source_admin,
        application.settings_admin,
    )

    await register_admin_commands(bot, settings)
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
