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

from .TaskShared import *
from .TaskBoardCardExecution import AgentTaskTaskBoardCardExecutionMixin
from .TaskBoardFinalization import AgentTaskTaskBoardFinalizationMixin
from .TaskBoardPatching import AgentTaskTaskBoardPatchingMixin
from .TaskBoardProjection import AgentTaskTaskBoardProjectionMixin
from .TaskBoardReadback import AgentTaskTaskBoardReadbackMixin
from .TaskBoardRuntimeOptions import AgentTaskTaskBoardRuntimeOptionsMixin
from .TaskBoardScopedRetrieval import AgentTaskTaskBoardScopedRetrievalMixin
from .TaskBoardSourceRefs import AgentTaskTaskBoardSourceRefsMixin


class AgentTaskTaskBoardStrategyMixin(
    AgentTaskTaskBoardCardExecutionMixin,
    AgentTaskTaskBoardRuntimeOptionsMixin,
    AgentTaskTaskBoardScopedRetrievalMixin,
    AgentTaskTaskBoardSourceRefsMixin,
    AgentTaskTaskBoardFinalizationMixin,
    AgentTaskTaskBoardReadbackMixin,
    AgentTaskTaskBoardPatchingMixin,
    AgentTaskTaskBoardProjectionMixin,
):
    _latest_taskboard_acceptance_index: dict[str, Any] | None

    async def _run_taskboard(self) -> dict[str, Any]:
        iteration_index = 1
        try:
            if self._task_deadline_exceeded():
                return await self._terminate_timed_out(iteration_index)
            await self._emit_progress(
                iteration_index,
                "context",
                "TaskBoard: building a Workspace context pack for board planning.",
            )
            await self._apply_guidance_boundary(iteration_index=iteration_index, boundary="taskboard_context")
            context_pack = await self._await_task_deadline(
                self._build_context(),
                stage="context",
            )
            await self._emit("agent_task.taskboard.context", context_pack)

            resumed_taskboard_state = (
                self._resumed_taskboard_state if isinstance(self._resumed_taskboard_state, Mapping) else None
            )
            resumed_revision: TaskBoardRevision | None = None
            if resumed_taskboard_state is not None and isinstance(resumed_taskboard_state.get("revision"), Mapping):
                resumed_revision = TaskBoardRevision.from_value(resumed_taskboard_state["revision"])
            if resumed_revision is None:
                planning_result = self._initial_taskboard_plan_from_shape_analysis()
                if planning_result is None:
                    await self._emit_progress(
                        iteration_index,
                        "taskboard_plan",
                        "TaskBoard: asking the model to plan the initial board.",
                    )
                    planning_result = await self._await_task_deadline(
                        self._request_taskboard_plan(context_pack),
                        stage="taskboard_plan",
                    )
                else:
                    await self._emit_progress(
                        iteration_index,
                        "taskboard_plan",
                        "TaskBoard: reusing the task-shape analysis initial board plan.",
                    )
                board_revision = planning_result.revision
                planning_policy = planning_result.planning_policy
            else:
                planning_policy = resolve_task_board_planning_policy(
                    self._taskboard_effort(),
                    metadata={
                        "execution_strategy": self.execution_strategy,
                        "task_id": self.id,
                        "resume": True,
                    },
                )
                board_revision = resumed_revision
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )

        if resumed_revision is None and self._taskboard_should_fallback_to_flat(board_revision):
            self.diagnostics.setdefault("execution_strategy_gates", []).append(
                {
                    "gate": "taskboard_small_linear_board_fallback",
                    "accepted": True,
                    "card_count": len(getattr(board_revision.graph, "cards", ()) or ()),
                }
            )
            self._set_effective_execution_strategy("flat", source="taskboard_small_linear_board_fallback")
            return await self._run_flat()

        board = TaskBoard(
            board_revision,
            handler=lambda context: self._run_taskboard_card(context, context_pack),
            planning_policy=planning_policy,
            scheduler=self._taskboard_scheduler(),
        )
        if resumed_revision is None:
            await self._record_phase(
                "taskboard_planned",
                iteration=iteration_index,
                diagnostics={
                    "board_id": board.revision.board_id,
                    "revision_id": board.revision.revision_id,
                    "card_count": len(board.revision.graph.cards),
                    "execution_strategy": self.execution_strategy,
                },
            )
            await self._emit(
                "agent_task.taskboard.plan",
                {
                    "revision": board.revision.to_dict(),
                    "planning_policy": planning_policy.to_prompt_payload(),
                },
            )
        else:
            await self._record_phase(
                "taskboard_resumed",
                iteration=iteration_index,
                diagnostics={
                    "board_id": board.revision.board_id,
                    "revision_id": board.revision.revision_id,
                    "card_count": len(board.revision.graph.cards),
                    "execution_strategy": self.execution_strategy,
                    "checkpoint_stage": resumed_taskboard_state.get("stage") if resumed_taskboard_state else None,
                    "checkpoint_tick_index": resumed_taskboard_state.get("tick_index")
                    if resumed_taskboard_state
                    else None,
                },
            )
            await self._emit(
                "agent_task.taskboard.resumed",
                {
                    "revision_id": board.revision.revision_id,
                    "tick_index": resumed_taskboard_state.get("tick_index") if resumed_taskboard_state else None,
                },
            )
        lifecycle_flow = TriggerFlow(name=f"agent-task-taskboard-lifecycle-{ self.id }")
        tick_requested_event = f"agent_task.taskboard.lifecycle.tick.requested.{ self.id }"
        finalize_requested_event = f"agent_task.taskboard.lifecycle.finalize.requested.{ self.id }"
        revision_state_key = "taskboard_revision_json"
        initial_tick_index = 1
        if resumed_taskboard_state is not None:
            try:
                initial_tick_index = max(int(resumed_taskboard_state.get("tick_index") or 0) + 1, 1)
            except (TypeError, ValueError):
                initial_tick_index = 1

        def _board_from_revision(revision: TaskBoardRevision | Mapping[str, Any]) -> TaskBoard:
            return TaskBoard(
                revision,
                handler=lambda context: self._run_taskboard_card(context, context_pack),
                planning_policy=planning_policy,
                scheduler=self._taskboard_scheduler(),
            )

        def _pack_revision_state(revision: TaskBoardRevision | Mapping[str, Any]) -> str:
            effective_revision = TaskBoardRevision.from_value(revision)
            return json.dumps(effective_revision.to_dict(), ensure_ascii=False, sort_keys=True)

        def _unpack_revision_state(data: TriggerFlowRuntimeData[Any, Any, Any]) -> TaskBoardRevision:
            raw_revision = data.get_state(revision_state_key, None, inherit=False)
            if isinstance(raw_revision, str) and raw_revision.strip():
                return TaskBoardRevision.from_value(json.loads(raw_revision))
            if isinstance(raw_revision, Mapping):
                return TaskBoardRevision.from_value(raw_revision)
            return TaskBoardRevision.from_value(board.revision)

        async def _sync_latest_acceptance_index(data: TriggerFlowRuntimeData[Any, Any, Any]) -> None:
            latest_acceptance_index = getattr(self, "_latest_taskboard_acceptance_index", None)
            if isinstance(latest_acceptance_index, Mapping):
                await data.async_set_state(
                    "taskboard_acceptance_index",
                    DataFormatter.sanitize(latest_acceptance_index),
                    emit=False,
                )

        async def start_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            max_ticks = self._taskboard_max_ticks()
            max_ticks_source = self._taskboard_max_ticks_source()
            topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick_requested_event": tick_requested_event,
                "finalize_requested_event": finalize_requested_event,
                "max_ticks": max_ticks,
                "max_ticks_source": max_ticks_source,
                "tick_fanout": (
                    "taskboard_runtime_frontier_signal_net"
                    if self._taskboard_scheduler() == "frontier"
                    else "taskboard_runtime_signal_net"
                ),
            }
            await data.async_set_state(revision_state_key, _pack_revision_state(board.revision), emit=False)
            await data.async_set_state("tick_index", initial_tick_index, emit=False)
            await data.async_set_state("max_ticks", max_ticks, emit=False)
            await data.async_set_state("runtime_topology", topology, emit=False)
            if resumed_taskboard_state is not None and isinstance(resumed_taskboard_state.get("acceptance_index"), Mapping):
                self._latest_taskboard_acceptance_index = DataFormatter.sanitize(
                    resumed_taskboard_state.get("acceptance_index")
                )
                await _sync_latest_acceptance_index(data)
            await self._record_phase(
                "taskboard_lifecycle_started",
                iteration=iteration_index,
                diagnostics={
                    "revision_id": board.revision.revision_id,
                    "runtime_topology": topology,
                },
            )
            await self._emit(
                "agent_task.taskboard.lifecycle.started",
                {
                    "revision_id": board.revision.revision_id,
                    "runtime_topology": topology,
                },
            )
            await data.async_emit_nowait(tick_requested_event, {"tick_index": initial_tick_index})
            return {"runtime_topology": topology}

        async def run_lifecycle_tick(data: TriggerFlowRuntimeData[Any, Any, Any]):
            if data.get_state("terminal_result", None, inherit=False) is not None:
                return data.get_state("terminal_result", inherit=False)
            if data.get_state("final_result", None, inherit=False) is not None:
                return data.get_state("final_result", inherit=False)
            payload = data.input if isinstance(data.input, Mapping) else {}
            try:
                tick_index = int(payload.get("tick_index") or data.get_state("tick_index", 1, inherit=False) or 1)
            except (TypeError, ValueError):
                tick_index = 1
            await self._apply_guidance_boundary(iteration_index=tick_index, boundary="taskboard_tick")
            max_ticks = data.get_state("max_ticks", None, inherit=False)
            max_ticks_int: int | None
            try:
                max_ticks_int = int(max_ticks) if max_ticks is not None else None
            except (TypeError, ValueError):
                max_ticks_int = self._taskboard_max_ticks()
            if self._task_deadline_exceeded():
                result = await self._terminate_timed_out(tick_index, stage="taskboard_tick")
                await data.async_set_state("terminal_result", result, emit=False)
                return result

            revision = _unpack_revision_state(data)
            current_board = _board_from_revision(revision)
            schedule = current_board.schedule()
            tick_concurrency = self._taskboard_concurrency()
            evidence_view = build_task_board_evidence_view(current_board.revision).to_dict()
            runtime_topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick": DataFormatter.sanitize(data.get_state("runtime_topology", {}, inherit=False) or {}),
            }
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.scheduled",
                self._taskboard_scheduled_stream_payload(
                    schedule=schedule,
                    evidence_view=evidence_view,
                    concurrency=tick_concurrency,
                ),
            )
            if not schedule.runnable_card_ids:
                await data.async_set_state(revision_state_key, _pack_revision_state(current_board.revision), emit=False)
                await data.async_set_state("terminal_reason", "no_runnable_cards", emit=False)
                await self._record_taskboard_checkpoint(
                    stage="tick",
                    tick_index=tick_index,
                    revision=current_board.revision,
                    runtime_topology=runtime_topology,
                    terminal_reason="no_runnable_cards",
                )
                await _sync_latest_acceptance_index(data)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
                return {"terminal": False, "status": "ready_to_finalize"}

            try:
                tick_result = await self._await_task_deadline(
                    current_board.async_run_tick(timeout=None, concurrency=tick_concurrency),
                    stage="taskboard_tick",
                )
            except _AgentTaskDeadlineExceeded as error:
                result = await self._terminate_timed_out(
                    tick_index,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result
            except TimeoutError:
                result = await self._terminate_timed_out(
                    tick_index,
                    stage="taskboard_tick",
                    reason="TaskBoard tick timed out.",
                    limit_name="taskboard_tick_timeout_seconds",
                    timeout_seconds=self._taskboard_tick_timeout(),
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result

            await data.async_set_state(revision_state_key, _pack_revision_state(tick_result.revision), emit=False)
            await data.async_set_state("tick_index", tick_index + 1, emit=False)
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.completed",
                self._taskboard_completed_stream_payload(tick_result),
            )
            tick_runtime_topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
            }
            await self._record_phase(
                "taskboard_tick",
                iteration=tick_index,
                diagnostics={
                    "revision_id": tick_result.revision.revision_id,
                    "runnable_card_ids": list(tick_result.schedule.runnable_card_ids),
                    "completed_card_ids": list(tick_result.schedule.completed_card_ids),
                    "concurrency": tick_concurrency,
                    "runtime_topology": tick_runtime_topology,
                },
            )
            await self._record_taskboard_checkpoint(
                stage="tick",
                tick_index=tick_index,
                revision=tick_result.revision,
                runtime_topology=tick_runtime_topology,
            )
            await _sync_latest_acceptance_index(data)
            if self._taskboard_revision_completed(tick_result.revision):
                await data.async_set_state("terminal_reason", "board_completed", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
            elif max_ticks_int is not None and tick_index >= max_ticks_int:
                await data.async_set_state("terminal_reason", "max_ticks_reached", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
            else:
                await data.async_emit_nowait(tick_requested_event, {"tick_index": tick_index + 1})
            return tick_result.revision.to_dict()

        async def finalize_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            if data.get_state("terminal_result", None, inherit=False) is not None:
                return data.get_state("terminal_result", inherit=False)
            if data.get_state("final_result", None, inherit=False) is not None:
                return data.get_state("final_result", inherit=False)
            await self._apply_guidance_boundary(
                iteration_index=max(int(data.get_state("tick_index", 1, inherit=False) or 1) - 1, 1),
                boundary="taskboard_finalize",
            )
            revision = _unpack_revision_state(data)
            previous_acceptance_index = data.get_state("taskboard_acceptance_index", None, inherit=False)
            if not isinstance(previous_acceptance_index, Mapping):
                previous_acceptance_index = getattr(self, "_latest_taskboard_acceptance_index", None)
            try:
                result = await self._finalize_taskboard(
                    revision,
                    context_pack=context_pack,
                    previous_acceptance_index=previous_acceptance_index if isinstance(previous_acceptance_index, Mapping) else None,
                )
            except _AgentTaskDeadlineExceeded as error:
                result = await self._terminate_timed_out(
                    max(len(self.iterations) + 1, 1),
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result
            if isinstance(result, Mapping) and result.get("terminal") is False and result.get("status") == "repair_requested":
                raw_revision = result.get("revision")
                if isinstance(raw_revision, Mapping):
                    repair_revision = TaskBoardRevision.from_value(raw_revision)
                    await data.async_set_state(revision_state_key, _pack_revision_state(repair_revision), emit=False)
                    next_tick_index = int(data.get_state("tick_index", 1, inherit=False) or 1)
                    await data.async_set_state("terminal_reason", "final_verification_repair", emit=False)
                    await self._record_taskboard_checkpoint(
                        stage="finalize",
                        tick_index=max(next_tick_index - 1, 1),
                        revision=repair_revision,
                        runtime_topology=DataFormatter.sanitize(
                            data.get_state("runtime_topology", {}, inherit=False) or {}
                        ),
                        terminal_reason="final_verification_repair",
                        final_result=result,
                    )
                    await _sync_latest_acceptance_index(data)
                    await data.async_emit_nowait(tick_requested_event, {"tick_index": next_tick_index})
                return result
            await data.async_set_state("final_result", result, emit=False)
            checkpoint_result = self.result if isinstance(self.result, Mapping) else result
            await self._record_taskboard_checkpoint(
                stage="finalize",
                tick_index=max(int(data.get_state("tick_index", 1, inherit=False) or 1) - 1, 1),
                revision=revision,
                runtime_topology=DataFormatter.sanitize(data.get_state("runtime_topology", {}, inherit=False) or {}),
                terminal_reason=str(data.get_state("terminal_reason", "", inherit=False) or "") or None,
                final_result=checkpoint_result if isinstance(checkpoint_result, Mapping) else None,
            )
            await _sync_latest_acceptance_index(data)
            return result

        lifecycle_flow.to(start_lifecycle, name="task_board.lifecycle.start")
        lifecycle_flow.when(tick_requested_event).to(run_lifecycle_tick, name="task_board.lifecycle.tick")
        lifecycle_flow.when(finalize_requested_event).to(finalize_lifecycle, name="task_board.lifecycle.finalize")

        execution = lifecycle_flow.create_execution(auto_close=False, concurrency=1)
        await execution.async_start(board.revision.to_dict())
        snapshot = await execution.async_close()
        result = snapshot.get("terminal_result") or snapshot.get("final_result")
        if isinstance(result, Mapping):
            return dict(result)
        raw_revision = snapshot.get(revision_state_key)
        if isinstance(raw_revision, str) and raw_revision.strip():
            revision = TaskBoardRevision.from_value(json.loads(raw_revision))
        else:
            revision = TaskBoardRevision.from_value(board.revision)
        latest_acceptance_index = getattr(self, "_latest_taskboard_acceptance_index", None)
        return await self._finalize_taskboard(
            revision,
            context_pack=context_pack,
            previous_acceptance_index=latest_acceptance_index if isinstance(latest_acceptance_index, Mapping) else None,
        )

    async def _request_taskboard_plan(self, context_pack: "WorkspaceContextPackage"):
        policy = resolve_task_board_planning_policy(
            self._taskboard_effort(),
            metadata={"execution_strategy": self.execution_strategy, "task_id": self.id},
        )
        language_policy = self._language_policy()
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract(),
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "planning_policy": policy.to_prompt_payload(),
                "taskboard_harness_policy": {
                    "acceptance_index": {
                        "schema_version": "task_board_acceptance_index/v1",
                        "authority": "projection_only",
                        "semantic_owner": "verifier",
                    },
                    "handoff_projection": {
                        "schema_version": "task_board_handoff_projection/v1",
                        "authority": "orientation_only",
                    },
                    "preflight": {
                        "allowed_only_with_mounted_capabilities": True,
                        "metadata_fields": [
                            "preflight_kind",
                            "requires_capability_ids",
                            "requires_workspace_refs",
                            "focus_item_ids",
                        ],
                    },
                },
                "planner_capabilities": self._planner_capabilities(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Plan a card board for this submitted task. "
            "Do not discuss route selection. "
            "Use task_context_contract for run-date facts, current/latest/as-of source boundaries, and ref-backed "
            "intermediate-resource handling. It is not a resource cap. "
            "Use the planning_policy as vocabulary guidance for orchestration complexity, evidence depth, "
            "reflection density, and repair tendency. Do not create hard budgets, fixed card counts, "
            "or action allowlists from the effort profile. "
            "Plan card objectives and done_when conditions around user-visible outcomes, not around one "
            "specific provider, endpoint, file format, or auxiliary guidance source unless the user explicitly "
            "requires that exact source or artifact. Mark replaceable evidence attempts, optional guidance, "
            "style checks, and non-critical cross-checks as optional or degradable through failure_policy. "
            "Card ids are optional short hints only; keep them readable and do not spend tokens inventing opaque ids. "
            "Use allowed_execution_shape='control' for synthesis, verification, finalization, or board-continuation "
            "decision cards that should be handled by one structured model request. Use allowed_execution_shape='readback' "
            "for cards whose only job is bounded cold artifact readback. Use an action-capable shape such as 'actions' "
            "or 'auto' for cards that need external tools, Workspace operations, side effects, or mixed action/readback work. "
            "When readiness checks are necessary, express them as optional preflight metadata on cards "
            "(preflight_kind, requires_capability_ids, requires_workspace_refs, focus_item_ids) and only for mounted "
            "capabilities or existing Workspace refs; do not require universal git, browser, shell, or init-script checks. "
            "After evidence fan-in, do not create a serial chain of control-only cards for synthesis, finalization, "
            "review, and next-step decision when one control card can return the deliverable, sufficient/gaps, "
            "next_board_action, diagnostics, and optional patch_proposal. Multiple dependent control cards are only "
            "justified for distinct user-visible artifacts, different upstream evidence sets, or materially separate "
            "decisions that cannot be verified in one request."
        )
        request.output(task_board_planning_output_schema(), format="json")
        raw_plan = await self._await_task_request(request.async_get_data(), stage="taskboard_plan")
        if not isinstance(raw_plan, Mapping):
            raise TypeError("TaskBoard planning request must return a mapping.")
        return coerce_task_board_planning_result(
            raw_plan,
            board_id=self.id,
            graph_id=f"{self.id}.taskboard",
            effort=self._taskboard_effort(),
            planning_policy=policy,
            metadata={"execution_strategy": self.execution_strategy},
        )

    def _initial_taskboard_plan_from_shape_analysis(self):
        if self.execution_strategy != "auto":
            return None
        raw_plan = self.task_shape_analysis.get("initial_taskboard_plan") if isinstance(self.task_shape_analysis, Mapping) else None
        if not isinstance(raw_plan, Mapping) or not raw_plan:
            return None
        policy = resolve_task_board_planning_policy(
            self._taskboard_effort(),
            metadata={"execution_strategy": self.execution_strategy, "task_id": self.id, "source": "task_shape_analysis"},
        )
        try:
            planning_result = coerce_task_board_planning_result(
                raw_plan,
                board_id=self.id,
                graph_id=f"{self.id}.taskboard",
                effort=self._taskboard_effort(),
                planning_policy=policy,
                metadata={"execution_strategy": self.execution_strategy, "source": "task_shape_analysis"},
            )
        except Exception as error:
            self.diagnostics.setdefault("taskboard_initial_plan", []).append(
                {
                    "source": "task_shape_analysis",
                    "accepted": False,
                    "error": {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    },
                }
            )
            return None
        self.diagnostics.setdefault("taskboard_initial_plan", []).append(
            {
                "source": "task_shape_analysis",
                "accepted": True,
                "card_count": len(planning_result.revision.graph.cards),
            }
        )
        return planning_result

    def _taskboard_should_fallback_to_flat(self, revision: Any) -> bool:
        if self.execution_strategy != "auto":
            return False
        cards = list(getattr(getattr(revision, "graph", None), "cards", ()) or ())
        if not cards or len(cards) > 2:
            return False
        if any(str(getattr(card, "failure_policy", "required") or "required") != "required" for card in cards):
            return False
        if any(self._taskboard_card_has_complex_contract(card) for card in cards):
            return False
        shapes = {str(getattr(card, "allowed_execution_shape", "auto") or "auto").strip().lower() for card in cards}
        if shapes.intersection({"readback", "artifact_readback", "control", "model_control", "synthesis", "finalize"}):
            return False
        dependency_edges = sum(len(getattr(card, "depends_on", ()) or ()) for card in cards)
        if len(cards) == 1:
            return True
        if dependency_edges != 1:
            return False
        depended_on = {str(dep) for card in cards for dep in (getattr(card, "depends_on", ()) or ())}
        card_ids = {str(getattr(card, "id", "")) for card in cards}
        return depended_on.issubset(card_ids)

    @staticmethod
    def _taskboard_card_has_complex_contract(card: Any) -> bool:
        if getattr(card, "policy_scope_refs", ()):
            return True
        if getattr(card, "input_refs", ()):
            return True
        contract = getattr(card, "evidence_contract", {})
        if not isinstance(contract, Mapping) or not contract:
            return False
        if contract.get("scoped_retrieval"):
            return True
        if contract.get("evidence_to_use"):
            return True
        informational_keys = {"action_block", "done_when", "failure_policy", "evidence_to_use"}
        return any(key not in informational_keys and value not in (None, "", [], {}) for key, value in contract.items())

    @staticmethod
    def _taskboard_revision_completed(revision: Any) -> bool:
        cards = list(revision.graph.cards)
        if not cards:
            return False
        for card in cards:
            result = revision.card_results.get(card.id)
            status = str(result.status if result is not None else card.status).strip().lower()
            if task_board_card_required(card):
                if status != "completed":
                    return False
            elif status not in {"completed", "failed", "setback", "blocked", "skipped"}:
                return False
        return True

    @staticmethod
    def _taskboard_can_attempt_degraded_final(revision: Any, schedule: Any) -> bool:
        if getattr(schedule, "runnable_card_ids", ()):
            return False
        return any(str(result.status).strip().lower() == "completed" for result in revision.card_results.values())

    @classmethod
    def _taskboard_terminal_status(cls, revision: Any, schedule: Any) -> str:
        card_by_id = revision.graph.card_by_id()
        required_statuses = {
            card_id: str(result.status)
            for card_id, result in revision.card_results.items()
            if task_board_card_required(card_by_id[card_id])
        }
        if "failed" in required_statuses.values():
            return "failed"
        if "blocked" in required_statuses.values() or "setback" in required_statuses.values() or schedule.blocked_card_ids:
            return "blocked"
        if cls._taskboard_revision_completed(revision):
            return "completed"
        return "running"

__all__ = ["AgentTaskTaskBoardStrategyMixin"]
