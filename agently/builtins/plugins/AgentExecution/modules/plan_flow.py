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

from functools import lru_cache

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, TypedDict, cast

from pydantic import BaseModel, Field

from agently.core.orchestration import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataFormatter

from .model_stage import run_model_stage

if TYPE_CHECKING:
    from agently.core.orchestration import TriggerFlowExecution
    from .execution import AgentExecution


_ANALYZE_EVENT = "agent_execution.plan.analyze"
_CLARIFY_EVENT = "agent_execution.plan.clarify"
_FINALIZE_EVENT = "agent_execution.plan.finalize"
_RUNTIME_RESOURCE = "agent_execution_plan_runtime"


class _PlanQuestionData(TypedDict):
    question: str
    why_needed: str


class _PlanReadinessData(TypedDict):
    plan_ready: bool
    planning_goal: str
    final_deliverable: str
    readiness_summary: str
    questions: list[_PlanQuestionData]


class _PlanClarificationData(TypedDict):
    questions: list[_PlanQuestionData]
    response: object


class _PlanQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=600)
    why_needed: str = Field(min_length=1, max_length=600)


class _PlanReadiness(BaseModel):
    plan_ready: bool
    planning_goal: str = Field(min_length=1, max_length=2_000)
    final_deliverable: str = Field(min_length=1, max_length=2_000)
    readiness_summary: str = Field(min_length=1, max_length=2_000)
    questions: list[_PlanQuestion] = Field(default_factory=list)


@dataclass(frozen=True)
class PlanExecutionConfig:
    max_questions_per_round: int = 3
    max_clarification_rounds: int = 3


class _PlanExecutionRuntime:
    def __init__(
        self,
        execution: "AgentExecution",
        config: PlanExecutionConfig,
    ) -> None:
        self.execution = execution
        self.config = config

    async def analyze(
        self,
        *,
        clarification_round: int,
        clarifications: list[_PlanClarificationData],
    ) -> _PlanReadinessData:
        value = await run_model_stage(
            self.execution,
            producer="plan",
            stage=f"readiness_{clarification_round + 1}",
            stage_input={
                "clarification_round": clarification_round,
                "clarifications": clarifications,
                **({"revision_feedback": self.execution._rework_feedback,
                    "previous_candidate": self.execution._revision_history[self.execution.revision - 1].result}
                   if self.execution.revision else {}),
            },
            stage_info={
                "max_questions_per_round": self.config.max_questions_per_round,
                "remaining_clarification_rounds": max(
                    0,
                    self.config.max_clarification_rounds - clarification_round,
                ),
            },
            stage_instructions=[
                "Determine whether the supplied request contains enough information to produce an actionable plan.",
                "Ask only for facts whose absence materially changes the plan; "
                "use explicit assumptions for ordinary optional preferences.",
                f"Return at most {self.config.max_questions_per_round} concise clarification questions.",
                "Set plan_ready=true only when no clarification question remains, and then return questions=[].",
                "Do not produce the final plan in this readiness stage.",
            ],
            output=_PlanReadiness,
        )
        return _normalize_readiness(
            value.value,
            max_questions=self.config.max_questions_per_round,
        )

    async def finalize(
        self,
        *,
        readiness: _PlanReadinessData,
        clarifications: list[_PlanClarificationData],
    ) -> object:
        result = await run_model_stage(
            self.execution,
            producer="plan",
            stage="final_plan",
            stage_input={
                "validated_readiness": readiness,
                "clarifications": clarifications,
                **({"revision_feedback": self.execution._rework_feedback,
                    "previous_candidate": self.execution._revision_history[self.execution.revision - 1].result}
                   if self.execution.revision else {}),
            },
            stage_info={
                "result_role": "terminal actionable plan",
                "structured_output_declared": bool(
                    self.execution.prompt_snapshot.get("output")
                ),
            },
            stage_instructions=[
                "Produce the actionable plan requested by the user; do not execute the planned deliverable.",
                "Ground the plan in the original request, supplied constraints, and explicit clarification replies.",
                "Make scope, assumptions, ordered work, dependencies, acceptance "
                "checks, risks, and human decision points concrete when relevant.",
                "Do not repeat the readiness questionnaire or claim that the planned work has already been completed.",
                "Honor the caller's structured output contract when one is declared; "
                "otherwise return readable Markdown.",
            ],
            preserve_external_output=True,
        )
        return result.value


def _require_runtime(data: TriggerFlowRuntimeData) -> _PlanExecutionRuntime:
    runtime = data.require_resource(_RUNTIME_RESOURCE)
    if not isinstance(runtime, _PlanExecutionRuntime):
        raise TypeError("Plan Execution TriggerFlow runtime resource is invalid.")
    return runtime


async def _initialize_plan(data: TriggerFlowRuntimeData) -> None:
    await data.async_set_state("clarification_round", 0, emit=False)
    runtime = _require_runtime(data)
    prior = runtime.execution._producer_state
    clarifications = prior.get("clarifications", []) if runtime.execution.revision else []
    await data.async_set_state("clarifications", clarifications, emit=False)
    await data.async_emit(_ANALYZE_EVENT, None)


async def _analyze_plan(data: TriggerFlowRuntimeData) -> _PlanReadinessData:
    runtime = _require_runtime(data)
    raw_round = data.get_state("clarification_round", 0)
    clarification_round = raw_round if isinstance(raw_round, int) else 0
    raw_clarifications = data.get_state("clarifications", [])
    clarifications = cast(
        list[_PlanClarificationData],
        [dict(item) for item in raw_clarifications if isinstance(item, Mapping)]
        if isinstance(raw_clarifications, list)
        else [],
    )
    readiness = await runtime.analyze(
        clarification_round=clarification_round,
        clarifications=clarifications,
    )
    await data.async_set_state("readiness", readiness, emit=False)
    return readiness


async def _route_readiness(data: TriggerFlowRuntimeData) -> None:
    runtime = _require_runtime(data)
    readiness = cast(
        _PlanReadinessData,
        dict(data.value) if isinstance(data.value, Mapping) else {},
    )
    if readiness.get("plan_ready") is True:
        await data.async_emit(_FINALIZE_EVENT, readiness)
        return
    raw_round = data.get_state("clarification_round", 0)
    clarification_round = raw_round if isinstance(raw_round, int) else 0
    if clarification_round >= runtime.config.max_clarification_rounds:
        raise RuntimeError(
            "Plan Execution exhausted its clarification safety limit before the request became plan-ready."
        )
    await data.async_emit(_CLARIFY_EVENT, readiness)


async def _request_clarification(data: TriggerFlowRuntimeData) -> object:
    from agently.base import execution_exchange

    runtime = _require_runtime(data)
    readiness = cast(
        _PlanReadinessData,
        dict(data.value) if isinstance(data.value, Mapping) else {},
    )
    questions = readiness.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Plan Execution can pause only with at least one validated clarification question.")
    raw_round = data.get_state("clarification_round", 0)
    clarification_round = raw_round if isinstance(raw_round, int) else 0
    exchange_payload = {
        "subject": "Plan clarification",
        "round": clarification_round + 1,
        "questions": DataFormatter.sanitize(questions),
        "readiness_summary": readiness.get("readiness_summary"),
    }
    routing = await execution_exchange.async_route(
        {
            "exchange_kind": "clarification",
            "audit_metadata": {
                "source": "AgentExecution:plan",
                "subject": "Plan clarification",
            },
        },
        settings=runtime.execution.request.settings,
    )
    routing = dict(routing or {})
    raw_channel_id = routing.get("channel_id")
    channel_id = str(raw_channel_id) if raw_channel_id is not None else None
    raw_provider_id = routing.get("provider_id")
    provider_id = str(raw_provider_id) if raw_provider_id is not None else None
    raw_hot_wait_timeout = routing.get("hot_wait_timeout")
    hot_wait_timeout = (
        float(raw_hot_wait_timeout)
        if isinstance(raw_hot_wait_timeout, (int, float))
        and not isinstance(raw_hot_wait_timeout, bool)
        else None
    )
    return await data.async_pause_for(
        type="exchange",
        exchange_kind="clarification",
        payload=exchange_payload,
        interrupt_id=f"plan-clarification-{clarification_round + 1}",
        resume_to="next",
        channel_id=channel_id,
        provider_id=provider_id,
        wait_mode=str(routing.get("wait_mode") or "connected"),
        hot_wait_timeout=hot_wait_timeout,
        cold_persistence_policy=str(
            routing.get("cold_persistence_policy") or "persist"
        ),
        request_payload_schema={
            "type": "object",
            "required": ["questions"],
            "properties": {"questions": {"type": "array"}},
        },
        response_payload_schema={
            "oneOf": [
                {"type": "object"},
                {"type": "array"},
                {"type": "string"},
            ]
        },
        audit_metadata={
            "source": "AgentExecution:plan",
            "subject": "Plan clarification",
            "clarification_round": clarification_round + 1,
        },
    )


async def _accept_clarification(data: TriggerFlowRuntimeData) -> None:
    response = DataFormatter.sanitize(data.value)
    if response is None or response == "" or response == [] or response == {}:
        raise ValueError("Plan Execution clarification response cannot be empty.")
    readiness = data.get_state("readiness", {})
    questions = readiness.get("questions", []) if isinstance(readiness, Mapping) else []
    raw_clarifications = data.get_state("clarifications", [])
    clarifications = cast(
        list[_PlanClarificationData],
        list(raw_clarifications) if isinstance(raw_clarifications, list) else [],
    )
    clarifications.append(
        {
            "questions": DataFormatter.sanitize(questions),
            "response": response,
        }
    )
    raw_round = data.get_state("clarification_round", 0)
    clarification_round = raw_round if isinstance(raw_round, int) else 0
    await data.async_set_state("clarifications", clarifications, emit=False)
    await data.async_set_state(
        "clarification_round",
        clarification_round + 1,
        emit=False,
    )
    await data.async_emit(_ANALYZE_EVENT, None)


async def _finalize_plan(data: TriggerFlowRuntimeData) -> None:
    runtime = _require_runtime(data)
    readiness = data.get_state("readiness", {})
    clarifications = data.get_state("clarifications", [])
    result = await runtime.finalize(
        readiness=cast(
            _PlanReadinessData,
            dict(readiness) if isinstance(readiness, Mapping) else {},
        ),
        clarifications=cast(
            list[_PlanClarificationData],
            [dict(item) for item in clarifications if isinstance(item, Mapping)]
            if isinstance(clarifications, list)
            else [],
        ),
    )
    await data.async_set_state("execution_result", result, emit=False)


@lru_cache(maxsize=1)
def _build_plan_flow() -> TriggerFlow[Any, Any, Any]:
    flow: TriggerFlow[Any, Any, Any] = TriggerFlow(name="agent-execution-plan")
    flow.to(_initialize_plan)
    flow.when(_ANALYZE_EVENT).to(_analyze_plan).to(_route_readiness)
    flow.when(_CLARIFY_EVENT).to(_request_clarification).to(_accept_clarification)
    flow.when(_FINALIZE_EVENT).to(_finalize_plan)
    return flow


async def _drive_connected_exchanges(
    parent_execution: "AgentExecution",
    flow_execution: "TriggerFlowExecution[Any, Any, Any]",
) -> None:
    from agently.base import execution_exchange

    while True:
        pending = flow_execution.get_pending_interrupts()
        if not pending:
            return
        interrupt = next(iter(pending.values()))
        interrupt_id = str(interrupt.get("id") or "")
        envelope = interrupt.get("external_wait_request")
        envelope = dict(envelope) if isinstance(envelope, Mapping) else {}
        views = DataFormatter.sanitize(
            execution_exchange.project_pending_exchanges(flow_execution)
        )
        normalized_views = views if isinstance(views, list) else []
        wait_mode = str(envelope.get("wait_mode") or "disconnected")
        if wait_mode == "disconnected":
            await parent_execution.execution_context.async_notify_exchange(
                "pending",
                normalized_views,
                meta={"execution": "plan", "interrupt_id": interrupt_id},
            )
            raise RuntimeError(
                "Plan Execution reached a durable clarification pause, but AgentExecution "
                "cannot yet return a resumable Execution handle. Configure connected interaction."
            )

        wait_task = asyncio.create_task(
            execution_exchange.async_hot_wait(flow_execution, interrupt)
        )
        await asyncio.sleep(0)
        await parent_execution.execution_context.async_notify_exchange(
            "pending",
            normalized_views,
            meta={"execution": "plan", "interrupt_id": interrupt_id},
        )
        resolved = await wait_task
        if not resolved:
            raise TimeoutError(
                "Plan Execution clarification timed out before a connected response arrived."
            )
        resolved_interrupt = flow_execution.get_interrupt(interrupt_id)
        resolved_views = (
            [
                DataFormatter.sanitize(
                    execution_exchange.project_exchange(
                        flow_execution.id,
                        dict(resolved_interrupt),
                    )
                )
            ]
            if isinstance(resolved_interrupt, Mapping)
            else []
        )
        await parent_execution.execution_context.async_notify_exchange(
            "resolved",
            resolved_views,
            meta={"execution": "plan", "interrupt_id": interrupt_id},
        )


async def run_plan_execution(
    execution: "AgentExecution",
    config: PlanExecutionConfig,
) -> object:
    if bool(getattr(execution, "_ensure_long_output_enabled", False)):
        raise ValueError(
            "Plan Execution cannot be combined with ensure_long_output(); the Execution's "
            "terminal plan stage does not use direct-route transport continuation."
        )
    runtime = _PlanExecutionRuntime(execution, config)
    flow_execution = _build_plan_flow().create_execution(
        auto_close=False,
        runtime_resources={_RUNTIME_RESOURCE: runtime},
        parent_run_context=execution.agent_execution_run_context,
        intervention_mode=None,
    )
    try:
        await flow_execution.async_start(None)
        await _drive_connected_exchanges(execution, flow_execution)
        snapshot = await flow_execution.async_close(reason="agent_execution_completed")
    except BaseException:
        if not flow_execution.is_closed():
            with suppress(BaseException):
                await flow_execution.async_close(
                    reason="agent_execution_failed",
                    pending_interrupts="cancel",
                )
        raise
    if not isinstance(snapshot, Mapping) or "execution_result" not in snapshot:
        raise RuntimeError("Plan Execution completed without a terminal plan result.")
    diagnostic = execution.diagnostics.get("execution_run", {})
    if isinstance(diagnostic, dict):
        diagnostic["clarification_rounds"] = int(
            snapshot.get("clarification_round", 0)
        )
        execution.diagnostics["execution_run"] = diagnostic
    execution._producer_state = {"kind": "plan", "clarifications": snapshot.get("clarifications", []),
                                 "readiness": snapshot.get("readiness")}
    execution._review_contract = {
        "deliverable_role": "An actionable plan, not execution of the planned task.",
        "clarifications": snapshot.get("clarifications", []),
        **({"rework_feedback": execution._rework_feedback} if execution.revision else {}),
    }
    return snapshot["execution_result"]


def _normalize_readiness(
    value: object,
    *,
    max_questions: int,
) -> _PlanReadinessData:
    if not isinstance(value, Mapping):
        raise TypeError("Plan Execution readiness stage must return a mapping.")
    plan_ready = value.get("plan_ready")
    if not isinstance(plan_ready, bool):
        raise TypeError("Plan Execution readiness field `plan_ready` must be Boolean.")
    normalized_text: dict[str, str] = {}
    for key in ("planning_goal", "final_deliverable", "readiness_summary"):
        item = str(value.get(key) or "").strip()
        if not item:
            raise ValueError(f"Plan Execution readiness field `{key}` cannot be empty.")
        normalized_text[key] = item
    raw_questions = value.get("questions", [])
    if not isinstance(raw_questions, list):
        raise TypeError("Plan Execution readiness field `questions` must be a list.")
    if len(raw_questions) > max_questions:
        raise ValueError(
            f"Plan Execution readiness returned more than {max_questions} questions."
        )
    questions: list[_PlanQuestionData] = []
    for index, item in enumerate(raw_questions, start=1):
        if not isinstance(item, Mapping):
            raise TypeError(
                f"Plan Execution readiness question {index} must be a mapping."
            )
        question = str(item.get("question") or "").strip()
        why_needed = str(item.get("why_needed") or "").strip()
        if not question or not why_needed:
            raise ValueError(
                f"Plan Execution readiness question {index} requires question and why_needed."
            )
        questions.append({"question": question, "why_needed": why_needed})
    if plan_ready and questions:
        raise ValueError("Plan-ready output must not contain clarification questions.")
    if not plan_ready and not questions:
        raise ValueError("Non-ready plan output must contain a clarification question.")
    return {
        "plan_ready": plan_ready,
        "planning_goal": normalized_text["planning_goal"],
        "final_deliverable": normalized_text["final_deliverable"],
        "readiness_summary": normalized_text["readiness_summary"],
        "questions": questions,
    }


__all__ = ["PlanExecutionConfig", "run_plan_execution"]
