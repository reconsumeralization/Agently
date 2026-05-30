import pytest
import yaml

from agently.utils import Settings
from agently.utils.ModelPool import resolve_model_pool_settings


def test_settings():
    root_settings = Settings()
    parent_settings = Settings(parent=root_settings)
    child_settings = Settings(parent=parent_settings)
    root_settings.set("test", 1)
    assert child_settings.get() == {"test": 1}


def test_settings_load_yaml_file_with_auto_env(tmp_path, monkeypatch):
    settings = Settings()
    settings.register_path_mappings("OpenAICompatible", "plugins.ModelRequester.OpenAICompatible")
    config_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"

    config_path.write_text(
        yaml.safe_dump(
            {
                "OpenAICompatible": {
                    "base_url": "${ENV.TEST_BASE_URL}",
                    "auth": "${ENV.TEST_API_KEY}",
                }
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text("TEST_BASE_URL=https://example.com/v1\nTEST_API_KEY=secret-key\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_BASE_URL", raising=False)
    monkeypatch.delenv("TEST_API_KEY", raising=False)

    settings.load("yaml_file", str(config_path), auto_load_env=True)

    assert settings["OpenAICompatible.base_url"] == "https://example.com/v1"
    assert settings["OpenAICompatible.auth"] == "secret-key"
    assert settings["plugins.ModelRequester.OpenAICompatible.base_url"] == "https://example.com/v1"
    assert settings["plugins.ModelRequester.OpenAICompatible.auth"] == "secret-key"


def test_settings_load_yaml_file_applies_path_mapping_without_auto_env(tmp_path):
    settings = Settings()
    settings.register_path_mappings("OpenAICompatible", "plugins.ModelRequester.OpenAICompatible")
    config_path = tmp_path / "settings.yaml"

    config_path.write_text(
        yaml.safe_dump({"OpenAICompatible": {"model": "deepseek-chat"}}),
        encoding="utf-8",
    )

    settings.load("yaml_file", str(config_path))

    assert settings["OpenAICompatible.model"] == "deepseek-chat"
    assert settings["plugins.ModelRequester.OpenAICompatible.model"] == "deepseek-chat"


def test_settings_load_yaml_file_applies_kv_mapping(tmp_path):
    settings = Settings()
    settings.register_kv_mappings(
        "profile",
        "prod",
        {"runtime.show_model_logs": "off", "runtime.httpx_log_level": "WARNING"},
    )
    config_path = tmp_path / "settings.yaml"

    config_path.write_text(yaml.safe_dump({"profile": "prod"}), encoding="utf-8")

    settings.load("yaml_file", str(config_path))

    assert settings["profile"] == "prod"
    assert settings["runtime.show_model_logs"] == "off"
    assert settings["runtime.httpx_log_level"] == "WARNING"


def test_settings_load_yaml_file_keep_placeholder_when_env_missing(tmp_path, monkeypatch):
    settings = Settings()
    config_path = tmp_path / "settings.yaml"

    config_path.write_text(
        yaml.safe_dump(
            {
                "OpenAICompatible": {
                    "auth": "${ENV.MISSING_API_KEY}",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSING_API_KEY", raising=False)

    settings.load("yaml_file", str(config_path), auto_load_env=True)

    assert settings["OpenAICompatible.auth"] == "${ENV.MISSING_API_KEY}"


def test_settings_load_yaml_file_raise_on_missing_env(tmp_path, monkeypatch):
    settings = Settings()
    config_path = tmp_path / "settings.yaml"

    config_path.write_text(
        yaml.safe_dump(
            {
                "OpenAICompatible": {
                    "auth": "${ENV.MISSING_API_KEY}",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSING_API_KEY", raising=False)

    with pytest.raises(KeyError, match="MISSING_API_KEY"):
        settings.load("yaml_file", str(config_path), auto_load_env=True, raise_empty=True)


def test_model_pool_unmapped_key_keeps_inherited_model():
    settings = Settings()
    settings.set("plugins.ModelRequester.OpenAICompatible.model", "deepseek-chat")

    resolve_model_pool_settings("reason", settings)

    assert settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-chat"


def test_model_pool_mapped_key_updates_model():
    settings = Settings()
    settings.set("plugins.ModelRequester.OpenAICompatible.model", "deepseek-chat")
    settings.set("model_pool", {"reason": "deepseek-reasoner"})

    resolve_model_pool_settings("reason", settings)

    assert settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-reasoner"
