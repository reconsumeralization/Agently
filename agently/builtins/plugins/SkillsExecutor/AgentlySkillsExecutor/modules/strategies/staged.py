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

"""Staged execution strategy — sequential step-by-step on TriggerFlow.

Uses ``flow.to()`` / ``flow.when()`` / ``data.emit_nowait()`` to run steps
sequentially, each driven by a ReasonBlock (± ReadBlock). Prior step outputs
are folded into subsequent prompts.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from agently.core.orchestration.TriggerFlow import TriggerFlow
from agently.builtins.blocks import ReasonBlock, ReadBlock, FinalizeBlock
from agently.utils.Settings import Settings


async def run_staged_execution(
    *,
    task: str,
    plan: dict[str, Any],
    context: Any,
    settings: Settings | None = None,
    step_budget: int = 12,
    model_key: str = "reason",
    semantic_outputs: dict[str, Any] | None = None,
    artifact_inline_limit: int = 65536,
    allow_escalation: bool = False,
    escalation_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Execute a skill using the staged strategy on TriggerFlow.

    *plan* must contain ``execution_stages`` (from frontmatter). Each stage
    dict has ``description`` (the ReasonBlock prompt) and optionally
    ``resources`` (list of ``{skill_id, path}`` for ReadBlock).

    When *allow_escalation* is True, the strategy detects tool-use requests in
    step outputs and dynamically escalates to react mode, carrying forward the
    accumulated step outputs as observation history.

    Returns the assembled output from FinalizeBlock.
    """
    stages = plan.get("execution_stages") or plan.get("stages") or plan.get("steps") or []
    if not stages:
        return {
            "status": "error",
            "error": "Staged strategy requires execution_stages in the plan.",
        }

    original_stage_count = len(stages)
    stages = stages[:step_budget]
    budget_exhausted = original_stage_count > len(stages)

    await context.async_emit_runtime_stream(
        {
            "type": "skills.staged.start",
            "action": "start",
            "payload": {
                "step_count": len(stages),
                "strategy": "staged",
                "model_key": model_key,
            },
        }
    )
    if budget_exhausted:
        await context.async_emit_runtime_stream(
            {
                "type": "skills.execution.budget_exhausted",
                "action": "abort",
                "payload": {
                    "reason": "step_budget_exhausted",
                    "strategy": "staged",
                    "active_phase": "plan",
                    "step_count": len(stages),
                    "step_budget": step_budget,
                    "total_steps": original_stage_count,
                },
            }
        )

    flow = TriggerFlow(name=f"staged-skill-{uuid.uuid4().hex[:8]}")

    # ── Initial handler: store config, kick off first step ──
    async def start(data: Any) -> None:
        await data.async_set_state("task", task)
        await data.async_set_state("stages", stages)
        await data.async_set_state("model_key", model_key)
        await data.async_set_state("artifact_inline_limit", artifact_inline_limit)
        await data.async_set_state("step_outputs", [])
        await data.async_set_state("step_index", 0)
        await data.async_set_state("step_budget", step_budget)
        await data.async_set_state("escalated_to_react", False)
        data.emit_nowait("STEP")

    # ── Step handler: run one ReasonBlock + optional ReadBlock ──
    async def run_step(data: Any) -> None:
        idx = data.get_state("step_index", 0)
        stages_list = data.get_state("stages", [])
        if idx >= len(stages_list):
            data.emit_nowait("FINALIZE")
            return

        stage = stages_list[idx]
        step_desc = stage.get("description", str(stage))
        step_outputs = data.get_state("step_outputs", [])

        await context.async_emit_runtime_stream(
            {
                "type": "skills.staged.step_start",
                "action": "start",
                "payload": {"step_index": idx, "total_steps": len(stages_list)},
            }
        )

        # Optional resource reads
        for res in stage.get("resources", []):
            read = ReadBlock(max_bytes=artifact_inline_limit)
            await read.execute(
                skill_id=res.get("skill_id", ""),
                path=res.get("path", ""),
                context=context,
            )

        prompt = _build_step_prompt(
            task=task,
            step=step_desc,
            step_index=idx,
            total_steps=len(stages_list),
            prior_outputs=step_outputs,
        )

        reason = ReasonBlock(
            model_key=data.get_state("model_key", model_key),
            stream_bridge=True,
        )

        try:
            step_result = await reason.execute(prompt=prompt, context=context)
        except Exception as exc:
            step_result = {"error": str(exc)}

        step_outputs.append({
            "step_index": idx,
            "description": step_desc,
            "output": step_result,
        })
        await data.async_set_state("step_outputs", step_outputs)
        await data.async_set_state("step_index", idx + 1)

        await context.async_emit_runtime_stream(
            {
                "type": "skills.staged.step_done",
                "action": "done",
                "payload": {"step_index": idx, "total_steps": len(stages_list)},
            }
        )

        # E2: Check for dynamic escalation to react
        if allow_escalation and _detect_tool_need(step_result, escalation_tools):
            await context.async_emit_runtime_stream(
                {
                    "type": "skills.staged.escalate",
                    "action": "escalate",
                    "payload": {
                        "from_strategy": "staged",
                        "to_strategy": "react",
                        "at_step": idx,
                        "reason": "Model output indicates tool use is needed.",
                    },
                }
            )
            data.emit_nowait("ESCALATE")
        else:
            data.emit_nowait("STEP")

    # ── Escalate handler: transition from staged to react ──
    async def escalate(data: Any) -> None:
        step_outputs = data.get_state("step_outputs", [])
        remaining_budget = data.get_state("step_budget", step_budget) - len(step_outputs)

        # Build observation history from prior step outputs
        history: list[dict[str, Any]] = []
        for so in step_outputs:
            history.append({
                "name": f"stage-{so.get('step_index', '?')}",
                "result": str(so.get("output", ""))[:artifact_inline_limit],
            })

        await context.async_emit_runtime_stream(
            {
                "type": "skills.staged.escalated",
                "action": "escalated",
                "payload": {
                    "history_carried": len(history),
                    "remaining_budget": max(1, remaining_budget),
                },
            }
        )

        from .react import run_react_execution

        react_result = await run_react_execution(
            task=task,
            plan=plan,
            context=context,
            settings=settings,
            step_budget=max(1, remaining_budget),
            model_key=data.get_state("model_key", model_key),
            allowed_tools=escalation_tools or [],
            artifact_inline_limit=artifact_inline_limit,
        )
        await data.async_set_state("result", react_result)
        await data.async_set_state("escalated_to_react", True)

    # ── Finalize handler: assemble terminal output ──
    async def finalize(data: Any) -> None:
        step_outputs = data.get_state("step_outputs", [])
        finalize_block = FinalizeBlock(
            model_key=data.get_state("model_key", model_key),
            semantic_outputs=semantic_outputs,
        )
        result = await finalize_block.execute(
            context=context,
            collected_outputs={"steps": step_outputs, "task": task},
        )
        await data.async_set_state("result", result)

    flow.to(start)
    flow.when("STEP").to(run_step)
    flow.when("ESCALATE").to(escalate)
    flow.when("FINALIZE").to(finalize)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    state = await execution.async_close()

    result = state.get("result", {})

    await context.async_emit_runtime_stream(
        {
            "type": "skills.staged.done",
            "action": "done",
            "payload": {
                "step_count": len(stages),
                "status": "success",
            },
        }
    )

    return result if isinstance(result, dict) else {"output": result}


def _build_step_prompt(
    *,
    task: str,
    step: str,
    step_index: int,
    total_steps: int,
    prior_outputs: list[dict[str, Any]],
) -> str:
    parts = [
        f"## Task\n{task}",
        f"## Step {step_index + 1} of {total_steps}\n{step}",
    ]
    if prior_outputs:
        parts.append(
            "## Prior Step Outputs\n"
            + "\n".join(
                f"Step {o['step_index'] + 1}: {str(o.get('output', ''))[:800]}"
                for o in prior_outputs[-3:]
            )
        )
    return "\n\n".join(parts)


_TOOL_REQUEST_PATTERNS = [
    re.compile(r"I need (?:to|a)\s+\w*\s*(?:search|look.?up|find|calculate|compute|fetch|query|retrieve|call|run|execute)", re.IGNORECASE),
    re.compile(r"(?:let me|I will|I'll|I must|I have to)\s+\w*\s*(?:search|look.?up|find|calculate|compute|fetch|query|retrieve)", re.IGNORECASE),
    re.compile(r"\"next_tool\"\s*:", re.IGNORECASE),
]


def _detect_tool_need(
    step_result: Any,
    escalation_tools: list[str] | None = None,
) -> bool:
    """Detect if a staged step output indicates tool use is needed."""
    text = str(step_result)
    if not text.strip():
        return False
    # Check if any tool name from escalation_tools appears in the output
    if escalation_tools:
        for tool_name in escalation_tools:
            if tool_name.lower() in text.lower():
                return True
    # Check regex patterns for tool request language
    for pattern in _TOOL_REQUEST_PATTERNS:
        if pattern.search(text):
            return True
    # Check if output contains JSON-like next_tool pattern
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("next_tool"):
            return True
    except (json.JSONDecodeError, TypeError):
        pass
    return False
