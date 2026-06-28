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
from .TaskBoardPatching import AgentTaskTaskBoardPatchingMixin
from .TaskBoardProjection import AgentTaskTaskBoardProjectionMixin
from .TaskBoardReadback import AgentTaskTaskBoardReadbackMixin


_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION = (
    "Apply source_ref_policy. A source ref with content_state='ref_only' proves only that a URL, path, "
    "download, snapshot, note, or artifact ref was discovered or materialized; it is not evidence that the "
    "source content has been read. Use it as content support only after a bounded readback/content preview is "
    "available. If the deliverable depends on unread source content, request readback with target_refs or call "
    "the available readback action; otherwise label the ref as discovered-only and do not claim facts from it. "
    "When target refs point at Workspace/repository/file evidence, prefer scoped search/readback that returns "
    "locator_ref or evidence_snippet before requesting broad content. "
)


class AgentTaskTaskBoardStrategyMixin(
    AgentTaskTaskBoardReadbackMixin,
    AgentTaskTaskBoardPatchingMixin,
    AgentTaskTaskBoardProjectionMixin,
):
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
            context_pack = await self._await_task_deadline(
                self._build_context(),
                stage="context",
            )
            await self._emit("agent_task.taskboard.context", context_pack)

            await self._emit_progress(
                iteration_index,
                "taskboard_plan",
                "TaskBoard: asking the model to plan the initial board.",
            )
            planning_result = await self._await_task_deadline(
                self._request_taskboard_plan(context_pack),
                stage="taskboard_plan",
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )

        board = TaskBoard(
            planning_result.revision,
            handler=lambda context: self._run_taskboard_card(context, context_pack),
            planning_policy=planning_result.planning_policy,
        )
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
                "planning_policy": planning_result.planning_policy.to_prompt_payload(),
            },
        )
        lifecycle_flow = TriggerFlow(name=f"agent-task-taskboard-lifecycle-{ self.id }")
        tick_requested_event = f"agent_task.taskboard.lifecycle.tick.requested.{ self.id }"
        finalize_requested_event = f"agent_task.taskboard.lifecycle.finalize.requested.{ self.id }"
        revision_state_key = "taskboard_revision_json"

        def _board_from_revision(revision: TaskBoardRevision | Mapping[str, Any]) -> TaskBoard:
            return TaskBoard(
                revision,
                handler=lambda context: self._run_taskboard_card(context, context_pack),
                planning_policy=planning_result.planning_policy,
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

        async def start_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            max_ticks = self._taskboard_max_ticks()
            max_ticks_source = self._taskboard_max_ticks_source()
            topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick_requested_event": tick_requested_event,
                "finalize_requested_event": finalize_requested_event,
                "max_ticks": max_ticks,
                "max_ticks_source": max_ticks_source,
                "tick_fanout": "taskboard_runtime_signal_net",
            }
            await data.async_set_state(revision_state_key, _pack_revision_state(board.revision), emit=False)
            await data.async_set_state("tick_index", 1, emit=False)
            await data.async_set_state("max_ticks", max_ticks, emit=False)
            await data.async_set_state("runtime_topology", topology, emit=False)
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
            await data.async_emit_nowait(tick_requested_event, {"tick_index": 1})
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
            await self._record_phase(
                "taskboard_tick",
                iteration=tick_index,
                diagnostics={
                    "revision_id": tick_result.revision.revision_id,
                    "runnable_card_ids": list(tick_result.schedule.runnable_card_ids),
                    "completed_card_ids": list(tick_result.schedule.completed_card_ids),
                    "concurrency": tick_concurrency,
                    "runtime_topology": {
                        "driver": "triggerflow_taskboard_lifecycle",
                        "tick": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
                    },
                },
            )
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
            revision = _unpack_revision_state(data)
            try:
                result = await self._finalize_taskboard(revision, context_pack=context_pack)
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
                    await data.async_emit_nowait(tick_requested_event, {"tick_index": next_tick_index})
                return result
            await data.async_set_state("final_result", result, emit=False)
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
        return await self._finalize_taskboard(revision, context_pack=context_pack)

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
                "planner_capabilities": self._planner_capabilities(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Plan a TaskBoard for this submitted task. "
            "TaskBoard is already selected by the caller; do not decide whether to use TaskBoard. "
            "Use task_context_contract for run-date facts, current/latest/as-of source boundaries, and ref-backed "
            "intermediate-resource handling. It is not a resource cap. "
            "Use the planning_policy as vocabulary guidance for orchestration complexity, evidence depth, "
            "reflection density, and repair tendency. Do not create hard budgets, fixed card counts, "
            "or action allowlists from the effort profile. "
            "Plan card objectives and done_when conditions around user-visible outcomes, not around one "
            "specific provider, endpoint, file format, or auxiliary guidance source unless the user explicitly "
            "requires that exact source or artifact. Mark replaceable evidence attempts, optional guidance, "
            "style checks, and non-critical cross-checks as optional or degradable through failure_policy. "
            "Use allowed_execution_shape='control' for synthesis, verification, finalization, or board-continuation "
            "decision cards that should be handled by one structured model request. Use allowed_execution_shape='readback' "
            "for cards whose only job is bounded cold artifact readback. Use an action-capable shape such as 'actions' "
            "or 'auto' for cards that need external tools, Workspace operations, side effects, or mixed action/readback work. "
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

    async def _run_taskboard_card(self, context: Any, context_pack: "WorkspaceContextPackage") -> TaskBoardCardResult:
        if self._taskboard_card_uses_readback(context.card):
            result = await self._run_taskboard_readback_card(context, context_pack)
        elif self._taskboard_card_uses_control_request(context.card):
            result = await self._run_taskboard_control_card(context, context_pack)
        else:
            result = await self._run_taskboard_agent_card(context, context_pack)
        if str(result.status).strip().lower() in _TASKBOARD_RECOVERABLE_CARD_STATUSES:
            result = await self._maybe_run_taskboard_card_acp_recovery(context, result)
        if self._should_record_process_reflection("taskboard_card", plan={}):
            await self._record_reflection(
                max(0, len(self.iterations)),
                phase="taskboard_card",
                subject_ref=None,
                summary={
                    "assessment": f"TaskBoard card {getattr(context.card, 'card_id', '')} returned {result.status}.",
                    "status": result.status,
                    "card_id": getattr(context.card, "card_id", ""),
                    "completion_evidence": False,
                },
            )
        return result

    async def _maybe_run_taskboard_card_acp_recovery(
        self,
        context: Any,
        result: TaskBoardCardResult,
    ) -> TaskBoardCardResult:
        status = str(result.status).strip().lower()
        if status not in _TASKBOARD_RECOVERABLE_CARD_STATUSES:
            return result
        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or getattr(card, "card_id", "") or result.card_id).strip()
        card_to_dict = getattr(card, "to_dict", None)
        card_payload = card_to_dict() if callable(card_to_dict) else DataFormatter.sanitize(card)
        plan = {
            "execution_shape": "taskboard",
            "effective_execution_shape": "taskboard",
            "step_instruction": str(getattr(card, "objective", "") or ""),
            "expected_evidence": list(getattr(card, "required_outputs", ()) or ()),
            "rationale": "TaskBoard card failed after its execution attempts; ACP recovery may provide fallback evidence.",
            "taskboard_card_id": card_id,
            "taskboard_card": DataFormatter.sanitize(card_payload),
        }
        failed_result = {
            "status": status,
            "step_result": result.output_digest or result.preview or "",
            "evidence": ["TaskBoard card failure evidence was captured."],
            "remaining_work": ["Recover or replace the failed TaskBoard card output."],
            "taskboard_card_result": result.to_dict(),
        }
        failed_meta = {
            "execution_id": f"{self.id}:taskboard:{card_id or result.card_id}:failed-card",
            "status": status,
            "route": {
                "selected_route": "taskboard_card",
                "status": status,
                "card_id": card_id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
            },
            "logs": {
                "route_logs": {"taskboard_card": result.to_dict()},
                "errors": list(result.diagnostics),
            },
            "diagnostics": {
                "taskboard_card": result.to_dict(),
            },
        }
        recovery_iteration_index = max(int(self.max_iterations or 1), len(self.iterations) + 1, 1)
        recovered_result, recovered_meta = await self._maybe_run_acp_recovery(
            recovery_iteration_index,
            plan=plan,
            execution_result=failed_result,
            execution_meta=failed_meta,
        )
        route = recovered_meta.get("route") if isinstance(recovered_meta, Mapping) else {}
        if not isinstance(route, Mapping) or route.get("selected_route") != "acp_recovery":
            return result
        return self._taskboard_card_result_from_acp_recovery(
            original=result,
            recovered_result=recovered_result,
            recovered_meta=recovered_meta,
        )

    def _taskboard_card_result_from_acp_recovery(
        self,
        *,
        original: TaskBoardCardResult,
        recovered_result: Any,
        recovered_meta: Mapping[str, Any],
    ) -> TaskBoardCardResult:
        recovered_status = str(recovered_meta.get("status") or "").strip().lower()
        route = recovered_meta.get("route") if isinstance(recovered_meta.get("route"), Mapping) else {}
        route_status = str(route.get("status") or "").strip().lower() if isinstance(route, Mapping) else ""
        recovered_ok = recovered_status in {"success", "completed"} or route_status in {"success", "completed"}
        recovered_map = recovered_result if isinstance(recovered_result, Mapping) else {}
        diagnostics = [
            *list(original.diagnostics),
            {
                "code": "taskboard.card.acp_recovery",
                "status": "completed" if recovered_ok else "failed",
                "recovered": recovered_ok,
                "original_status": original.status,
                "route": DataFormatter.sanitize(route),
                "workspace_refs": DataFormatter.sanitize(recovered_meta.get("workspace_refs", {})),
            },
        ]
        preview = {
            "status": "completed" if recovered_ok else "failed",
            "answer": recovered_map.get("step_result") or "ACP fallback completed.",
            "acp_recovery": DataFormatter.sanitize(recovered_map.get("acp_recovery", recovered_result)),
            "original_card_result": original.to_dict(),
            "recovery_meta": DataFormatter.sanitize(recovered_meta),
        }
        metadata = dict(original.metadata)
        metadata.update(
            {
                "acp_recovery": True,
                "acp_recovered": recovered_ok,
                "original_status": original.status,
                "recovery_route": DataFormatter.sanitize(route),
            }
        )
        return TaskBoardCardResult(
            card_id=original.card_id,
            status="completed" if recovered_ok else original.status,
            output_digest=str(recovered_map.get("step_result") or "ACP fallback completed."),
            preview=preview,
            artifact_refs=original.artifact_refs,
            file_refs=original.file_refs,
            diagnostics=tuple(diagnostics),
            patch_proposal=original.patch_proposal,
            metadata=metadata,
        )

    async def _run_taskboard_agent_card(
        self, context: Any, context_pack: "WorkspaceContextPackage"
    ) -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        readback_records = self._taskboard_action_artifact_recall_records(evidence_view)
        dependency_readbacks = await self._taskboard_dependency_action_artifact_readbacks(
            evidence_view,
            card_id=str(getattr(context.card, "id", "") or ""),
            context_pack=context_pack,
        )
        max_attempts = self._taskboard_card_max_attempts()
        previous_errors: list[dict[str, Any]] = []
        language_policy = self._language_policy()
        for attempt_index in range(1, max_attempts + 1):
            execution = self._create_bounded_child_execution(
                lineage={
                    "task_id": self.id,
                    "iteration_id": f"taskboard:{context.card.id}:attempt:{attempt_index}",
                    "step_id": "taskboard_card",
                    "scope": {
                        "strategy_phase": "taskboard_card_execution",
                        "card_id": context.card.id,
                        "attempt_index": attempt_index,
                    },
                },
                route_policy={
                    "allowed_routes": ["model_request"],
                    "on_violation": "block",
                    "owner": "AgentTaskTaskBoard",
                    "step_execution_shape": "taskboard_card",
                },
                recall_records=cast(Sequence[Mapping[str, Any]], readback_records),
                recall_source="AgentTaskTaskBoard.evidence_view",
            )
            source_refs = self._collect_taskboard_source_refs(
                evidence_view,
                dependency_readbacks,
                context.dependency_results,
                max_refs=_TASKBOARD_SOURCE_REFS_MAX,
            )
            card_input_payload = {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract(),
                "card": context.card.to_dict(),
                "dependency_results": self._compact_taskboard_dependency_results(context.dependency_results),
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_prompt(evidence_view),
                "dependency_readbacks": dependency_readbacks,
                "available_readback": self._taskboard_available_readback(evidence_view),
                "source_ref_policy": self._taskboard_source_ref_policy(),
                "scoped_retrieval": self._taskboard_card_scoped_retrieval(context.card),
                "retrieval_policy": scoped_retrieval_policy(),
                "workspace_delivery_policy": self._taskboard_workspace_delivery_policy(context),
                "source_refs": source_refs,
                "previous_attempt_errors": previous_errors,
                "attempt": {
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                },
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "language_policy": language_policy,
            }
            card_instruction = (
                "Execute exactly one TaskBoard card as a bounded AgentExecution step. "
                "Use task_context_contract.current_time when the card needs current/latest/as-of evidence; label older "
                "or historical source material with its time boundary. "
                "Use TaskBoard evidence view as the hot summary; request full content only through available "
                "Workspace or Action refs when needed. If previous_attempt_errors is non-empty, avoid repeating "
                "the same failing source or method when a bounded fallback can satisfy the card. dependency_readbacks "
                "contains framework-prefetched bounded readback previews for dependency Action artifacts that were "
                "structurally truncated or marked full_value_available; inspect those before declaring dependency "
                "evidence missing. If available_readback lists Action artifact refs and the prefetched previews are "
                "still insufficient, call read_action_artifact with the artifact_id and action_call_id before blocking "
                "on missing evidence. If scoped_retrieval_results is present, those are already executed bounded "
                "Workspace search facts; use visible evidence_snippet content only within the excerpt, and treat "
                "locator_ref records as targets for later readback/search rather than source-content proof. Return card-local evidence "
                "and remaining work. If the card's original method fails but equivalent evidence or a bounded fallback "
                "is available, return status completed with diagnostics that explain the degraded source boundary. "
                "Only return failed or blocked when the card cannot produce the required outcome or the missing "
                "evidence is truly critical. If this card produces the user-facing deliverable, use candidate_final_result, "
                "final_result, or artifact_markdown only when the complete body is short enough for the bounded output. "
                "When this bounded card response is a compact control plane for a long, sectioned, or file-backed "
                "deliverable, return only an artifact_manifest with path='final.md' and section ids/titles/brief intent; "
                "do not include full section content in artifact_manifest, artifact_markdown, candidate_final_result, "
                "final_result, or answer. AgentTask will stream the long body into Workspace and read it back. "
                "AgentTask will write/read back Workspace files and produce trusted file_refs; do not invent file_refs "
                "for deliverables. Apply workspace_delivery_policy: when this card is authorized to write required "
                "final deliverable paths, use the required path in artifact_manifest.path instead of a working/evidence path. "
                "If the task is source-grounded, include concrete source URLs, file paths, or "
                "evidence refs from source_refs/dependency_readbacks in the deliverable body; do not mention a "
                "source title or local downloaded filename without its verifier-visible URL/path when such a ref "
                f"exists. {_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION}Review or "
                "verification cards must not put review notes in those deliverable fields unless they include the "
                "full corrected deliverable body. Do not claim the whole task is complete; TaskBoard and AgentTask "
                "own lifecycle completion."
            )
            card_output_schema = {
                "status": (str, "completed, blocked, or failed for this card", False),
                "answer": (str, "Card-local result or artifact summary", True),
                "candidate_final_result": (
                    str,
                    "Complete user-facing deliverable body when this card directly produces one",
                    False,
                ),
                "final_result": (
                    str,
                    "Complete final deliverable body when this card directly produces the final answer",
                    False,
                ),
                "artifact_markdown": (
                    str,
                    "Bounded short markdown deliverable only; when this bounded JSON response is a compact control plane for a long, sectioned, or file-backed deliverable, return an artifact_manifest outline without full section content",
                    False,
                ),
                "artifact_manifest": (
                    dict,
                    "Preferred Workspace artifact manifest for sectioned or file-backed deliverables",
                    False,
                ),
                "file_refs": (
                    [dict],
                    "Existing evidence refs only; deliverable refs become trusted only after AgentTask Workspace write/readback",
                    False,
                ),
                "evidence": ([str], "Evidence produced or used by this card", False),
                "remaining_work": ([str], "Remaining work for this card, empty when complete", False),
                "diagnostics": ([dict], "Optional card diagnostics", False),
            }
            work_unit = WorkUnitIntent(
                id=f"taskboard:{context.card.id}:attempt:{attempt_index}",
                origin="taskboard_card",
                objective=str(getattr(context.card, "objective", "") or ""),
                input_payload=card_input_payload,
                input_refs=tuple(dict(item) for item in source_refs if isinstance(item, Mapping)),
                expected_deliverable={
                    "required_outputs": list(getattr(context.card, "required_outputs", ()) or ()),
                    "allowed_execution_shape": self._taskboard_card_execution_shape(context.card),
                },
                evidence_requirements=tuple(
                    {"required_output": str(item), "source": "taskboard_card"}
                    for item in list(getattr(context.card, "required_outputs", ()) or ())
                ),
                delivery_contract={
                    "card": DataFormatter.sanitize(context.card.to_dict()),
                    "execution_prompt": DataFormatter.sanitize(self._execution_prompt_context()),
                    "task_context_contract": self._task_context_contract(),
                    "scoped_retrieval": DataFormatter.sanitize(self._taskboard_card_scoped_retrieval(context.card)),
                },
                quality_gates=(
                    {
                        "kind": "taskboard_card_status",
                        "allowed_statuses": ["completed", "blocked", "failed", "skipped"],
                    },
                ),
                runtime_preferences={
                    "handler": "agent_task_bounded_step",
                    "preferred_execution_shape": "taskboard_card",
                    "strategy": "taskboard",
                    "card_id": context.card.id,
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                },
            )
            carrier_plan = self._taskboard_card_carrier_plan(context.card)

            async def run_card_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
                carrier_output_policy = self._carrier_output_policy_from_block_context(_context)
                effective_card_input_payload = self._taskboard_card_payload_with_scoped_retrieval_results(
                    card_input_payload,
                    _context,
                )
                card_result, card_meta = await self._run_bounded_child_execution(
                    execution=execution,
                    language_policy=language_policy,
                    input_payload=effective_card_input_payload,
                    instruction=card_instruction,
                    output_schema=card_output_schema,
                    output_format=self._carrier_control_output_format(carrier_output_policy),
                    use_output=self._carrier_uses_control_output(carrier_output_policy),
                    carrier_output_policy=carrier_output_policy,
                    started_event=f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.execution.started",
                    started_payload={
                        "card_id": context.card.id,
                        "attempt_index": attempt_index,
                        "max_attempts": max_attempts,
                    },
                    stream_bridge=lambda child_execution: self._bridge_taskboard_card_execution_stream(
                        context.card.id,
                        child_execution,
                    ),
                    data_waiter=lambda awaitable: self._await_taskboard_card_execution(
                        awaitable,
                        card_id=context.card.id,
                        stage="data",
                    ),
                    meta_waiter=lambda awaitable: self._await_taskboard_card_execution(
                        awaitable,
                        card_id=context.card.id,
                        stage="meta",
                    ),
                )
                return {
                    "execution_result": DataFormatter.sanitize(card_result),
                    "execution_meta": DataFormatter.sanitize(card_meta),
                }

            try:
                card_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                    work_unit=work_unit,
                    plan=carrier_plan,
                    context_pack=context_pack,
                    execution_id=f"{self.id}:taskboard:{context.card.id}:attempt:{attempt_index}",
                    handler=run_card_work_unit,
                    start_payload={
                        "card_id": context.card.id,
                        "attempt_index": attempt_index,
                        "max_attempts": max_attempts,
                    },
                )
            except Exception as error:
                execution_id = str(getattr(execution, "id", "") or "") or None
                retry_diagnostic = self._taskboard_card_retry_diagnostic(
                    card_id=context.card.id,
                    error=error,
                    execution_id=execution_id,
                    attempt_index=attempt_index,
                    max_attempts=max_attempts,
                )
                previous_errors.append(retry_diagnostic)
                if attempt_index < max_attempts and self._taskboard_card_error_retryable(error):
                    self.diagnostics.setdefault("taskboard_card_retries", []).append(retry_diagnostic)
                    await self._emit(
                        f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.execution.retry",
                        retry_diagnostic,
                    )
                    continue
                return self._failed_taskboard_card_result(
                    card_id=context.card.id,
                    error=error,
                    execution_id=execution_id,
                )
            card_output, delivery_plan = self._prepare_taskboard_workspace_artifact_delivery(
                card_output,
                context,
                deliverable_mode=self._workspace_artifact_delivery_mode(card_output),
            )
            card_output = await self._deliver_workspace_artifact(
                card_output,
                plan=delivery_plan,
                execution_meta=cast(dict[str, Any], execution_meta),
                source=f"agent_task.taskboard.card.{context.card.id}.workspace_artifact",
                context_pack=context_pack,
                card_context=context,
            )
            summary = self._execution_log_summary(cast(dict[str, Any], execution_meta))
            card_status = self._taskboard_card_status(card_output, execution_meta)
            diagnostics = []
            if isinstance(card_output, Mapping):
                raw_diagnostics = card_output.get("diagnostics")
                if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                    diagnostics.extend(
                        dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics
                    )
            output_file_refs: list[Any] = []
            if isinstance(card_output, Mapping):
                raw_file_refs = card_output.get("file_refs")
                if isinstance(raw_file_refs, Sequence) and not isinstance(raw_file_refs, str | bytes | bytearray):
                    output_file_refs.extend(DataFormatter.sanitize(item) for item in raw_file_refs)
                artifact_manifest = card_output.get("artifact_manifest")
                if isinstance(artifact_manifest, Mapping):
                    manifest_refs = artifact_manifest.get("file_refs")
                    if isinstance(manifest_refs, Sequence) and not isinstance(manifest_refs, str | bytes | bytearray):
                        output_file_refs.extend(DataFormatter.sanitize(item) for item in manifest_refs)
            compact_block_carrier = self._compact_block_carrier_for_taskboard_meta(
                execution_meta.get("block_carrier", {}),
                blocks=execution_meta.get("blocks"),
            )
            diagnostics.append(
                {
                    "execution_id": execution_meta.get("execution_id"),
                    "route": DataFormatter.sanitize(execution_meta.get("route", {})),
                    "evidence_summary": DataFormatter.sanitize(summary),
                    "block_carrier": compact_block_carrier,
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                    "previous_attempt_errors": previous_errors,
                }
            )
            patch_proposal = (
                self._taskboard_scoped_retrieval_continuation_patch(context, card_output, diagnostics)
                if isinstance(card_output, Mapping)
                else None
            )
            return TaskBoardCardResult(
                card_id=context.card.id,
                status=card_status,
                preview=DataFormatter.sanitize(card_output),
                artifact_refs=tuple(
                    [
                        *(summary.get("artifact_refs", []) if isinstance(summary.get("artifact_refs"), list) else []),
                        *output_file_refs,
                    ]
                ),
                file_refs=tuple(ref for ref in output_file_refs if isinstance(ref, Mapping)),
                diagnostics=tuple(diagnostics),
                patch_proposal=patch_proposal,
                metadata={
                    "execution_id": execution_meta.get("execution_id"),
                    "execution_strategy": self.execution_strategy,
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                    "block_carrier": compact_block_carrier,
                },
            )
        return self._failed_taskboard_card_result(
            card_id=context.card.id,
            error=RuntimeError("TaskBoard card execution exhausted retry attempts."),
            execution_id=None,
        )

    async def _run_taskboard_control_card(
        self, context: Any, context_pack: "WorkspaceContextPackage"
    ) -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        dependency_readbacks = await self._taskboard_dependency_action_artifact_readbacks(
            evidence_view,
            card_id=str(getattr(context.card, "id", "") or ""),
            context_pack=context_pack,
        )
        source_refs = self._collect_taskboard_source_refs(
            evidence_view,
            dependency_readbacks,
            context.dependency_results,
            max_refs=_TASKBOARD_SOURCE_REFS_MAX,
        )
        language_policy = self._language_policy()
        control_input_payload = {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "task_context_contract": self._task_context_contract(),
            "card": context.card.to_dict(),
            "dependency_results": self._compact_taskboard_dependency_results(context.dependency_results),
            "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_prompt(evidence_view),
            "dependency_readbacks": dependency_readbacks,
            "available_readback": self._taskboard_available_readback(evidence_view),
            "source_ref_policy": self._taskboard_source_ref_policy(),
            "workspace_delivery_policy": self._taskboard_workspace_delivery_policy(context),
            "source_refs": source_refs,
            "context_pack": DataFormatter.sanitize(context_pack),
            "execution_prompt": self._execution_prompt_context(),
            "planning_policy": (
                context.planning_policy.to_prompt_payload() if context.planning_policy is not None else {}
            ),
            "language_policy": language_policy,
        }
        control_instruction = (
            "Execute one TaskBoard control card with a single structured model request. "
            "This card is for synthesis, verification, finalization, or deciding the next board action; "
            "Use task_context_contract.current_time when current/latest/as-of evidence matters, and label older "
            "or historical source material with its time boundary. "
            "do not plan or call tools from this request. Use TaskBoardEvidenceView as the hot evidence summary "
            "and preserve cold refs as pointers. dependency_readbacks contains framework-prefetched bounded "
            "readback previews for dependency Action artifacts that were structurally truncated or marked "
            "full_value_available; inspect those before declaring dependency evidence missing. If bounded previews "
            "and dependency_readbacks are insufficient, set next_board_action to 'readback' or 'repair' and explain "
            "the exact missing refs or gaps instead of inventing facts. If a concrete URL, path, or ref must be "
            "fetched or materialized before continuing, put it in target_refs; do not mention it only in gaps prose. "
            "When the card can produce the user-facing deliverable, use artifact_markdown, candidate_final_result, "
            "or final_result only when the complete body is short enough for the bounded output. When this bounded "
            "card response is a compact control plane for a long, sectioned, or file-backed deliverable, return only "
            "an artifact_manifest with path='final.md' plus section ids/titles/brief intent; do not include full section content in artifact_manifest, artifact_markdown, "
            "candidate_final_result, final_result, or answer. AgentTask will stream the long body into Workspace "
            "and read it back. "
            "AgentTask will write/read back Workspace files and produce "
            "trusted file_refs. Do not invent file_refs for deliverables. If the task is source-grounded, include "
            "the concrete source URLs, file paths, or evidence refs used by the deliverable in the deliverable body; "
            "do not mention a source title without its verifier-visible URL/path when such a ref exists. "
            "Apply workspace_delivery_policy: when this card is authorized to write required final deliverable paths, "
            "use the required path in artifact_manifest.path instead of a working/evidence path. "
            f"{_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION}Also return whether the card is sufficient "
            "and what continuation, if any, the board should consider."
        )
        control_output_schema = {
            "status": (str, "completed, blocked, failed, or skipped for this card", False),
            "answer": (str, "Card-local synthesis or decision summary", True),
            "candidate_final_result": (
                str,
                "Complete user-facing deliverable body when this card directly produces one",
                False,
            ),
            "final_result": (
                str,
                "Complete final deliverable body when this card directly produces the final answer",
                False,
            ),
                "artifact_markdown": (
                    str,
                    "Bounded short markdown deliverable only; when this bounded JSON response is a compact control plane for a long, sectioned, or file-backed deliverable, return an artifact_manifest outline without full section content",
                    False,
                ),
            "artifact_manifest": (
                dict,
                "Preferred Workspace artifact manifest proposal for sectioned or file-backed deliverables",
                False,
            ),
            "file_refs": (
                [dict],
                "Existing evidence refs only; model-declared deliverable refs are untrusted until framework write/readback",
                False,
            ),
            "sufficient": (bool, "True when this card has enough evidence to satisfy its objective", False),
            "next_board_action": (
                str,
                "finalize, continue, readback, repair, patch, block, or stop",
                False,
            ),
            "gaps": ([str], "Evidence or quality gaps that remain after this control request", False),
            "target_refs": (
                [str],
                "Concrete URLs, paths, or refs that must be fetched/materialized as new evidence when readback needs more than existing refs",
                False,
            ),
            "evidence": ([str], "Evidence used by this control card", False),
            "remaining_work": ([str], "Remaining work for this card, empty when complete", False),
            "diagnostics": ([dict], "Optional control-card diagnostics", False),
            "patch_proposal": (
                dict,
                "Optional TaskBoardPatch or Workspace text patch proposal when next_board_action is patch",
                False,
            ),
        }
        work_unit = WorkUnitIntent(
            id=f"taskboard:{context.card.id}:control",
            origin="taskboard_card",
            objective=str(getattr(context.card, "objective", "") or ""),
            input_payload=control_input_payload,
            input_refs=tuple(dict(item) for item in source_refs if isinstance(item, Mapping)),
            expected_deliverable={
                "required_outputs": list(getattr(context.card, "required_outputs", ()) or ()),
                "allowed_execution_shape": "control",
            },
            evidence_requirements=tuple(
                {"required_output": str(item), "source": "taskboard_control_card"}
                for item in list(getattr(context.card, "required_outputs", ()) or ())
            ),
            delivery_contract={
                "card": DataFormatter.sanitize(context.card.to_dict()),
                "execution_prompt": {
                    "output": DataFormatter.sanitize(control_output_schema),
                    "output_format": "json",
                },
                "task_context_contract": self._task_context_contract(),
            },
            quality_gates=(
                {
                    "kind": "taskboard_control_card_status",
                    "allowed_statuses": ["completed", "blocked", "failed", "skipped"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_control_request",
                "preferred_execution_shape": "taskboard_control",
                "strategy": "taskboard",
                "card_id": context.card.id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_control",
            "effective_execution_shape": "taskboard_control",
            "step_instruction": str(getattr(context.card, "objective", "") or ""),
            "expected_evidence": list(getattr(context.card, "required_outputs", ()) or ()),
            "rationale": "Execute one TaskBoard control card through the shared Block carrier.",
            "step_scope": {},
        }

        async def run_control_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            carrier_output_policy = self._carrier_output_policy_from_block_context(_context)
            request = self.agent.create_temp_request()
            self._apply_language_policy_to_request(request, language_policy)
            request_payload = dict(control_input_payload)
            if isinstance(carrier_output_policy, Mapping):
                request_payload["carrier_output_policy"] = DataFormatter.sanitize(dict(carrier_output_policy))
            request.input(request_payload)
            request.instruct(control_instruction)
            request.output(
                dict(control_output_schema),
                format=self._carrier_control_output_format(carrier_output_policy),
            )
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.control.started",
                {"card_id": context.card.id},
            )
            result_handle = request.get_result()
            control_output = await self._await_taskboard_card_execution(
                self._consume_taskboard_control_request(context.card.id, result_handle),
                card_id=context.card.id,
                stage="control",
            )
            control_status = self._taskboard_control_card_status(control_output)
            return {
                "execution_result": DataFormatter.sanitize(control_output),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{context.card.id}:control",
                    "status": control_status,
                    "route": {
                        "selected_route": "model_request",
                        "status": "completed",
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [
                        {
                            "execution_kind": "taskboard_control_request",
                            "execution_strategy": self.execution_strategy,
                            "card_id": context.card.id,
                            "carrier_output_policy": DataFormatter.sanitize(carrier_output_policy),
                        }
                    ],
                },
            }

        try:
            card_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{context.card.id}:control",
                handler=run_control_work_unit,
                start_payload={"card_id": context.card.id},
            )
        except Exception as error:
            return self._failed_taskboard_card_result(
                card_id=context.card.id,
                error=error,
                execution_id=None,
            )
        required_deliverables = self._required_workspace_deliverables()
        allow_workspace_delivery = self._taskboard_control_output_allows_workspace_delivery(card_output)
        deliverable_mode = self._workspace_artifact_delivery_mode(card_output) if allow_workspace_delivery else None
        prefer_stream_draft = False
        if (
            allow_workspace_delivery
            and not deliverable_mode
            and required_deliverables
            and self._taskboard_context_card_is_leaf(context)
            and isinstance(card_output, Mapping)
        ):
            deliverable_mode = "sectioned_workspace_artifact"
            prefer_stream_draft = True
            card_output = dict(card_output)
            if not isinstance(card_output.get("artifact_manifest"), Mapping):
                card_output["artifact_manifest"] = {
                    "path": required_deliverables[0],
                    "sections": [
                        {
                            "id": "deliverable",
                            "title": "Required deliverable",
                            "intent": "Satisfy the task output contract",
                        }
                    ],
                }
        if allow_workspace_delivery:
            card_output, delivery_plan = self._prepare_taskboard_workspace_artifact_delivery(
                card_output,
                context,
                deliverable_mode=deliverable_mode,
                prefer_stream_draft=prefer_stream_draft,
            )
            card_output = await self._deliver_workspace_artifact(
                card_output,
                plan=delivery_plan,
                execution_meta=cast(dict[str, Any], execution_meta),
                source=f"agent_task.taskboard.card.{context.card.id}.workspace_artifact",
                context_pack=context_pack,
                card_context=context,
            )
        if isinstance(card_output, Mapping):
            card_output = await self._materialize_taskboard_workspace_patch(context, card_output)
        diagnostics = []
        if isinstance(card_output, Mapping):
            raw_diagnostics = card_output.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(
                    dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics
                )
        diagnostics.append(
            {
                "execution_kind": "taskboard_control_request",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "next_board_action": card_output.get("next_board_action") if isinstance(card_output, Mapping) else None,
                "sufficient": card_output.get("sufficient") if isinstance(card_output, Mapping) else None,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
            }
        )
        card_status = self._taskboard_control_card_status(card_output)
        patch_proposal = (
            self._taskboard_control_patch_proposal(context, card_output, diagnostics)
            if isinstance(card_output, Mapping)
            else None
        )
        if patch_proposal is not None and any(
            str(item.get("code") or "") == "taskboard.control.invalid_model_patch_proposal"
            for item in diagnostics
            if isinstance(item, Mapping)
        ):
            diagnostics.append(
                {
                    "code": "taskboard.control.auto_readback_patch",
                    "message": "Converted invalid model readback intent into a TaskBoardPatch with readback and continuation cards.",
                    "card_id": context.card.id,
                }
            )
        elif patch_proposal is not None and isinstance(card_output, Mapping):
            raw_patch_proposal = card_output.get("patch_proposal")
            if not isinstance(raw_patch_proposal, Mapping):
                diagnostics.append(
                    {
                        "code": "taskboard.control.auto_readback_patch",
                        "message": "Converted next_board_action=readback into a TaskBoardPatch with readback and continuation cards.",
                        "card_id": context.card.id,
                    }
                )
        if patch_proposal is None and isinstance(card_output, Mapping):
            patch_proposal = self._taskboard_scoped_retrieval_continuation_patch(
                context,
                card_output,
                diagnostics,
            )
        output_file_refs: list[Any] = []
        if isinstance(card_output, Mapping):
            raw_file_refs = card_output.get("file_refs")
            if isinstance(raw_file_refs, Sequence) and not isinstance(raw_file_refs, str | bytes | bytearray):
                output_file_refs.extend(DataFormatter.sanitize(item) for item in raw_file_refs)
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=card_status,
            preview=DataFormatter.sanitize(card_output),
            artifact_refs=tuple(output_file_refs),
            file_refs=tuple(ref for ref in output_file_refs if isinstance(ref, Mapping)),
            diagnostics=tuple(diagnostics),
            patch_proposal=patch_proposal,
            metadata={
                "execution_id": execution_meta.get("execution_id"),
                "execution_kind": "taskboard_control_request",
                "execution_strategy": self.execution_strategy,
                "next_board_action": card_output.get("next_board_action") if isinstance(card_output, Mapping) else None,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
            },
        )

    async def _consume_taskboard_control_request(self, card_id: str, result_handle: Any) -> Any:
        async for item in result_handle.get_async_generator(type="instant"):
            await self._emit_taskboard_control_stream_item(card_id, item)
        return await result_handle.async_get_data(raise_ensure_failure=False)

    async def _emit_taskboard_control_stream_item(
        self,
        card_id: str,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        return await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.control.{raw_path}",
            getattr(item, "value", None),
            event_type="delta",
            delta=getattr(item, "delta", None),
            is_complete=bool(getattr(item, "is_complete", False)),
            meta={
                "task_id": self.id,
                "status": self.status,
                "stage": "taskboard_card_control",
                "card_id": card_id,
                "stream_kind": "taskboard_control_request",
                "control_path": raw_path,
            },
        )

    async def _bridge_taskboard_card_execution_stream(self, card_id: str, execution: Any) -> None:
        try:
            async for item in execution.get_async_generator(type="instant"):
                await self._emit_taskboard_card_execution_stream_item(card_id, execution, item)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.diagnostics.setdefault("stream_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "card_id": card_id,
                    "stage": "taskboard_card",
                    "child_execution_id": str(getattr(execution, "id", "") or ""),
                }
            )

    async def _emit_taskboard_card_execution_stream_item(
        self,
        card_id: str,
        execution: Any,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        item_meta = getattr(item, "meta", None)
        meta: dict[str, Any] = {
            "task_id": self.id,
            "status": self.status,
            "stage": "taskboard_card",
            "card_id": card_id,
            "stream_kind": "child_execution",
            "child_execution_id": str(getattr(execution, "id", "") or ""),
            "child_path": raw_path,
            "child_source": str(getattr(item, "source", "") or ""),
            "child_route": str(getattr(item, "route", "") or ""),
        }
        if isinstance(item_meta, Mapping):
            meta["child_meta"] = DataFormatter.sanitize(dict(item_meta))
        return await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.execution.{raw_path}",
            getattr(item, "value", None),
            event_type=event_type,
            delta=getattr(item, "delta", None),
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            meta=meta,
        )

    async def _await_taskboard_card_execution(
        self,
        awaitable: Awaitable[Any],
        *,
        card_id: str,
        stage: str,
    ) -> Any:
        timeout = self._taskboard_card_timeout()
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as error:
            raise TimeoutError(
                f"TaskBoard card '{card_id}' {stage} request timed out after {timeout} seconds."
            ) from error

    def _failed_taskboard_card_result(
        self,
        *,
        card_id: str,
        error: Exception,
        execution_id: str | None = None,
    ) -> TaskBoardCardResult:
        message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
        is_timeout = self._is_timeout_error(error)
        if is_timeout and message == error.__class__.__name__:
            message = (
                f"TaskBoard card '{card_id}' execution timed out after " f"{self._task_request_timeout()} seconds."
            )
        diagnostic = {
            "type": error.__class__.__name__,
            "code": "taskboard.card.timeout" if is_timeout else "taskboard.card.execution_error",
            "message": message,
            "card_id": card_id,
            "execution_id": execution_id,
            "execution_strategy": self.execution_strategy,
            "stage": "taskboard_card",
            "timeout_seconds": self._taskboard_card_timeout() if is_timeout else None,
            "status": "failed",
        }
        self.diagnostics.setdefault("taskboard_card_errors", []).append(diagnostic)
        return TaskBoardCardResult(
            card_id=card_id,
            status="failed",
            preview=f"TaskBoard card execution failed: { error.__class__.__name__}: { message }",
            diagnostics=(diagnostic,),
            metadata={
                "execution_id": execution_id,
                "execution_strategy": self.execution_strategy,
                "status": "failed",
            },
        )

    @classmethod
    def _taskboard_final_refs_from_evidence_view(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
                return
            for item in value:
                if isinstance(item, Mapping):
                    refs.append(dict(DataFormatter.sanitize(item)))

        collect(evidence_view.get("artifact_refs"))
        collect(evidence_view.get("file_refs"))
        cards = evidence_view.get("cards")
        if isinstance(cards, Sequence) and not isinstance(cards, str | bytes | bytearray):
            for card in cards:
                if not isinstance(card, Mapping):
                    continue
                collect(card.get("artifact_refs"))
                collect(card.get("file_refs"))
        return cls._dedupe_ref_records(refs)

    async def _finalize_taskboard(self, revision: Any, *, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        schedule = TaskBoard(revision, handler=lambda _context: None).schedule()
        result_status = self._taskboard_terminal_status(revision, schedule)
        evidence_view = build_task_board_evidence_view(revision).to_dict()
        candidate_final_result = await self._taskboard_candidate_final_result_with_readback(revision, evidence_view)
        can_attempt_degraded_final = self._taskboard_can_attempt_degraded_final(revision, schedule)
        if result_status != "completed" and not can_attempt_degraded_final:
            self.status = "blocked" if result_status == "blocked" else "error"
            self.result = {
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "reason": "TaskBoard did not reach a completed board state.",
                "taskboard": {
                    "revision": revision.to_dict(),
                    "schedule": schedule.to_dict(),
                    "evidence_view": evidence_view,
                },
            }
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        final = await self._request_taskboard_final(
            revision,
            evidence_view,
            candidate_final_result=candidate_final_result,
            board_status=result_status,
            schedule=schedule,
            allow_degraded_final=result_status != "completed",
        )
        final = self._normalize_taskboard_final_result(final, candidate_final_result)
        accepted = self._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        final_verification: dict[str, Any] | None = None
        final_refs = self._taskboard_final_refs_from_evidence_view(evidence_view)
        if accepted:
            final_execution_result = {
                "status": "completed",
                "accepted": accepted,
                "final_result": final.get("final_result", ""),
                "reason": final.get("reason", ""),
                "missing_criteria": final.get("missing_criteria", []),
                "file_refs": final_refs,
                "artifact_refs": final_refs,
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_stream(evidence_view),
            }
            final_execution_meta = {
                "status": "completed",
                "route": {
                    "selected_route": "agent_task",
                    "execution_strategy": self.execution_strategy,
                    "effective_execution_strategy": self.effective_execution_strategy,
                },
                "logs": {"artifact_refs": final_refs},
                "workspace_refs": {"agent_task_artifacts": final_refs},
                "diagnostics": {"taskboard_terminal_status": result_status},
            }
            try:
                final_verification = await self._request_verification(
                    max(len(self.iterations) + 1, 1),
                    plan={
                        "execution_shape": "taskboard",
                        "effective_execution_shape": "taskboard",
                        "deliverable_mode": "workspace_artifact",
                        "expected_evidence": "TaskBoard final deliverable and trusted Workspace refs",
                    },
                    execution_result=final_execution_result,
                    execution_meta=final_execution_meta,
                    context_pack=context_pack,
                )
            except Exception as error:
                message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                final_verification = {
                    "is_complete": False,
                    "requires_block": True,
                    "reason": "TaskBoard final verification failed structurally.",
                    "failure_analysis": message,
                    "acceptance_delta": ["TaskBoard final verification must return structured completion status."],
                    "missing_criteria": ["TaskBoard final verification did not produce a valid structured result."],
                    "replan_instruction": "Run a continuation step that produces verifier-readable final evidence.",
                    "repair_constraints": ["Preserve trusted Workspace refs and final deliverable evidence."],
                    "next_step_requirements": ["Return structured verification fields."],
                    "final_result_required": True,
                    "final_result": "",
                    "guard_reasons": ["taskboard_final_verification_error"],
                    "error": {"type": error.__class__.__name__, "message": message},
                }
            missing_deliverables = await self._missing_required_workspace_deliverables()
            if missing_deliverables:
                self._guard_missing_required_deliverables(final_verification, missing_deliverables)
            if final_verification is not None and not bool(final_verification.get("is_complete")):
                repair_revision = None
                if not bool(final_verification.get("requires_block")):
                    repair_revision = self._taskboard_final_verification_repair_revision(
                        revision,
                        final=final,
                        final_verification=final_verification,
                    )
                if repair_revision is not None:
                    await self._record_phase(
                        "taskboard_final_repair_requested",
                        diagnostics={
                            "revision_id": repair_revision.revision_id,
                            "previous_revision_id": revision.revision_id,
                            "reason": final_verification.get("reason"),
                            "missing_criteria": final_verification.get("missing_criteria", []),
                        },
                    )
                    await self._emit(
                        "agent_task.taskboard.final_verification.repair_requested",
                        {
                            "revision_id": repair_revision.revision_id,
                            "previous_revision_id": revision.revision_id,
                            "missing_criteria": final_verification.get("missing_criteria", []),
                        },
                    )
                    return {
                        "terminal": False,
                        "status": "repair_requested",
                        "revision": repair_revision.to_dict(),
                        "final_verification": DataFormatter.sanitize(final_verification),
                    }
                accepted = False
                final = dict(final)
                final["accepted"] = False
                final["reason"] = final_verification.get("reason") or "TaskBoard final verification failed."
                final["missing_criteria"] = final_verification.get("missing_criteria", [])
                final["final_result"] = final_verification.get("final_result") or final.get("final_result", "")
        self.status = "completed" if accepted else "blocked"
        self.result = {
            "status": self.status,
            "accepted": accepted,
            "artifact_status": "accepted" if accepted else "partial",
            "task_id": self.id,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "final_result": final.get("final_result", ""),
            "reason": final.get("reason", ""),
            "missing_criteria": final.get("missing_criteria", []),
            "taskboard": {
                "revision": revision.to_dict(),
                "schedule": schedule.to_dict(),
                "evidence_view": evidence_view,
                "terminal_status": result_status,
                "degraded_finalization_attempted": result_status != "completed",
                "final_verification": final_verification,
            },
        }
        await self._record_phase(
            "terminal",
            diagnostics={
                "status": self.status,
                "accepted": accepted,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "taskboard_revision_id": revision.revision_id,
            },
        )
        await self._emit("agent_task.completed" if accepted else "agent_task.blocked", self.result)
        return {"terminal": True, "status": self.status}

    async def _request_taskboard_final(
        self,
        revision: Any,
        evidence_view: Mapping[str, Any],
        *,
        candidate_final_result: str = "",
        board_status: str = "completed",
        schedule: Any = None,
        allow_degraded_final: bool = False,
    ) -> dict[str, Any]:
        language_policy = self._language_policy()
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "board_status": board_status,
                "allow_degraded_final": allow_degraded_final,
                "schedule": DataFormatter.sanitize(schedule.to_dict() if schedule is not None else {}),
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_prompt(evidence_view),
                "revision": self._compact_taskboard_revision_for_prompt(revision),
                "candidate_final_result": self._compact_verifier_prompt_value(candidate_final_result),
                "execution_prompt": self._execution_prompt_context(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Synthesize the final result for this TaskBoard task from completed card evidence. "
            "Verify every success criterion. Use the hot evidence view for summaries and preserve cold refs "
            "as evidence pointers; do not invent unsupported facts. When candidate_final_result contains a "
            "complete answer/report/artifact body that satisfies the criteria, preserve it as final_result "
            "instead of rewriting it into a shorter summary. For source-grounded tasks, the final_result must include "
            "the concrete source URLs, file paths, or evidence refs that support the deliverable; source titles or "
            "general source names without verifier-visible URL/path refs are not enough when refs are available. "
            "If allow_degraded_final is true, the board has stopped with failed, blocked, skipped, or pending "
            "cards. You may still accept only when the completed/degraded evidence is enough to satisfy the "
            "user goal and success criteria with explicit missing-source or degraded-source boundaries in "
            "the final_result. If critical evidence is missing, set accepted=false and explain the missing criteria."
        )
        request.output(
            {
                "accepted": (bool, "True only when all success criteria are satisfied", True),
                "reason": (str, "Concise final verification reason", True),
                "final_result": (str, "Final business result when accepted", True),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when accepted", False),
            },
            format="json",
        )
        result = await self._await_task_request(request.async_get_data(), stage="taskboard_finalize")
        if isinstance(result, Mapping):
            return dict(result)
        return {"accepted": False, "reason": str(result), "final_result": "", "missing_criteria": self.success_criteria}

    def _taskboard_candidate_final_result(self, revision: Any) -> str:
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        card_results = getattr(revision, "card_results", {}) or {}
        depended_on: set[str] = set()
        for card in cards:
            depended_on.update(str(card_id) for card_id in getattr(card, "depends_on", ()) or ())
        leaf_ids = {str(getattr(card, "id", "")) for card in cards if str(getattr(card, "id", "")) not in depended_on}
        structured_candidates: list[str] = []
        leaf_fallback_candidates: list[str] = []
        fallback_candidates: list[str] = []
        for card_id, result in card_results.items():
            if str(getattr(result, "status", "")) != "completed":
                continue
            preview = getattr(result, "preview", None)
            structured_candidate = self._candidate_final_result_from_execution_result(
                preview,
                include_answer=False,
            )
            if structured_candidate:
                structured_candidates.append(structured_candidate)
                continue
            fallback_candidate = self._candidate_final_result_from_execution_result(
                preview,
                include_answer=True,
            )
            if not fallback_candidate:
                continue
            fallback_candidates.append(fallback_candidate)
            if not leaf_ids or str(card_id) in leaf_ids:
                leaf_fallback_candidates.append(fallback_candidate)
        if structured_candidates:
            return max(structured_candidates, key=len, default="")
        if leaf_fallback_candidates:
            return max(leaf_fallback_candidates, key=len, default="")
        return max(fallback_candidates, key=len, default="")

    async def _taskboard_candidate_final_result_with_readback(
        self,
        revision: Any,
        evidence_view: Mapping[str, Any],
    ) -> str:
        hot_candidate = self._taskboard_candidate_final_result(revision)
        readback_candidate = await self._taskboard_workspace_candidate_from_refs(evidence_view)
        if not readback_candidate:
            return hot_candidate
        if (
            not hot_candidate
            or len(readback_candidate) > len(hot_candidate)
            or self._looks_like_workspace_artifact_placeholder(hot_candidate)
        ):
            return readback_candidate
        return hot_candidate

    @classmethod
    def _normalize_taskboard_final_result(cls, final: dict[str, Any], candidate_final_result: str) -> dict[str, Any]:
        candidate = candidate_final_result.strip()
        if not candidate:
            return final
        accepted = cls._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        if not accepted:
            return final
        final_result = str(final.get("final_result") or "").strip()
        if (
            not final_result
            or cls._looks_like_candidate_prefix(final_result, candidate)
            or cls._candidate_substantially_more_complete(final_result, candidate)
        ):
            normalized = dict(final)
            normalized["final_result"] = candidate
            return normalized
        return final

    @staticmethod
    def _looks_like_candidate_prefix(value: str, candidate: str) -> bool:
        value = value.strip()
        candidate = candidate.strip()
        if not value or len(value) >= len(candidate):
            return False
        if candidate.startswith(value):
            return True
        compact_value = " ".join(value.split())
        compact_candidate = " ".join(candidate.split())
        return len(compact_value) < len(compact_candidate) and compact_candidate.startswith(compact_value)

    @staticmethod
    def _candidate_substantially_more_complete(value: str, candidate: str) -> bool:
        value = value.strip()
        candidate = candidate.strip()
        if len(candidate) < 1200 or len(value) >= len(candidate):
            return False
        return len(value) <= max(800, len(candidate) // 2)

    def _taskboard_effort(self) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping):
            effort = agent_task_options.get("effort")
            if effort is not None:
                return effort
        return "medium"

    def _taskboard_tick_timeout(self) -> float | None:
        return self._taskboard_option_timeout("taskboard_tick_timeout_seconds")

    def _taskboard_card_timeout(self) -> float | None:
        return self._taskboard_option_timeout("taskboard_card_timeout_seconds")

    def _taskboard_card_max_attempts(self) -> int:
        value = self._taskboard_option("taskboard_card_max_attempts")
        try:
            attempts = int(value) if value is not None else 2
        except (TypeError, ValueError):
            attempts = 2
        return min(max(1, attempts), 5)

    def _taskboard_card_error_retryable(self, error: Exception) -> bool:
        if self._is_timeout_error(error):
            return True
        text = f"{ error.__class__.__name__}: { str(error) }".lower()
        retry_markers = (
            "429",
            "chunked",
            "connect",
            "connection",
            "eof",
            "parse_failed",
            "rate limit",
            "request failed",
            "request_failed",
            "temporarily",
            "timeout",
            "tls",
        )
        return any(marker in text for marker in retry_markers)

    def _taskboard_card_retry_diagnostic(
        self,
        *,
        card_id: str,
        error: Exception,
        execution_id: str | None,
        attempt_index: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
        return {
            "type": error.__class__.__name__,
            "code": "taskboard.card.timeout" if self._is_timeout_error(error) else "taskboard.card.execution_error",
            "message": message[:1000],
            "card_id": card_id,
            "execution_id": execution_id,
            "execution_strategy": self.execution_strategy,
            "stage": "taskboard_card",
            "attempt_index": attempt_index,
            "max_attempts": max_attempts,
            "retry_scheduled": attempt_index < max_attempts,
            "timeout_seconds": self._taskboard_card_timeout() if self._is_timeout_error(error) else None,
            "status": "retrying" if attempt_index < max_attempts else "failed",
        }

    def _taskboard_max_ticks(self) -> int | None:
        value = self._taskboard_option("taskboard_max_ticks")
        if value is None:
            return self.max_iterations
        try:
            ticks = int(value)
        except (TypeError, ValueError):
            return self.max_iterations
        return max(1, ticks)

    def _taskboard_max_ticks_source(self) -> str:
        if self._taskboard_option("taskboard_max_ticks") is not None:
            return "taskboard_option"
        if self.max_iterations is not None:
            return "explicit_max_iterations"
        return "unbounded_default"

    def _taskboard_concurrency(self) -> int | None:
        value = self._taskboard_option("taskboard_concurrency")
        if value is None:
            return None
        try:
            concurrency = int(value)
        except (TypeError, ValueError):
            return None
        return concurrency if concurrency > 0 else None

    def _taskboard_option_timeout(self, key: str) -> float | None:
        value = self._taskboard_option(key)
        if value is None:
            return None
        return self._normalize_timeout(value)

    def _taskboard_option(self, key: str) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping) and key in agent_task_options:
            return agent_task_options.get(key)
        return self.options.get(key)

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
            elif status not in {"completed", "failed", "blocked", "skipped"}:
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
        if "blocked" in required_statuses.values() or schedule.blocked_card_ids:
            return "blocked"
        if cls._taskboard_revision_completed(revision):
            return "completed"
        return "running"

    @staticmethod
    def _taskboard_card_status(card_output: Any, execution_meta: Mapping[str, Any]) -> str:
        execution_status = str(execution_meta.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            return "failed"
        if isinstance(card_output, Mapping):
            status = str(card_output.get("status") or "completed").strip().lower()
            if status in {"completed", "blocked", "failed", "skipped"}:
                return status
            remaining = card_output.get("remaining_work")
            if isinstance(remaining, Sequence) and not isinstance(remaining, str | bytes | bytearray) and remaining:
                return "blocked"
        return "completed"

    @classmethod
    def _taskboard_control_card_status(cls, card_output: Any) -> str:
        if isinstance(card_output, Mapping):
            status = str(card_output.get("status") or "completed").strip().lower()
            if status in {"completed", "blocked", "failed", "skipped"}:
                return status
            next_action = str(card_output.get("next_board_action") or "").strip().lower()
            if next_action in {"readback", "needs_readback", "repair", "patch", "continue", "block"}:
                return "blocked"
            remaining = card_output.get("remaining_work")
            gaps = card_output.get("gaps")
            if cls._has_remaining_work(remaining) or cls._has_remaining_work(gaps):
                return "blocked"
        return "completed"

    @staticmethod
    def _taskboard_card_execution_shape(card: Any) -> str:
        return str(getattr(card, "allowed_execution_shape", "") or "auto").strip().lower().replace("-", "_")

    @classmethod
    def _taskboard_card_uses_control_request(cls, card: Any) -> bool:
        return cls._taskboard_card_execution_shape(card) in _TASKBOARD_CONTROL_CARD_SHAPES

    @classmethod
    def _taskboard_card_uses_readback(cls, card: Any) -> bool:
        return cls._taskboard_card_execution_shape(card) in _TASKBOARD_READBACK_CARD_SHAPES

    def _prepare_taskboard_workspace_artifact_delivery(
        self,
        card_output: Any,
        context: Any,
        *,
        deliverable_mode: str | None,
        prefer_stream_draft: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        plan: dict[str, Any] = {"deliverable_mode": str(deliverable_mode or "").strip()}
        if prefer_stream_draft:
            plan["prefer_stream_draft"] = True
        if not plan["deliverable_mode"] or not isinstance(card_output, Mapping):
            return card_output, plan
        required_paths = {str(path or "").strip() for path in self._required_workspace_deliverables()}
        final_card_paths = [
            path for path in self._taskboard_context_final_workspace_deliverables(context) if path in required_paths
        ]
        if final_card_paths:
            manifest = card_output.get("artifact_manifest")
            manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
            requested_path = self._workspace_artifact_manifest_path(manifest_dict)
            if requested_path in final_card_paths:
                return card_output, plan
            manifest_dict["path"] = final_card_paths[0]
            result = dict(card_output)
            result["artifact_manifest"] = manifest_dict
            diagnostics: list[Any] = []
            raw_diagnostics = result.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(raw_diagnostics)
            elif raw_diagnostics:
                diagnostics.append(raw_diagnostics)
            diagnostics.append(
                {
                    "code": "taskboard.workspace_artifact.final_path_authorized",
                    "message": "A framework-marked final TaskBoard card is authorized to write the required deliverable path.",
                    "requested_path": requested_path,
                    "final_path": final_card_paths[0],
                }
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return result, plan
        if not required_paths or self._taskboard_context_card_is_leaf(context):
            return card_output, plan

        manifest = card_output.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        requested_path = self._workspace_artifact_manifest_path(manifest_dict)
        if requested_path not in required_paths:
            return card_output, plan

        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or "card").strip() or "card"
        safe_card_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in card_id) or "card"
        file_name = Path(requested_path).name or "artifact.md"
        relocated_path = f"working/taskboard/{safe_card_id}/{file_name}"
        manifest_dict["path"] = relocated_path

        result = dict(card_output)
        result["artifact_manifest"] = manifest_dict
        diagnostics: list[Any] = []
        raw_diagnostics = result.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(raw_diagnostics)
        elif raw_diagnostics:
            diagnostics.append(raw_diagnostics)
        diagnostics.append(
            {
                "code": "taskboard.workspace_artifact.final_path_relocated_for_intermediate_card",
                "message": "A non-leaf TaskBoard card cannot write a required final deliverable path.",
                "card_id": card_id,
                "requested_path": requested_path,
                "relocated_path": relocated_path,
            }
        )
        result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return result, plan

    @classmethod
    def _taskboard_context_final_workspace_deliverables(cls, context: Any) -> list[str]:
        card = getattr(context, "card", None)
        metadata = getattr(card, "metadata", None)
        if not isinstance(metadata, Mapping):
            return []
        return cls._normalize_string_list(metadata.get("final_workspace_deliverables"))

    def _taskboard_workspace_delivery_policy(self, context: Any) -> dict[str, Any]:
        required_paths = self._required_workspace_deliverables()
        final_card_paths = [
            path for path in self._taskboard_context_final_workspace_deliverables(context) if path in required_paths
        ]
        can_write_required = bool(required_paths and (final_card_paths or self._taskboard_context_card_is_leaf(context)))
        return {
            "schema_version": "agent_task_taskboard_workspace_delivery/v1",
            "required_deliverables": required_paths,
            "authorized_final_deliverable_paths": final_card_paths or (required_paths if can_write_required else []),
            "can_write_required_deliverables": can_write_required,
            "policy": (
                "Use required deliverable paths for final or framework-marked repair/continuation cards. "
                "Use working refs for intermediate evidence cards."
            ),
        }

    def _taskboard_card_scoped_retrieval(self, card: Any) -> dict[str, Any]:
        for container in (
            getattr(card, "metadata", None),
            getattr(card, "evidence_contract", None),
        ):
            if not isinstance(container, Mapping):
                continue
            normalized = self._normalize_scoped_retrieval_plan(container.get("scoped_retrieval"))
            if normalized:
                return normalized
        return {}

    def _taskboard_card_carrier_plan(self, card: Any) -> dict[str, Any]:
        plan = {
            "execution_shape": "taskboard_card",
            "effective_execution_shape": "taskboard_card",
            "step_instruction": str(getattr(card, "objective", "") or ""),
            "expected_evidence": list(getattr(card, "required_outputs", ()) or ()),
            "rationale": "Execute one TaskBoard card through the shared Block carrier.",
            "step_scope": {},
        }
        scoped_retrieval = self._taskboard_card_scoped_retrieval(card)
        if scoped_retrieval:
            plan["scoped_retrieval"] = scoped_retrieval
        return plan

    def _taskboard_card_payload_with_scoped_retrieval_results(
        self,
        card_input_payload: Mapping[str, Any],
        block_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(card_input_payload)
        scoped_results = self._scoped_retrieval_results_from_block_context(block_context)
        if scoped_results:
            payload["scoped_retrieval_results"] = DataFormatter.sanitize(scoped_results)
        return payload

    @classmethod
    def _taskboard_source_ref_policy(cls) -> dict[str, Any]:
        return {
            "schema_version": "agent_task_taskboard_source_refs/v1",
            "content_states": {
                "ref_only": (
                    "The URL/path/artifact was discovered or materialized, but the current input does not contain "
                    "a bounded content readback for it."
                ),
                "bounded_readback_available": (
                    "The current input contains a bounded readback or content preview for this ref. Use only the "
                    "visible preview unless a later block reads more."
                ),
            },
            "rules": [
                "Keep downloads, webpage snapshots, notes, generated code, and extracted text as cold refs unless "
                "a later block needs scoped content.",
                "Do not claim source contents from ref_only records.",
                "Use scoped retrieval query groups for Workspace/repository/file evidence before broad reads when it can reduce prompt input.",
                "Use search_surface='workspace_index' for Workspace SQLite/FTS records, 'workspace_files' for bounded file search, or 'workspace_index_and_files' when both bounded surfaces are justified; for workspace_index records, put collection names in filters.collection, do not put collection names in path, and use filters.kind only when the exact record kind is provided; never infer a generic kind such as note. For workspace_files, query is content text or an exact phrase, path is the directory/file scope, and pattern is one file glob such as *.md, * or **. Do not put list/read/search commands in query.",
                "Treat truncated evidence snippets as partial facts; downstream consumers decide whether to request wider scoped retrieval, readback, or continuation.",
                "Treat local search results as bounded facts, not as semantic acceptance.",
                "When unread source content is required, return next_board_action=readback with concrete "
                "target_refs or use an available readback action.",
                "If a ref remains unread but is still useful, label it as discovered-only in the deliverable or "
                "diagnostics.",
            ],
            "scoped_retrieval_policy": scoped_retrieval_policy(),
        }

    @classmethod
    def _taskboard_source_ref_content_state(cls, candidate: Mapping[str, Any]) -> str:
        raw_state = str(
            candidate.get("content_state")
            or candidate.get("readback_state")
            or candidate.get("materialization_state")
            or ""
        ).strip()
        if raw_state in {"bounded_readback_available", "bounded_preview_available", "content_read"}:
            return "bounded_readback_available"
        if raw_state in {"ref_only", "discovered_only", "unread"}:
            return "ref_only"

        readback_keys = (
            "content_preview",
            "value_preview",
            "readback_preview",
            "bounded_preview",
            "file_readbacks",
            "readbacks",
            "workspace_readback",
            "artifact_readback",
        )
        for key in readback_keys:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return "bounded_readback_available"
            if isinstance(value, Mapping) and value:
                return "bounded_readback_available"
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray) and value:
                return "bounded_readback_available"
        return "ref_only"

    @classmethod
    def _collect_taskboard_source_refs(
        cls,
        *values: Any,
        max_refs: int = _TASKBOARD_SOURCE_REFS_MAX,
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        url_keys = {
            "source_url",
            "selected_url",
            "requested_url",
            "canonical_url",
            "url",
            "href",
        }
        metadata_keys = {
            "role",
            "source",
            "record_id",
            "collection",
            "kind",
            "artifact_id",
            "action_call_id",
            "label",
            "title",
        }

        def normalize_url(raw: Any) -> str:
            text = str(raw or "").strip()
            if text.startswith("http://") or text.startswith("https://"):
                return text
            return ""

        def add(candidate: Mapping[str, Any]) -> None:
            if len(refs) >= max_refs:
                return
            record: dict[str, Any] = {}
            for key in url_keys:
                url = normalize_url(candidate.get(key))
                if url:
                    record[key] = url
            path = str(candidate.get("path") or "").strip()
            if path and len(path) <= 500:
                record["path"] = path
            for key in metadata_keys:
                if key in record or key == "path":
                    continue
                item = candidate.get(key)
                if item is None:
                    continue
                if isinstance(item, (str, int, float, bool)):
                    text = str(item).strip()
                    if text:
                        record[key] = text[:500]
            if not record:
                return
            if not any(key in record for key in url_keys) and not record.get("path"):
                return
            record["content_state"] = cls._taskboard_source_ref_content_state(candidate)
            dedupe_key = "|".join(
                str(record.get(field) or "")
                for field in (
                    "source_url",
                    "selected_url",
                    "requested_url",
                    "canonical_url",
                    "url",
                    "href",
                    "path",
                    "record_id",
                    "artifact_id",
                    "action_call_id",
                )
            )
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            refs.append(DataFormatter.sanitize(record))

        def visit(value: Any, *, depth: int = 0) -> None:
            if len(refs) >= max_refs or depth > 8:
                return
            if isinstance(value, Mapping):
                add(value)
                for item in value.values():
                    if isinstance(item, (Mapping, list, tuple)):
                        visit(item, depth=depth + 1)
                return
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    visit(item, depth=depth + 1)
                    if len(refs) >= max_refs:
                        break

        for value in values:
            visit(value)
            if len(refs) >= max_refs:
                break
        return refs


__all__ = ["AgentTaskTaskBoardStrategyMixin"]
