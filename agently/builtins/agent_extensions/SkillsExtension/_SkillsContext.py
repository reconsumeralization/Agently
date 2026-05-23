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

# ── Agent → Plugin context adapter ────────────────────────────────────────────
# AgentSkillsRuntimeContext bridges the Agent's internal API (settings, model
# requests, runtime stream emission) to the SkillsExecutor plugin protocols
# (SkillsPlanningContext / SkillsExecutionContext / SkillsRuntimeContext).
#
# This adapter pattern keeps the plugin implementation decoupled from concrete
# Agent internals. When other plugins need Agent-owned services, follow this
# pattern: define a context protocol in types/plugins/, implement the adapter
# here or in a sibling module, and provide a factory function.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from agently.types.plugins import SkillsRuntimeContext


class AgentSkillsRuntimeContext:
    """Agent component adapter passed into SkillsExecutor plugins."""

    def __init__(
        self,
        agent: Any,
        *,
        runtime_stream_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ):
        self.agent = agent
        self._runtime_stream_handler = runtime_stream_handler

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.agent.settings.get(key, default)

    async def async_request_model(
        self,
        *,
        prompt: Any,
        output_schema: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] = "auto",
        ensure_keys: list[str] | None = None,
        max_retries: int = 3,
        stream_handler: Callable[[Any], Awaitable[None] | None] | None = None,
    ) -> Any:
        request = self.agent.create_temp_request().input(prompt)
        if output_schema is not None:
            request = request.output(output_schema, format=output_format)
        response = request.get_response()
        if stream_handler is not None:
            async for item in response.get_async_generator(type="instant"):
                maybe_awaitable = stream_handler(item)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
        result = await response.async_get_data(
            ensure_keys=ensure_keys,
            max_retries=max(1, max_retries),
            raise_ensure_failure=False,
        )
        return result

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None:
        if self._runtime_stream_handler is None:
            return
        maybe_awaitable = self._runtime_stream_handler(item)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable


def create_agent_skills_runtime_context(
    agent: Any,
    *,
    runtime_stream_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> SkillsRuntimeContext:
    return AgentSkillsRuntimeContext(agent, runtime_stream_handler=runtime_stream_handler)
