"""Composition root: configuration validation and wiring."""

from __future__ import annotations

import pytest

from news_monitor.app import Application, _validate_settings, build_application
from news_monitor.config.settings import Settings
from news_monitor.telegram.moderation import ModerationService
from tests.conftest import ADMIN_ID, STRANGER_ID

# Structurally valid but obviously fake: aiogram only checks the shape.
FAKE_TOKEN = "42:fake"


def _settings(tmp_path, **overrides) -> Settings:
    data = {
        "_env_file": None,
        "telegram_bot_token": FAKE_TOKEN,
        "telegram_admin_ids": str(ADMIN_ID),
        "telegram_channel_id": "@channel",
        "openrouter_api_key": "fake-key",
        "openrouter_model": "test/model",
        "database_url": f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        "sources_file": "config/sources.yaml",
        "dry_run": True,
    }
    data.update(overrides)
    return Settings(**data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_bot_token", ""),
        ("telegram_admin_ids", ""),
        ("openrouter_api_key", ""),
    ],
)
def test_missing_configuration_stops_startup(tmp_path, field, value) -> None:
    settings = _settings(tmp_path, **{field: value})
    with pytest.raises(RuntimeError) as excinfo:
        _validate_settings(settings)
    assert "missing required configuration" in str(excinfo.value)


def test_channel_is_required_only_outside_dry_run(tmp_path) -> None:
    _validate_settings(_settings(tmp_path, telegram_channel_id="", dry_run=True))
    with pytest.raises(RuntimeError):
        _validate_settings(_settings(tmp_path, telegram_channel_id="", dry_run=False))


def test_error_message_never_contains_a_secret_value(tmp_path) -> None:
    settings = _settings(tmp_path, telegram_admin_ids="")
    with pytest.raises(RuntimeError) as excinfo:
        _validate_settings(settings)
    assert FAKE_TOKEN not in str(excinfo.value)
    assert "fake-key" not in str(excinfo.value)


async def test_application_wires_the_enabled_source(tmp_path) -> None:
    settings = _settings(tmp_path)
    application = await build_application(settings)

    try:
        assert isinstance(application, Application)
        assert list(application.pipelines) == ["flagman"]
        assert application.pipelines["flagman"].source.adapter_type == "flagman_homepage"
        assert [job.id for job in application.scheduler.scheduler.get_jobs()] == [
            "pipeline:flagman"
        ]

        moderation = application.moderation
        assert isinstance(moderation, ModerationService)
        assert moderation.is_authorized(ADMIN_ID) is True
        assert moderation.is_authorized(STRANGER_ID) is False
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await application.bot.session.close()


async def test_database_schema_is_created_on_startup(tmp_path) -> None:
    settings = _settings(tmp_path)
    application = await build_application(settings)
    try:
        item, created = await application.repository.register_discovered(
            source_id="flagman",
            source_name="Flagman",
            url="https://flagman.bg/statia/1-novina",
        )
        assert created is True
        assert item.id > 0
    finally:
        application.scheduler.shutdown()
        await application.fetcher.aclose()
        await application.assessor.aclose()
        await application.bot.session.close()
