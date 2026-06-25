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
from pathlib import Path
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


@dataclass(frozen=True)
class TaskBoardTickExecution:
    board: "TaskBoard"
    previous_revision: TaskBoardRevision
    schedule: TaskBoardSchedulePlan
    execution: Any
    card_requested_event: str
    cards_completed_event: str
    card_run_binding_id: str

    def save(
        self,
        path: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
        require_idle: bool = False,
    ):
        return self.execution.save(path, encoding=encoding, require_idle=require_idle)

    def load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: list[Any] | None = None,
        validate_resources: bool = False,
    ):
        self.execution.load(
            state,
            encoding=encoding,
            runtime_resources=runtime_resources,
            execution_resources=execution_resources,
            validate_resources=validate_resources,
        )
        return self

    def inspect_load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: list[Any] | None = None,
    ):
        return self.execution.inspect_load(
            state,
            encoding=encoding,
            runtime_resources=runtime_resources,
            execution_resources=execution_resources,
        )

    async def async_start(self):
        await self.execution.async_start(self.previous_revision.to_dict())
        return self

    async def async_resume_pending(self):
        expected = self.execution.get_state("expected_card_ids", [], inherit=False)
        collected = self.execution.get_state("collected_card_results", {}, inherit=False)
        if not isinstance(expected, Sequence) or isinstance(expected, str | bytes | bytearray):
            return self
        if not isinstance(collected, Mapping):
            collected = {}
        missing_card_ids = [str(card_id) for card_id in expected if str(card_id) not in collected]
        if not missing_card_ids and expected:
            ordered = [collected[str(card_id)] for card_id in expected if str(card_id) in collected]
            await self.execution.async_emit(self.cards_completed_event, ordered)
            return self
        for card_id in missing_card_ids:
            await self.execution.async_emit_nowait(self.card_requested_event, {"card_id": card_id})
        return self

    async def async_close(self, *, timeout: float | None = None) -> TaskBoardTickResult:
        snapshot = await self.execution.async_close(timeout=timeout)
        return self.board._finalize_tick_snapshot(
            self.previous_revision,
            snapshot,
        )


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
        tick_execution = await self.async_start_tick(concurrency=concurrency)
        return await tick_execution.async_close(timeout=timeout)

    async def async_start_tick(
        self,
        *,
        concurrency: int | None = None,
    ) -> TaskBoardTickExecution:
        tick_execution = self.create_tick_execution(concurrency=concurrency)
        return await tick_execution.async_start()

    def create_tick_execution(
        self,
        *,
        concurrency: int | None = None,
    ) -> TaskBoardTickExecution:
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
        return TaskBoardTickExecution(
            board=self,
            previous_revision=previous_revision,
            schedule=schedule,
            execution=execution,
            card_requested_event=card_requested_event,
            cards_completed_event=cards_completed_event,
            card_run_binding_id=card_run_binding_id,
        )

    def _finalize_tick_snapshot(
        self,
        previous_revision: TaskBoardRevision,
        snapshot: Mapping[str, Any],
    ) -> TaskBoardTickResult:
        schedule_payload = snapshot.get("schedule")
        if isinstance(schedule_payload, Mapping):
            schedule = TaskBoardSchedulePlan(
                revision_id=str(schedule_payload.get("revision_id") or previous_revision.revision_id),
                runnable_card_ids=tuple(schedule_payload.get("runnable_card_ids") or ()),
                blocked_card_ids=tuple(schedule_payload.get("blocked_card_ids") or ()),
                completed_card_ids=tuple(schedule_payload.get("completed_card_ids") or ()),
                diagnostics=tuple(schedule_payload.get("diagnostics") or ()),
                metadata=dict(schedule_payload.get("metadata") or {}),
            )
        else:
            schedule = schedule_task_board_revision(previous_revision)
        if "revision" in snapshot:
            next_revision = TaskBoardRevision.from_value(snapshot["revision"])
        else:
            collected_payload = snapshot.get("collected_card_results")
            collected_results: list[TaskBoardCardResult] = []
            if isinstance(collected_payload, Mapping):
                for value in collected_payload.values():
                    collected_results.append(TaskBoardCardResult.from_value(value))
            collected_ids = {result.card_id for result in collected_results}
            expected_payload = snapshot.get("expected_card_ids")
            if isinstance(expected_payload, Sequence) and not isinstance(
                expected_payload, str | bytes | bytearray
            ):
                expected_card_ids = [str(card_id) for card_id in expected_payload if str(card_id)]
            else:
                expected_card_ids = [str(card_id) for card_id in schedule.runnable_card_ids if str(card_id)]
            missing_results = [
                _interrupted_card_result(
                    card_id=card_id,
                    previous_revision=previous_revision,
                    snapshot=snapshot,
                )
                for card_id in expected_card_ids
                if card_id not in collected_ids
            ]
            collected_results.extend(missing_results)
            diagnostic = {
                "code": "taskboard.tick.incomplete_snapshot",
                "message": "TriggerFlow tick closed before a finalized TaskBoard revision was written.",
                "snapshot_status": snapshot.get("status"),
                "pending_tasks_cancelled": snapshot.get("pending_tasks_cancelled"),
                "expected_card_ids": expected_card_ids,
                "collected_card_ids": sorted(collected_ids),
                "interrupted_card_ids": [result.card_id for result in missing_results],
            }
            operations: list[Mapping[str, Any]] = [
                {"op": "record_card_result", "result": result.to_dict()} for result in collected_results
            ]
            evidence_refs: list[Mapping[str, Any]] = []
            for result in collected_results:
                evidence_refs.extend(result.artifact_refs)
                evidence_refs.extend(result.file_refs)
            operations.append({"op": "append_diagnostic", "diagnostic": diagnostic})
            if collected_results:
                patch = TaskBoardPatch(
                    base_revision=previous_revision.revision_id,
                    operations=tuple(operations),
                    evidence_refs=tuple(evidence_refs),
                    source="task_board.tick.incomplete_snapshot",
                )
                next_revision = apply_task_board_patch(previous_revision, patch)
            else:
                patch = TaskBoardPatch(
                    base_revision=previous_revision.revision_id,
                    operations=tuple(operations),
                    source="task_board.tick.incomplete_snapshot",
                )
                next_revision = apply_task_board_patch(previous_revision, patch)
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


def _interrupted_card_result(
    *,
    card_id: str,
    previous_revision: TaskBoardRevision,
    snapshot: Mapping[str, Any],
) -> TaskBoardCardResult:
    diagnostic = {
        "code": "taskboard.tick.card_interrupted",
        "message": "TaskBoard card did not produce a result before the tick closed.",
        "card_id": card_id,
        "board_id": previous_revision.board_id,
        "revision_id": previous_revision.revision_id,
        "snapshot_status": snapshot.get("status"),
        "pending_tasks_cancelled": snapshot.get("pending_tasks_cancelled"),
        "status": "failed",
    }
    return TaskBoardCardResult(
        card_id=card_id,
        status="failed",
        preview="TaskBoard card execution interrupted before producing a result.",
        diagnostics=(diagnostic,),
        metadata={
            "status": "failed",
            "interrupted": True,
            "source": "task_board.tick.incomplete_snapshot",
        },
    )

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
