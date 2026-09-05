"""Hard validation of an execution's assembled final value, without replay."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from agently.core.model.ModelRequestResultDataFlow import ModelRequestResultDataFlow
from agently.types.data import OutputValidateContext, OutputValidateHandler, OutputValidateResult

if TYPE_CHECKING:
    from .execution import AgentExecution


async def validate_final_output(
    execution: "AgentExecution", result: object, handlers: list[OutputValidateHandler],
) -> None:
    """Reuse ModelRequest's callback contract at the execution-owned boundary."""
    if not handlers:
        return
    value = (result.model_dump() if isinstance(result, BaseModel)
             else dict(result) if isinstance(result, Mapping) else {"value": result})
    for index, handler in enumerate(handlers):
        name = ModelRequestResultDataFlow.handler_name(handler, index)
        context = OutputValidateContext(
            value=value, agent_name=execution.agent.name, response_id="",
            attempt_index=0, retry_count=0, max_retries=0,
            prompt=execution.request.prompt, settings=execution.request.settings,
            request_run_context=execution.agent_execution_run_context, model_run_context=None,
            response_text=result if isinstance(result, str) else "",
            parsed_result=result, result_object=result if isinstance(result, BaseModel) else None,
            meta={"scope": "agent_execution_final", "execution_id": execution.id,
                  "validator_name": name},
        )
        try:
            raw = handler(value, context)
            if inspect.isawaitable(raw):
                raw = await raw
            outcome = ModelRequestResultDataFlow.normalize_validate_result(
                cast(OutputValidateResult, raw), validator_name=name,
            )
        except Exception as error:
            outcome = ModelRequestResultDataFlow.normalize_validate_error(error, validator_name=name)
        # No provider accounting is invented for a host-only final check.
        execution.diagnostics["validation"] = {
            "scope": "agent_execution_final", "passed": outcome["ok"],
            "validator_name": name, "completed": index + 1, "declared": len(handlers),
        }
        await execution.emit_stream(
            "validation.passed" if outcome["ok"] else "validation.failed",
            execution.diagnostics["validation"], source="agent_execution",
            route=execution.route_info.get("selected_route"),
        )
        if not outcome["ok"]:
            raise ModelRequestResultDataFlow.build_validation_failure_exception(outcome)
