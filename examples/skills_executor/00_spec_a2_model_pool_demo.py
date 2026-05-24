"""Spec A2 demo: model pool + key pool resolution end-to-end.

Run:
    python examples/skills_executor/00_spec_a2_model_pool_demo.py

This demo validates the three-layer model pool + key pool resolution:
  1. model_key → model_pool → model_name
  2. model_name → key_pool_strategy → key_id → key_pool → api_key
  3. Backward compat: absent model_key uses global model + key

It makes a real model call to verify resolution works end-to-end.

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-chat
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

from agently import Agently
from agently.utils.ModelPool import resolve_model_pool_settings, _select_key


def _check_env():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing DEEPSEEK_API_KEY. Put it in your shell or .env before running this example."
        )
    return api_key


async def main():
    api_key = _check_env()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    default_model = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat")

    # ═══════════════════════════════════════════════════════════
    #  Set up global model (fallback / backward compat)
    # ═══════════════════════════════════════════════════════════
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": base_url,
            "model": default_model,
            "model_type": "chat",
            "auth": api_key,
        },
    )
    Agently.set_settings("debug", False)

    # ═══════════════════════════════════════════════════════════
    #  Create agent with model pool + key pool
    # ═══════════════════════════════════════════════════════════
    agent = Agently.create_agent("spec-a2-demo")

    agent.set_settings(
        "model_pool",
        {
            "reason": default_model,  # map "reason" → the default model
        },
    )
    agent.set_settings(
        "key_pool",
        {
            "primary": api_key,
        },
    )
    agent.set_settings(
        "key_pool_strategy",
        {
            default_model: {
                "mode": "fixed",
                "pool": ["primary"],
            },
        },
    )

    # ═══════════════════════════════════════════════════════════
    #  Demo 1: unit-level resolution tests (no model call)
    # ═══════════════════════════════════════════════════════════
    print("═" * 60)
    print("Demo 1: resolution unit tests (no model call)")
    print("═" * 60)

    # 1a. model_key → model_name
    from agently.utils.Settings import Settings
    test_settings = Settings(name="Test-Settings")
    test_settings.set("model_pool", {"reason": "deepseek-v4-pro", "reason_fast": "deepseek-v4-flash"})
    test_settings.set("key_pool", {"prod-1": "sk-key1", "prod-2": "sk-key2"})
    test_settings.set(
        "key_pool_strategy",
        {
            "deepseek-v4-pro": {"mode": "round_robin", "pool": ["prod-1", "prod-2"]},
            "deepseek-v4-flash": {"mode": "random", "pool": ["prod-1", "prod-2"]},
        },
    )
    test_settings.set("plugins.ModelRequester.activate", "OpenAICompatible")

    resolve_model_pool_settings("reason", test_settings)
    assert test_settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-v4-pro"
    print("  1a. model_key 'reason' → model_name 'deepseek-v4-pro': OK")

    # 1b. key_pool_strategy resolution
    key_id = _select_key("round_robin", ["prod-1", "prod-2"], "test-model", {"prod-1": "sk-a", "prod-2": "sk-b"})
    assert key_id in ("prod-1", "prod-2")
    print(f"  1b. round_robin selected: {key_id}: OK")

    key_id = _select_key("random", ["prod-1", "prod-2"], "test-model-2", {"prod-1": "sk-a", "prod-2": "sk-b"})
    assert key_id in ("prod-1", "prod-2")
    print(f"  1c. random selected: {key_id}: OK")

    key_id = _select_key("fixed", ["prod-1", "prod-2"], "test-model-3", {"prod-1": "sk-a", "prod-2": "sk-b"})
    assert key_id == "prod-1"
    print(f"  1d. fixed selected: {key_id}: OK")

    # 1e. env placeholder resolution
    from agently.utils.ModelPool import _resolve_env
    os.environ["_TEST_POOL_VAR"] = "test-resolved-value"
    result = _resolve_env("${ENV._TEST_POOL_VAR}")
    assert result == "test-resolved-value"
    print(f"  1e. env placeholder resolved: '{result}': OK")
    result = _resolve_env("${_TEST_POOL_VAR}")
    assert result == "test-resolved-value"
    print(f"  1f. bare placeholder resolved: '{result}': OK")
    del os.environ["_TEST_POOL_VAR"]

    # ═══════════════════════════════════════════════════════════
    #  Demo 2: backward compat — no model_key → global model
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 2: backward compat (no model_key)")
    print("═" * 60)

    agent.input("Reply with exactly 'OK' and nothing else.")
    result = await agent.async_get_text()
    print(f"  Response (global model, no model_key): '{result.strip()}'")
    assert result.strip() == "OK", f"Expected 'OK', got '{result.strip()}'"
    print("  backward compat: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 3: model_key via create_temp_request → real call
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 3: model_key via create_temp_request")
    print("═" * 60)

    # Manual pipe: create_temp_request → input → get_response → get_text
    request = agent.create_temp_request(model_key="reason")
    request.input("Reply with exactly 'OK' and nothing else.")
    response = request.get_response()
    result = await response.async_get_text()
    print(f"  Response (model_key='reason'): '{result.strip()}'")
    assert result.strip() == "OK", f"Expected 'OK', got '{result.strip()}'"
    print("  model_key via create_temp_request: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 4: model_key via async_request_model (Skills path)
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 4: model_key via async_request_model (Skills path)")
    print("═" * 60)

    from agently.builtins.agent_extensions.SkillsExtension._SkillsContext import (
        create_agent_skills_runtime_context,
    )

    context = create_agent_skills_runtime_context(agent)
    result = await context.async_request_model(
        prompt="Reply with exactly 'OK' and nothing else.",
        model_key="reason",
        output_format="flat_markdown",
    )
    print(f"  Response (model_key='reason' via Skills path): '{str(result).strip()}'")
    assert str(result).strip() == "OK", f"Expected 'OK', got '{str(result).strip()}'"
    print("  model_key via async_request_model: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 5: model_key with output_schema (structured output)
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 5: model_key with structured output")
    print("═" * 60)

    result = await context.async_request_model(
        prompt="Return a JSON object with a single key 'status' set to 'ok'.",
        model_key="reason",
        output_schema={"status": (str, "ok or error")},
        output_format="json",
    )
    print(f"  Response: {result}")
    assert result.get("status") == "ok", f"Expected status 'ok', got {result}"
    print("  model_key + structured output: OK")

    print()
    print("✅ Spec A2: model pool + key pool resolution validated end-to-end")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
