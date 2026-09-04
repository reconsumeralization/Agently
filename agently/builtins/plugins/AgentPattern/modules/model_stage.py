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

from copy import deepcopy
from typing import Literal, TYPE_CHECKING

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from agently.core.model import ModelRequestResult
    from agently.types.plugins import AgentExecution


_OUTPUT_PROMPT_KEYS = frozenset({"output", "output_format", "ensure_all_keys"})


async def run_model_stage(
    execution: "AgentExecution",
    *,
    pattern: Literal["plan", "long_content"],
    stage: str,
    stage_input: object,
    stage_info: object,
    stage_instructions: list[str],
    output: object | None = None,
    preserve_external_output: bool = False,
) -> object:
    """Run one explicit ModelRequest under the owning AgentExecution.

    Pattern stages reuse the root request's model/settings/capability contract,
    but internal schemas never inherit the root output validator. Only a
    terminal stage that promises the caller's result may preserve those
    request-local extension handlers.
    """

    request = execution.agent.create_request(
        name=f"{execution.agent.name}-{pattern}-{stage}",
        inherit_agent_prompt=False,
        inherit_extension_handlers=True,
        model_key=getattr(execution.request, "_model_key", None),
    )
    local_settings = execution.request.settings.get(inherit=False)
    if isinstance(local_settings, dict):
        request.settings.update(deepcopy(local_settings))

    prompt_snapshot = deepcopy(dict(execution.prompt_snapshot))
    if not preserve_external_output:
        for key in _OUTPUT_PROMPT_KEYS:
            prompt_snapshot.pop(key, None)
    request.prompt.update(prompt_snapshot)
    request.prompt.set(
        "input",
        {
            "original_input": DataFormatter.sanitize(execution.prompt_snapshot.get("input")),
            "pattern_stage_input": DataFormatter.sanitize(stage_input),
        },
    )
    request.prompt.append(
        "info",
        {
            "agent_pattern": pattern,
            "pattern_stage": stage,
            "stage_information": DataFormatter.sanitize(stage_info),
        },
    )
    request.prompt.append(
        "instruct",
        {
            "agent_pattern_stage": [
                *stage_instructions,
                "Return only this stage's declared result; do not expose hidden chain-of-thought.",
            ]
        },
    )

    context_package = await execution.async_read_task_context(
        consumer_id=f"agent_pattern:{pattern}:{stage}:{execution.id}",
        phase=stage,
    )
    context_lanes: dict[str, list[dict[str, object]]] = {
        "instruct": [],
        "info": [],
        "examples": [],
    }
    for block in context_package.blocks:
        item = {
            "content": DataFormatter.sanitize(block.content),
            "role": block.role,
            "ref": block.source_ref,
            "completeness": block.completeness,
        }
        if block.role == "instruction":
            context_lanes["instruct"].append(item)
        elif block.role == "example":
            context_lanes["examples"].append(item)
        else:
            context_lanes["info"].append(item)
    for lane, items in context_lanes.items():
        if items:
            request.prompt.append(lane, {"task_context_blocks": items})

    if output is not None:
        request.output(output, format="json")
    if preserve_external_output:
        local_handlers = execution.request.extension_handlers.get(inherit=False)
        if isinstance(local_handlers, dict):
            request.extension_handlers.update(local_handlers)

    await execution.emit_stream(
        "pattern.stage.started",
        {"pattern": pattern, "stage": stage},
        route="agent_pattern",
        source="agent_pattern",
        meta={"pattern": pattern, "stage": stage},
    )
    result = request.get_result(
        parent_run_context=execution.agent_execution_run_context,
    )
    execution.record_model_response_id(result.id)
    try:
        value = (
            await result.async_get_data()
            if output is not None or bool(execution.prompt_snapshot.get("output"))
            else await result.async_get_text()
        )
    finally:
        accepted_result = result._accepted_retry_result or result
        if accepted_result is not result:
            execution.record_model_response_id(accepted_result.id)
        response_id = str(accepted_result.response_id or accepted_result.id)
        execution.record_context_consumption(
            context_package,
            request_id=response_id,
        )
    await _record_action_logs(
        execution,
        result._accepted_retry_result or result,
    )
    await execution.emit_stream(
        "pattern.stage.completed",
        {
            "pattern": pattern,
            "stage": stage,
            "response_id": str(accepted_result.response_id or accepted_result.id),
        },
        route="agent_pattern",
        source="agent_pattern",
        meta={"pattern": pattern, "stage": stage},
    )
    _record_stage_diagnostic(execution, pattern=pattern, stage=stage)
    return value


async def _record_action_logs(
    execution: "AgentExecution",
    result: "ModelRequestResult",
) -> None:
    full_result_data = result.full_result_data
    extra = (
        full_result_data.get("extra", {})
        if isinstance(full_result_data, dict)
        else {}
    )
    if not isinstance(extra, dict):
        return
    action_logs = extra.get("action_logs", [])
    if isinstance(action_logs, list):
        for item in action_logs:
            await execution.record_action_log(
                item,
                route="agent_pattern",
                source="action",
            )
    tool_logs = extra.get("tool_logs", [])
    if isinstance(tool_logs, list):
        for item in tool_logs:
            await execution.record_action_log(
                item,
                route="agent_pattern",
                source="tool",
            )


def _record_stage_diagnostic(
    execution: "AgentExecution",
    *,
    pattern: str,
    stage: str,
) -> None:
    current = execution.diagnostics.get("pattern_run", {})
    diagnostic = dict(current) if isinstance(current, dict) else {}
    stages = diagnostic.get("stages", [])
    normalized_stages = list(stages) if isinstance(stages, list) else []
    normalized_stages.append(stage)
    diagnostic.update(
        {
            "name": pattern,
            "model_request_count": int(diagnostic.get("model_request_count", 0)) + 1,
            "stages": normalized_stages,
        }
    )
    execution.diagnostics["pattern_run"] = diagnostic


__all__ = ["run_model_stage"]
