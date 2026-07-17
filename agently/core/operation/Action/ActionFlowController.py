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
        await self._action._async_emit_action_flow_observation(observation)

    async def async_execute_action_calls(
        self,
        *,
        action_calls: list[dict[str, Any]],
        settings: "Settings",
        agent_name: str = "Manual",
        parent_run_context=None,
        action_execution_handler: "ActionExecutionHandler | None" = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ) -> list[ActionResult]:
        """Execute a host-owned Action batch without entering an ActionLoop.

        Planning and iterative continuation belong to ``ActionFlow.async_run``.
        This internal path is for callers that already own complete ActionCall
        values. It deliberately keeps execution inside ActionRuntime and emits
        the same canonical per-Action lifecycle observations as an ActionFlow.
        """

        from agently.core.runtime.RuntimeContext import (
            bind_runtime_context,
            get_current_agent_execution_context,
            resolve_parent_run_context,
        )
        from agently.types.data import RunContext

        action = self._action
        normalized_calls: list[ActionCall] = []
        for index, raw_call in enumerate(action_calls):
            normalized = action._normalize_action_call(raw_call)
            if normalized is None:
                raise ValueError(f"action_calls[{index}] is not a valid ActionCall.")
            normalized_calls.append(normalized)
        if not normalized_calls:
            return []

        parent_run_context = resolve_parent_run_context(parent_run_context)
        batch_run = RunContext.create(
            run_kind="action",
            parent=parent_run_context,
            agent_name=agent_name,
            session_id=(
                str(settings.get("runtime.session_id"))
                if settings.get("runtime.session_id", None)
                else None
            ),
            meta={
                "action_count": len(normalized_calls),
                "action_type": "action_calls",
            },
        )
        parent_artifact_scope = action._artifact_scope_from_agent_execution_context(
            get_current_agent_execution_context(),
        )
        artifact_scope = parent_artifact_scope or action._artifact_scope_from_run_context(batch_run)
        owns_artifact_scope = artifact_scope.get("kind") == "action_run"
        action_runs = [
            batch_run.create_child(
                run_kind="action",
                meta={
                    "action_type": "tool",
                    "action_name": str(command.get("action_id") or "unknown"),
                    "command_index": command_index,
                },
            )
            for command_index, command in enumerate(normalized_calls)
        ]

        async def emit(
            kind: str,
            *,
            command_index: int,
            message: str,
            payload: dict[str, Any],
            level: str = "INFO",
            error: BaseException | None = None,
        ) -> None:
            await action._async_emit_action_flow_observation(
                {
                    "kind": kind,
                    "source": "ActionRuntime",
                    "level": level,
                    "message": message,
                    "payload": payload,
                    "error": error,
                    "run": action_runs[command_index],
                    "compat_event_family": None,
                }
            )

        async def async_call_scoped_action(name: str, kwargs: dict[str, Any]) -> Any:
            return await action._async_call_action_with_scope(
                name,
                kwargs,
                artifact_scope=artifact_scope,
            )

        resolved_execution_handler = action.action_runtime.resolve_execution_handler(
            action_execution_handler,
        )
        bounded_records: list[ActionResult] = []
        try:
            with bind_runtime_context(
                parent_run_context=batch_run,
                tool_phase_run_context=batch_run,
                settings=settings,
            ):
                for command_index, command in enumerate(normalized_calls):
                    action_id = str(command.get("action_id") or "unknown")
                    await emit(
                        "action_started",
                        command_index=command_index,
                        message=f"Action '{action_id}' started.",
                        payload={
                            "agent_name": agent_name,
                            "command_index": command_index,
                            "action_type": "tool",
                            "action_name": action_id,
                            "command": command,
                        },
                    )

                try:
                    raw_records = await resolved_execution_handler(
                        {
                            "prompt": None,
                            "settings": settings,
                            "agent_name": agent_name,
                            "round_index": 0,
                            "max_rounds": 0,
                            "done_plans": [],
                            "last_round_records": [],
                            "parent_run_context": parent_run_context,
                            "artifact_scope": artifact_scope,
                            "action": action,
                            "runtime": action.action_runtime,
                        },
                        {
                            "action_calls": normalized_calls,
                            "async_call_action": async_call_scoped_action,
                            "concurrency": concurrency,
                            "timeout": timeout,
                            "trusted_policy_overrides": {},
                        },
                    )
                except BaseException as error:
                    for command_index, command in enumerate(normalized_calls):
                        action_id = str(command.get("action_id") or "unknown")
                        await emit(
                            "action_failed",
                            command_index=command_index,
                            level="WARNING",
                            message=f"Action '{action_id}' failed.",
                            payload={
                                "agent_name": agent_name,
                                "command_index": command_index,
                                "action_type": "tool",
                                "action_name": action_id,
                            },
                            error=error,
                        )
                    raise

                records = action._normalize_execution_records(
                    raw_records,
                    normalized_calls,
                    artifact_scope=artifact_scope,
                )
                bounded_records = action._to_action_flow_return_records(records)
                execution_context = get_current_agent_execution_context()
                record_action_records = getattr(execution_context, "record_action_records", None)
                if callable(record_action_records):
                    record_action_records(
                        [
                            {
                                **record,
                                "command_index": min(record_index, len(action_runs) - 1),
                            }
                            for record_index, record in enumerate(bounded_records)
                        ],
                        source="ActionRuntime",
                    )

                for record_index, record in enumerate(bounded_records):
                    command_index = min(record_index, len(action_runs) - 1)
                    action_id = str(record.get("action_id") or record.get("tool_name") or "unknown")
                    status = str(record.get("status") or "").strip().lower()
                    if bool(record.get("success")):
                        kind = "action_completed"
                        level = "INFO"
                        message = f"Action '{action_id}' completed."
                    elif status == "approval_required":
                        kind = "action_approval_required"
                        level = "WARNING"
                        message = f"Action '{action_id}' requires approval."
                    elif status == "blocked":
                        kind = "action_blocked"
                        level = "WARNING"
                        message = f"Action '{action_id}' blocked."
                    else:
                        kind = "action_failed"
                        level = "WARNING"
                        message = f"Action '{action_id}' failed."
                    await emit(
                        kind,
                        command_index=command_index,
                        level=level,
                        message=message,
                        payload={
                            "agent_name": agent_name,
                            "record_index": record_index,
                            "command_index": command_index,
                            "action_type": "tool",
                            "action_name": action_id,
                            "record": record,
                        },
                    )
        finally:
            if owns_artifact_scope:
                action._release_artifact_scope(artifact_scope)

        if owns_artifact_scope:
            bounded_records = action._project_released_artifact_scope(
                bounded_records,
                artifact_scope,
            )
        return action.to_model_visible_records(
            action._to_action_flow_return_records(bounded_records)
        )

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
