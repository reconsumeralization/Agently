import logging
import pytest
import yaml
from agently import Agently


_RUNTIME_LOG_KEYS = (
    "debug",
    "runtime.show_model_logs",
    "runtime.show_tool_logs",
    "runtime.show_trigger_flow_logs",
    "runtime.show_runtime_logs",
    "runtime.httpx_log_level",
)


def _snapshot_runtime_log_settings():
    return {key: Agently.settings.get(key, None) for key in _RUNTIME_LOG_KEYS}


def _restore_runtime_log_settings(snapshot):
    for key, value in snapshot.items():
        Agently.settings.set(key, value)
    level_name = Agently.settings.get("runtime.httpx_log_level", "WARNING")
    level = getattr(logging, str(level_name).upper(), logging.WARNING)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(level)


@pytest.mark.asyncio
async def test_settings():
    Agently.set_settings("test", "test")
    assert Agently.settings["test"] == "test"


def test_agently_set_api_key_and_alias_mapping():
    original_api_key = Agently.settings.get("agently.api_key", None)
    try:
        Agently.set_api_key("official-key")
        assert Agently.settings["agently.api_key"] == "official-key"

        Agently.set_settings("agently_api_key", "official-key-alias")
        assert Agently.settings["agently.api_key"] == "official-key-alias"
    finally:
        Agently.set_settings("agently.api_key", original_api_key)


def test_agently_load_settings_file(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"

    config_path.write_text(
        yaml.safe_dump(
            {
                "test_main_package": {
                    "base_url": "${ENV.TEST_MAIN_PACKAGE_BASE_URL}",
                }
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text("TEST_MAIN_PACKAGE_BASE_URL=https://example.com\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MAIN_PACKAGE_BASE_URL", raising=False)

    Agently.load_settings("yaml_file", str(config_path), auto_load_env=True)

    assert Agently.settings["test_main_package.base_url"] == "https://example.com"


def test_agently_load_settings_refresh_httpx_log_level(tmp_path):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "httpx_log_level": "INFO",
                }
            }
        ),
        encoding="utf-8",
    )

    Agently.load_settings("yaml_file", str(config_path))

    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("httpcore").level == logging.INFO


def test_agently_set_debug_mapping_profiles():
    snapshot = _snapshot_runtime_log_settings()
    try:
        Agently.set_settings("debug", True)
        assert Agently.settings["debug"] == "simple"
        assert Agently.settings["runtime.show_model_logs"] == "simple"
        assert Agently.settings["runtime.show_tool_logs"] == "simple"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "simple"
        assert Agently.settings["runtime.show_runtime_logs"] == "simple"
        assert logging.getLogger("httpx").level == logging.WARNING

        Agently.set_settings("debug", "detail")
        assert Agently.settings["debug"] == "detail"
        assert Agently.settings["runtime.show_model_logs"] == "detail"
        assert Agently.settings["runtime.show_runtime_logs"] == "detail"
        assert logging.getLogger("httpx").level == logging.INFO

        Agently.set_settings("debug", False)
        assert Agently.settings["debug"] == "off"
        assert Agently.settings["runtime.show_model_logs"] == "off"
        assert Agently.settings["runtime.show_tool_logs"] == "off"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "off"
        assert Agently.settings["runtime.show_runtime_logs"] == "off"
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        _restore_runtime_log_settings(snapshot)


def test_agently_load_settings_applies_debug_mapping(tmp_path):
    snapshot = _snapshot_runtime_log_settings()
    try:
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.safe_dump({"debug": "detail"}), encoding="utf-8")

        Agently.load_settings("yaml_file", str(config_path))

        assert Agently.settings["debug"] == "detail"
        assert Agently.settings["runtime.show_model_logs"] == "detail"
        assert Agently.settings["runtime.show_tool_logs"] == "detail"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "detail"
        assert Agently.settings["runtime.show_runtime_logs"] == "detail"
        assert logging.getLogger("httpx").level == logging.INFO
    finally:
        _restore_runtime_log_settings(snapshot)
