from typing import Any, cast

from agently.types.settings import OpenAICompatibleSettings
from agently.utils import Settings, resolve_model_profile
from agently.utils.ModelPool import resolve_model_pool_settings


settings = Settings()

settings.set_settings(
    OpenAICompatibleSettings(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-flash",
        api_key="${ENV.DEEPSEEK_API_KEY}",
        request_options={
            "thinking": {"type": "disabled"},
            "temperature": 0,
        },
    )
)

settings.set("model_pool", {"support-chat": "deepseek-v4-flash-prod"})
settings.set(
    "model_profiles",
    {
        "deepseek-v4-flash-prod": {
            "provider": "OpenAICompatible",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "api_key_pool": "deepseek-prod",
            "request_options": {
                "thinking": {"type": "disabled"},
                "temperature": 0,
            },
        }
    },
)
settings.set(
    "api_key_pools",
    {
        "deepseek-prod": {
            "strategy": "fixed",
            "keys": [{"id": "primary", "value": "example-key"}],
        }
    },
)

preflight = resolve_model_profile("support-chat", settings)
print("preflight_provider", preflight["provider"])
print("preflight_model", preflight["model"])
print("preflight_base_url", preflight["base_url"])
print("preflight_auth_present", preflight["auth_present"])

try:
    resolve_model_profile("support-caht", settings)
except ValueError:
    unknown_model_key_rejected = True
else:
    unknown_model_key_rejected = False
print("unknown_model_key_rejected", unknown_model_key_rejected)

resolve_model_pool_settings("support-chat", settings)

resolved = cast(dict[str, Any], settings.get("plugins.ModelRequester.OpenAICompatible", {}) or {})
print("provider", settings.get("plugins.ModelRequester.activate", "OpenAICompatible"))
print("model", resolved.get("model"))
print("base_url", resolved.get("base_url"))
print("api_key", resolved.get("api_key"))
print("temperature", resolved.get("request_options", {}).get("temperature"))

# Expected key output from this infrastructure-only configuration probe:
# preflight_provider OpenAICompatible
# preflight_model deepseek-v4-flash
# preflight_base_url https://api.deepseek.com/v1
# preflight_auth_present True
# unknown_model_key_rejected True
# provider OpenAICompatible
# model deepseek-v4-flash
# base_url https://api.deepseek.com/v1
# api_key example-key
# temperature 0
#
# This example does not call a model. It validates that typed settings remain
# dict-compatible, that read-only preflight does not expose a credential, that
# unknown configured aliases fail closed, and that
# model_pool -> model_profiles -> api_key_pools resolves into the provider
# namespace read by the active ModelRequester plugin.
