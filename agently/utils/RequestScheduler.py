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

"""Optional per-provider model request scheduling.

A ``RequestScheduler`` bounds how fast and how concurrently model requests are
issued to a provider, so several long-running tasks do not self-amplify into a
provider rate-limit storm. It also exposes a clamped exponential backoff helper
for retry paths.

Scheduling is opt-in: with no configured concurrency or rate limit, ``slot(...)``
is a no-op and request behavior is unchanged. Concurrency/rate primitives are
keyed by ``(provider, running event loop)`` so a process-wide scheduler stays
correct when reused across different event loops (e.g. across tests).
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ProviderScheduleConfig:
    """Per-provider scheduling limits. None disables that dimension."""

    max_concurrency: int | None = None
    rate_per_second: float | None = None


class _TokenBucket:
    """Minimal token bucket: at most ``rate`` starts per second, burst 1."""

    def __init__(self, rate_per_second: float):
        self._min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval


@dataclass
class _LoopState:
    semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    buckets: dict[str, _TokenBucket] = field(default_factory=dict)


class RequestScheduler:
    def __init__(self) -> None:
        self._configs: dict[str, ProviderScheduleConfig] = {}
        # Keyed by id(running loop) so primitives never cross event loops.
        self._loop_states: dict[int, _LoopState] = {}

    def configure(
        self,
        provider: str,
        *,
        max_concurrency: int | None = None,
        rate_per_second: float | None = None,
    ) -> "RequestScheduler":
        provider = str(provider or "").strip()
        if not provider:
            raise ValueError("RequestScheduler.configure requires a non-empty provider name.")
        self._configs[provider] = ProviderScheduleConfig(
            max_concurrency=_positive_int_or_none(max_concurrency),
            rate_per_second=_positive_float_or_none(rate_per_second),
        )
        return self

    def configure_from_settings(self, provider: str, settings: Any) -> "RequestScheduler":
        """Read ``model_request.scheduler`` config for a provider from settings.

        Supports a global block and a per-provider override:
        ``model_request.scheduler.max_concurrency`` / ``.rate_per_second`` and
        ``model_request.scheduler.providers.<provider>.{max_concurrency,rate_per_second}``.
        """
        get = getattr(settings, "get", None)
        if not callable(get):
            return self
        max_concurrency: Any = get("model_request.scheduler.max_concurrency", None)
        rate_per_second: Any = get("model_request.scheduler.rate_per_second", None)
        providers = get("model_request.scheduler.providers", None)
        if isinstance(providers, dict) and provider in providers and isinstance(providers[provider], dict):
            override = providers[provider]
            max_concurrency = override.get("max_concurrency", max_concurrency)
            rate_per_second = override.get("rate_per_second", rate_per_second)
        if max_concurrency is None and rate_per_second is None:
            return self
        return self.configure(
            provider,
            max_concurrency=max_concurrency,
            rate_per_second=rate_per_second,
        )

    def is_active(self, provider: str) -> bool:
        config = self._configs.get(str(provider))
        return bool(config and (config.max_concurrency or config.rate_per_second))

    @asynccontextmanager
    async def slot(self, provider: str) -> AsyncIterator[None]:
        provider = str(provider)
        config = self._configs.get(provider)
        if config is None or not (config.max_concurrency or config.rate_per_second):
            yield
            return
        state = self._loop_state()
        if config.rate_per_second:
            bucket = state.buckets.get(provider)
            if bucket is None:
                bucket = _TokenBucket(config.rate_per_second)
                state.buckets[provider] = bucket
            await bucket.acquire()
        if config.max_concurrency:
            semaphore = state.semaphores.get(provider)
            if semaphore is None:
                semaphore = asyncio.Semaphore(config.max_concurrency)
                state.semaphores[provider] = semaphore
            async with semaphore:
                yield
        else:
            yield

    def _loop_state(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        key = id(loop)
        state = self._loop_states.get(key)
        if state is None:
            state = _LoopState()
            self._loop_states[key] = state
        return state

    @staticmethod
    def backoff_delay(
        attempt: int,
        *,
        base: float = 0.5,
        cap: float = 30.0,
        jitter: bool = True,
    ) -> float:
        """Clamped exponential backoff for retry attempt ``attempt`` (1-based)."""
        attempt = max(1, int(attempt))
        delay = min(cap, base * (2 ** (attempt - 1)))
        if jitter:
            # Full jitter: a value in [0, delay].
            delay = random.uniform(0, delay)
        return delay


def _positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _positive_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
