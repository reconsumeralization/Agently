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

Uses ``flow.to()`` / ``flow.when()`` / ``data.emit_nowait()`` to drive the
reasoning loop. Bounded by a step budget with a stop condition (model sets
``final: true`` in its structured decision).
"""

from __future__ import annotations

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
    ``{next_action: ..., next_tool: ..., final: bool}``. The loop terminates
    when ``final=True`` or the step budget is exhausted.
    """
    allowed_tools = allowed_tools or []
    allowed_actions = allowed_actions or []

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

    # ── Start handler: init state, build prompt, kick off REASON ──
    async def start(data: Any) -> None:
        await data.async_set_state("task", task)
        await data.async_set_state("step_count", 0)
        await data.async_set_state("step_budget", step_budget)
        await data.async_set_state("observation_history", [])
        await data.async_set_state("model_key", model_key)
        data.emit_nowait("REASON")

    # ── Reason handler: model decides next action ──
    async def reason(data: Any) -> None:
        step_count = data.get_state("step_count", 0)
        history = data.get_state("observation_history", [])

        prompt = _build_react_prompt(
            task=data.get_state("task", task),
            step=step_count,
            history=history,
        )

        reason_block = ReasonBlock(
            model_key=data.get_state("model_key", model_key),
            output_format="json",
            stream_bridge=True,
        )

        try:
            decision = await reason_block.execute(prompt=prompt, context=context)
        except Exception as exc:
            decision = {"next_action": f"error: {exc}", "final": True}

        # Ensure decision is a dict
        if not isinstance(decision, dict):
            decision = {"next_action": str(decision), "final": False}

        await data.async_set_state("current_decision", decision)

        # If decision requires action, emit ACT; otherwise check for final
        next_tool = decision.get("next_tool")
        if next_tool:
            data.emit_nowait("ACT")
        else:
            data.emit_nowait("OBSERVE")

    # ── Act handler: execute the decided action ──
    async def act(data: Any) -> None:
        decision = data.get_state("current_decision", {})

        action_spec = {
            "type": decision.get("next_type", "tool"),
            "name": decision.get("next_tool", ""),
            "kwargs": decision.get("next_kwargs", {}) or {},
        }

        act_block = ActBlock(
            allowed_tools=set(allowed_tools),
            allowed_actions=set(allowed_actions),
            allow_scripts=allow_scripts,
            artifact_inline_limit=artifact_inline_limit,
            default_deny=True,
        )

        try:
            act_result = await act_block.execute(
                action_spec=action_spec,
                context=context,
            )
        except Exception as exc:
            act_result = {"name": action_spec.get("name", "unknown"), "error": str(exc)}

        await data.async_set_state("current_act_result", act_result)
        data.emit_nowait("OBSERVE")

    # ── Observe handler: fold result, check stop conditions ──
    async def observe(data: Any) -> None:
        decision = data.get_state("current_decision", {})
        act_result = data.get_state("current_act_result", {})
        step_count = data.get_state("step_count", 0) + 1
        budget = data.get_state("step_budget", step_budget)
        history = data.get_state("observation_history", [])

        await data.async_set_state("step_count", step_count)

        # Build observation
        observation: dict[str, Any] = {
            "name": act_result.get("name", decision.get("next_tool", "reason")),
            "result": act_result.get("result", decision.get("next_action", "")),
            "error": act_result.get("error"),
        }

        observe_block = ObserveBlock(artifact_inline_limit=artifact_inline_limit)
        folded = await observe_block.execute(
            observation=observation,
            context=context,
        )

        history.append(folded)
        await data.async_set_state("observation_history", history)

        # Check stop conditions
        is_final = decision.get("final", False)
        budget_exhausted = step_count >= budget

        if is_final or budget_exhausted:
            data.emit_nowait("FINALIZE")
        else:
            data.emit_nowait("REASON")

    # ── Finalize handler: assemble terminal output ──
    async def finalize(data: Any) -> None:
        history = data.get_state("observation_history", [])
        step_count = data.get_state("step_count", 0)
        budget = data.get_state("step_budget", step_budget)

        finalize_block = FinalizeBlock(model_key=data.get_state("model_key", model_key))
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
) -> str:
    """Build the react decision prompt with structured output instructions."""
    history_text = ""
    if history:
        history_text = "## Observation History\n" + "\n".join(
            f"- Step {i}: [{h.get('name', 'unknown')}] {str(h.get('result', ''))[:200]}"
            for i, h in enumerate(history)
        )

    return f"""## Task
{task}

## Instructions
You are in a reason→act→observe loop. At each step:
1. Decide the next action to take
2. Output a JSON object with:
   - "next_action": the action to take as a natural language instruction
   - "next_tool": tool name if a tool call is needed, otherwise null
   - "final": true if the task is complete, false to continue

{history_text}

## Current Step: {step + 1}

Respond with JSON only."""
