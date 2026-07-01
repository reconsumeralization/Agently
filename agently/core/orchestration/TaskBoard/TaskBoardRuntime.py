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
import json
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
        topology = self.execution.get_state("runtime_topology", {}, inherit=False)
        if isinstance(topology, Mapping) and topology.get("scheduler") == "frontier":
            expected_json = self.execution.get_state("expected_card_ids_json", "[]", inherit=False)
            collected_json = self.execution.get_state("collected_card_results_json", "{}", inherit=False)
            try:
                expected = json.loads(expected_json) if isinstance(expected_json, str) else []
            except json.JSONDecodeError:
                expected = []
            try:
                collected = json.loads(collected_json) if isinstance(collected_json, str) else {}
            except json.JSONDecodeError:
                collected = {}
            if not isinstance(expected, Sequence) or isinstance(expected, str | bytes | bytearray):
                expected = []
            if not isinstance(collected, Mapping):
                collected = {}
            missing_card_ids = [str(card_id) for card_id in expected if str(card_id) not in collected]
            for card_id in missing_card_ids:
                await self.execution.async_emit_nowait(self.card_requested_event, {"card_id": card_id})
            if not missing_card_ids:
                await self.execution.async_emit(self.cards_completed_event, {"reason": "frontier_resume_quiesced"})
            return self

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
        scheduler: str | None = None,
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
        self.scheduler = _normalize_task_board_scheduler(
            scheduler or self.metadata.get("scheduler") or self.metadata.get("taskboard_scheduler")
        )
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
        if self.scheduler == "frontier":
            return self._create_frontier_tick_execution(concurrency=concurrency)
        return self._create_batch_tick_execution(concurrency=concurrency)

    def _create_batch_tick_execution(
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
                    "scheduler": "batch",
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

    def _create_frontier_tick_execution(
        self,
        *,
        concurrency: int | None = None,
    ) -> TaskBoardTickExecution:
        previous_revision = self.revision
        initial_schedule = schedule_task_board_revision(previous_revision)
        flow = TriggerFlow(name=f"{ self.name }-frontier-tick-{ previous_revision.revision_id }")
        card_requested_event = f"task_board.card.requested.{ previous_revision.revision_id }"
        cards_completed_event = f"task_board.cards.completed.{ previous_revision.revision_id }"
        card_run_binding_id = f"task_board.tick.run_card.{ previous_revision.revision_id }"
        collect_lock = asyncio.Lock()

        def revision_from_state(data: TriggerFlowRuntimeData[Any, Any, Any]) -> TaskBoardRevision:
            raw_revision_json = data.get_state("revision_json", None, inherit=False)
            if isinstance(raw_revision_json, str) and raw_revision_json.strip():
                return TaskBoardRevision.from_value(json.loads(raw_revision_json))
            raw_revision = data.get_state("revision", None, inherit=False)
            if isinstance(raw_revision, Mapping):
                return TaskBoardRevision.from_value(raw_revision)
            return previous_revision

        def string_set_from_state(data: TriggerFlowRuntimeData[Any, Any, Any], key: str) -> set[str]:
            raw_json = data.get_state(f"{ key }_json", None, inherit=False)
            if isinstance(raw_json, str) and raw_json.strip():
                try:
                    value = json.loads(raw_json)
                except json.JSONDecodeError:
                    value = []
                if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                    return {str(item) for item in value if str(item)}
            value = data.get_state(key, [], inherit=False)
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                return {str(item) for item in value if str(item)}
            return set()

        def collected_results_from_state(data: TriggerFlowRuntimeData[Any, Any, Any]) -> dict[str, Any]:
            raw_json = data.get_state("collected_card_results_json", None, inherit=False)
            if isinstance(raw_json, str) and raw_json.strip():
                try:
                    value = json.loads(raw_json)
                except json.JSONDecodeError:
                    value = {}
                return dict(value) if isinstance(value, Mapping) else {}
            value = data.get_state("collected_card_results", {}, inherit=False)
            return dict(value) if isinstance(value, Mapping) else {}

        async def store_revision_state(
            data: TriggerFlowRuntimeData[Any, Any, Any],
            revision: TaskBoardRevision,
            schedule: TaskBoardSchedulePlan,
            *,
            running_card_ids: set[str],
            dispatched_card_ids: set[str],
        ):
            await data.async_set_state("revision_json", json.dumps(revision.to_dict(), ensure_ascii=False), emit=False)
            await data.async_set_state("schedule_json", json.dumps(schedule.to_dict(), ensure_ascii=False), emit=False)
            await data.async_set_state(
                "running_card_ids_json",
                json.dumps(sorted(running_card_ids), ensure_ascii=False),
                emit=False,
            )
            await data.async_set_state(
                "dispatched_card_ids_json",
                json.dumps(sorted(dispatched_card_ids), ensure_ascii=False),
                emit=False,
            )
            await data.async_set_state(
                "expected_card_ids_json",
                json.dumps(sorted(running_card_ids), ensure_ascii=False),
                emit=False,
            )

        async def prepare_frontier_dispatch(
            data: TriggerFlowRuntimeData[Any, Any, Any],
            revision: TaskBoardRevision,
        ) -> tuple[list[str], bool]:
            running_card_ids = string_set_from_state(data, "running_card_ids")
            dispatched_card_ids = string_set_from_state(data, "dispatched_card_ids")
            if str(revision.status).strip().lower() in {"completed", "failed", "blocked"}:
                schedule = schedule_task_board_revision(revision)
                await store_revision_state(
                    data,
                    revision,
                    schedule,
                    running_card_ids=running_card_ids,
                    dispatched_card_ids=dispatched_card_ids,
                )
                return [], not running_card_ids

            schedule = schedule_task_board_revision(revision)
            runnable = [
                card_id
                for card_id in schedule.runnable_card_ids
                if card_id not in running_card_ids and card_id not in dispatched_card_ids
            ]
            running_card_ids.update(runnable)
            dispatched_card_ids.update(runnable)
            await store_revision_state(
                data,
                revision,
                schedule,
                running_card_ids=running_card_ids,
                dispatched_card_ids=dispatched_card_ids,
            )
            return runnable, not running_card_ids and not runnable

        async def emit_card_requests(data: TriggerFlowRuntimeData[Any, Any, Any], card_ids: Sequence[str]):
            for card_id in card_ids:
                await data.async_emit_nowait(card_requested_event, {"card_id": card_id})

        async def prepare_tick(data: TriggerFlowRuntimeData[Any, Any, Any]):
            await data.async_put_into_stream(
                {
                    "event": "task_board.tick.started",
                    "board_id": previous_revision.board_id,
                    "revision_id": previous_revision.revision_id,
                    "runnable_card_ids": list(initial_schedule.runnable_card_ids),
                    "scheduler": "frontier",
                }
            )
            await data.async_set_state("previous_revision", previous_revision.to_dict())
            await data.async_set_state("collected_card_results_json", json.dumps({}, ensure_ascii=False), emit=False)
            await data.async_set_state("frontier_closed", False)
            await data.async_set_state(
                "runtime_topology",
                {
                    "fanout": "signal_net_frontier_overlay",
                    "scheduler": "frontier",
                    "card_requested_event": card_requested_event,
                    "cards_completed_event": cards_completed_event,
                    "card_run_binding_id": card_run_binding_id,
                    "concurrency": concurrency,
                },
            )
            async with collect_lock:
                card_ids, frontier_complete = await prepare_frontier_dispatch(data, previous_revision)
            await emit_card_requests(data, card_ids)
            if frontier_complete:
                await data.async_set_state("frontier_closed", True)
                await data.async_emit(cards_completed_event, {"reason": "no_runnable_cards"})
            return {"runnable_card_ids": list(card_ids), "scheduler": "frontier"}

        async def run_card_and_collect(data: TriggerFlowRuntimeData[Any, Any, Any]):
            card_payload = data.input if isinstance(data.input, Mapping) else {}
            card_id = str(card_payload.get("card_id") or "")
            start_revision = revision_from_state(data)
            start_schedule = schedule_task_board_revision(start_revision)
            card_by_id = start_revision.graph.card_by_id()
            card = card_by_id[card_id]
            result = await self._async_run_card(start_revision, start_schedule, card)

            async with collect_lock:
                current_revision = revision_from_state(data)
                next_revision = _apply_frontier_card_result(current_revision, result)
                collected = collected_results_from_state(data)
                collected[str(result.card_id)] = result.to_dict()
                await data.async_set_state(
                    "collected_card_results_json",
                    json.dumps(collected, ensure_ascii=False),
                    emit=False,
                )
                running_card_ids = string_set_from_state(data, "running_card_ids")
                running_card_ids.discard(str(result.card_id))
                card_ids, frontier_complete = await prepare_frontier_dispatch(data, next_revision)

            await emit_card_requests(data, card_ids)
            if frontier_complete:
                await data.async_set_state("frontier_closed", True)
                await data.async_emit(cards_completed_event, {"reason": "frontier_quiesced"})
            return result.to_dict()

        async def apply_frontier_results(data: TriggerFlowRuntimeData[Any, Any, Any]):
            next_revision = revision_from_state(data)
            final_schedule = schedule_task_board_revision(next_revision)
            await data.async_set_state("frontier_closed", True)
            await data.async_set_state("revision", next_revision.to_dict(), emit=False)
            await data.async_set_state("schedule", final_schedule.to_dict(), emit=False)
            await data.async_set_state(
                "card_results",
                {card_id: result.to_dict() for card_id, result in next_revision.card_results.items()},
                emit=False,
            )
            await data.async_put_into_stream(
                {
                    "event": "task_board.tick.completed",
                    "board_id": next_revision.board_id,
                    "revision_id": next_revision.revision_id,
                    "scheduler": "frontier",
                }
            )
            return next_revision.to_dict()

        flow.to(prepare_tick, name="task_board.tick.frontier.prepare")
        flow.when(cards_completed_event).to(apply_frontier_results, name="task_board.tick.frontier.apply")
        execution = flow.create_execution(auto_close=False, concurrency=concurrency)
        execution.on(
            card_requested_event,
            run_card_and_collect,
            binding_id=card_run_binding_id,
            handler_ref="task_board.tick.run_card",
            metadata={
                "board_id": previous_revision.board_id,
                "revision_id": previous_revision.revision_id,
                "role": "task_board_card_frontier",
            },
        )
        return TaskBoardTickExecution(
            board=self,
            previous_revision=previous_revision,
            schedule=initial_schedule,
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
        if not isinstance(schedule_payload, Mapping):
            schedule_json = snapshot.get("schedule_json")
            if isinstance(schedule_json, str) and schedule_json.strip():
                try:
                    parsed_schedule = json.loads(schedule_json)
                except json.JSONDecodeError:
                    parsed_schedule = None
                if isinstance(parsed_schedule, Mapping):
                    schedule_payload = parsed_schedule
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
        elif isinstance(snapshot.get("revision_json"), str) and str(snapshot.get("revision_json")).strip():
            next_revision = TaskBoardRevision.from_value(json.loads(str(snapshot["revision_json"])))
        else:
            collected_payload = snapshot.get("collected_card_results")
            if not isinstance(collected_payload, Mapping):
                collected_json = snapshot.get("collected_card_results_json")
                if isinstance(collected_json, str) and collected_json.strip():
                    try:
                        parsed_collected = json.loads(collected_json)
                    except json.JSONDecodeError:
                        parsed_collected = None
                    if isinstance(parsed_collected, Mapping):
                        collected_payload = parsed_collected
            collected_results: list[TaskBoardCardResult] = []
            if isinstance(collected_payload, Mapping):
                for value in collected_payload.values():
                    collected_results.append(TaskBoardCardResult.from_value(value))
            collected_ids = {result.card_id for result in collected_results}
            expected_payload = snapshot.get("expected_card_ids")
            if not isinstance(expected_payload, Sequence) or isinstance(
                expected_payload, str | bytes | bytearray
            ):
                expected_json = snapshot.get("expected_card_ids_json")
                if isinstance(expected_json, str) and expected_json.strip():
                    try:
                        parsed_expected = json.loads(expected_json)
                    except json.JSONDecodeError:
                        parsed_expected = None
                    if isinstance(parsed_expected, Sequence) and not isinstance(
                        parsed_expected, str | bytes | bytearray
                    ):
                        expected_payload = parsed_expected
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


def _normalize_task_board_scheduler(value: Any) -> str:
    text = str(value or "batch").strip().lower().replace("-", "_")
    if text in {"frontier", "event_driven", "evented", "dynamic_frontier"}:
        return "frontier"
    return "batch"


def _apply_frontier_card_result(
    revision: TaskBoardRevision,
    result: TaskBoardCardResult,
) -> TaskBoardRevision:
    try:
        return _apply_card_results(revision, [result])
    except ValueError as error:
        if result.patch_proposal is None or "base_revision" not in str(error):
            raise
        diagnostic = {
            "code": "taskboard.frontier.stale_patch_proposal",
            "message": "TaskBoard frontier scheduler blocked a stale card patch proposal instead of rebasing it.",
            "card_id": result.card_id,
            "revision_id": revision.revision_id,
            "error": str(error),
            "status": "blocked",
        }
        blocked_result = TaskBoardCardResult(
            card_id=result.card_id,
            status="blocked",
            output_digest=result.output_digest,
            preview=result.preview,
            artifact_refs=result.artifact_refs,
            file_refs=result.file_refs,
            diagnostics=(*result.diagnostics, diagnostic),
            metadata={
                **dict(result.metadata),
                "blocked_by_frontier_scheduler": True,
                "stale_patch_proposal": True,
            },
        )
        return _apply_card_results(revision, [blocked_result])
