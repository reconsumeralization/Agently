# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

from typing import Any, Literal

from agently.types.data import SkillExecutionPlan
from agently.types.plugins import SkillsExecutionContext
from agently.utils.DataGuardian import _copy_public

from ..contexts import RuntimeStreamCaptureContext
from ..strategies import run_react_execution
from ._utils import to_int


async def run_react_strategy(
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
    strategy_name: str = "react",
):
    del skill_logs, output_format, strategy_name
    ec = effort_config or {}
    step_budget = to_int(ec.get("step_budget") or executor.registry.settings.get("skills.react_max_steps", 30), 30)
    model_key = str(ec.get("reason_key") or executor._stage_model_key(plan, "reason"))
    artifact_inline_limit = to_int(ec.get("artifact_inline_limit") or executor.registry.settings.get("skills.artifact_inline_limit", 65536), 65536)
    capture_context = RuntimeStreamCaptureContext(context, runtime_stream)

    try:
        result = await run_react_execution(
            task=task,
            plan=dict(plan),
            context=capture_context,
            settings=executor.registry.settings,
            step_budget=step_budget,
            model_key=model_key,
            allowed_tools=[],
            allowed_actions=[],
            allow_scripts=False,
            artifact_inline_limit=artifact_inline_limit,
        )
    except Exception as error:
        return executor._build_execution(
            execution_id=execution_id,
            status="error",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=[],
            output={"error": str(error)},
            effort=effort,
            execution_mode="react",
        )

    return executor._build_execution(
        execution_id=execution_id,
        status="success",
        plan=plan,
        runtime_stream=runtime_stream,
        skill_logs=[],
        output=_copy_public(result),
        effort=effort,
        execution_mode="react",
    )
