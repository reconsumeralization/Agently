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

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, TYPE_CHECKING, cast

from agently.core.model.ModelRequestResultDataFlow import ModelRequestResultDataFlow
from agently.utils import DataFormatter, DataLocator

if TYPE_CHECKING:
    from .execution import AgentExecution
    from agently.types.data import OutputValidateHandler


async def run_model_request_route(
    execution: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"],
    ensure_keys: list[str] | None,
    ensure_all_keys: bool | None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
    key_style: Literal["dot", "slash"],
    max_retries: int,
    raise_ensure_failure: bool,
) -> Any:
    agent_execution_run_context = await execution._async_emit_agent_execution_started_once()
    prompt_bound_required_skills: list[dict[str, Any]] = []
    collect_prompt_bound_skills = getattr(execution.agent, "_prompt_bound_required_skill_records", None)
    if callable(collect_prompt_bound_skills):
        try:
            raw_prompt_bound_skills = collect_prompt_bound_skills()
            if isinstance(raw_prompt_bound_skills, list):
                prompt_bound_required_skills = [
                    DataFormatter.sanitize(item)
                    for item in raw_prompt_bound_skills
                    if isinstance(item, Mapping)
                ]
        except Exception:
            prompt_bound_required_skills = []
    if prompt_bound_required_skills:
        route_logs = execution.logs.setdefault("route_logs", {})
        if isinstance(route_logs, dict):
            route_logs["prompt_bound_skills"] = prompt_bound_required_skills
        await execution.emit_stream(
            "skills.prompt_bound",
            {"selected_skills": prompt_bound_required_skills},
            route="model_request",
            source="skills_manager",
            meta={"binding": "prompt_guidance"},
        )
    if ensure_all_keys is not None:
        execution.request.prompt.set("ensure_all_keys", ensure_all_keys)
    result = execution.request.get_result(parent_run_context=agent_execution_run_context)
    execution._model_request_result = result
    execution.record_model_response_id(result.id)
    stream_meta = {
        "response_id": result.response_id,
        "request_run_id": result.request_run_context.run_id if result.request_run_context is not None else None,
        "model_run_id": result.model_run_context.run_id if result.model_run_context is not None else None,
        "attempt_index": result.attempt_index,
    }
    has_structured_stream = bool(execution.prompt_snapshot.get("output"))
    if has_structured_stream:
        structured_completion_policies = _structured_stream_completion_policies(
            result,
            ensure_keys=ensure_keys,
            key_style=key_style,
        )
        async for item in result.get_async_generator(type="instant"):
            await execution.bridge_model_stream_item(
                item,
                route="model_request",
                meta=stream_meta,
            )
            if _structured_stream_snapshot_satisfies_policies(
                result.full_result_data,
                structured_completion_policies,
                key_style=key_style,
            ):
                await _close_structured_response_stream(result)
                break
    else:
        async for event, data in result.get_async_generator(type="all"):
            if event in {"action", "tool"}:
                await execution.record_action_log(
                    data,
                    route="model_request",
                    source="action" if event == "action" else "tool",
                )
            elif event == "delta":
                await execution.emit_stream(
                    "model.delta",
                    data,
                    route="model_request",
                    source="model_request",
                    delta=str(data),
                    event_type="delta",
                    is_complete=False,
                    meta={**stream_meta, "specific_event": "delta"},
                )
            elif event in {"reasoning_delta", "original_delta"}:
                await execution.emit_stream(
                    f"model.{event}",
                    data,
                    route="model_request",
                    source="model_request",
                    delta=str(data),
                    event_type="delta",
                    is_complete=False,
                    meta={**stream_meta, "specific_event": event},
                )
            elif event in {"tool_calls", "reasoning_done", "original_done"}:
                await execution.emit_stream(
                    f"model.{event}",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "specific_event": event},
                )
            elif event == "status":
                await execution.emit_stream(
                    "$status",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "field_path": "$status", "specific_event": "status"},
                )
            elif event == "done":
                await execution.emit_stream(
                    "model.text",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "specific_event": "done"},
                )
            elif event in {"meta", "extra", "error"}:
                await execution.emit_stream(
                    f"model.{event}",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "specific_event": event},
                )
    data = await result.async_get_data(
        type=type,
        ensure_keys=ensure_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
    )
    full_result_data = result.full_result_data
    extra = full_result_data.get("extra", {}) if isinstance(full_result_data, dict) else {}
    if isinstance(extra, dict):
        action_logs = extra.get("action_logs", [])
        if isinstance(action_logs, list):
            for log in action_logs:
                await execution.record_action_log(log, route="model_request", source="action")
        tool_logs = extra.get("tool_logs", [])
        if isinstance(tool_logs, list):
            for log in tool_logs:
                await execution.record_action_log(log, route="model_request", source="tool")
    capability_failure = await _required_action_failure(execution, route="model_request")
    if capability_failure is not None:
        execution.status = "blocked"
        execution.close_snapshot = {
            "status": "blocked",
            "route": "model_request",
            "required_capabilities": capability_failure,
        }
        execution.diagnostics.setdefault("required_capabilities", []).append(capability_failure)
        await execution.emit_stream(
            "route.required_capability.blocked",
            capability_failure,
            route="model_request",
            source="agent_execution",
            meta={"status": "blocked"},
        )
        return {
            "status": "blocked",
            "accepted": False,
            "artifact_status": "blocked",
            "reason": capability_failure["reason"],
            "final_response": (
                "Task encountered a blocking condition. "
                f"No complete final deliverable was accepted. Reason: {capability_failure['reason']}"
            ),
            "required_capabilities": capability_failure,
        }
    execution.close_snapshot = {"status": "success", "route": "model_request"}
    return data


def _structured_stream_completion_policies(
    result: Any,
    *,
    ensure_keys: list[str] | None,
    key_style: Literal["dot", "slash"],
) -> dict[str, Literal["presence", "not_null"]]:
    data_flow = getattr(result, "_data_flow", None)
    auto_policies: dict[str, Literal["presence", "not_null"]] = {}
    get_auto_policies = getattr(data_flow, "get_auto_ensure_policies", None)
    if callable(get_auto_policies):
        try:
            raw_auto_policies = get_auto_policies(key_style=key_style)
            if isinstance(raw_auto_policies, Mapping):
                auto_policies = {
                    str(key): cast(Literal["presence", "not_null"], value)
                    for key, value in raw_auto_policies.items()
                    if value in {"presence", "not_null"}
                }
        except Exception:
            auto_policies = {}
    if ensure_keys is None:
        active_keys = list(auto_policies)
    elif len(ensure_keys) == 0:
        active_keys = []
    else:
        active_keys = ModelRequestResultDataFlow.merge_ensure_keys(list(auto_policies), ensure_keys)
    if not active_keys:
        return {}
    policies = ModelRequestResultDataFlow.resolve_ensure_policies(active_keys, auto_policies)
    policies.update(_structured_stream_preferred_mapping_policies(result, key_style=key_style))
    return policies


def _structured_stream_preferred_mapping_policies(
    result: Any,
    *,
    key_style: Literal["dot", "slash"],
) -> dict[str, Literal["presence"]]:
    try:
        prompt_output = result.prompt.to_prompt_object().output
    except Exception:
        return {}
    if not isinstance(prompt_output, Mapping):
        return {}
    policies: dict[str, Literal["presence"]] = {}
    for key, value in prompt_output.items():
        if not key:
            continue
        field_shape = value[0] if isinstance(value, tuple) and value else value
        if field_shape is dict or isinstance(field_shape, Mapping):
            path = str(key) if key_style == "dot" else f"/{ key }"
            policies[path] = "presence"
    return policies


def _structured_stream_snapshot_satisfies_policies(
    full_result_data: Any,
    policies: Mapping[str, Literal["presence", "not_null"]],
    *,
    key_style: Literal["dot", "slash"],
) -> bool:
    if not policies:
        return False
    if not isinstance(full_result_data, Mapping):
        return False
    snapshot = full_result_data.get("parsed_result")
    if not isinstance(snapshot, (Mapping, Sequence)) or isinstance(snapshot, str | bytes | bytearray):
        return False
    empty = object()
    for path, policy in policies.items():
        located_value = DataLocator.locate_path_in_dict(snapshot, path, key_style, default=empty)
        if located_value is empty:
            return False
        if policy == "not_null" and not ModelRequestResultDataFlow.ensure_value_is_present(located_value):
            return False
    return True


async def _close_structured_response_stream(result: Any) -> None:
    response_parser = getattr(result, "_response_parser", None)
    close = getattr(response_parser, "async_close", None)
    if callable(close):
        await cast(Callable[[], Awaitable[Any]], close)()


async def _required_action_failure(execution: "AgentExecution", *, route: str) -> dict[str, Any] | None:
    required_actions = execution.required_action_ids()
    if not required_actions:
        return None
    action_logs = execution.logs.get("action_logs", [])
    if not isinstance(action_logs, list):
        action_logs = []
    statuses: dict[str, str] = {}
    for item in action_logs:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or item.get("id") or item.get("name") or "").strip()
        if not action_id:
            continue
        statuses[action_id] = str(item.get("status") or "").strip().lower()
    successful_statuses = {"success", "succeeded", "partial_success"}
    missing = [action_id for action_id in required_actions if action_id not in statuses]
    failed = [
        action_id
        for action_id in required_actions
        if action_id in statuses and statuses[action_id] not in successful_statuses
    ]
    if not missing and not failed:
        return None
    reason_bits = []
    if missing:
        reason_bits.append(f"missing required action evidence: {', '.join(missing)}")
    if failed:
        reason_bits.append(f"required action did not succeed: {', '.join(failed)}")
    return {
        "route": route,
        "required_actions": required_actions,
        "missing_actions": missing,
        "failed_actions": failed,
        "action_statuses": statuses,
        "reason": "; ".join(reason_bits),
    }


async def run_skills_route(execution: "AgentExecution", route_meta: dict[str, Any]) -> Any:
    mode = cast(Any, route_meta.get("mode", "model_decision"))
    task = execution.task_target()
    agent = cast(Any, execution.agent)
    execution_access_policy = execution.effective_options.get("access_control_policy", {})
    settings_overrides = (
        {"access_control_policy": dict(execution_access_policy)}
        if isinstance(execution_access_policy, Mapping)
        else {}
    )
    output = execution.prompt_snapshot.get("output")
    route_options = execution.route_options("skills")
    route_output_format = route_options.get("output_format")
    output_format = (
        str(route_output_format)
        if route_output_format is not None
        else execution.prompt_snapshot.get("output_format")
    )
    if route_output_format is not None:
        execution.record_consumed_option("routes.skills.output_format", output_format, owner="AgentlySkillsManager")
    effort = route_options.get("effort")
    if effort is not None:
        effort = str(effort)
        execution.record_consumed_option("routes.skills.effort", effort, owner="AgentlySkillsManager")
    plan = await agent.async_resolve_skills_plan(
        task,
        skills=route_meta.get("skills"),
        skills_packs=route_meta.get("skills_packs"),
        mode=mode,
        output=output,
        output_format=output_format,
        _settings_overrides=settings_overrides,
    )
    await execution.emit_stream(
        "route.skills.plan",
        plan,
        route="skills",
        source="skills_manager",
        meta={"status": plan.get("status")},
    )
    if not plan.get("selected_skills"):
        if mode == "required" or plan.get("status") in {"blocked", "rejected"}:
            async def bridge_runtime_stream(item: dict[str, Any]):
                await execution.bridge_task_dag_stream_item(item, route="skills")

            skills_execution = await agent.async_execute_skills_plan(
                task,
                plan=plan,
                output_format=output_format,
                stream_handler=bridge_runtime_stream,
                effort=effort,
                _settings_overrides=settings_overrides,
            )
            execution.raise_if_limit_exceeded()
            execution.close_snapshot = skills_execution.close_snapshot
            execution.logs["route_logs"] = dict(skills_execution.to_dict())
            execution.status = skills_execution.status
            return skills_execution.output
        await execution.emit_stream(
            "route.fallback",
            {"from": "skills", "to": "model_request", "reason": "no_matching_skill"},
            route="model_request",
        )
        return await run_model_request_route(
            execution,
            type="parsed",
            ensure_keys=None,
            ensure_all_keys=None,
            validate_handler=None,
            key_style="dot",
            max_retries=3,
            raise_ensure_failure=True,
        )

    async def bridge_runtime_stream(item: dict[str, Any]):
        await execution.bridge_task_dag_stream_item(item, route="skills")

    skills_execution = await agent.async_execute_skills_plan(
        task,
        plan=plan,
        output_format=output_format,
        stream_handler=bridge_runtime_stream,
        effort=effort,
        _settings_overrides=settings_overrides,
    )
    execution.raise_if_limit_exceeded()
    for log in skills_execution.skill_logs:
        await execution.emit_stream(
            f"skills.{ log.get('skill_id', 'skill') }",
            log,
            route="skills",
            source="skills_manager",
            stage_id=None,
        )
    for log in skills_execution.action_logs:
        await execution.record_action_log(log, route="skills", source="action")
    execution.close_snapshot = skills_execution.close_snapshot
    execution.logs["route_logs"] = dict(skills_execution.to_dict())
    execution.status = skills_execution.status
    return skills_execution.output
