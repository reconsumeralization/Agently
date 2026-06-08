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


import inspect
from typing import Any, TYPE_CHECKING, cast

from agently.types.data import ActionCall, ActionExecutionRequest, ActionPlanningRequest, ActionResult, ActionRunContext
from agently.types.plugins import ActionExecutionHandler, ActionPlanningHandler

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.utils import Settings
    from .Action import Action


class ActionFlowController:
    def __init__(self, action: "Action"):
        self._action = action

    def create_action_runtime(self, plugin_name: str | None = None):
        action = self._action
        runtime_name = plugin_name
        if not isinstance(runtime_name, str) or runtime_name.strip() == "":
            runtime_name = str(action.settings["plugins.ActionRuntime.activate"])
        runtime_plugin = cast(type[Any], action.plugin_manager.get_plugin("ActionRuntime", runtime_name))
        return runtime_plugin(action=action, plugin_manager=action.plugin_manager, settings=action.settings)

    def create_named_action_runtime(self, plugin_name: str, **kwargs):
        action = self._action
        runtime_plugin = cast(type[Any], action.plugin_manager.get_plugin("ActionRuntime", plugin_name))
        return runtime_plugin(action=action, plugin_manager=action.plugin_manager, settings=action.settings, **kwargs)

    def create_action_flow(self, plugin_name: str | None = None):
        action = self._action
        flow_name = plugin_name
        if not isinstance(flow_name, str) or flow_name.strip() == "":
            flow_name = str(action.settings["plugins.ActionFlow.activate"])
        flow_plugin = cast(type[Any], action.plugin_manager.get_plugin("ActionFlow", flow_name))
        return flow_plugin(plugin_manager=action.plugin_manager, settings=action.settings)

    def create_named_action_flow(self, plugin_name: str, **kwargs):
        action = self._action
        flow_plugin = cast(type[Any], action.plugin_manager.get_plugin("ActionFlow", plugin_name))
        return flow_plugin(plugin_manager=action.plugin_manager, settings=action.settings, **kwargs)

    def set_loop_options(
        self,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ):
        action = self._action
        if max_rounds is not None:
            if not isinstance(max_rounds, int) or max_rounds < 0:
                raise ValueError("max_rounds must be an integer >= 0.")
            action.action_settings.set("loop.max_rounds", max_rounds)
            action.tool_settings.set("loop.max_rounds", max_rounds)
        if concurrency is not None:
            if not isinstance(concurrency, int) or concurrency <= 0:
                raise ValueError("concurrency must be an integer > 0.")
            action.action_settings.set("loop.concurrency", concurrency)
            action.tool_settings.set("loop.concurrency", concurrency)
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("timeout must be a number > 0.")
            action.action_settings.set("loop.timeout", float(timeout))
            action.tool_settings.set("loop.timeout", float(timeout))
        return action

    def register_action_planning_handler(self, handler: "ActionPlanningHandler | None"):
        self._action.action_runtime.register_action_planning_handler(handler)
        return self._action

    def register_action_execution_handler(self, handler: "ActionExecutionHandler | None"):
        self._action.action_runtime.register_action_execution_handler(handler)
        return self._action

    def resolve_planning_protocol(self, settings: "Settings", planning_protocol: str | None = None):
        return self._action.action_runtime.resolve_planning_protocol(settings, planning_protocol)

    async def default_structured_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        runtime = cast(Any, self._action.action_runtime)
        return await runtime._default_structured_planning_handler(context, request)

    async def default_native_tool_call_planning_handler(
        self,
        context: ActionRunContext,
        request: ActionPlanningRequest,
    ):
        runtime = cast(Any, self._action.action_runtime)
        return await runtime._default_native_tool_call_planning_handler(context, request)

    async def default_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        runtime = cast(Any, self._action.action_runtime)
        return await runtime._default_planning_handler(context, request)

    async def default_action_execution_handler(self, context: ActionRunContext, request: ActionExecutionRequest):
        runtime = cast(Any, self._action.action_runtime)
        return await runtime._default_action_execution_handler(context, request)

    async def async_generate_action_call(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        planning_handler: "ActionPlanningHandler | None" = None,
        done_plans: list[ActionResult] | None = None,
        last_round_records: list[ActionResult] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list[ActionCall]:
        return await self._action.action_runtime.async_generate_action_call(
            prompt=prompt,
            settings=settings,
            action_list=action_list,
            agent_name=agent_name,
            planning_handler=planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    async def async_generate_tool_command(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        tool_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        plan_analysis_handler: "ActionPlanningHandler | None" = None,
        done_plans: list[ActionResult] | None = None,
        last_round_records: list[ActionResult] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list[ActionCall]:
        return await self._action.action_runtime.async_generate_tool_command(
            prompt=prompt,
            settings=settings,
            tool_list=tool_list,
            agent_name=agent_name,
            plan_analysis_handler=plan_analysis_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    async def async_emit_action_flow_observation(self, observation: dict[str, Any]):
        from agently.core.runtime.RuntimeEvents import async_emit_action_flow_observation

        await async_emit_action_flow_observation(observation)

    async def async_plan_and_execute(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]] | None = None,
        tool_list: list[dict[str, Any]] | None = None,
        agent_name: str = "Manual",
        parent_run_context=None,
        planning_handler: "ActionPlanningHandler | None" = None,
        plan_analysis_handler: "ActionPlanningHandler | None" = None,
        action_execution_handler: "ActionExecutionHandler | None" = None,
        tool_execution_handler: "ActionExecutionHandler | None" = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
    ) -> list[ActionResult]:
        action = self._action
        resolved_action_list = action_list if isinstance(action_list, list) else tool_list if isinstance(tool_list, list) else []
        if len(resolved_action_list) == 0:
            return []

        selected_planning_handler = planning_handler if planning_handler is not None else plan_analysis_handler
        selected_execution_handler = (
            action_execution_handler if action_execution_handler is not None else tool_execution_handler
        )

        run_kwargs = {
            "action": action,
            "prompt": prompt,
            "settings": settings,
            "action_list": resolved_action_list,
            "agent_name": agent_name,
            "parent_run_context": parent_run_context,
            "planning_handler": action.action_runtime.resolve_planning_handler(selected_planning_handler),
            "execution_handler": action.action_runtime.resolve_execution_handler(selected_execution_handler),
            "max_rounds": max_rounds,
            "concurrency": concurrency,
            "timeout": timeout,
            "planning_protocol": planning_protocol,
        }
        try:
            accepts_runtime_observation_handler = (
                "runtime_observation_handler" in inspect.signature(action.action_flow.async_run).parameters
            )
        except (TypeError, ValueError):
            accepts_runtime_observation_handler = False
        if accepts_runtime_observation_handler:
            run_kwargs["runtime_observation_handler"] = self.async_emit_action_flow_observation

        return await action.action_flow.async_run(**run_kwargs)
