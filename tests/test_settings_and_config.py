"""Settings parsing and sources.yaml validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from news_monitor.config.settings import Settings
from news_monitor.config.sources import get_source, load_enabled_sources, load_sources
from news_monitor.sources.registry import available_adapters

SOURCES_FILE = "config/sources.yaml"


def _settings(**overrides) -> Settings:
    data = {
        "_env_file": None,
        "telegram_bot_token": "token-value",
        "openrouter_api_key": "key-value",
    }
    data.update(overrides)
    return Settings(**data)


def test_admin_ids_are_parsed_from_a_list() -> None:
    settings = _settings(telegram_admin_ids="111, 222 333;444")
    assert settings.admin_ids == (111, 222, 333, 444)


def test_single_admin_id_is_still_supported() -> None:
    settings = _settings(telegram_admin_id="777")
    assert settings.admin_ids == (777,)


def test_both_admin_variables_are_merged_without_duplicates() -> None:
    settings = _settings(telegram_admin_ids="111,222", telegram_admin_id="222")
    assert settings.admin_ids == (111, 222)


def test_missing_admin_configuration_authorizes_nobody() -> None:
    settings = _settings()
    assert settings.admin_ids == ()
    assert settings.is_admin(111) is False
    assert settings.is_admin(None) is False


@pytest.mark.parametrize("user_id", [None, 0, 999, -111])
def test_is_admin_rejects_everyone_else(user_id) -> None:
    settings = _settings(telegram_admin_ids="111,222")
    assert settings.is_admin(user_id) is False


def test_is_admin_accepts_configured_ids() -> None:
    settings = _settings(telegram_admin_ids="111,222")
    assert settings.is_admin(111) is True
    assert settings.is_admin(222) is True


def test_non_numeric_admin_id_is_refused() -> None:
    with pytest.raises(ValueError):
        _settings(telegram_admin_ids="admin").admin_ids


def test_secrets_are_not_exposed_by_repr() -> None:
    settings = _settings(
        telegram_bot_token="super-secret-token", openrouter_api_key="super-secret-key"
    )
    rendered = f"{settings!r} {settings!s}"
    assert "super-secret-token" not in rendered
    assert "super-secret-key" not in rendered
    assert "super-secret-token" not in str(settings.telegram_bot_token)
    assert settings.bot_token == "super-secret-token"


def test_importance_threshold_range_is_enforced() -> None:
    with pytest.raises(ValidationError):
        _settings(importance_threshold=101)
    with pytest.raises(ValidationError):
        _settings(importance_threshold=-1)


def test_project_sources_file_is_valid() -> None:
    sources = load_sources(SOURCES_FILE)
    assert {source.id for source in sources} == {"flagman", "bg24", "burgas24"}


def test_flagman_is_enabled_with_a_registered_adapter() -> None:
    flagman = get_source(SOURCES_FILE, "flagman")
    assert flagman.enabled is True
    assert flagman.adapter_type in available_adapters()
    assert flagman.min_interval_minutes >= 1
    assert [section.name for section in flagman.sections] == ["Главная страница"]


def test_sources_without_an_adapter_stay_disabled() -> None:
    for source in load_sources(SOURCES_FILE):
        if source.adapter_type not in available_adapters():
            assert source.enabled is False, source.id


def test_only_sources_with_an_adapter_are_loaded_as_enabled() -> None:
    for source in load_enabled_sources(SOURCES_FILE):
        assert source.adapter_type in available_adapters()


def test_unknown_source_is_reported() -> None:
    with pytest.raises(KeyError):
        get_source(SOURCES_FILE, "unknown")
