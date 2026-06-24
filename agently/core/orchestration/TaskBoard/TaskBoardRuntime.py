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
        flow = TriggerFlow(name=f"{ self.name }-tick-{ previous_revision.revision_id }")

        async def run_tick(data: TriggerFlowRuntimeData[Any, Any, Any]):
            revision = TaskBoardRevision.from_value(data.input)
            schedule = schedule_task_board_revision(revision)
            await data.async_put_into_stream(
                {
                    "event": "task_board.tick.started",
                    "board_id": revision.board_id,
                    "revision_id": revision.revision_id,
                    "runnable_card_ids": list(schedule.runnable_card_ids),
                }
            )
            next_revision = await self._async_execute_schedule(revision, schedule, concurrency=concurrency)
            await data.async_set_state("previous_revision", revision.to_dict())
            await data.async_set_state("schedule", schedule.to_dict())
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

        flow.to(run_tick, name="task_board.tick")
        execution = flow.create_execution(auto_close=False)
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

    async def _async_execute_schedule(
        self,
        revision: TaskBoardRevision,
        schedule: TaskBoardSchedulePlan,
        *,
        concurrency: int | None,
    ) -> TaskBoardRevision:
        if not schedule.runnable_card_ids:
            return revision
        card_by_id = revision.graph.card_by_id()
        if concurrency is None or concurrency <= 1:
            next_revision = revision
            for card_id in schedule.runnable_card_ids:
                result = await self._async_run_card(next_revision, schedule, card_by_id[card_id])
                next_revision = _apply_card_results(next_revision, (result,))
                if _card_result_stops_current_tick(result):
                    return next_revision
            return next_revision
        else:
            import asyncio

            semaphore = asyncio.Semaphore(concurrency)

            async def run_one(card: TaskBoardCard):
                async with semaphore:
                    return await self._async_run_card(revision, schedule, card)

            results = list(await asyncio.gather(*(run_one(card_by_id[card_id]) for card_id in schedule.runnable_card_ids)))
        return _apply_card_results(revision, results)

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


def _card_result_stops_current_tick(result: TaskBoardCardResult) -> bool:
    status = str(result.status).strip().lower()
    if status in {"failed", "blocked"}:
        return True
    return result.patch_proposal is not None


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
