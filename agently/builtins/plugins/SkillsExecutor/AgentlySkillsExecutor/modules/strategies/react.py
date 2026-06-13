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

"""React execution strategy — reason→act→observe loop on TriggerFlow.

Uses ``flow.to()`` / ``flow.when()`` / ``data.async_emit()`` to drive the
reasoning loop with blocking event dispatch, ensuring sequential state
transitions. Bounded by a step budget with a stop condition (model sets
``final: true`` in its structured decision).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from agently.core.orchestration.TriggerFlow import TriggerFlow
from agently.builtins.blocks import (
    ReasonBlock,
    ActBlock,
    ObserveBlock,
    FinalizeBlock,
)
from agently.utils.Settings import Settings


async def run_react_execution(
    *,
    task: str,
    plan: dict[str, Any],
    context: Any,
    settings: Settings | None = None,
    step_budget: int = 30,
    model_key: str = "reason",
    allowed_tools: list[str] | None = None,
    allowed_actions: list[str] | None = None,
    required_actions: list[str] | None = None,
    allow_scripts: bool = False,
    artifact_inline_limit: int = 65536,
    action_concurrency: int | None = None,
    skill_prompt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a skill using the react (reason→act→observe) strategy.

    The model receives a structured decision prompt and must output
    ``{next_action: ..., next_tool: ..., next_kwargs: ..., final: bool}``.
    The loop terminates when ``final=True`` or the step budget is exhausted.
    """
    allowed_tools = allowed_tools or []
    allowed_actions = allowed_actions or []
    required_actions = _normalize_unique_strings(required_actions)
    if skill_prompt is None:
        skill_prompt = _skill_prompt_from_plan(plan)
    if action_concurrency is None and settings is not None:
        configured_action_concurrency = settings.get("skills.react_action_concurrency", None)
        action_concurrency = (
            configured_action_concurrency
            if isinstance(configured_action_concurrency, int) and configured_action_concurrency > 0
            else None
        )
    if not isinstance(action_concurrency, int) or action_concurrency <= 0:
        action_concurrency = None

    await context.async_emit_runtime_stream(
        {
            "type": "skills.react.start",
            "action": "start",
            "payload": {
                "strategy": "react",
                "step_budget": step_budget,
                "model_key": model_key,
                "allowed_tools": allowed_tools,
                "allowed_actions": allowed_actions,
                "required_actions": required_actions,
            },
        }
    )

    flow = TriggerFlow(name=f"react-skill-{uuid.uuid4().hex[:8]}")

    # ── Start handler: init state, kick off first REASON ──
    async def start(data: Any) -> None:
        await data.async_set_state("task", task)
        await data.async_set_state("step_count", 0)
        await data.async_set_state("step_budget", step_budget)
        await data.async_set_state("observation_history", [])
        await data.async_set_state("succeeded_required_actions", [])
        await data.async_set_state("model_key", model_key)
        await data.async_emit("REASON")

    # ── Reason handler: model decides next action ──
    async def reason(data: Any) -> None:
        step_count = data.get_state("step_count", 0)
        history = data.get_state("observation_history", [])
        current_task = data.get_state("task", task)
        succeeded_required_actions = _normalize_unique_strings(data.get_state("succeeded_required_actions", []))

        # Check budget before making a model call
        budget = data.get_state("step_budget", step_budget)
        if _required_actions_satisfied(required_actions, succeeded_required_actions) or step_count >= budget:
            await data.async_emit("FINALIZE")
            return

        if _can_delegate_action_round(context, allowed_tools, allowed_actions):
            try:
                action_records = await context.async_execute_action_round(
                    prompt=_build_action_runtime_prompt(
                        task=current_task,
                        step=step_count,
                        history=history,
                        skill_prompt=skill_prompt,
                        required_actions=required_actions,
                        completed_required_actions=succeeded_required_actions,
                    ),
                    allowed_tools=allowed_tools,
                    allowed_actions=allowed_actions,
                    concurrency=action_concurrency,
                    max_rounds=1,
                )
            except Exception as exc:
                action_records = [{"status": "error", "error": str(exc), "action_id": "action_runtime"}]

            await data.async_set_state(
                "current_decision",
                {
                    "next_action": "action_runtime_round",
                    "final": len(action_records) == 0,
                    "action_runtime": True,
                },
            )
            await data.async_set_state(
                "current_act_results",
                [_normalize_action_runtime_record(record) for record in action_records],
            )
            await context.async_emit_runtime_stream(
                {
                    "type": "skills.react.action_runtime_round",
                    "action": "done",
                    "payload": {
                        "step": step_count + 1,
                        "action_count": len(action_records),
                    },
                }
            )
            await data.async_emit("OBSERVE")
            return

        prompt = _build_react_prompt(
            task=current_task,
            step=step_count,
            history=history,
            allowed_tools=allowed_tools,
            skill_prompt=skill_prompt,
            required_actions=required_actions,
            completed_required_actions=succeeded_required_actions,
        )

        reason_block = ReasonBlock(
            model_key=data.get_state("model_key", model_key),
            output_format="json",
            stream_bridge=True,
        )

        # Use output_schema so the framework parses and validates the JSON response
        decision_schema = {
            "next_action": (str, "Natural language description of the next action."),
            "next_tool": ((str, type(None)), "Tool name if a tool call is needed, otherwise None."),
            "next_kwargs": ((dict, type(None)), "Dict of arguments for the tool, or None/empty dict."),
            "next_actions": ((list, type(None)), "Array of parallel tool calls, or None."),
            "next_type": ((str, type(None)), "Optional type hint: tool/action/script."),
            "final": (bool, "True if the task is complete."),
        }

        try:
            decision = await reason_block.execute(
                prompt=prompt,
                context=context,
                output_schema=decision_schema,
            )
        except Exception as exc:
            decision = {"next_action": f"error: {exc}", "final": True}

        # Parse JSON string responses (safety net if output_schema parsing didn't fire)
        if isinstance(decision, str):
            try:
                decision = json.loads(decision)
            except json.JSONDecodeError:
                decision = {"next_action": decision, "final": False}

        # Ensure decision is a dict
        if not isinstance(decision, dict):
            decision = {"next_action": str(decision), "final": False}

        await data.async_set_state("current_decision", decision)

        # Dispatch: parallel actions, single action, or direct observe
        next_actions = decision.get("next_actions")
        next_tool = decision.get("next_tool")
        if isinstance(next_actions, list) and next_actions:
            await data.async_emit("ACT")
        elif next_tool:
            await data.async_emit("ACT")
        else:
            await data.async_emit("OBSERVE")

    # ── Act handler: execute the decided action(s) ──
    async def act(data: Any) -> None:
        decision = data.get_state("current_decision", {})

        # Build action specs — single or parallel
        next_actions = decision.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            action_specs = _build_parallel_specs(next_actions, decision)
        else:
            action_specs = [_build_single_spec(decision)]

        act_block = ActBlock(
            allowed_tools=set(allowed_tools),
            allowed_actions=set(allowed_actions),
            allow_scripts=allow_scripts,
            artifact_inline_limit=artifact_inline_limit,
            default_deny=True,
        )

        async def _execute_one(spec: dict[str, Any]) -> dict[str, Any]:
            try:
                return await act_block.execute(action_spec=spec, context=context)
            except Exception as exc:
                return {"name": spec.get("name", "unknown"), "error": str(exc)}

        if len(action_specs) == 1:
            act_results = [await _execute_one(action_specs[0])]
        elif hasattr(context, "async_execute_action_specs") and all(
            spec.get("type", "tool") in {"tool", "action"} for spec in action_specs
        ):
            raw_results = await context.async_execute_action_specs(
                action_specs,
                concurrency=action_concurrency,
            )
            act_results = [
                _normalize_action_runtime_result(spec, raw)
                for spec, raw in zip(action_specs, raw_results)
            ]
        else:
            act_results = []
            for spec in action_specs:
                act_results.append(await _execute_one(spec))

        await data.async_set_state("current_act_results", act_results)
        await data.async_emit("OBSERVE")

    # ── Observe handler: fold result(s), check stop conditions ──
    async def observe(data: Any) -> None:
        decision = data.get_state("current_decision", {})
        act_results = data.get_state("current_act_results") or [data.get_state("current_act_result", {})]
        act_results = [r for r in act_results if isinstance(r, dict)]
        step_count = data.get_state("step_count", 0) + 1
        budget = data.get_state("step_budget", step_budget)
        history = data.get_state("observation_history", [])

        await data.async_set_state("step_count", step_count)

        observe_block = ObserveBlock(artifact_inline_limit=artifact_inline_limit)

        # Fold each action result into the observation history
        for act_result in act_results:
            obs_name = act_result.get("name") or decision.get("next_tool") or "reason"
            obs_result = act_result.get("result") if "result" in act_result else decision.get("next_action", "")
            obs_status = str(act_result.get("status") or "").strip()
            if not obs_status:
                obs_status = "failed" if act_result.get("error") else "success"

            observation: dict[str, Any] = {
                "action_id": act_result.get("action_id") or obs_name,
                "name": obs_name,
                "result": obs_result,
                "error": act_result.get("error"),
                "status": obs_status,
            }

            folded = await observe_block.execute(
                observation=observation,
                context=context,
            )
            history.append(folded)

        succeeded_required_actions = _normalize_unique_strings(data.get_state("succeeded_required_actions", []))
        if required_actions:
            for action_id in _succeeded_action_ids(act_results):
                if action_id in required_actions and action_id not in succeeded_required_actions:
                    succeeded_required_actions.append(action_id)
            await data.async_set_state("succeeded_required_actions", succeeded_required_actions)

        await data.async_set_state("observation_history", history)

        # Clear per-step state for next iteration
        await data.async_set_state("current_decision", {})
        await data.async_set_state("current_act_result", {})
        await data.async_set_state("current_act_results", [])

        # Check stop conditions
        is_final = decision.get("final", False)
        budget_exhausted = step_count >= budget
        required_satisfied = _required_actions_satisfied(required_actions, succeeded_required_actions)

        if is_final or required_satisfied or budget_exhausted:
            await data.async_emit("FINALIZE")
        else:
            await data.async_emit("REASON")

    # ── Finalize handler: assemble terminal output ──
    async def finalize(data: Any) -> None:
        history = data.get_state("observation_history", [])
        step_count = data.get_state("step_count", 0)
        budget = data.get_state("step_budget", step_budget)

        finalize_block = FinalizeBlock()
        result = await finalize_block.execute(
            context=context,
            collected_outputs={
                "history": history,
                "step_count": step_count,
                "budget_exhausted": step_count >= budget,
                "task": data.get_state("task", task),
            },
        )
        await data.async_set_state("result", result)

    # ── Wire the flow ──
    flow.to(start)
    flow.when("REASON").to(reason)
    flow.when("ACT").to(act)
    flow.when("OBSERVE").to(observe)
    flow.when("FINALIZE").to(finalize)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    state = await execution.async_close()

    result = state.get("result", {})

    await context.async_emit_runtime_stream(
        {
            "type": "skills.react.done",
            "action": "done",
            "payload": {
                "steps_executed": state.get("step_count", 0),
                "status": "success",
            },
        }
    )

    return result if isinstance(result, dict) else {"output": result}


def _build_react_prompt(
    *,
    task: str,
    step: int,
    history: list[dict[str, Any]],
    allowed_tools: list[str] | None = None,
    skill_prompt: dict[str, Any] | None = None,
    required_actions: list[str] | None = None,
    completed_required_actions: list[str] | None = None,
) -> str:
    """Build the react decision prompt with structured output instructions."""
    tools_section = ""
    if allowed_tools:
        tools_section = (
            "## Available Tools\n"
            + "\n".join(f"- {t}" for t in allowed_tools)
            + "\n\nWhen using a tool, pass arguments via `next_kwargs` as a dict.\n"
        )

    history_text = ""
    if history:
        history_text = "## Observation History\n" + "\n".join(
            f"- Step {i}: [{h.get('name', 'unknown')}] {str(h.get('result', ''))[:300]}"
            for i, h in enumerate(history[-10:])  # keep last 10 for context window
        )

    skill_context = ""
    if skill_prompt:
        skill_context = (
            "## Selected Skill Guidance Context\n"
            "Apply this selected SKILL.md guidance when planning and evaluating actions:\n"
            f"{json.dumps(skill_prompt, ensure_ascii=False, default=str)}\n\n"
        )

    parallel_hint = ""
    if allowed_tools and len(allowed_tools) > 1:
        parallel_hint = (
            "- \"next_actions\": array of {{next_tool, next_kwargs}} for parallel independent tool calls\n"
        )

    required_section = ""
    if required_actions:
        required_section = (
            "## Required Host Actions\n"
            f"- required_actions: {json.dumps(required_actions, ensure_ascii=False)}\n"
            f"- completed_required_actions: {json.dumps(completed_required_actions or [], ensure_ascii=False)}\n"
            "Prefer remaining required actions when they are necessary for the task. Mark final only after required actions are complete or no further action is possible.\n\n"
        )

    return f"""## Task
{task}

{skill_context}
{required_section}
{tools_section}
## Instructions
You are in a reason→act→observe loop. At each step:
1. Decide the next action to take
2. Output a JSON object with:
   - "next_action": natural language description of the action
   - "next_tool": tool name if a tool call is needed, otherwise null
   - "next_kwargs": dict of arguments for the tool, or empty dict {{}}
   - "final": true if the task is complete, false to continue
{parallel_hint}
{history_text}

## Current Step: {step + 1}

Respond with JSON only."""


def _can_delegate_action_round(
    context: Any,
    allowed_tools: list[str] | None,
    allowed_actions: list[str] | None,
) -> bool:
    if not callable(getattr(context, "async_execute_action_round", None)):
        return False
    return bool(allowed_tools or allowed_actions)


def _build_action_runtime_prompt(
    *,
    task: str,
    step: int,
    history: list[dict[str, Any]],
    skill_prompt: dict[str, Any] | None = None,
    required_actions: list[str] | None = None,
    completed_required_actions: list[str] | None = None,
) -> dict[str, Any]:
    completed_required_actions = _normalize_unique_strings(completed_required_actions)
    required_actions = _normalize_unique_strings(required_actions)
    remaining_required_actions = [
        action_id for action_id in required_actions
        if action_id not in completed_required_actions
    ]
    prompt: dict[str, Any] = {
        "task": task,
        "react_step": step + 1,
        "observation_history": history[-10:],
        "required_actions": required_actions,
        "completed_required_actions": completed_required_actions,
        "remaining_required_actions": remaining_required_actions,
        "instruction": (
            "Plan and execute the next useful action for this Skills react loop. "
            "When skill_context is present, apply its selected SKILL.md guidance. "
            "When remaining_required_actions is non-empty, prefer those actions in dependency order. "
            "Use available action schemas exactly. If no more action is needed, "
            "respond without action calls."
        ),
    }
    if skill_prompt:
        prompt["skill_context"] = skill_prompt
    return prompt


def _normalize_unique_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _required_actions_satisfied(
    required_actions: list[str],
    succeeded_actions: list[str],
) -> bool:
    if not required_actions:
        return False
    succeeded = set(succeeded_actions)
    return all(action_id in succeeded for action_id in required_actions)


def _succeeded_action_ids(records: list[dict[str, Any]]) -> list[str]:
    succeeded: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("error"):
            continue
        status = str(record.get("status") or "").strip().lower()
        if status and status not in {"success", "succeeded", "partial_success"}:
            continue
        action_id = str(record.get("action_id") or record.get("name") or record.get("tool_name") or "").strip()
        if action_id and action_id not in succeeded:
            succeeded.append(action_id)
    return succeeded


def _skill_prompt_from_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    selected = plan.get("selected_skills")
    if not isinstance(selected, list) or not selected:
        return None
    selected_skill_guidance: list[dict[str, Any]] = []
    resource_indexes: list[Any] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        guidance = item.get("guidance")
        if not isinstance(guidance, dict):
            guidance = {}
        selected_skill_guidance.append(
            {
                "skill_id": item.get("skill_id"),
                "display_name": item.get("display_name"),
                "path": guidance.get("path", "SKILL.md"),
                "content": guidance.get("content", ""),
            }
        )
        if "resource_index" in item:
            resource_indexes.append(item.get("resource_index"))
    if not selected_skill_guidance and not resource_indexes:
        return None
    return {
        "skills_execution_policy": [
            "Use the selected Skills as model-readable SKILL.md instructions.",
            "Use the full guidance content as the source of behavior, not Agently decision-card summaries.",
            "Do not claim bundled resources were executed unless an explicit Action or environment did so.",
        ],
        "selected_skill_guidance": selected_skill_guidance,
        "resource_indexes": resource_indexes,
        "expected_result_shape": plan.get("expected_result_shape", {}),
    }


def _build_single_spec(decision: dict[str, Any]) -> dict[str, Any]:
    """Build a single action spec from a decision dict."""
    tool_kwargs = decision.get("next_kwargs") or {}
    if not isinstance(tool_kwargs, dict):
        tool_kwargs = {}
    if not tool_kwargs:
        known_keys = {"next_action", "next_tool", "next_actions", "next_type", "next_kwargs", "final"}
        tool_kwargs = {
            k: v for k, v in decision.items()
            if k not in known_keys and v is not None
        }
    return {
        "type": decision.get("next_type", "tool"),
        "name": decision.get("next_tool", ""),
        "kwargs": tool_kwargs,
    }


def _build_parallel_specs(
    next_actions: list[dict[str, Any]],
    decision: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build action specs for parallel (fan-out) execution."""
    specs = []
    for item in next_actions:
        if not isinstance(item, dict):
            continue
        kwargs = item.get("next_kwargs") or {}
        if not isinstance(kwargs, dict):
            kwargs = {}
        specs.append({
            "type": item.get("next_type", decision.get("next_type", "tool")),
            "name": item.get("next_tool", ""),
            "kwargs": kwargs,
        })
    return specs


def _normalize_action_runtime_result(
    spec: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    status = result.get("status")
    error = result.get("error")
    if status not in {None, "success"} and not error:
        error = str(status)
    return {
        "act_type": spec.get("type", "tool"),
        "action_id": result.get("action_id") or result.get("tool_name") or spec.get("name", ""),
        "name": result.get("action_id") or result.get("tool_name") or spec.get("name", ""),
        "result": result.get("result", result.get("data", result)),
        "error": error,
        "status": status or ("failed" if error else "success"),
        "action_result": result,
    }


def _normalize_action_runtime_record(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    error = result.get("error")
    if status not in {None, "success"} and not error:
        error = str(status)
    return {
        "act_type": "action",
        "action_id": result.get("action_id") or result.get("tool_name") or "action_runtime",
        "name": result.get("action_id") or result.get("tool_name") or "action_runtime",
        "result": result.get("result", result.get("data", result)),
        "error": error,
        "status": status or ("failed" if error else "success"),
        "action_result": result,
    }
