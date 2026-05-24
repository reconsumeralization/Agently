# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Model pool + key pool resolution.

Resolves a caller-provided ``model_key`` into a concrete model name and API key
by walking the three-layer agent settings configuration::

    model_key → model_pool → model_name → key_pool_strategy → key_id → key_pool → api_key

Every step falls back gracefully when the corresponding pool is absent or doesn't
contain the expected key, preserving full backward compatibility with single-model
setups. An unmapped model key is treated as "use the request's inherited model",
not as a concrete provider model name.
"""

from __future__ import annotations

import os
import random
import re
import threading
from typing import Any

_ENV_PLACEHOLDER = re.compile(r"\$\{\s*(?:ENV\.)?([^}]+?)\s*\}")

# Module-level state for key selection strategies (process-scoped).
_round_robin_counters: dict[str, int] = {}
_usage_counters: dict[str, int] = {}
_lock = threading.Lock()


def _resolve_env(value: str) -> str:
    """Resolve ``${ENV.VAR_NAME}`` placeholders against ``os.environ``."""

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return _ENV_PLACEHOLDER.sub(_replace, value)


def _select_key(mode: str, pool: list[str], model_name: str, key_pool: dict[str, str]) -> str:
    """Apply a key selection strategy and return a *key_id* from *pool*."""
    if not pool:
        raise ValueError("key_pool_strategy pool must not be empty")

    if mode == "fixed":
        return pool[0]

    if mode == "random":
        return random.choice(pool)

    with _lock:
        if mode == "round_robin":
            idx = _round_robin_counters.get(model_name, 0)
            selected = pool[idx % len(pool)]
            _round_robin_counters[model_name] = idx + 1
            return selected

        if mode == "least_used":
            selected = min(pool, key=lambda kid: _usage_counters.get(kid, 0))
            _usage_counters[selected] = _usage_counters.get(selected, 0) + 1
            return selected

    raise ValueError(f"Unknown key_pool_strategy mode: {mode!r}")


def resolve_model_pool_settings(model_key: str, settings: Any) -> None:
    """Resolve *model_key* → model name + API key and inject into *settings*.

    Reads ``model_pool``, ``key_pool``, and ``key_pool_strategy`` from
    *settings* (walking the full parent chain).  Injected values are written
    into *settings* at the paths that the active ``ModelRequester`` plugin
    already reads.

    This is a no-op when *model_key* is ``None`` or empty.
    """
    if not model_key:
        return

    model_pool: dict[str, str] = settings.get("model_pool", {}) or {}
    key_pool: dict[str, str] = settings.get("key_pool", {}) or {}
    key_pool_strategy: dict[str, dict[str, Any]] = settings.get("key_pool_strategy", {}) or {}

    # Step 1: model_key → model_name. If no mapping is configured, leave the
    # request's inherited provider settings untouched. Internal keys such as
    # "reason" must not leak to the provider as literal model names.
    model_name = model_pool.get(model_key)
    if not model_name:
        return

    # Determine the active ModelRequester plugin namespace
    active_plugin = str(settings.get("plugins.ModelRequester.activate", "OpenAICompatible"))
    ns = f"plugins.ModelRequester.{active_plugin}"

    # Step 2: model_name → key_pool_strategy → key_id → api_key
    strategy = key_pool_strategy.get(model_name)
    api_key: str | None = None
    if strategy:
        mode = strategy.get("mode", "fixed")
        pool_ids: list[str] = strategy.get("pool", [])
        if pool_ids:
            selected_key_id = _select_key(mode, pool_ids, model_name, key_pool)
            raw_key = key_pool.get(selected_key_id, "")
            if raw_key:
                api_key = _resolve_env(raw_key)

    # Step 3: Inject resolved values into request settings.
    # The ModelRequester plugin's SettingsNamespace reads exactly these paths.
    settings.set(f"{ns}.model", model_name)
    if api_key is not None:
        settings.set(f"{ns}.api_key", api_key)
