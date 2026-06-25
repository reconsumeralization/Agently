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

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agently.core.orchestration.TriggerFlow import TriggerFlow
from agently.types.data import (
    TaskBoardCard,
    TaskBoardCardResult,
    TaskBoardPatch,
    TaskBoardRevision,
    TaskBoardSchedulePlan,
)
from agently.types.trigger_flow import TriggerFlowRuntimeData

from .TaskBoardPlanning import (
    TaskBoardPlanningPolicy,
    resolve_task_board_planning_policy,
)
from .TaskBoardValidation import (
    TaskBoardValidator,
    apply_task_board_patch,
    schedule_task_board_revision,
)


@dataclass(frozen=True)
class TaskBoardContext:
    revision: TaskBoardRevision
    card: TaskBoardCard
    schedule: TaskBoardSchedulePlan
    dependency_results: Mapping[str, TaskBoardCardResult] = field(default_factory=dict)
    model: Any = None
    workspace: Any = None
    effort: str = "medium"
    planning_policy: TaskBoardPlanningPolicy | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


TaskBoardHandler = Callable[[TaskBoardContext], Any]


@dataclass(frozen=True)
class TaskBoardTickResult:
    previous_revision: TaskBoardRevision
    revision: TaskBoardRevision
    schedule: TaskBoardSchedulePlan
    card_results: Mapping[str, TaskBoardCardResult]
    triggerflow_snapshot: Mapping[str, Any]


class TaskBoard:
    def __init__(
        self,
        revision: TaskBoardRevision | Mapping[str, Any],
        *,
        handler: TaskBoardHandler | Mapping[str, TaskBoardHandler],
        model: Any = None,
        workspace: Any = None,
        effort: str = "medium",
        planning_policy: TaskBoardPlanningPolicy | Mapping[str, Any] | None = None,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        validator: TaskBoardValidator | None = None,
    ):
        self.revision = TaskBoardRevision.from_value(revision)
        self.handler = handler
        self.model = model
        self.workspace = workspace
        self.effort = effort
        self.planning_policy = (
            planning_policy
            if isinstance(planning_policy, TaskBoardPlanningPolicy)
            else resolve_task_board_planning_policy(effort, metadata=planning_policy if isinstance(planning_policy, Mapping) else None)
        )
        self.name = name or f"task-board-{ self.revision.board_id }"
        self.metadata = dict(metadata or {})
        self.validator = validator or TaskBoardValidator()
        self.validator.validate(self.revision)

    def schedule(self) -> TaskBoardSchedulePlan:
        return self.validator.schedule(self.revision)

    async def async_run_tick(
        self,
        *,
        timeout: float | None = None,
        concurrency: int | None = None,
    ) -> TaskBoardTickResult:
        previous_revision = self.revision
        schedule = schedule_task_board_revision(previous_revision)
        card_by_id = previous_revision.graph.card_by_id()
        flow = TriggerFlow(name=f"{ self.name }-tick-{ previous_revision.revision_id }")
        card_requested_event = f"task_board.card.requested.{ previous_revision.revision_id }"
        cards_completed_event = f"task_board.cards.completed.{ previous_revision.revision_id }"
        card_run_binding_id = f"task_board.tick.run_card.{ previous_revision.revision_id }"
        collect_lock = asyncio.Lock()

        async def prepare_tick(data: TriggerFlowRuntimeData[Any, Any, Any]):
            await data.async_put_into_stream(
                {
                    "event": "task_board.tick.started",
                    "board_id": previous_revision.board_id,
                    "revision_id": previous_revision.revision_id,
                    "runnable_card_ids": list(schedule.runnable_card_ids),
                }
            )
            await data.async_set_state("previous_revision", previous_revision.to_dict())
            await data.async_set_state("schedule", schedule.to_dict())
            await data.async_set_state(
                "runtime_topology",
                {
                    "fanout": "signal_net_dynamic_overlay",
                    "card_requested_event": card_requested_event,
                    "cards_completed_event": cards_completed_event,
                    "card_run_binding_id": card_run_binding_id,
                    "concurrency": concurrency,
                },
            )
            if not schedule.runnable_card_ids:
                await data.async_set_state("revision", previous_revision.to_dict())
                await data.async_set_state(
                    "card_results",
                    {card_id: result.to_dict() for card_id, result in previous_revision.card_results.items()},
                )
                await data.async_put_into_stream(
                    {
                        "event": "task_board.tick.completed",
                        "board_id": previous_revision.board_id,
                        "revision_id": previous_revision.revision_id,
                    }
                )
                return {"runnable_card_ids": []}
            await data.async_set_state("expected_card_ids", list(schedule.runnable_card_ids))
            await data.async_set_state("collected_card_results", {})
            for card_id in schedule.runnable_card_ids:
                await data.async_emit_nowait(card_requested_event, {"card_id": card_id})
            return {"runnable_card_ids": list(schedule.runnable_card_ids)}

        async def run_card_and_collect(data: TriggerFlowRuntimeData[Any, Any, Any]):
            card_payload = data.input if isinstance(data.input, Mapping) else {}
            card_id = str(card_payload.get("card_id") or "")
            card = card_by_id[card_id]
            result = await self._async_run_card(previous_revision, schedule, card)
            async with collect_lock:
                collected = data.get_state("collected_card_results", {}, inherit=False)
                if not isinstance(collected, dict):
                    collected = {}
                collected[str(result.card_id)] = result.to_dict()
                await data.async_set_state("collected_card_results", collected)
                expected = data.get_state("expected_card_ids", [], inherit=False)
                expected_count = len(expected) if isinstance(expected, Sequence) and not isinstance(expected, str | bytes | bytearray) else 0
                if expected_count and len(collected) >= expected_count:
                    ordered = [collected[str(card_id)] for card_id in expected if str(card_id) in collected]
                    await data.async_emit(cards_completed_event, ordered)
            return result.to_dict()

        async def apply_results(data: TriggerFlowRuntimeData[Any, Any, Any]):
            values = data.input if isinstance(data.input, Sequence) and not isinstance(data.input, str | bytes | bytearray) else []
            results = [TaskBoardCardResult.from_value(value) for value in values]
            next_revision = _apply_card_results(previous_revision, results)
            await data.async_set_state("revision", next_revision.to_dict())
            await data.async_set_state(
                "card_results",
                {card_id: result.to_dict() for card_id, result in next_revision.card_results.items()},
            )
            await data.async_put_into_stream(
                {
                    "event": "task_board.tick.completed",
                    "board_id": next_revision.board_id,
                    "revision_id": next_revision.revision_id,
                }
            )
            return next_revision.to_dict()

        flow.to(prepare_tick, name="task_board.tick.prepare")
        flow.when(cards_completed_event).to(apply_results, name="task_board.tick.apply")
        execution = flow.create_execution(auto_close=False, concurrency=concurrency)
        execution.on(
            card_requested_event,
            run_card_and_collect,
            binding_id=card_run_binding_id,
            handler_ref="task_board.tick.run_card",
            metadata={
                "board_id": previous_revision.board_id,
                "revision_id": previous_revision.revision_id,
                "role": "task_board_card_fanout",
            },
        )
        await execution.async_start(previous_revision.to_dict())
        snapshot = await execution.async_close(timeout=timeout)
        next_revision = TaskBoardRevision.from_value(snapshot["revision"])
        schedule = TaskBoardSchedulePlan(
            revision_id=snapshot["schedule"]["revision_id"],
            runnable_card_ids=tuple(snapshot["schedule"]["runnable_card_ids"]),
            blocked_card_ids=tuple(snapshot["schedule"].get("blocked_card_ids") or ()),
            completed_card_ids=tuple(snapshot["schedule"].get("completed_card_ids") or ()),
            diagnostics=tuple(snapshot["schedule"].get("diagnostics") or ()),
            metadata=dict(snapshot["schedule"].get("metadata") or {}),
        )
        self.revision = next_revision
        return TaskBoardTickResult(
            previous_revision=previous_revision,
            revision=next_revision,
            schedule=schedule,
            card_results=dict(next_revision.card_results),
            triggerflow_snapshot=snapshot,
        )

    async def _async_run_card(
        self,
        revision: TaskBoardRevision,
        schedule: TaskBoardSchedulePlan,
        card: TaskBoardCard,
    ) -> TaskBoardCardResult:
        handler = self._resolve_handler(card)
        dependency_results = {
            dependency: revision.card_results[dependency]
            for dependency in card.depends_on
            if dependency in revision.card_results
        }
        context = TaskBoardContext(
            revision=revision,
            card=card,
            schedule=schedule,
            dependency_results=dependency_results,
            model=self.model,
            workspace=self.workspace,
            effort=self.effort,
            planning_policy=self.planning_policy,
            metadata=self.metadata,
        )
        raw_result = handler(context)
        if inspect.isawaitable(raw_result):
            raw_result = await raw_result
        return _coerce_card_result(card.id, raw_result)

    def _resolve_handler(self, card: TaskBoardCard) -> TaskBoardHandler:
        if isinstance(self.handler, Mapping):
            handler = self.handler.get(card.id) or self.handler.get(card.allowed_execution_shape) or self.handler.get("*")
            if handler is None:
                raise ValueError(
                    f"TaskBoard has no handler for card '{ card.id }' or shape '{ card.allowed_execution_shape }'."
                )
            return handler
        return self.handler


def _apply_card_results(
    revision: TaskBoardRevision,
    results: Sequence[TaskBoardCardResult],
) -> TaskBoardRevision:
    if not results:
        return revision
    operations: list[Mapping[str, Any]] = []
    diagnostics: list[Mapping[str, Any]] = []
    evidence_refs: list[Mapping[str, Any]] = []
    for result in results:
        operations.append({"op": "record_card_result", "result": result.to_dict()})
        diagnostics.extend(result.diagnostics)
        evidence_refs.extend(result.artifact_refs)
        evidence_refs.extend(result.file_refs)
        if result.patch_proposal is not None:
            proposal = TaskBoardPatch.from_value(result.patch_proposal)
            if proposal.base_revision != revision.revision_id:
                raise ValueError(
                    f"TaskBoardCardResult patch_proposal for card '{ result.card_id }' has base_revision "
                    f"'{ proposal.base_revision }', expected '{ revision.revision_id }'."
                )
            operations.extend(proposal.operations)
            diagnostics.extend(proposal.diagnostics)
            evidence_refs.extend(proposal.evidence_refs)
    patch = TaskBoardPatch(
        base_revision=revision.revision_id,
        source="task_board.tick",
        operations=tuple(operations),
        diagnostics=tuple(diagnostics),
        evidence_refs=tuple(evidence_refs),
    )
    return apply_task_board_patch(revision, patch)

def _coerce_card_result(card_id: str, value: Any) -> TaskBoardCardResult:
    if isinstance(value, TaskBoardCardResult):
        if value.card_id != card_id:
            raise ValueError(f"TaskBoard handler returned result for '{ value.card_id }', expected '{ card_id }'.")
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        data.setdefault("card_id", card_id)
        data.setdefault("status", "completed")
        return TaskBoardCardResult.from_value(data)
    return TaskBoardCardResult(
        card_id=card_id,
        status="completed",
        preview=value,
    )
