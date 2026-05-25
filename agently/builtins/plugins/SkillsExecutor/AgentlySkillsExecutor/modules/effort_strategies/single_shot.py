# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

from typing import Any, Literal

from agently.types.data import SkillExecutionPlan
from agently.types.plugins import SkillsExecutionContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list


async def run_single_shot_strategy(
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
    strategy_name: str = "single_shot",
):
    del effort_config, strategy_name
    prompt = executor._build_prompt(task=task, plan=plan)
    await executor._emit_runtime_item(
        context=context,
        runtime_stream=runtime_stream,
        item={
            "type": "skills.prompt_only.start",
            "action": "start",
            "skill_ids": [str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills"))],
        },
    )

    async def stream_model_item(item: Any):
        await executor._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.model_stream",
                "action": str(getattr(item, "event_type", "delta") or "delta"),
                "path": getattr(item, "path", None),
                "value": getattr(item, "value", None),
                "delta": getattr(item, "delta", None),
                "is_complete": bool(getattr(item, "is_complete", False)),
            },
        )

    try:
        effective_output_format = executor._resolve_output_format(plan, output_format)
        result = await context.async_request_model(
            prompt=prompt,
            output_schema=executor._output_schema(plan),
            output_format=effective_output_format,
            ensure_keys=None,
            max_retries=3,
            stream_handler=stream_model_item,
            model_key=executor._stage_model_key(plan, "finalizer"),
        )
    except Exception as error:
        return executor._build_execution(
            execution_id=execution_id,
            status="error",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=skill_logs,
            output={"error": str(error)},
            effort=effort,
            execution_mode="single_shot",
        )

    for selection in _ensure_list(plan.get("selected_skills")):
        selection_data = _ensure_dict(selection)
        skill_logs.append({
            "skill_id": selection_data.get("skill_id"),
            "status": "success",
            "execution_mode": "prompt_only",
            "guidance_path": _ensure_dict(selection_data.get("guidance")).get("path", "SKILL.md"),
            "decision_card_checksum": _ensure_dict(selection_data.get("decision_card")).get("checksum", ""),
        })
    await executor._emit_runtime_item(
        context=context,
        runtime_stream=runtime_stream,
        item={
            "type": "skills.prompt_only.done",
            "action": "done",
            "skill_ids": [str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills"))],
        },
    )
    return executor._build_execution(
        execution_id=execution_id,
        status="success",
        plan=plan,
        runtime_stream=runtime_stream,
        skill_logs=skill_logs,
        output=_copy_public(result),
        effort=effort,
        execution_mode="single_shot",
    )
