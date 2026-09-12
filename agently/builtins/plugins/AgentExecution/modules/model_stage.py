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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from agently.core.model import ModelRequestResult
    from .execution import AgentExecution


_OUTPUT_PROMPT_KEYS = frozenset({"output", "output_format", "ensure_all_keys"})


@dataclass(frozen=True)
class ModelStageResult:
    value: object
    request_id: str


async def run_model_stage(
    execution: "AgentExecution",
    *,
    producer: Literal["plan", "long_content", "long_task"],
    stage: str,
    stage_input: object,
    stage_info: object,
    stage_instructions: list[str],
    output: object | None = None,
    preserve_external_output: bool = False,
    inherit_extension_handlers: bool = True,
    prompt_projection: Mapping[str, object] | None = None,
    read_task_context: bool = True,
    ensure_long_output: bool = False,
) -> ModelStageResult:
    """Run one explicit ModelRequest under the owning AgentExecution.

    Execution stages reuse the root request's model/settings/capability contract,
    but stage schemas never inherit caller final-output validators.
    The outer execution validates the final returned value once.
    """

    request = execution.agent.create_request(
        name=f"{execution.agent.name}-{producer}-{stage}",
        inherit_agent_prompt=False,
        inherit_extension_handlers=inherit_extension_handlers,
        model_key=getattr(execution.request, "_model_key", None),
    )
    local_settings = execution.request.settings.get(inherit=False)
    if isinstance(local_settings, dict):
        request.settings.update(deepcopy(local_settings))

    prompt_snapshot = deepcopy(dict(execution.prompt_snapshot if prompt_projection is None else prompt_projection))
    if not preserve_external_output:
        for key in _OUTPUT_PROMPT_KEYS:
            prompt_snapshot.pop(key, None)
    request.prompt.update(prompt_snapshot)
    if prompt_projection is None:
        request.prompt.set(
            "input",
            {
                "original_input": DataFormatter.sanitize(execution.prompt_snapshot.get("input")),
                "execution_stage_input": DataFormatter.sanitize(stage_input),
            },
        )
        request.prompt.append(
            "info",
            {
                "execution_plugin": producer,
                "execution_stage": stage,
                "stage_information": DataFormatter.sanitize(stage_info),
            },
        )
        request.prompt.append(
            "instruct",
            {
                "agent_execution_stage": [
                    *stage_instructions,
                    "Return only this stage's declared result; do not expose hidden chain-of-thought.",
                ]
            },
        )

    context_package = (
        await execution.async_read_task_context(
            consumer_id=f"agent_execution:{producer}:{stage}:{execution.id}",
            phase=stage,
        )
        if read_task_context
        else None
    )
    context_lanes: dict[str, list[dict[str, object]]] = {
        "instruct": [],
        "info": [],
        "examples": [],
    }
    for block in context_package.blocks if context_package is not None else []:
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
    # StateData merges lists with the parent; None shadows inherited callbacks.
    request.extension_handlers.set("validate_handlers", None)

    delivery = None
    if ensure_long_output:
        from .long_output import LongOutputDelivery

        request.settings.set("$agent_execution.ensure_long_output", True)
        delivery = LongOutputDelivery(
            execution,
            request=request,
            ensure_keys=None,
            ensure_all_keys=None,
            validate_handler=None,
            key_style="dot",
            max_retries=3,
            raise_ensure_failure=True,
        )
        delivery.preflight()

    await execution.emit_stream(
        "execution.stage.started",
        {"plugin": producer, "stage": stage},
        route=execution._selected_route[0] if execution._selected_route else producer,
        source="agent_execution",
        meta={"plugin": producer, "stage": stage},
    )
    result = request.get_result(
        parent_run_context=execution.agent_execution_run_context,
    )
    execution.record_model_response_id(result.id)
    try:
        if delivery is not None:
            from .long_output import LongOutputError

            events = [item async for item in result.get_async_generator(type="instant")]
            if delivery.needs_continuation(await result.async_get_meta(), await result.async_get_text()):
                await delivery.accept_initial(result, streaming_events=events)
                value = await delivery.run_continuation_flow()
            else:
                delivery.validate_complete_carrier(await result.async_get_text())
                value = await result.async_get_data()
                if result._accepted_retry_result is not None:
                    if delivery.needs_continuation(
                        await result._accepted_retry_result.async_get_meta(),
                        await result._accepted_retry_result.async_get_text(),
                    ):
                        raise LongOutputError("A length-limited validation replacement cannot use the ordinary completion path.")
                    delivery.validate_complete_carrier(await result._accepted_retry_result.async_get_text())
        else:
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
        if context_package is not None:
            execution.record_context_consumption(context_package, request_id=response_id)
    await _record_action_logs(
        execution,
        result._accepted_retry_result or result,
    )
    await execution.emit_stream(
        "execution.stage.completed",
        {
            "plugin": producer,
            "stage": stage,
            "response_id": str(accepted_result.response_id or accepted_result.id),
        },
        route=execution._selected_route[0] if execution._selected_route else producer,
        source="agent_execution",
        meta={"plugin": producer, "stage": stage},
    )
    _record_stage_diagnostic(execution, producer=producer, stage=stage)
    return ModelStageResult(value=value, request_id=response_id)


async def _record_action_logs(
    execution: "AgentExecution",
    result: "ModelRequestResult",
) -> None:
    full_result_data = result.full_result_data
    extra = full_result_data.get("extra", {}) if isinstance(full_result_data, dict) else {}
    if not isinstance(extra, dict):
        return
    action_logs = extra.get("action_logs", [])
    if isinstance(action_logs, list):
        for item in action_logs:
            await execution.record_action_log(
                item,
                route=(
                    execution._selected_route[0]
                    if execution._selected_route
                    else execution.producer_route or execution.name
                ),
                source="action",
            )
    tool_logs = extra.get("tool_logs", [])
    if isinstance(tool_logs, list):
        for item in tool_logs:
            await execution.record_action_log(
                item,
                route=(
                    execution._selected_route[0]
                    if execution._selected_route
                    else execution.producer_route or execution.name
                ),
                source="tool",
            )


def _record_stage_diagnostic(
    execution: "AgentExecution",
    *,
    producer: str,
    stage: str,
) -> None:
    current = execution.diagnostics.get("execution_run", {})
    diagnostic = dict(current) if isinstance(current, dict) else {}
    stages = diagnostic.get("stages", [])
    normalized_stages = list(stages) if isinstance(stages, list) else []
    normalized_stages.append(stage)
    diagnostic.update(
        {
            "name": producer,
            "model_request_count": int(diagnostic.get("model_request_count", 0)) + 1,
            "stages": normalized_stages,
        }
    )
    execution.diagnostics["execution_run"] = diagnostic


__all__ = ["run_model_stage"]
