# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

from typing import Any, Literal

from agently.types.data import SkillExecutionPlan
from agently.types.plugins import SkillsExecutionContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list

from ._utils import to_int


async def run_runtime_chain_strategy(
    *,
    executor: Any,
    context: SkillsExecutionContext,
    task: str,
    plan: SkillExecutionPlan,
    execution_id: str,
    runtime_stream: list[dict[str, Any]],
    skill_logs: list[dict[str, Any]],
    output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None = None,
    effort_config: dict[str, Any] | None = None,
    effort: str | None = None,
    strategy_name: str = "runtime_chain",
):
    del skill_logs, strategy_name
    ec = effort_config or {}
    phases = ["preflight", "research", "plan", "execute", "verify", "reflect", "finalize"]
    configured_phases = ec.get("chain_phases")
    if isinstance(configured_phases, list) and configured_phases:
        phases = [str(item) for item in configured_phases if str(item).strip()]
    retry_count = to_int(ec.get("retry_count"), 1 if effort == "normal" else 2 if effort == "max" else 0)
    phase_outputs: dict[str, Any] = {}
    selected_cards = [
        _ensure_dict(item).get("card") or _ensure_dict(item).get("decision_card") or {}
        for item in _ensure_list(plan.get("selected_skills"))
    ]
    selected_guidance = [
        {
            "skill_id": _ensure_dict(item).get("skill_id"),
            "display_name": _ensure_dict(item).get("display_name"),
            "path": _ensure_dict(_ensure_dict(item).get("guidance")).get("path", "SKILL.md"),
            "content": _ensure_dict(_ensure_dict(item).get("guidance")).get("content", ""),
        }
        for item in _ensure_list(plan.get("selected_skills"))
    ]
    resource_indexes = [
        executor._compact_resource_index(_ensure_dict(item).get("resource_index", {}))
        for item in _ensure_list(plan.get("selected_skills"))
    ]

    async def run_phase(
        phase: str,
        *,
        attempt: int = 0,
        schema: Any = None,
        ensure_keys: list[str] | None = None,
    ) -> Any:
        stage_key = executor._stage_key_for_phase(phase)
        model_key = executor._stage_model_key(plan, stage_key)
        await executor._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.runtime_chain.phase_start",
                "action": "start",
                "phase": phase,
                "attempt": attempt,
                "model_key": model_key,
            },
        )
        result = await context.async_request_model(
            prompt={
                "skills_runtime_phase": phase,
                "task": task,
                "attempt": attempt,
                "selected_skill_cards": selected_cards,
                "selected_skill_guidance": selected_guidance,
                "resource_indexes": resource_indexes,
                "prior_phase_outputs": _copy_public(phase_outputs),
                "instructions": executor._phase_instruction(phase),
            },
            model_key=model_key,
            output_schema=schema,
            output_format=executor._resolve_output_format(plan, output_format),
            ensure_keys=ensure_keys,
            max_retries=3,
        )
        await executor._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.runtime_chain.phase_done",
                "action": "done",
                "phase": phase,
                "attempt": attempt,
                "model_key": model_key,
            },
        )
        return result

    try:
        for phase in phases:
            if phase in {"execute", "verify", "reflect", "finalize"}:
                continue
            phase_outputs[phase] = await run_phase(phase)

        attempt = 0
        while True:
            phase_outputs["execute"] = await run_phase("execute", attempt=attempt)
            phase_outputs["verify"] = await run_phase(
                "verify",
                attempt=attempt,
                schema={
                    "passed": (bool, "True when the execution result satisfies the task and selected Skill guidance.", True),
                    "issues": ([str], "Specific gaps that require retry, if any."),
                    "reason": (str, "Concise verification reason."),
                },
                ensure_keys=["passed"],
            )
            verifier_result = _ensure_dict(phase_outputs.get("verify"))
            passed = bool(verifier_result.get("passed", True))
            phase_outputs["reflect"] = await run_phase("reflect", attempt=attempt)
            if passed or attempt >= retry_count:
                break
            attempt += 1

        phase_outputs["finalize"] = await run_phase(
            "finalize",
            schema=executor._output_schema(plan),
            ensure_keys=None,
        )
    except Exception as error:
        return executor._build_execution(
            execution_id=execution_id,
            status="error",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=[],
            output={"error": str(error), "phase_outputs": _copy_public(phase_outputs)},
            effort=effort,
            execution_mode="runtime_chain",
        )

    return executor._build_execution(
        execution_id=execution_id,
        status="success",
        plan=plan,
        runtime_stream=runtime_stream,
        skill_logs=[],
        output=_copy_public(phase_outputs["finalize"]),
        effort=effort,
        execution_mode="runtime_chain",
    )
