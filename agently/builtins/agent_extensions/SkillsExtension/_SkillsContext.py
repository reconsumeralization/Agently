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

"""Agent → Plugin context adapter for Skills Manager.

``AgentSkillsManagerContext`` bridges the Agent's internal API (settings, model
requests, runtime stream emission, ExecutionResource handle) to the
``SkillsManager`` plugin protocols (``SkillsPlanningContext`` /
``SkillsExecutionContext`` / ``SkillsRuntimeContext``).

This adapter pattern keeps the plugin implementation decoupled from concrete
Agent internals. When other plugins need Agent-owned services, follow this
pattern: define a context protocol in ``agently.types.plugins``, implement the
adapter here or in a sibling module, and provide a factory function that the
Agent extension calls when constructing the context for the plugin.
"""


from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from agently.types.data import ModelStreamingHandler, SkillRuntimeStreamHandler
from agently.types.plugins import SkillsRuntimeContext


class AgentSkillsManagerContext:
    """Agent component adapter passed into SkillsManager plugins."""

    def __init__(
        self,
        agent: Any,
        *,
        settings_overrides: Mapping[str, Any] | None = None,
        runtime_stream_handler: SkillRuntimeStreamHandler | None = None,
        resource_reader: Callable[
            [str, str, int], str | Awaitable[str]
        ] | None = None,
    ):
        self.agent = agent
        self._settings_overrides = dict(settings_overrides or {})
        self._runtime_stream_handler = runtime_stream_handler
        self._resource_reader = resource_reader

    @staticmethod
    def _get_overlay_value(source: Mapping[str, Any], key: str) -> Any:
        if key in source:
            return source[key]
        current: Any = source
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return current

    def get_setting(self, key: str, default: Any = None) -> Any:
        value = self.agent.settings.get(key, default)
        override = self._get_overlay_value(self._settings_overrides, key)
        if isinstance(value, dict) and isinstance(override, Mapping):
            merged = dict(value)
            merged.update(dict(override))
            return merged
        if override is not None:
            return override
        return value

    async def async_request_model(
        self,
        *,
        prompt: Any,
        model_key: str | None = None,
        output_schema: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        ensure_keys: list[str] | None = None,
        max_retries: int = 3,
        stream_handler: ModelStreamingHandler | None = None,
    ) -> Any:
        request = self.agent.create_temp_request(model_key=model_key).input(self._normalize_model_prompt(prompt))
        if output_schema is not None:
            request = request.output(output_schema, format=output_format)
        result_handle = request.get_result()
        if stream_handler is not None:
            async for item in result_handle.get_async_generator(type="instant"):
                maybe_awaitable = stream_handler(item)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
        result = await result_handle.async_get_data(
            ensure_keys=ensure_keys,
            max_retries=max(1, max_retries),
            raise_ensure_failure=False,
        )
        return result

    def _normalize_model_prompt(self, prompt: Any) -> Any:
        if isinstance(prompt, str):
            return prompt
        return json.dumps(prompt, ensure_ascii=False, default=str)

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None:
        if self._runtime_stream_handler is None:
            return
        maybe_awaitable = self._runtime_stream_handler(item)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    # ── Acting surface ──

    async def async_call_tool(self, name: str, /, **kwargs: Any) -> Any:
        return await self.agent.action.async_call_tool(name, kwargs)

    async def async_call_action(self, name: str, /, **kwargs: Any) -> Any:
        return await self.agent.action.async_call_action(name, kwargs)

    async def async_execute_action_specs(
        self,
        action_specs: list[dict[str, Any]],
        *,
        concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute Skills action specs through the agent's ActionRuntime."""
        action_calls: list[dict[str, Any]] = []
        for spec in action_specs:
            if not isinstance(spec, dict):
                continue
            action_name = str(spec.get("name", "")).strip()
            if not action_name:
                continue
            action_kwargs = spec.get("kwargs", {}) or {}
            if not isinstance(action_kwargs, dict):
                action_kwargs = {}
            action_calls.append(
                {
                    "action_id": action_name,
                    "tool_name": action_name,
                    "action_input": action_kwargs,
                    "tool_kwargs": action_kwargs,
                    "purpose": str(spec.get("purpose") or spec.get("next_action") or f"Use {action_name}"),
                    "source_protocol": "skills_react",
                }
            )

        if not action_calls:
            return []

        async def _use_prebuilt_action_calls(_context: dict[str, Any], _request: dict[str, Any]):
            return {
                "next_action": "execute",
                "use_action": True,
                "action_calls": action_calls,
                "tool_commands": action_calls,
                "execution_commands": action_calls,
            }

        results = await self.agent.action.async_plan_and_execute(
            prompt=self.agent.request.prompt,
            settings=self.agent.settings,
            action_list=self.agent.action.get_action_list(),
            agent_name=self.agent.name,
            planning_handler=_use_prebuilt_action_calls,
            max_rounds=1,
            concurrency=concurrency,
        )
        requested_action_ids = {str(call.get("action_id") or "") for call in action_calls}
        return [
            dict(item)
            for item in results
            if str(item.get("action_id") or item.get("tool_name") or "") in requested_action_ids
        ]

    async def async_execute_action_round(
        self,
        *,
        prompt: Any,
        allowed_tools: list[str] | None = None,
        allowed_actions: list[str] | None = None,
        concurrency: int | None = None,
        max_rounds: int = 1,
        planning_protocol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delegate one react reason+act round to the Agent ActionRuntime.

        The ActionRuntime owns action/tool planning prompts, kwargs schemas,
        native tool-call support, execution, policy, and MCP-backed actions. The
        Skills react strategy supplies the loop context and allowed names only.
        """
        allowed_names = {
            str(name)
            for name in [*(allowed_tools or []), *(allowed_actions or [])]
            if str(name).strip()
        }
        agent_tag = f"agent-{ self.agent.name }"
        action_list = self.agent.action.get_action_list(tags=[agent_tag])
        if allowed_names:
            action_list = [
                item for item in action_list
                if str(item.get("action_id") or item.get("name") or "") in allowed_names
            ]
        if not action_list:
            return []

        request = self.agent.create_temp_request().input(prompt)
        results = await self.agent.action.async_plan_and_execute(
            prompt=request.prompt,
            settings=self.agent.settings,
            action_list=action_list,
            agent_name=self.agent.name,
            max_rounds=max(1, max_rounds),
            concurrency=concurrency,
            planning_protocol=planning_protocol,
        )
        return [dict(item) for item in results]

    # ── Progressive disclosure ──

    async def async_read_resource(
        self, *, skill_id: str, path: str, max_bytes: int = 262144
    ) -> str:
        if self._resource_reader is None:
            raise RuntimeError(
                "async_read_resource is not available: no resource_reader was "
                "provided when constructing the Skills runtime context."
            )
        result = self._resource_reader(skill_id, path, max_bytes)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── Execution environment ──

    @property
    def execution_resource(self) -> Any | None:
        return getattr(self.agent, "execution_resource", None)


def create_agent_skills_manager_context(
    agent: Any,
    *,
    settings_overrides: Mapping[str, Any] | None = None,
    runtime_stream_handler: SkillRuntimeStreamHandler | None = None,
    resource_reader: Callable[
        [str, str, int], str | Awaitable[str]
    ] | None = None,
) -> SkillsRuntimeContext:
    return AgentSkillsManagerContext(
        agent,
        settings_overrides=settings_overrides,
        runtime_stream_handler=runtime_stream_handler,
        resource_reader=resource_reader,
    )


AgentSkillsRuntimeContext = AgentSkillsManagerContext
create_agent_skills_runtime_context = create_agent_skills_manager_context
