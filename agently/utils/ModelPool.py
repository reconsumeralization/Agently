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

Resolves a caller-provided ``model_key`` into concrete provider settings. The
current layered shape is::

    model_key -> model_pool -> model_profile -> api_key_pool -> key

Legacy ``model_pool``/``key_pool_strategy``/``key_pool`` settings remain
supported for compatibility.
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


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return dict(value) if isinstance(value, dict) else {}


def _resolve_api_key_pool(
    *,
    pool_id: str,
    api_key_pools: dict[str, Any],
    legacy_key_pool: dict[str, str],
) -> str | None:
    pool_config = _as_dict(api_key_pools.get(pool_id))
    if not pool_config:
        return None
    mode = str(pool_config.get("strategy") or pool_config.get("mode") or "fixed")
    raw_entries = pool_config.get("keys")
    if raw_entries is None:
        raw_entries = pool_config.get("pool", [])
    if not isinstance(raw_entries, list):
        return None

    key_values: dict[str, str] = {}
    key_ids: list[str] = []
    for index, raw_entry in enumerate(raw_entries):
        if isinstance(raw_entry, str):
            key_id = raw_entry
            raw_key = legacy_key_pool.get(raw_entry, raw_entry)
        else:
            entry = _as_dict(raw_entry)
            if not entry:
                continue
            key_id = str(entry.get("id") or entry.get("key_id") or entry.get("name") or f"{ pool_id }[{ index }]")
            raw_key = entry.get("value")
            if raw_key is None:
                raw_key = entry.get("api_key")
            if raw_key is None:
                raw_key = entry.get("auth")
            if raw_key is None:
                raw_key = legacy_key_pool.get(key_id)
        if raw_key is None:
            continue
        key_ids.append(key_id)
        key_values[key_id] = _resolve_env(str(raw_key))
    if not key_ids:
        return None
    selected_key_id = _select_key(mode, key_ids, f"api_key_pool:{ pool_id }", key_values)
    return key_values.get(selected_key_id)


def _resolve_legacy_api_key(
    *,
    model_name: str,
    key_pool: dict[str, str],
    key_pool_strategy: dict[str, dict[str, Any]],
) -> str | None:
    strategy = key_pool_strategy.get(model_name)
    if not strategy:
        return None
    mode = strategy.get("mode", "fixed")
    pool_ids: list[str] = strategy.get("pool", [])
    if not pool_ids:
        return None
    selected_key_id = _select_key(str(mode), pool_ids, model_name, key_pool)
    raw_key = key_pool.get(selected_key_id, "")
    return _resolve_env(raw_key) if raw_key else None


def _profile_from_model_pool(
    model_key: str,
    model_pool: dict[str, Any],
    model_profiles: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    model_ref = model_pool.get(model_key)
    if not model_ref:
        return None, {}
    if isinstance(model_ref, dict):
        inline_profile = dict(model_ref)
        return str(inline_profile.get("model") or model_key), inline_profile
    if isinstance(model_ref, str) and model_ref in model_profiles:
        profile = _as_dict(model_profiles.get(model_ref))
        return str(profile.get("model") or model_ref), profile
    return str(model_ref), {}


def resolve_model_pool_settings(model_key: str, settings: Any) -> None:
    """Resolve *model_key* into provider, model, profile fields, and API key.

    Reads the current layered ``model_pool``/``model_profiles``/
    ``api_key_pools`` shape, with fallback support for legacy ``key_pool`` and
    ``key_pool_strategy``. Injected values are written into the ModelRequester
    settings namespace that the selected provider already reads.

    This is a no-op when *model_key* is ``None`` or empty.
    """
    if not model_key:
        return

    model_pool: dict[str, Any] = settings.get("model_pool", {}) or {}
    model_profiles: dict[str, Any] = settings.get("model_profiles", {}) or {}
    api_key_pools: dict[str, Any] = settings.get("api_key_pools", {}) or {}
    key_pool: dict[str, str] = settings.get("key_pool", {}) or {}
    key_pool_strategy: dict[str, dict[str, Any]] = settings.get("key_pool_strategy", {}) or {}

    model_name, profile = _profile_from_model_pool(model_key, model_pool, model_profiles)
    if not model_name:
        return

    active_plugin = str(settings.get("plugins.ModelRequester.activate", "OpenAICompatible"))
    provider = str(profile.get("provider") or active_plugin)
    if provider != active_plugin:
        settings.set("plugins.ModelRequester.activate", provider)
    ns = f"plugins.ModelRequester.{provider}"

    strategy = key_pool_strategy.get(model_name)
    api_key: str | None = None
    if profile.get("api_key_pool"):
        api_key = _resolve_api_key_pool(
            pool_id=str(profile["api_key_pool"]),
            api_key_pools=api_key_pools,
            legacy_key_pool=key_pool,
        )
    if api_key is None:
        raw_profile_key = profile.get("api_key")
        if raw_profile_key is None:
            raw_profile_key = profile.get("auth") if isinstance(profile.get("auth"), str) else None
        if raw_profile_key is not None:
            api_key = _resolve_env(str(raw_profile_key))
    if api_key is None and strategy:
        api_key = _resolve_legacy_api_key(
            model_name=model_name,
            key_pool=key_pool,
            key_pool_strategy=key_pool_strategy,
        )

    settings.set(f"{ns}.model", model_name)
    for key, value in profile.items():
        if key in {"provider", "model", "api_key_pool", "api_key"}:
            continue
        if value is not None:
            settings.set(f"{ ns }.{ key }", value)
    if api_key is not None:
        settings.set(f"{ns}.api_key", api_key)
