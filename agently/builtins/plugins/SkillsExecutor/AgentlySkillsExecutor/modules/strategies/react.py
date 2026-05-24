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

from agently.core.TriggerFlow import TriggerFlow
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
    allow_scripts: bool = False,
    artifact_inline_limit: int = 4096,
) -> dict[str, Any]:
    """Execute a skill using the react (reason→act→observe) strategy.

    The model receives a structured decision prompt and must output
    ``{next_action: ..., next_tool: ..., next_kwargs: ..., final: bool}``.
    The loop terminates when ``final=True`` or the step budget is exhausted.
    """
    allowed_tools = allowed_tools or []
    allowed_actions = allowed_actions or []
    action_concurrency = settings.get("skills.react_action_concurrency", None) if settings is not None else None
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
        await data.async_set_state("model_key", model_key)
        await data.async_emit("REASON")

    # ── Reason handler: model decides next action ──
    async def reason(data: Any) -> None:
        step_count = data.get_state("step_count", 0)
        history = data.get_state("observation_history", [])
        current_task = data.get_state("task", task)

        # Check budget before making a model call
        budget = data.get_state("step_budget", step_budget)
        if step_count >= budget:
            await data.async_emit("FINALIZE")
            return

        prompt = _build_react_prompt(
            task=current_task,
            step=step_count,
            history=history,
            allowed_tools=allowed_tools,
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

            observation: dict[str, Any] = {
                "name": obs_name,
                "result": obs_result,
                "error": act_result.get("error"),
            }

            folded = await observe_block.execute(
                observation=observation,
                context=context,
            )
            history.append(folded)

        await data.async_set_state("observation_history", history)

        # Clear per-step state for next iteration
        await data.async_set_state("current_decision", {})
        await data.async_set_state("current_act_result", {})
        await data.async_set_state("current_act_results", [])

        # Check stop conditions
        is_final = decision.get("final", False)
        budget_exhausted = step_count >= budget

        if is_final or budget_exhausted:
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

    parallel_hint = ""
    if allowed_tools and len(allowed_tools) > 1:
        parallel_hint = (
            "- \"next_actions\": array of {{next_tool, next_kwargs}} for parallel independent tool calls\n"
        )

    return f"""## Task
{task}

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
        "name": result.get("action_id") or result.get("tool_name") or spec.get("name", ""),
        "result": result.get("result", result.get("data", result)),
        "error": error,
        "action_result": result,
    }
