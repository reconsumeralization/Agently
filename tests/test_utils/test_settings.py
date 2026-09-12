import pytest
import yaml
from typing import Any, cast
from typing_extensions import assert_type

from agently.types.data import ModelProfileResolution
from agently.types.settings import OpenAICompatibleSettings
from agently.utils import Settings, SettingsNamespace, resolve_model_profile
from agently.utils.ModelPool import resolve_api_key_failover, resolve_model_pool_settings


def test_settings():
    root_settings = Settings()
    parent_settings = Settings(parent=root_settings)
    child_settings = Settings(parent=parent_settings)
    root_settings.set("test", 1)
    assert child_settings.get() == {"test": 1}


def test_settings_accepts_typed_settings_model():
    settings = Settings()

    settings.set_settings(
        OpenAICompatibleSettings(
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key="typed-key",
        )
    )

    assert settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-chat"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.base_url") == "https://api.deepseek.com"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.api_key") == "typed-key"


def test_typed_settings_reject_unknown_fields():
    with pytest.raises(ValueError):
        OpenAICompatibleSettings.model_validate({"model": "deepseek-chat", "unknown": True})


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


def test_model_pool_unknown_key_fails_when_alias_pool_is_configured():
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("plugins.ModelRequester.OpenAICompatible.model", "inherited-model")
    settings.set("model_pool", {"known-model": "configured-model"})

    with pytest.raises(ValueError, match="Unknown model_key 'unknown-model'"):
        resolve_model_pool_settings("unknown-model", settings)
    with pytest.raises(ValueError, match="Unknown model_key 'unknown-model'"):
        resolve_model_profile("unknown-model", settings)

    assert settings.get("plugins.ModelRequester.activate") == "OpenAICompatible"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.model") == "inherited-model"


def test_resolve_model_profile_is_read_only_and_redacts_auth(monkeypatch):
    monkeypatch.setenv("PROFILE_KEY", "profile-secret")
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("model_pool", {"reasoning": "reasoning-profile"})
    settings.set(
        "model_profiles",
        {
            "reasoning-profile": {
                "provider": "OpenAICompatible",
                "model": "reasoning-model",
                "base_url": "https://provider.example/v1",
                "api_key_pool": "reasoning-keys",
            }
        },
    )
    settings.set(
        "api_key_pools",
        {
            "reasoning-keys": {
                "selection": {"strategy": "round_robin"},
                "keys": [{"id": "primary", "value": "${ENV.PROFILE_KEY}"}],
            }
        },
    )
    before = settings.get()

    resolved = resolve_model_profile("reasoning", settings)

    assert_type(resolved, ModelProfileResolution)
    assert resolved == {
        "model_key": "reasoning",
        "provider": "OpenAICompatible",
        "model": "reasoning-model",
        "base_url": "https://provider.example/v1",
        "full_url": None,
        "auth_present": True,
    }
    assert settings.get() == before
    assert "profile-secret" not in repr(resolved)


def test_resolve_model_profile_reports_inherited_single_model_settings():
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("plugins.ModelRequester.OpenAICompatible.model", "inherited-model")
    settings.set("plugins.ModelRequester.OpenAICompatible.base_url", "https://provider.example/v1")
    settings.set("plugins.ModelRequester.OpenAICompatible.api_key", "secret")

    resolved = resolve_model_profile("optional-stage-key", settings)

    assert resolved == {
        "model_key": "optional-stage-key",
        "provider": "OpenAICompatible",
        "model": "inherited-model",
        "base_url": "https://provider.example/v1",
        "full_url": None,
        "auth_present": True,
    }


def test_resolve_model_profile_reports_missing_env_auth(monkeypatch):
    monkeypatch.delenv("MISSING_PROFILE_KEY", raising=False)
    settings = Settings()
    settings.set("plugins.ModelRequester.OpenAICompatible.api_key", "inherited-secret")
    settings.set(
        "model_pool",
        {
            "reasoning": {
                "provider": "OpenAICompatible",
                "model": "reasoning-model",
                "api_key": "${ENV.MISSING_PROFILE_KEY}",
            }
        },
    )

    resolved = resolve_model_profile("reasoning", settings)

    assert resolved["auth_present"] is False


def test_resolve_model_profile_reports_missing_env_key_pool_auth(monkeypatch):
    monkeypatch.delenv("MISSING_POOL_KEY", raising=False)
    settings = Settings()
    settings.set("plugins.ModelRequester.OpenAICompatible.api_key", "inherited-secret")
    settings.set(
        "model_pool",
        {
            "reasoning": {
                "provider": "OpenAICompatible",
                "model": "reasoning-model",
                "api_key_pool": "reasoning-keys",
            }
        },
    )
    settings.set(
        "api_key_pools",
        {
            "reasoning-keys": {
                "selection": {"strategy": "fixed"},
                "keys": [{"id": "primary", "value": "${ENV.MISSING_POOL_KEY}"}],
            }
        },
    )

    resolved = resolve_model_profile("reasoning", settings)

    assert resolved["auth_present"] is False


def test_model_pool_mapped_key_updates_model():
    settings = Settings()
    settings.set("plugins.ModelRequester.OpenAICompatible.model", "deepseek-chat")
    settings.set("model_pool", {"reason": "deepseek-reasoner"})

    resolve_model_pool_settings("reason", settings)

    assert settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-reasoner"


def test_model_pool_profile_updates_provider_settings_and_key_pool(monkeypatch):
    monkeypatch.setenv("REASON_KEY_A", "key-a")
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("model_pool", {"reason": "deepseek-reason-profile"})
    settings.set(
        "model_profiles",
        {
            "deepseek-reason-profile": {
                "provider": "OpenAICompatible",
                "model": "deepseek-reasoner",
                "base_url": "https://api.deepseek.com",
                "api_key_pool": "deepseek-prod",
                "request_options": {"temperature": 0},
            }
        },
    )
    settings.set(
        "api_key_pools",
        {
            "deepseek-prod": {
                "strategy": "fixed",
                "keys": [{"id": "reason-a", "value": "${ENV.REASON_KEY_A}"}],
            }
        },
    )

    resolve_model_pool_settings("reason", settings)

    assert settings.get("plugins.ModelRequester.activate") == "OpenAICompatible"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-reasoner"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.base_url") == "https://api.deepseek.com"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.api_key") == "key-a"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.request_options") == {"temperature": 0}


def test_model_pool_profile_can_switch_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_KEY_A", "anthropic-key-a")
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("model_pool", {"long-context": "anthropic-prod"})
    settings.set(
        "model_profiles",
        {
            "anthropic-prod": {
                "provider": "AnthropicCompatible",
                "base_url": "https://api.anthropic.com/v1",
                "model": "claude-sonnet-4-20250514",
                "api_key_pool": "anthropic-prod",
                "max_tokens": 4096,
            }
        },
    )
    settings.set(
        "api_key_pools",
        {
            "anthropic-prod": {
                "strategy": "fixed",
                "keys": [{"id": "anthropic-a", "value": "${ENV.ANTHROPIC_KEY_A}"}],
            }
        },
    )

    resolve_model_pool_settings("long-context", settings)

    assert settings.get("plugins.ModelRequester.activate") == "AnthropicCompatible"
    assert settings.get("plugins.ModelRequester.AnthropicCompatible.model") == "claude-sonnet-4-20250514"
    assert settings.get("plugins.ModelRequester.AnthropicCompatible.base_url") == "https://api.anthropic.com/v1"
    assert settings.get("plugins.ModelRequester.AnthropicCompatible.api_key") == "anthropic-key-a"
    assert settings.get("plugins.ModelRequester.AnthropicCompatible.max_tokens") == 4096


def test_api_key_pool_selection_handler_can_choose_key(monkeypatch):
    monkeypatch.setenv("REASON_KEY_A", "key-a")
    monkeypatch.setenv("REASON_KEY_B", "key-b")
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("model_pool", {"reason": "deepseek-reason-profile"})
    settings.set(
        "model_profiles",
        {
            "deepseek-reason-profile": {
                "provider": "OpenAICompatible",
                "model": "deepseek-reasoner",
                "api_key_pool": "deepseek-prod",
            }
        },
    )

    def select_second_key(context):
        assert context.pool_id == "deepseek-prod"
        return context.keys[1]["id"]

    settings.set(
        "api_key_pools",
        cast(Any, {
            "deepseek-prod": {
                "selection": select_second_key,
                "keys": [
                    {"id": "reason-a", "value": "${ENV.REASON_KEY_A}"},
                    {"id": "reason-b", "value": "${ENV.REASON_KEY_B}"},
                ],
            }
        }),
    )

    resolve_model_pool_settings("reason", settings)

    assert settings.get("plugins.ModelRequester.OpenAICompatible.api_key") == "key-b"
    runtime = cast(dict[str, Any], settings.get("plugins.ModelRequester.OpenAICompatible._api_key_pool_runtime"))
    assert runtime["selected_key_id"] == "reason-b"


def test_api_key_pool_failover_try_next_updates_request_local_key(monkeypatch):
    monkeypatch.setenv("REASON_KEY_A", "key-a")
    monkeypatch.setenv("REASON_KEY_B", "key-b")
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("model_pool", {"reason": "deepseek-reason-profile"})
    settings.set(
        "model_profiles",
        {
            "deepseek-reason-profile": {
                "provider": "OpenAICompatible",
                "model": "deepseek-reasoner",
                "api_key_pool": "deepseek-prod",
            }
        },
    )
    settings.set(
        "api_key_pools",
        {
            "deepseek-prod": {
                "selection": {"strategy": "fixed"},
                "failover": {"strategy": "try_next", "max_attempts": 2, "retry_status_codes": [429]},
                "keys": [
                    {"id": "reason-a", "value": "${ENV.REASON_KEY_A}"},
                    {"id": "reason-b", "value": "${ENV.REASON_KEY_B}"},
                ],
            }
        },
    )
    resolve_model_pool_settings("reason", settings)
    plugin_settings = SettingsNamespace(settings, "plugins.ModelRequester.OpenAICompatible")

    decision = resolve_api_key_failover(
        plugin_settings,
        error=RuntimeError("rate limited"),
        status_code=429,
        response_text="rate limit",
        provider="OpenAICompatible",
    )

    assert decision.retry is True
    assert decision.key_id == "reason-b"
    assert decision.api_key == "key-b"
    assert settings.get("plugins.ModelRequester.OpenAICompatible.api_key") == "key-b"


def test_api_key_pool_failover_handler_can_raise_or_choose_specific_key(monkeypatch):
    monkeypatch.setenv("REASON_KEY_A", "key-a")
    monkeypatch.setenv("REASON_KEY_B", "key-b")
    settings = Settings()
    settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
    settings.set("model_pool", {"reason": "deepseek-reason-profile"})
    settings.set(
        "model_profiles",
        {
            "deepseek-reason-profile": {
                "provider": "OpenAICompatible",
                "model": "deepseek-reasoner",
                "api_key_pool": "deepseek-prod",
            }
        },
    )

    def choose_key(error, context):
        assert isinstance(error, RuntimeError)
        if context.status_code == 422:
            return "raise"
        return {"key_entry": context.keys[1]}

    settings.set(
        "api_key_pools",
        cast(Any, {
            "deepseek-prod": {
                "failover": {"handler": choose_key, "max_attempts": 2},
                "keys": [
                    {"id": "reason-a", "value": "${ENV.REASON_KEY_A}"},
                    {"id": "reason-b", "value": "${ENV.REASON_KEY_B}"},
                ],
            }
        }),
    )
    resolve_model_pool_settings("reason", settings)
    plugin_settings = SettingsNamespace(settings, "plugins.ModelRequester.OpenAICompatible")

    no_retry = resolve_api_key_failover(plugin_settings, error=RuntimeError("bad request"), status_code=422)
    retry = resolve_api_key_failover(plugin_settings, error=RuntimeError("quota"), status_code=429)

    assert no_retry.retry is False
    assert retry.retry is True
    assert retry.key_id == "reason-b"
