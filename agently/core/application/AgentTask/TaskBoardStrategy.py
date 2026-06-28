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

from agently.core.orchestration import TaskBoardValidator
from agently.types.data import TaskBoardPatch

from .TaskShared import *


_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION = (
    "Apply source_ref_policy. A source ref with content_state='ref_only' proves only that a URL, path, "
    "download, snapshot, note, or artifact ref was discovered or materialized; it is not evidence that the "
    "source content has been read. Use it as content support only after a bounded readback/content preview is "
    "available. If the deliverable depends on unread source content, request readback with target_refs or call "
    "the available readback action; otherwise label the ref as discovered-only and do not claim facts from it. "
    "When target refs point at Workspace/repository/file evidence, prefer scoped search/readback that returns "
    "locator_ref or evidence_snippet before requesting broad content. "
)


class AgentTaskTaskBoardStrategyMixin(AgentTaskMixinBase):
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

    @classmethod
    def _compact_taskboard_card_result_for_prompt(cls, result: Any) -> dict[str, Any]:
        try:
            effective = TaskBoardCardResult.from_value(result)
        except Exception:
            return {
                "status": "unknown",
                "preview": cls._compact_verifier_prompt_value(result, max_chars=_TASKBOARD_PROMPT_RESULT_CHARS),
            }
        artifact_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.artifact_refs)[:12]]
        if len(effective.artifact_refs) > 12:
            artifact_refs.append({"omitted": len(effective.artifact_refs) - 12, "reason": "prompt_budget"})
        file_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.file_refs)[:12]]
        if len(effective.file_refs) > 12:
            file_refs.append({"omitted": len(effective.file_refs) - 12, "reason": "prompt_budget"})
        diagnostics = list(effective.diagnostics)
        compact = {
            "schema_version": effective.schema_version,
            "card_id": effective.card_id,
            "status": effective.status,
            "output_digest": effective.output_digest,
            "preview": cls._compact_verifier_prompt_value(
                effective.preview,
                max_chars=_TASKBOARD_PROMPT_RESULT_CHARS,
            ),
            "artifact_refs": artifact_refs,
            "file_refs": file_refs,
            "diagnostics": cls._compact_verifier_prompt_value(diagnostics[:8], max_chars=1200),
            "metadata": cls._compact_verifier_prompt_value(effective.metadata, max_chars=1000),
        }
        if len(diagnostics) > 8:
            compact["diagnostics_omitted"] = {"count": len(diagnostics) - 8, "reason": "prompt_budget"}
        return compact

    @classmethod
    def _compact_taskboard_card_result_for_stream(cls, result: Any) -> dict[str, Any]:
        try:
            effective = TaskBoardCardResult.from_value(result)
        except Exception:
            return {
                "status": "unknown",
                "preview": cls._compact_verifier_prompt_value(result, max_chars=700),
            }
        artifact_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.artifact_refs)[:4]]
        file_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.file_refs)[:4]]
        return {
            "schema_version": effective.schema_version,
            "card_id": effective.card_id,
            "status": effective.status,
            "output_digest": effective.output_digest,
            "preview": cls._compact_verifier_prompt_value(effective.preview, max_chars=700),
            "artifact_refs": artifact_refs,
            "artifact_refs_omitted": max(0, len(effective.artifact_refs) - 4),
            "file_refs": file_refs,
            "file_refs_omitted": max(0, len(effective.file_refs) - 4),
            "diagnostics": cls._compact_verifier_prompt_value(list(effective.diagnostics)[:4], max_chars=700),
            "metadata": cls._compact_verifier_prompt_value(effective.metadata, max_chars=500),
        }

    @classmethod
    def _compact_taskboard_dependency_results(cls, dependency_results: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(card_id): cls._compact_taskboard_card_result_for_prompt(result)
            for card_id, result in dict(dependency_results).items()
        }

    @classmethod
    def _compact_taskboard_revision_for_prompt(
        cls,
        revision: Any,
        *,
        include_card_results: bool = True,
    ) -> dict[str, Any]:
        effective = TaskBoardRevision.from_value(revision)
        cards = []
        for card in effective.graph.cards:
            cards.append(
                {
                    "id": card.id,
                    "status": card.status,
                    "objective": card.objective,
                    "depends_on": list(card.depends_on),
                    "required_outputs": list(card.required_outputs),
                    "allowed_execution_shape": card.allowed_execution_shape,
                    "failure_policy": card.failure_policy,
                    "evidence_contract": cls._compact_verifier_prompt_value(
                        card.evidence_contract,
                        max_chars=800,
                    ),
                    "metadata": cls._compact_verifier_prompt_value(card.metadata, max_chars=800),
                }
            )
        compact = {
            "schema_version": effective.schema_version,
            "board_id": effective.board_id,
            "revision_id": effective.revision_id,
            "status": effective.status,
            "graph": {
                "schema_version": effective.graph.schema_version,
                "graph_id": effective.graph.graph_id,
                "cards": cards,
                "metadata": cls._compact_verifier_prompt_value(effective.graph.metadata, max_chars=1000),
            },
            "card_result_statuses": {
                str(card_id): str(result.status) for card_id, result in effective.card_results.items()
            },
            "evidence_refs": [
                cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.evidence_refs)[:16]
            ],
            "diagnostics": cls._compact_verifier_prompt_value(list(effective.diagnostics)[:16], max_chars=1600),
            "metadata": cls._compact_verifier_prompt_value(effective.metadata, max_chars=1200),
        }
        if include_card_results:
            compact["card_results"] = {
                str(card_id): cls._compact_taskboard_card_result_for_prompt(result)
                for card_id, result in effective.card_results.items()
            }
        return compact

    @classmethod
    def _compact_taskboard_revision_for_stream(cls, revision: Any) -> dict[str, Any]:
        effective = TaskBoardRevision.from_value(revision)
        return {
            "schema_version": effective.schema_version,
            "board_id": effective.board_id,
            "revision_id": effective.revision_id,
            "status": effective.status,
            "graph_id": effective.graph.graph_id,
            "cards": [
                {
                    "id": card.id,
                    "status": card.status,
                    "depends_on": list(card.depends_on),
                    "failure_policy": card.failure_policy,
                }
                for card in effective.graph.cards
            ],
            "card_result_statuses": {
                str(card_id): str(result.status) for card_id, result in effective.card_results.items()
            },
        }

    @classmethod
    def _compact_taskboard_evidence_view_for_stream(cls, evidence_view: Mapping[str, Any]) -> Any:
        return cls._compact_verifier_prompt_value(evidence_view, max_chars=_TASKBOARD_STREAM_SUMMARY_CHARS)

    @staticmethod
    def _prompt_sequence(value: Any) -> Sequence[Any]:
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return value
        return ()

    @classmethod
    def _compact_taskboard_evidence_view_for_prompt(cls, evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        raw_cards = evidence_view.get("cards")
        cards = []
        for card in cls._prompt_sequence(raw_cards):
            if not isinstance(card, Mapping):
                continue
            diagnostics = []
            for diagnostic in list(cls._prompt_sequence(card.get("diagnostics")))[:4]:
                if isinstance(diagnostic, Mapping):
                    compact_diagnostic = dict(diagnostic)
                    if "block_carrier" in compact_diagnostic:
                        compact_diagnostic["block_carrier"] = cls._compact_block_carrier_for_taskboard_meta(
                            compact_diagnostic.get("block_carrier")
                        )
                    diagnostics.append(compact_diagnostic)
                else:
                    diagnostics.append({"value": diagnostic})
            metadata = card.get("metadata", {})
            if isinstance(metadata, Mapping) and "block_carrier" in metadata:
                metadata = dict(metadata)
                metadata["block_carrier"] = cls._compact_block_carrier_for_taskboard_meta(metadata.get("block_carrier"))
            workspace_operations = cls._taskboard_card_workspace_operations_for_prompt(
                diagnostics=diagnostics,
                metadata=metadata,
            )
            artifact_refs_source = cls._prompt_sequence(card.get("artifact_refs"))
            file_refs_source = cls._prompt_sequence(card.get("file_refs"))
            source_refs_value = card.get("source_refs")
            source_refs_sequence = cls._prompt_sequence(source_refs_value)
            source_refs_source = cls._collect_taskboard_source_refs(source_refs_value, max_refs=8)
            artifact_refs = [
                cls._compact_artifact_ref_for_verifier(ref)
                for ref in list(artifact_refs_source)[:8]
                if isinstance(ref, Mapping)
            ]
            file_refs = [
                cls._compact_artifact_ref_for_verifier(ref)
                for ref in list(file_refs_source)[:8]
                if isinstance(ref, Mapping)
            ]
            cards.append(
                {
                    "card_id": card.get("card_id", card.get("id")),
                    "status": card.get("status"),
                    "output_digest": card.get("output_digest"),
                    "preview": cls._compact_verifier_prompt_value(
                        card.get("preview", card.get("summary", card.get("answer"))),
                        max_chars=_TASKBOARD_PROMPT_RESULT_CHARS,
                    ),
                    "artifact_refs": artifact_refs,
                    "artifact_refs_omitted": max(0, len(artifact_refs_source) - 8),
                    "file_refs": file_refs,
                    "file_refs_omitted": max(0, len(file_refs_source) - 8),
                    "source_refs": source_refs_source,
                    "source_refs_omitted": max(0, len(source_refs_sequence) - 8),
                    "workspace_operations": workspace_operations,
                    "diagnostics": cls._compact_verifier_prompt_value(
                        diagnostics,
                        max_chars=800,
                    ),
                    "metadata": cls._compact_verifier_prompt_value(metadata, max_chars=600),
                }
            )
        artifact_refs_source = cls._prompt_sequence(evidence_view.get("artifact_refs"))
        file_refs_source = cls._prompt_sequence(evidence_view.get("file_refs"))
        source_refs_value = evidence_view.get("source_refs")
        source_refs_sequence = cls._prompt_sequence(source_refs_value)
        source_refs_source = cls._collect_taskboard_source_refs(source_refs_value, max_refs=16)
        artifact_refs = [
            cls._compact_artifact_ref_for_verifier(ref)
            for ref in list(artifact_refs_source)[:16]
            if isinstance(ref, Mapping)
        ]
        file_refs = [
            cls._compact_artifact_ref_for_verifier(ref)
            for ref in list(file_refs_source)[:16]
            if isinstance(ref, Mapping)
        ]
        return {
            "schema_version": evidence_view.get("schema_version"),
            "revision_id": evidence_view.get("revision_id"),
            "status_counts": DataFormatter.sanitize(evidence_view.get("status_counts", {})),
            "metadata": cls._compact_verifier_prompt_value(evidence_view.get("metadata", {}), max_chars=600),
            "cards": cards,
            "cards_omitted": max(0, len(cls._prompt_sequence(raw_cards)) - len(cards)),
            "artifact_refs": artifact_refs,
            "artifact_refs_omitted": max(0, len(artifact_refs_source) - 16),
            "file_refs": file_refs,
            "file_refs_omitted": max(0, len(file_refs_source) - 16),
            "source_refs": source_refs_source,
            "source_refs_omitted": max(0, len(source_refs_sequence) - 16),
        }

    @classmethod
    def _compact_block_carrier_for_taskboard_meta(
        cls,
        block_carrier: Any,
        *,
        blocks: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(block_carrier, Mapping):
            return {}
        raw_work_unit = block_carrier.get("work_unit")
        work_unit: Mapping[str, Any] = raw_work_unit if isinstance(raw_work_unit, Mapping) else {}
        raw_runtime_preferences = work_unit.get("runtime_preferences")
        runtime_preferences: Mapping[str, Any] = (
            raw_runtime_preferences if isinstance(raw_runtime_preferences, Mapping) else {}
        )
        raw_work_unit_result = block_carrier.get("work_unit_result")
        work_unit_result: Mapping[str, Any] = raw_work_unit_result if isinstance(raw_work_unit_result, Mapping) else {}
        raw_carrier_meta = work_unit_result.get("carrier_meta")
        carrier_meta: Mapping[str, Any] = raw_carrier_meta if isinstance(raw_carrier_meta, Mapping) else {}
        return {
            "work_unit": {
                "id": work_unit.get("id"),
                "origin": work_unit.get("origin"),
                "objective": cls._truncate_prompt_text(str(work_unit.get("objective") or ""), 700),
                "input_refs": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit.get("input_refs")))[:8],
                    max_chars=700,
                ),
                "expected_deliverable": cls._compact_verifier_prompt_value(
                    work_unit.get("expected_deliverable", {}),
                    max_chars=700,
                ),
                "evidence_requirements": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit.get("evidence_requirements")))[:8],
                    max_chars=700,
                ),
                "runtime_preferences": {
                    key: runtime_preferences.get(key)
                    for key in (
                        "handler",
                        "plan_block_kind",
                        "preferred_execution_shape",
                        "strategy",
                        "card_id",
                        "attempt_index",
                        "max_attempts",
                    )
                    if key in runtime_preferences
                },
            },
            "work_unit_result": {
                "id": work_unit_result.get("id"),
                "status": work_unit_result.get("status"),
                "summary": cls._compact_verifier_prompt_value(work_unit_result.get("summary"), max_chars=700),
                "candidate_final_result": cls._compact_verifier_prompt_value(
                    work_unit_result.get("candidate_final_result"),
                    max_chars=700,
                ),
                "artifact_manifest": cls._compact_verifier_prompt_value(
                    work_unit_result.get("artifact_manifest", {}),
                    max_chars=700,
                ),
                "evidence": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit_result.get("evidence")))[:8],
                    max_chars=700,
                ),
                "diagnostics": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit_result.get("diagnostics")))[:4],
                    max_chars=700,
                ),
                "carrier_meta": {
                    "snapshot_status": carrier_meta.get("snapshot_status"),
                    "execution_plan": cls._compact_verifier_prompt_value(
                        carrier_meta.get("execution_plan", {}),
                        max_chars=700,
                    ),
                    "execution_block_graph": cls._compact_verifier_prompt_value(
                        carrier_meta.get("execution_block_graph", {}),
                        max_chars=700,
                    ),
                },
            },
            "output_policy": DataFormatter.sanitize(block_carrier.get("output_policy", {})),
            "workspace_operations": cls._compact_taskboard_workspace_operations_for_carrier_meta(
                block_carrier,
                blocks,
            ),
            "block_graph": cls._compact_taskboard_blocks_for_carrier_meta(blocks),
        }

    @classmethod
    def _compact_taskboard_workspace_operations_for_carrier_meta(
        cls,
        block_carrier: Mapping[str, Any],
        blocks: Any,
    ) -> list[dict[str, Any]]:
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                execution_results = [
                    item for item in evidence.get("execution_block_results", []) if isinstance(item, Mapping)
                ]
                operations = [
                    cls._compact_taskboard_workspace_operation(item)
                    for item in execution_results
                    if str(item.get("kind") or "") == "workspace_operation"
                ][:8]
                if operations:
                    return operations
        direct_operations = block_carrier.get("workspace_operations")
        if isinstance(direct_operations, Sequence) and not isinstance(direct_operations, (str, bytes, bytearray)):
            return [
                cls._compact_taskboard_workspace_operation(item)
                for item in list(direct_operations)[:8]
                if isinstance(item, Mapping)
            ]
        return []

    @classmethod
    def _compact_taskboard_workspace_operation(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        output = item.get("output")
        output_summary: dict[str, Any] = {}
        if isinstance(output, Mapping):
            for output_key in (
                "operation",
                "query",
                "filters",
                "bounded",
                "locator_ref_count",
                "evidence_snippet_count",
                "diagnostics",
            ):
                if output_key in output:
                    output_summary[output_key] = cls._compact_verifier_prompt_value(
                        output.get(output_key),
                        max_chars=700,
                    )
            for output_key, source_key in (
                ("first_locator_ref", "locator_refs"),
                ("first_evidence_snippet", "evidence_snippets"),
            ):
                max_chars = 1800 if output_key == "first_evidence_snippet" else 900
                if output_key in output:
                    output_summary[output_key] = cls._compact_taskboard_workspace_ref_or_snippet(
                        output.get(output_key),
                        max_chars=max_chars,
                    )
                    continue
                source = output.get(source_key)
                if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)) and source:
                    output_summary[output_key] = cls._compact_taskboard_workspace_ref_or_snippet(
                        source[0],
                        max_chars=max_chars,
                    )
        return {
            key: item.get(key)
            for key in (
                "id",
                "plan_block_id",
                "source_plan_block_id",
                "execution_block_id",
                "kind",
                "status",
            )
            if key in item
        } | ({"output": output_summary} if output_summary else {})

    @classmethod
    def _compact_taskboard_workspace_ref_or_snippet(cls, value: Any, *, max_chars: int) -> Any:
        if not isinstance(value, Mapping):
            return cls._compact_verifier_prompt_value(value, max_chars=max_chars)
        compact: dict[str, Any] = {}
        for key in (
            "path",
            "line",
            "line_start",
            "line_end",
            "role",
            "content_state",
            "source",
            "query",
            "search_engine",
            "grep_tool",
            "bytes",
            "sha256",
        ):
            if key in value:
                compact[key] = value.get(key)
        content = value.get("content")
        if not isinstance(content, str):
            content = value.get("snippet")
        if not isinstance(content, str):
            content = value.get("text")
        if isinstance(content, str):
            compact["content"] = cls._truncate_prompt_text(content, max_chars)
        return cls._compact_verifier_prompt_value(compact or value, max_chars=max_chars)

    @classmethod
    def _taskboard_card_workspace_operations_for_prompt(
        cls,
        *,
        diagnostics: Sequence[Any],
        metadata: Any,
    ) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for container in (*diagnostics, metadata):
            if not isinstance(container, Mapping):
                continue
            block_carrier = container.get("block_carrier")
            if not isinstance(block_carrier, Mapping):
                continue
            raw_operations = block_carrier.get("workspace_operations")
            if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, (str, bytes, bytearray)):
                continue
            for operation in raw_operations:
                if not isinstance(operation, Mapping):
                    continue
                operations.append(cls._compact_taskboard_workspace_operation(operation))
                if len(operations) >= 4:
                    return operations
        return operations

    @staticmethod
    def _compact_taskboard_blocks_for_carrier_meta(blocks: Any) -> dict[str, Any]:
        if not isinstance(blocks, Mapping):
            return {"present": False, "execution_block_count": 0, "execution_block_kinds": []}
        graph = blocks.get("execution_block_graph")
        if not isinstance(graph, Mapping):
            graph = {}
        execution_blocks = [item for item in graph.get("execution_blocks", []) if isinstance(item, Mapping)]
        evidence = blocks.get("evidence")
        if not isinstance(evidence, Mapping):
            evidence = {}
        execution_results = [
            item for item in evidence.get("execution_block_results", []) if isinstance(item, Mapping)
        ]
        return {
            "present": bool(graph),
            "graph_id": graph.get("graph_id") or graph.get("execution_id") or graph.get("id"),
            "execution_block_count": len(execution_blocks),
            "execution_block_kinds": [
                str(item.get("kind") or "") for item in execution_blocks if str(item.get("kind") or "").strip()
            ],
            "execution_block_ids": [
                str(item.get("id") or "") for item in execution_blocks if str(item.get("id") or "").strip()
            ],
            "evidence_present": bool(evidence),
            "execution_block_result_count": len(execution_results),
            "execution_block_result_kinds": [
                str(item.get("kind") or "") for item in execution_results if str(item.get("kind") or "").strip()
            ],
        }

    @classmethod
    def _taskboard_scheduled_stream_payload(
        cls,
        *,
        schedule: Any,
        evidence_view: Mapping[str, Any],
        concurrency: int | None,
    ) -> dict[str, Any]:
        return {
            "schedule": DataFormatter.sanitize(schedule.to_dict()),
            "evidence_view": cls._compact_taskboard_evidence_view_for_stream(evidence_view),
            "concurrency": concurrency,
        }

    @classmethod
    def _taskboard_completed_stream_payload(cls, tick_result: Any) -> dict[str, Any]:
        evidence_view = build_task_board_evidence_view(tick_result.revision).to_dict()
        return {
            "revision": cls._compact_taskboard_revision_for_stream(tick_result.revision),
            "schedule": DataFormatter.sanitize(tick_result.schedule.to_dict()),
            "card_results": {
                str(card_id): cls._compact_taskboard_card_result_for_stream(result)
                for card_id, result in tick_result.card_results.items()
            },
            "evidence_view": cls._compact_taskboard_evidence_view_for_stream(evidence_view),
            "runtime_topology": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
        }

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

    @classmethod
    def _taskboard_control_output_allows_workspace_delivery(cls, card_output: Any) -> bool:
        if not isinstance(card_output, Mapping):
            return True
        status = str(card_output.get("status") or "").strip().lower()
        if status in {"blocked", "failed", "skipped", "error", "timed_out"}:
            return False
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        if next_action in {"readback", "needs_readback", "repair", "patch", "block", "stop"}:
            return False
        if card_output.get("sufficient") is False:
            return False
        if cls._has_remaining_work(card_output.get("remaining_work")):
            return False
        if cls._has_remaining_work(card_output.get("gaps")):
            if not (status == "completed" and card_output.get("sufficient") is True):
                return False
        return True

    @classmethod
    def _taskboard_control_patch_proposal(
        cls,
        context: Any,
        card_output: Mapping[str, Any],
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        raw_patch = card_output.get("patch_proposal")
        if isinstance(raw_patch, Mapping):
            if cls._taskboard_patch_proposal_is_workspace_patch(raw_patch):
                return None
            try:
                patch = TaskBoardPatch.from_value(raw_patch)
                revision = getattr(context, "revision", None)
                if revision is None:
                    raise ValueError("TaskBoard control patch validation requires a revision.")
                TaskBoardValidator().apply_patch(cast(TaskBoardRevision | Mapping[str, Any], revision), patch)
            except Exception as error:
                diagnostics.append(
                    {
                        "code": "taskboard.control.invalid_model_patch_proposal",
                        "message": _compact_agent_task_error_message(
                            error,
                            fallback="Model patch_proposal was not a valid TaskBoardPatch.",
                        ),
                        "card_id": str(getattr(getattr(context, "card", None), "id", "") or ""),
                        "requested_action": str(raw_patch.get("action") or ""),
                    }
                )
                if cls._taskboard_patch_proposal_requests_readback(raw_patch):
                    target_refs = cls._taskboard_patch_proposal_target_refs(raw_patch)
                    if not target_refs:
                        target_refs = cls._taskboard_control_output_target_refs(card_output)
                    auto_patch_input = dict(card_output)
                    auto_patch_input["next_board_action"] = "readback"
                    return cls._taskboard_control_auto_patch(
                        context,
                        auto_patch_input,
                        target_refs=target_refs,
                    )
                return None
            return DataFormatter.sanitize(patch.to_dict())
        return cls._taskboard_control_auto_patch(
            context,
            card_output,
            target_refs=cls._taskboard_control_output_target_refs(card_output),
        )

    async def _materialize_taskboard_workspace_patch(
        self,
        context: Any,
        card_output: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raw_patch = card_output.get("patch_proposal")
        if not isinstance(raw_patch, Mapping) or not self._taskboard_patch_proposal_is_workspace_patch(raw_patch):
            return card_output
        patched_output = dict(card_output)
        patched_output["workspace_patch_proposal"] = DataFormatter.sanitize(raw_patch)
        patched_output.pop("patch_proposal", None)
        card_id = str(getattr(getattr(context, "card", None), "id", "") or "")
        delivery = await self._apply_taskboard_workspace_patch(raw_patch, card_id=card_id)
        patched_output["workspace_patch_delivery"] = DataFormatter.sanitize(delivery)
        diagnostics = [dict(item) for item in self._taskboard_mapping_sequence(patched_output.get("diagnostics"))]
        if delivery.get("status") == "completed":
            diagnostics.append(
                {
                    "code": "taskboard.control.workspace_patch_applied",
                    "card_id": card_id,
                    "path": delivery.get("path"),
                    "operation_count": delivery.get("operation_count", 0),
                    "replacement_count": delivery.get("replacement_count", 0),
                    "source": "agent_task.taskboard.workspace_patch",
                }
            )
            file_refs = [dict(item) for item in self._taskboard_mapping_sequence(patched_output.get("file_refs"))]
            file_refs.extend(dict(item) for item in self._taskboard_mapping_sequence(delivery.get("file_refs")))
            patched_output["file_refs"] = DataFormatter.sanitize(self._dedupe_ref_records(file_refs))
            status = str(patched_output.get("status") or "").strip().lower()
            if status not in {"completed", "skipped"}:
                patched_output["status"] = "completed"
            if not patched_output.get("sufficient"):
                patched_output["sufficient"] = True
        else:
            diagnostics.append(
                {
                    "code": "taskboard.control.workspace_patch_failed",
                    "card_id": card_id,
                    "path": delivery.get("path"),
                    "reason": delivery.get("reason") or delivery.get("error"),
                    "source": "agent_task.taskboard.workspace_patch",
                }
            )
            patched_output["status"] = "blocked"
            patched_output["sufficient"] = False
            remaining_work = self._normalize_string_list(patched_output.get("remaining_work"))
            reason = str(delivery.get("reason") or "Workspace patch could not be applied.").strip()
            if reason:
                remaining_work.append(reason)
            patched_output["remaining_work"] = remaining_work
        patched_output["diagnostics"] = DataFormatter.sanitize(diagnostics)
        self.diagnostics.setdefault("taskboard_workspace_patch_delivery", []).append(
            DataFormatter.sanitize(delivery)
        )
        return DataFormatter.sanitize(patched_output)

    @staticmethod
    def _taskboard_patch_proposal_is_workspace_patch(patch_proposal: Mapping[str, Any]) -> bool:
        if not isinstance(patch_proposal, Mapping):
            return False
        if any(str(patch_proposal.get(key) or "").strip() for key in ("file", "path", "target_file", "target_path")):
            return True
        raw_operations = patch_proposal.get("operations") or patch_proposal.get("edits")
        if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, str | bytes | bytearray):
            return False
        workspace_ops = {"replace", "insert", "delete", "append", "write"}
        taskboard_ops = {
            "add_card",
            "update_card",
            "remove_card",
            "record_card_result",
            "append_diagnostic",
            "set_board_status",
            "update_metadata",
            "add_dependency",
            "remove_dependency",
        }
        has_workspace_op = False
        for operation in raw_operations:
            if not isinstance(operation, Mapping):
                continue
            op = str(operation.get("type") or operation.get("op") or operation.get("operation") or "").strip()
            if op in taskboard_ops:
                return False
            if op in workspace_ops:
                has_workspace_op = True
        return has_workspace_op

    @classmethod
    def _taskboard_workspace_patch_path(
        cls,
        patch_proposal: Mapping[str, Any],
        operation: Mapping[str, Any] | None = None,
    ) -> str:
        for source in (operation or {}, patch_proposal):
            for key in ("file", "path", "target_file", "target_path", "workspace_path"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    async def _apply_taskboard_workspace_patch(
        self,
        patch_proposal: Mapping[str, Any],
        *,
        card_id: str,
    ) -> dict[str, Any]:
        raw_operations = patch_proposal.get("operations") or patch_proposal.get("edits")
        if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, str | bytes | bytearray):
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "Workspace patch requires an operations list.",
            }
        operations = [dict(item) for item in raw_operations if isinstance(item, Mapping)]
        if not operations:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "Workspace patch has no valid operations.",
            }
        path = self._taskboard_workspace_patch_path(patch_proposal, operations[0])
        if not path:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "Workspace patch requires file/path.",
            }
        try:
            content = await self._read_workspace_patch_text(path)
            operation_records: list[dict[str, Any]] = []
            replacement_count = 0
            for index, operation in enumerate(operations):
                operation_path = self._taskboard_workspace_patch_path(patch_proposal, operation)
                if operation_path != path:
                    raise ValueError("Workspace patch operations must target one file per patch proposal.")
                content, record = self._apply_taskboard_workspace_patch_operation(
                    content,
                    operation,
                    index=index,
                )
                replacement_count += int(record.get("replacement_count") or 0)
                operation_records.append(record)
            write_result = await self.workspace.write_file(path, content, append=False)
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            return {
                "status": "failed",
                "card_id": card_id,
                "path": path,
                "reason": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "error": {"type": error.__class__.__name__},
            }
        ref = {
            "path": str(read_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or write_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or write_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": read_result.get("content_kind", "text"),
            "encoding": read_result.get("encoding"),
            "handler_id": read_result.get("handler_id"),
            "role": "workspace_artifact",
            "source": "agent_task.workspace_artifact.taskboard_patch",
            "card_id": card_id,
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
            "preview": self._truncate_prompt_text(str(read_result.get("content") or ""), _WORKSPACE_ARTIFACT_PREVIEW_BYTES),
        }
        return {
            "status": "completed",
            "card_id": card_id,
            "path": path,
            "operation_count": len(operation_records),
            "replacement_count": replacement_count,
            "operations": operation_records,
            "write": {
                "path": str(write_result.get("path") or path),
                "bytes": int(write_result.get("bytes") or 0),
                "sha256": str(write_result.get("sha256") or ""),
            },
            "readback": {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "read_bytes": ref["read_bytes"],
                "truncated": ref["truncated"],
                "handler_id": ref["handler_id"],
            },
            "file_refs": [ref],
        }

    async def _read_workspace_patch_text(self, path: str) -> str:
        target = self.workspace.resolve_file_path(path)
        max_bytes = max(int(target.stat().st_size) + 1, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        read_result = await self.workspace.read_file(path, max_bytes=max_bytes)
        if not bool(read_result.get("ok")):
            raise ValueError(f"Workspace file could not be read for patch: { path }")
        if bool(read_result.get("truncated")):
            raise ValueError(f"Workspace patch requires complete readback before editing: { path }")
        content = read_result.get("content")
        if not isinstance(content, str):
            raise ValueError(f"Workspace patch requires text content: { path }")
        return content

    @classmethod
    def _apply_taskboard_workspace_patch_operation(
        cls,
        content: str,
        operation: Mapping[str, Any],
        *,
        index: int,
    ) -> tuple[str, dict[str, Any]]:
        op = str(operation.get("type") or operation.get("op") or operation.get("operation") or "").strip().lower()
        if op != "replace":
            raise ValueError(f"Unsupported Workspace patch operation '{ op or '<empty>' }'.")
        old = str(operation.get("old") or operation.get("from") or operation.get("search") or "")
        if not old:
            raise ValueError("Workspace replace patch requires non-empty old/from/search text.")
        if "new" in operation:
            new = str(operation.get("new") or "")
        elif "to" in operation:
            new = str(operation.get("to") or "")
        else:
            new = str(operation.get("replacement") or "")
        match_count = content.count(old)
        if match_count <= 0:
            raise ValueError("Workspace replace patch old text was not found.")
        replace_all = cls._normalize_bool(operation.get("replace_all"), default=False)
        occurrence = cls._coerce_positive_int(operation.get("occurrence"))
        if occurrence is not None:
            if occurrence > match_count:
                raise ValueError("Workspace replace patch occurrence is greater than match count.")
            patched = cls._replace_nth(content, old, new, occurrence)
            replacement_count = 1
        elif replace_all:
            patched = content.replace(old, new)
            replacement_count = match_count
        elif match_count == 1:
            patched = content.replace(old, new, 1)
            replacement_count = 1
        else:
            raise ValueError(
                "Workspace replace patch matched multiple locations; set occurrence or replace_all explicitly."
            )
        return patched, {
            "index": index,
            "type": "replace",
            "match_count": match_count,
            "replacement_count": replacement_count,
        }

    @staticmethod
    def _replace_nth(content: str, old: str, new: str, occurrence: int) -> str:
        start = -1
        search_from = 0
        for _ in range(occurrence):
            start = content.find(old, search_from)
            if start < 0:
                return content
            search_from = start + len(old)
        return content[:start] + new + content[start + len(old) :]

    @staticmethod
    def _taskboard_mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return None
        return coerced if coerced > 0 else None

    @staticmethod
    def _taskboard_patch_proposal_requests_readback(patch_proposal: Mapping[str, Any]) -> bool:
        action = str(
            patch_proposal.get("action")
            or patch_proposal.get("next_board_action")
            or patch_proposal.get("patch_type")
            or patch_proposal.get("type")
            or ""
        ).strip().lower()
        return action.replace("-", "_") in {
            "readback",
            "needs_readback",
            "cold_readback",
            "artifact_readback",
            "readback_required",
        }

    @classmethod
    def _taskboard_patch_proposal_target_refs(cls, patch_proposal: Mapping[str, Any]) -> list[str]:
        raw_refs = patch_proposal.get("target_refs") or patch_proposal.get("refs") or patch_proposal.get("urls")
        return cls._normalize_taskboard_target_refs(raw_refs)

    @classmethod
    def _taskboard_control_output_target_refs(cls, card_output: Mapping[str, Any]) -> list[str]:
        return cls._normalize_taskboard_target_refs(card_output.get("target_refs"))

    @staticmethod
    def _normalize_taskboard_target_refs(raw_refs: Any) -> list[str]:
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[str] = []
        seen: set[str] = set()
        for item in raw_refs:
            text = ""
            if isinstance(item, Mapping):
                for key in ("target_ref", "url", "href", "uri", "path", "ref"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        text = value
                        break
            else:
                text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            refs.append(text)
        return refs[:8]

    @classmethod
    def _taskboard_control_auto_patch(
        cls,
        context: Any,
        card_output: Mapping[str, Any],
        *,
        target_refs: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        if next_action not in {"readback", "needs_readback"}:
            return None
        target_ref_list = [str(ref).strip() for ref in list(target_refs or ()) if str(ref).strip()]
        revision = getattr(context, "revision", None)
        card = getattr(context, "card", None)
        if revision is None or card is None:
            return None
        graph = getattr(revision, "graph", None)
        if graph is None or not hasattr(graph, "card_by_id"):
            return None
        existing_ids = set(graph.card_by_id())
        current_id = str(getattr(card, "id", "") or "").strip()
        if not current_id:
            return None

        def safe_id(raw: str) -> str:
            text = "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in raw.strip())
            text = text.strip(".-")
            return text or "card"

        def unique_id(prefix: str) -> str:
            base = safe_id(prefix)[:80]
            candidate = base
            index = 1
            while candidate in existing_ids:
                index += 1
                candidate = f"{base}-{index}"
            existing_ids.add(candidate)
            return candidate

        continuation_id = unique_id(f"{current_id}.continue")
        current_card = dict(card.to_dict() if hasattr(card, "to_dict") else {})
        if not current_card:
            return None
        current_metadata = dict(current_card.get("metadata") or {})
        if (
            str(current_metadata.get("generated_by") or "")
            in {
                "agent_task.taskboard.control_auto_readback",
                "agent_task.taskboard.control_auto_target_refs",
            }
            and (
                str(current_metadata.get("readback_card_id") or "").strip()
                or str(current_metadata.get("evidence_card_id") or "").strip()
            )
        ):
            return None
        source = (
            "agent_task.taskboard.control_auto_target_refs"
            if target_ref_list
            else "agent_task.taskboard.control_auto_readback"
        )
        evidence_card_id = (
            unique_id(f"{current_id}.evidence") if target_ref_list else unique_id(f"{current_id}.readback")
        )
        current_metadata.update(
            {
                "superseded_by": continuation_id,
                "auto_patch_reason": "next_board_action=readback",
            }
        )
        current_card.update(
            {
                "failure_policy": "degradable",
                "status": "blocked",
                "metadata": current_metadata,
            }
        )
        dependencies = list(getattr(card, "depends_on", ()) or [])
        readback_dependencies = cls._taskboard_auto_readback_scope(card, graph)
        gaps = cls._normalize_string_list(card_output.get("gaps"))
        remaining_work = cls._normalize_string_list(card_output.get("remaining_work"))
        if target_ref_list:
            readback_objective = (
                "Collect scoped evidence from the explicit target refs required before continuing the blocked "
                f"control card. Target refs: {'; '.join(target_ref_list)}"
            )
        else:
            readback_objective = "Read scoped cold evidence required before continuing the blocked control card."
        if gaps:
            readback_objective = f"{readback_objective} Gaps: {'; '.join(gaps[:3])}"
        continuation_objective = str(getattr(card, "objective", "") or "Continue the blocked TaskBoard card.").strip()
        if remaining_work:
            continuation_objective = f"{continuation_objective} Remaining work: {'; '.join(remaining_work[:3])}"
        final_workspace_deliverables = cls._normalize_string_list(
            current_metadata.get("final_workspace_deliverables")
        )
        if final_workspace_deliverables:
            continuation_objective = (
                f"{continuation_objective} Materialize required Workspace final deliverable path(s): "
                f"{'; '.join(final_workspace_deliverables)}"
            )
        evidence_metadata = {
            "evidence_scope": readback_dependencies,
            "generated_by": source,
            "source_card_id": current_id,
        }
        if target_ref_list:
            evidence_metadata["target_refs"] = target_ref_list
        continuation_metadata = {
            "generated_by": source,
            "continues_card_id": current_id,
            "readback_card_id": evidence_card_id,
            "evidence_card_id": evidence_card_id if target_ref_list else "",
        }
        if final_workspace_deliverables:
            continuation_metadata["final_workspace_deliverables"] = final_workspace_deliverables
        evidence_card = {
            "id": evidence_card_id,
            "objective": readback_objective,
            "depends_on": readback_dependencies,
            "required_outputs": (
                ["Evidence gathered from target refs or diagnostics explaining inaccessible refs."]
                if target_ref_list
                else ["Bounded readback previews for verifier-visible cold evidence."]
            ),
            "allowed_execution_shape": "actions" if target_ref_list else "readback",
            "failure_policy": "required",
            "metadata": evidence_metadata,
        }
        patch = {
            "base_revision": str(getattr(revision, "revision_id", "") or ""),
            "source": source,
            "operations": [
                {"op": "update_card", "card": current_card},
                {
                    "op": "add_card",
                    "card": evidence_card,
                },
                {
                    "op": "add_card",
                    "card": {
                        "id": continuation_id,
                        "objective": continuation_objective,
                        "depends_on": [*dependencies, evidence_card_id],
                        "required_outputs": list(getattr(card, "required_outputs", ()) or ()),
                        "allowed_execution_shape": str(getattr(card, "allowed_execution_shape", "") or "control"),
                        "failure_policy": str(getattr(card, "failure_policy", "") or "required"),
                        "metadata": continuation_metadata,
                    },
                },
                {
                    "op": "append_diagnostic",
                    "diagnostic": {
                        "code": "taskboard.control.auto_readback_patch",
                        "card_id": current_id,
                        "readback_card_id": evidence_card_id,
                        "continuation_card_id": continuation_id,
                        "target_ref_count": len(target_ref_list),
                    },
                },
            ],
            "diagnostics": [
                {
                    "code": "taskboard.control.auto_readback_patch",
                    "card_id": current_id,
                    "readback_card_id": evidence_card_id,
                    "continuation_card_id": continuation_id,
                    "target_ref_count": len(target_ref_list),
                }
            ],
        }
        return DataFormatter.sanitize(patch)

    @classmethod
    def _taskboard_auto_readback_scope(cls, card: Any, graph: Any) -> list[str]:
        """Scope auto-readback to direct dependencies plus their upstream evidence."""

        if graph is None or not hasattr(graph, "card_by_id"):
            return list(getattr(card, "depends_on", ()) or [])
        card_by_id = graph.card_by_id()
        ordered: list[str] = []
        seen: set[str] = set()

        def add(card_id: str) -> None:
            if not card_id or card_id in seen:
                return
            seen.add(card_id)
            ordered.append(card_id)

        def visit(card_id: str) -> None:
            add(card_id)
            dependency_card = card_by_id.get(card_id)
            if dependency_card is None:
                return
            for dependency_id in getattr(dependency_card, "depends_on", ()) or ():
                visit(str(dependency_id))

        for dependency_id in getattr(card, "depends_on", ()) or ():
            visit(str(dependency_id))
        return ordered

    def _taskboard_final_verification_repair_revision(
        self,
        revision: Any,
        *,
        final: Mapping[str, Any],
        final_verification: Mapping[str, Any],
    ) -> TaskBoardRevision | None:
        from agently.core.orchestration.TaskBoard import apply_task_board_patch

        effective_revision = TaskBoardRevision.from_value(revision)
        existing_ids = set(effective_revision.graph.card_by_id())
        completed_dependencies = [
            str(card_id)
            for card_id, result in effective_revision.card_results.items()
            if str(getattr(result, "status", "")).strip().lower() == "completed"
        ]
        if not completed_dependencies:
            return None
        gaps = [
            *self._normalize_string_list(final_verification.get("missing_criteria")),
            *self._normalize_string_list(final_verification.get("next_step_requirements")),
            *self._normalize_string_list(final_verification.get("acceptance_delta")),
        ]
        if not gaps:
            reason = str(final_verification.get("reason") or final.get("reason") or "").strip()
            if reason:
                gaps.append(reason)
        if not gaps:
            return None

        required_deliverables = self._required_workspace_deliverables()

        def safe_id(raw: str) -> str:
            text = "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in raw.strip())
            text = text.strip(".-")
            return text or "card"

        def unique_id(prefix: str) -> str:
            base = safe_id(prefix)[:80]
            candidate = base
            index = 1
            while candidate in existing_ids:
                index += 1
                candidate = f"{base}-{index}"
            existing_ids.add(candidate)
            return candidate

        repair_id = unique_id("final-verification-repair")
        gap_text = "; ".join(gaps[:6])
        required_outputs = [
            "Corrected final deliverable that resolves final verification gaps using existing evidence.",
        ]
        if required_deliverables:
            required_outputs.append(
                "Trusted Workspace final deliverable path(s): " + ", ".join(required_deliverables)
            )
        repair_card = {
            "id": repair_id,
            "objective": (
                "Repair the final TaskBoard deliverable using existing completed-card evidence and final "
                f"verification feedback. Address these gaps: {gap_text}. Produce a complete corrected "
                "deliverable; preserve verifier-visible source refs; remove, qualify, or replace unsupported "
                "facts instead of inventing evidence."
            ),
            "depends_on": completed_dependencies,
            "required_outputs": required_outputs,
            "allowed_execution_shape": "control",
            "failure_policy": "required",
            "evidence_contract": {
                "kind": "taskboard_final_verification_repair",
                "missing_criteria": self._normalize_string_list(final_verification.get("missing_criteria")),
                "next_step_requirements": self._normalize_string_list(final_verification.get("next_step_requirements")),
                "acceptance_delta": self._normalize_string_list(final_verification.get("acceptance_delta")),
                "reason": str(final_verification.get("reason") or ""),
            },
            "metadata": {
                "generated_by": "agent_task.taskboard.final_verification_repair",
                "repair_source": "final_verification",
                "previous_revision_id": effective_revision.revision_id,
                "final_workspace_deliverables": required_deliverables,
            },
        }
        diagnostic = {
            "code": "taskboard.final_verification.repair_patch",
            "repair_card_id": repair_id,
            "depends_on": completed_dependencies,
            "missing_criteria": self._normalize_string_list(final_verification.get("missing_criteria")),
            "reason": str(final_verification.get("reason") or ""),
        }
        patch = {
            "base_revision": effective_revision.revision_id,
            "source": "agent_task.taskboard.final_verification_repair",
            "operations": [
                {"op": "add_card", "card": repair_card},
                {"op": "append_diagnostic", "diagnostic": diagnostic},
                {"op": "set_board_status", "status": "running"},
            ],
            "diagnostics": [diagnostic],
        }
        try:
            repaired_revision = apply_task_board_patch(effective_revision, patch)
        except Exception as error:
            self.diagnostics.setdefault("taskboard_final_repair_patch_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "repair_card_id": repair_id,
                    "revision_id": effective_revision.revision_id,
                }
            )
            return None
        self.diagnostics.setdefault("taskboard_final_repair_patches", []).append(
            {
                "repair_card_id": repair_id,
                "previous_revision_id": effective_revision.revision_id,
                "revision_id": repaired_revision.revision_id,
                "missing_criteria": diagnostic["missing_criteria"],
            }
        )
        return repaired_revision

    async def _run_taskboard_readback_card(
        self,
        context: Any,
        context_pack: "WorkspaceContextPackage",
    ) -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        refs = self._taskboard_readback_artifact_refs(evidence_view)
        file_refs = self._taskboard_readback_file_refs(evidence_view)
        work_unit = WorkUnitIntent(
            id=f"taskboard:{context.card.id}:readback",
            origin="taskboard_card",
            objective=str(getattr(context.card, "objective", "") or "Read scoped cold evidence."),
            input_payload={
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract(),
                "card": context.card.to_dict(),
                "artifact_refs": DataFormatter.sanitize(refs),
                "file_refs": DataFormatter.sanitize(file_refs),
                "evidence_scope": evidence_card_ids or "all",
            },
            input_refs=tuple(
                dict(item)
                for item in [
                    *[ref for ref in refs if isinstance(ref, Mapping)],
                    *[ref for ref in file_refs if isinstance(ref, Mapping)],
                ]
            ),
            expected_deliverable={
                "allowed_execution_shape": "readback",
                "artifact_ref_count": len(refs),
                "file_ref_count": len(file_refs),
            },
            evidence_requirements=tuple(
                [
                    {
                        "artifact_id": str(ref.get("artifact_id") or ""),
                        "action_call_id": str(ref.get("action_call_id") or ""),
                        "source": "taskboard_readback_card",
                    }
                    for ref in refs
                    if isinstance(ref, Mapping)
                ]
                + [
                    {
                        "path": str(ref.get("path") or ""),
                        "source": "taskboard_workspace_file_readback",
                    }
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ]
            ),
            delivery_contract={
                "card": DataFormatter.sanitize(context.card.to_dict()),
                "execution_prompt": {"output_format": "json"},
            },
            quality_gates=(
                {
                    "kind": "taskboard_artifact_readback_status",
                    "allowed_statuses": ["completed", "blocked", "failed"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_artifact_readback",
                "plan_block_kind": "action_call",
                "preferred_execution_shape": "taskboard_readback",
                "strategy": "taskboard",
                "card_id": context.card.id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_readback",
            "effective_execution_shape": "taskboard_readback",
            "step_instruction": str(getattr(context.card, "objective", "") or "Read scoped cold evidence."),
            "expected_evidence": [
                {
                    "artifact_id": str(ref.get("artifact_id") or ""),
                    "action_call_id": str(ref.get("action_call_id") or ""),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ]
            + [
                {
                    "path": str(ref.get("path") or ""),
                }
                for ref in file_refs
                if isinstance(ref, Mapping)
            ],
            "rationale": "Execute one TaskBoard artifact readback card through the shared Block carrier.",
            "step_scope": {},
        }

        async def run_readback_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.started",
                {
                    "card_id": context.card.id,
                    "ref_count": len(refs),
                    "file_ref_count": len(file_refs),
                },
            )
            readbacks: list[dict[str, Any]] = []
            file_readbacks: list[dict[str, Any]] = []
            effective_file_refs = [dict(ref) for ref in file_refs if isinstance(ref, Mapping)]
            diagnostics: list[dict[str, Any]] = []
            if not refs and not file_refs:
                status = "blocked"
                success_count = 0
                failed_count = 0
                file_success_count = 0
                file_failed_count = 0
                diagnostics.append(
                    {
                        "code": "taskboard.readback.no_refs",
                        "card_id": context.card.id,
                        "evidence_scope": evidence_card_ids or "all",
                    }
                )
                payload = {
                    "status": status,
                    "answer": "No Action artifact refs or Workspace file refs are available for this readback card.",
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "evidence": [],
                    "remaining_work": [
                        "Upstream cards must produce Action artifact refs or Workspace file refs before readback can run."
                    ],
                    "diagnostics": diagnostics,
                }
            else:
                success_count = 0
                failed_count = 0
                action = getattr(self.agent, "action", None)
                reader = getattr(action, "async_read_action_artifact", None)
                if refs and not callable(reader):
                    success_count = 0
                    failed_count = len(refs)
                    diagnostics.append(
                        {
                            "code": "taskboard.readback.reader_unavailable",
                            "card_id": context.card.id,
                            "ref_count": len(refs),
                        }
                    )
                elif callable(reader):
                    for ref in refs:
                        artifact_id = str(ref.get("artifact_id") or "")
                        action_call_id = str(ref.get("action_call_id") or "")
                        try:
                            raw_readback = await self._await_taskboard_card_execution(
                                cast(Awaitable[Any], reader(artifact_id, action_call_id or None)),
                                card_id=context.card.id,
                                stage="readback",
                            )
                        except Exception as error:
                            raw_readback = {
                                "ok": False,
                                "status": "error",
                                "artifact_id": artifact_id,
                                "action_call_id": action_call_id,
                                "error": (
                                    f"{error.__class__.__name__}: "
                                    + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                                ),
                            }
                        compact = self._compact_taskboard_action_artifact_readback(raw_readback, ref)
                        readbacks.append(compact)
                        if not compact.get("ok"):
                            diagnostics.append(
                                {
                                    "code": "taskboard.readback.ref_failed",
                                    "artifact_id": artifact_id,
                                    "action_call_id": action_call_id,
                                    "status": compact.get("status"),
                                    "error": compact.get("error"),
                                }
                            )

                    success_count = sum(1 for item in readbacks if item.get("ok"))
                    failed_count = len(readbacks) - success_count
                discovered_file_refs = self._taskboard_file_refs_from_action_readbacks(readbacks)
                added_file_refs = self._merge_taskboard_file_refs(effective_file_refs, discovered_file_refs)
                if added_file_refs:
                    diagnostics.append(
                        {
                            "code": "taskboard.readback.workspace_file_refs_discovered",
                            "card_id": context.card.id,
                            "file_ref_count": len(added_file_refs),
                        }
                    )
                for ref in effective_file_refs:
                    path = str(ref.get("path") or "").strip()
                    try:
                        raw_file_readback = await self._await_taskboard_card_execution(
                            self.workspace.read_file(path, max_bytes=_TASKBOARD_READBACK_PREVIEW_CHARS),
                            card_id=context.card.id,
                            stage="workspace_file_readback",
                        )
                    except Exception as error:
                        raw_file_readback = {
                            "ok": False,
                            "readable": False,
                            "status": "error",
                            "path": path,
                            "error": (
                                f"{error.__class__.__name__}: "
                                + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                            ),
                        }
                    compact_file = self._compact_taskboard_workspace_file_readback(raw_file_readback, ref)
                    file_readbacks.append(compact_file)
                    if not compact_file.get("ok"):
                        diagnostics.append(
                            {
                                "code": "taskboard.readback.file_failed",
                                "path": path,
                                "status": compact_file.get("status"),
                                "error": compact_file.get("error"),
                            }
                        )
                file_success_count = sum(1 for item in file_readbacks if item.get("ok"))
                file_failed_count = len(file_readbacks) - file_success_count
                status = "completed" if (success_count + file_success_count) > 0 else "failed"
                remaining_work = []
                if failed_count:
                    remaining_work.append(f"{ failed_count } artifact refs could not be read.")
                if file_failed_count:
                    remaining_work.append(f"{ file_failed_count } Workspace file refs could not be read.")
                payload = {
                    "status": status,
                    "answer": (
                        f"Read { success_count } of { len(refs) } Action artifact refs and "
                        f"{ file_success_count } of { len(effective_file_refs) } Workspace file refs with bounded previews."
                    ),
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "file_refs": DataFormatter.sanitize(effective_file_refs),
                    "evidence": [
                        *[
                            f"artifact:{ item.get('artifact_id') } status={ item.get('status') }"
                            for item in readbacks
                            if item.get("artifact_id")
                        ],
                        *[
                            f"file:{ item.get('path') } status={ item.get('status') }"
                            for item in file_readbacks
                            if item.get("path")
                        ],
                    ],
                    "remaining_work": remaining_work,
                    "diagnostics": diagnostics,
                }

            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.completed",
                {
                    "card_id": context.card.id,
                    "status": status,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "ref_count": len(refs),
                    "file_success_count": file_success_count,
                    "file_failed_count": file_failed_count,
                    "file_ref_count": len(effective_file_refs),
                },
            )
            execution_diagnostic = {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(effective_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
            }
            return {
                "execution_result": DataFormatter.sanitize(payload),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{context.card.id}:readback",
                    "status": status,
                    "route": {
                        "selected_route": "action_artifact_readback",
                        "status": status,
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [execution_diagnostic],
                    "artifact_refs": DataFormatter.sanitize(refs),
                    "file_refs": DataFormatter.sanitize(effective_file_refs),
                },
                "action_evidence": [
                    {
                        "kind": "taskboard_artifact_readback",
                        "card_id": context.card.id,
                        "artifact_refs": DataFormatter.sanitize(refs),
                        "file_refs": DataFormatter.sanitize(effective_file_refs),
                        "readbacks": DataFormatter.sanitize(readbacks),
                        "file_readbacks": DataFormatter.sanitize(file_readbacks),
                        "status": status,
                    }
                ],
            }

        try:
            card_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{context.card.id}:readback",
                handler=run_readback_work_unit,
                start_payload={"card_id": context.card.id, "ref_count": len(refs)},
            )
        except Exception as error:
            return self._failed_taskboard_card_result(
                card_id=context.card.id,
                error=error,
                execution_id=None,
            )

        payload = dict(card_output) if isinstance(card_output, Mapping) else {"status": "failed", "answer": card_output}
        diagnostics = []
        raw_diagnostics = payload.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)
        success_count = int(payload.get("success_count", 0) or 0) if isinstance(payload.get("success_count"), int) else 0
        readbacks = payload.get("readbacks", [])
        if isinstance(readbacks, Sequence) and not isinstance(readbacks, str | bytes | bytearray):
            success_count = sum(1 for item in readbacks if isinstance(item, Mapping) and item.get("ok"))
        file_readbacks = payload.get("file_readbacks", [])
        file_success_count = 0
        if isinstance(file_readbacks, Sequence) and not isinstance(file_readbacks, str | bytes | bytearray):
            file_success_count = sum(1 for item in file_readbacks if isinstance(item, Mapping) and item.get("ok"))
        result_file_refs = [dict(ref) for ref in file_refs if isinstance(ref, Mapping)]
        raw_result_file_refs = payload.get("file_refs")
        if isinstance(raw_result_file_refs, Sequence) and not isinstance(
            raw_result_file_refs,
            str | bytes | bytearray,
        ):
            result_file_refs = [dict(ref) for ref in raw_result_file_refs if isinstance(ref, Mapping)]
        failed_count = max(0, len(refs) - success_count)
        file_failed_count = max(0, len(result_file_refs) - file_success_count)
        diagnostics.append(
            {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(result_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
            }
        )
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=str(payload.get("status") or "failed"),
            preview=DataFormatter.sanitize(payload),
            artifact_refs=tuple(refs),
            file_refs=tuple(result_file_refs),
            diagnostics=tuple(diagnostics),
            metadata={
                "execution_id": execution_meta.get("execution_id"),
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(result_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
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

    @staticmethod
    def _taskboard_action_artifact_recall_records(evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_refs = evidence_view.get("artifact_refs")
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            action_call_id = str(item.get("action_call_id") or "").strip()
            key = (artifact_id, action_call_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "artifact_id": artifact_id,
                    "action_call_id": action_call_id,
                    "artifact_type": str(item.get("artifact_type") or ""),
                    "role": str(item.get("role") or ""),
                    "label": str(item.get("label") or ""),
                    "media_type": str(item.get("media_type") or ""),
                    "bytes": item.get("bytes", item.get("size")),
                    "sha256": item.get("sha256"),
                    "truncated": bool(item.get("truncated")),
                    "full_value_available": bool(item.get("full_value_available", item.get("available", False))),
                }
            )
        if not refs:
            return []
        return [
            {
                "action_id": "taskboard_upstream_evidence",
                "status": "success",
                "artifact_refs": refs,
            }
        ]

    @classmethod
    def _taskboard_readback_artifact_refs(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = cls._taskboard_action_artifact_recall_records(evidence_view)
        if not records:
            return []
        refs = records[0].get("artifact_refs")
        if not isinstance(refs, list):
            return []
        return [dict(ref) for ref in refs if isinstance(ref, Mapping)]

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

    @classmethod
    def _taskboard_readback_file_refs(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def collect(value: Any) -> None:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
                return
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                sha = str(item.get("sha256") or "").strip()
                key = (path, sha)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(dict(DataFormatter.sanitize(item)))

        collect(evidence_view.get("file_refs"))
        collect(evidence_view.get("artifact_refs"))
        cards = evidence_view.get("cards")
        if isinstance(cards, Sequence) and not isinstance(cards, str | bytes | bytearray):
            for card in cards:
                if isinstance(card, Mapping):
                    collect(card.get("artifact_refs"))
                    collect(card.get("file_refs"))
        return refs

    @staticmethod
    def _taskboard_file_ref_key(ref: Mapping[str, Any]) -> tuple[str, str]:
        return (str(ref.get("path") or "").strip(), str(ref.get("sha256") or "").strip())

    @classmethod
    def _merge_taskboard_file_refs(
        cls,
        refs: list[dict[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        seen = {cls._taskboard_file_ref_key(ref) for ref in refs if cls._taskboard_file_ref_key(ref)[0]}
        added: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            path = str(candidate.get("path") or "").strip()
            if not path:
                continue
            item = dict(DataFormatter.sanitize(candidate))
            key = cls._taskboard_file_ref_key(item)
            if key in seen:
                continue
            seen.add(key)
            refs.append(item)
            added.append(item)
        return added

    @classmethod
    def _taskboard_file_refs_from_action_readbacks(cls, readbacks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def collect(value: Any, *, source_ref: Mapping[str, Any] | None = None) -> None:
            if not isinstance(value, Mapping):
                return
            raw_refs = value.get("file_refs")
            if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray):
                for raw_ref in raw_refs:
                    if not isinstance(raw_ref, Mapping):
                        continue
                    path = str(raw_ref.get("path") or "").strip()
                    if not path:
                        continue
                    item = dict(DataFormatter.sanitize(raw_ref))
                    if source_ref is not None:
                        item.setdefault("source", "taskboard_action_artifact_readback")
                        artifact_id = str(source_ref.get("artifact_id") or "").strip()
                        action_call_id = str(source_ref.get("action_call_id") or "").strip()
                        if artifact_id:
                            item.setdefault("source_artifact_id", artifact_id)
                        if action_call_id:
                            item.setdefault("source_action_call_id", action_call_id)
                    key = cls._taskboard_file_ref_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(item)
            for key in ("artifact_manifest", "read_preview", "value_preview", "data", "result"):
                nested = value.get(key)
                if isinstance(nested, Mapping):
                    collect(nested, source_ref=source_ref)

        for readback in readbacks:
            if isinstance(readback, Mapping):
                collect(readback, source_ref=readback)
        return refs

    @staticmethod
    def _taskboard_dependency_ref_needs_readback(ref: Mapping[str, Any]) -> bool:
        artifact_id = str(ref.get("artifact_id") or "").strip()
        if not artifact_id:
            return False
        role = str(ref.get("role") or "").strip().lower()
        if role and role not in {"output", "result", "artifact"}:
            return False
        if not bool(ref.get("available", True)) and not bool(ref.get("full_value_available")):
            return False
        if bool(ref.get("truncated")):
            return True
        try:
            size = int(ref.get("bytes", ref.get("size", 0)) or 0)
        except Exception:
            size = 0
        return bool(ref.get("full_value_available")) and size > _TASKBOARD_PROMPT_RESULT_CHARS

    async def _taskboard_dependency_action_artifact_readbacks(
        self,
        evidence_view: Mapping[str, Any],
        *,
        card_id: str,
        context_pack: "WorkspaceContextPackage",
    ) -> dict[str, Any]:
        refs = [
            ref
            for ref in self._taskboard_readback_artifact_refs(evidence_view)
            if self._taskboard_dependency_ref_needs_readback(ref)
        ][:_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS]
        payload: dict[str, Any] = {
            "schema_version": "agent_task_taskboard_dependency_readbacks/v1",
            "card_id": card_id,
            "ref_count": len(refs),
            "readbacks": [],
            "diagnostics": [],
            "bounded": {
                "preview_chars": _TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                "max_refs": _TASKBOARD_DEPENDENCY_READBACK_MAX_REFS,
            },
        }
        if not refs:
            return payload

        work_unit = WorkUnitIntent(
            id=f"taskboard:{card_id}:dependency-readback",
            origin="taskboard_card",
            objective="Read bounded dependency Action artifact previews before executing the card.",
            input_payload={
                "task_id": self.id,
                "goal": self.goal,
                "task_context_contract": self._task_context_contract(),
                "card_id": card_id,
                "artifact_refs": DataFormatter.sanitize(refs),
                "bounded": dict(payload["bounded"]),
            },
            input_refs=tuple(dict(item) for item in refs if isinstance(item, Mapping)),
            expected_deliverable={
                "allowed_execution_shape": "dependency_readback",
                "artifact_ref_count": len(refs),
            },
            evidence_requirements=tuple(
                {
                    "artifact_id": str(ref.get("artifact_id") or ""),
                    "action_call_id": str(ref.get("action_call_id") or ""),
                    "source": "taskboard_dependency_readback",
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ),
            delivery_contract={"execution_prompt": {"output_format": "json"}},
            quality_gates=(
                {
                    "kind": "taskboard_dependency_readback_status",
                    "allowed_statuses": ["completed", "failed"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_dependency_artifact_readback",
                "plan_block_kind": "action_call",
                "preferred_execution_shape": "taskboard_dependency_readback",
                "strategy": "taskboard",
                "card_id": card_id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_dependency_readback",
            "effective_execution_shape": "taskboard_dependency_readback",
            "step_instruction": "Read bounded dependency Action artifact previews before executing the card.",
            "expected_evidence": [
                {
                    "artifact_id": str(ref.get("artifact_id") or ""),
                    "action_call_id": str(ref.get("action_call_id") or ""),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ],
            "rationale": "Execute TaskBoard dependency artifact prefetch through the shared Block carrier.",
            "step_scope": {},
        }

        async def run_dependency_readback_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            action = getattr(self.agent, "action", None)
            reader = getattr(action, "async_read_action_artifact", None)
            readbacks: list[dict[str, Any]] = []
            diagnostics: list[dict[str, Any]] = []
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.dependency_readback.started",
                {"card_id": card_id, "ref_count": len(refs)},
            )
            if not callable(reader):
                diagnostics.append(
                    {
                        "code": "taskboard.dependency_readback.reader_unavailable",
                        "message": "Action artifact readback is unavailable on the bound Agent.",
                        "ref_count": len(refs),
                    }
                )
            else:
                for ref in refs:
                    artifact_id = str(ref.get("artifact_id") or "")
                    action_call_id = str(ref.get("action_call_id") or "")
                    try:
                        raw_readback = await self._await_taskboard_card_execution(
                            cast(Awaitable[Any], reader(artifact_id, action_call_id or None)),
                            card_id=card_id,
                            stage="dependency_readback",
                        )
                    except Exception as error:
                        raw_readback = {
                            "ok": False,
                            "status": "error",
                            "artifact_id": artifact_id,
                            "action_call_id": action_call_id,
                            "error": (
                                f"{error.__class__.__name__}: "
                                + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                            ),
                        }
                    compact = self._compact_taskboard_action_artifact_readback(
                        raw_readback,
                        ref,
                        max_chars=_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                    )
                    readbacks.append(compact)
                    if not compact.get("ok"):
                        diagnostics.append(
                            {
                                "code": "taskboard.dependency_readback.ref_failed",
                                "artifact_id": artifact_id,
                                "action_call_id": action_call_id,
                                "status": compact.get("status"),
                                "error": compact.get("error"),
                            }
                        )
            output = dict(payload)
            output["readbacks"] = readbacks
            output["diagnostics"] = diagnostics
            output["success_count"] = sum(1 for item in readbacks if item.get("ok"))
            failed_count = len(readbacks) - int(output["success_count"])
            status = "completed" if int(output["success_count"]) > 0 else "failed"
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.dependency_readback.completed",
                {
                    "card_id": card_id,
                    "ref_count": len(refs),
                    "success_count": output["success_count"],
                    "failed_count": failed_count,
                },
            )
            return {
                "execution_result": DataFormatter.sanitize(output),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{card_id}:dependency-readback",
                    "status": status,
                    "route": {
                        "selected_route": "action_artifact_dependency_readback",
                        "status": status,
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [
                        {
                            "execution_kind": "taskboard_dependency_artifact_readback",
                            "execution_strategy": self.execution_strategy,
                            "card_id": card_id,
                            "ref_count": len(refs),
                            "success_count": output["success_count"],
                            "failed_count": failed_count,
                        }
                    ],
                    "artifact_refs": DataFormatter.sanitize(refs),
                },
                "action_evidence": [
                    {
                        "kind": "taskboard_dependency_artifact_readback",
                        "card_id": card_id,
                        "artifact_refs": DataFormatter.sanitize(refs),
                        "readbacks": DataFormatter.sanitize(readbacks),
                        "status": status,
                    }
                ],
            }

        try:
            readback_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{card_id}:dependency-readback",
                handler=run_dependency_readback_work_unit,
                start_payload={"card_id": card_id, "ref_count": len(refs)},
            )
        except Exception as error:
            payload["diagnostics"] = [
                {
                    "code": "taskboard.dependency_readback.execution_failed",
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "ref_count": len(refs),
                }
            ]
            return DataFormatter.sanitize(payload)

        output_payload = dict(readback_output) if isinstance(readback_output, Mapping) else payload
        compact_carrier = self._compact_block_carrier_for_taskboard_meta(
            execution_meta.get("block_carrier", {}),
            blocks=execution_meta.get("blocks"),
        )
        self.diagnostics.setdefault("taskboard_dependency_readback_block_carriers", []).append(
            {
                "card_id": card_id,
                "ref_count": len(refs),
                "block_carrier": compact_carrier,
            }
        )
        return DataFormatter.sanitize(output_payload)

    @classmethod
    def _compact_taskboard_action_artifact_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
        *,
        max_chars: int = _TASKBOARD_READBACK_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "status": "invalid_result",
                "error": f"Action artifact reader returned { type(readback).__name__ }.",
            }
        artifact_id = str(readback.get("artifact_id") or ref.get("artifact_id") or "")
        action_call_id = str(readback.get("action_call_id") or ref.get("action_call_id") or "")
        value = readback.get("value", readback.get("data", readback.get("result")))
        original_chars = cls._serialized_prompt_chars(value)
        preview = cls._compact_verifier_prompt_value(value, max_chars=max_chars)
        preview_chars = cls._serialized_prompt_chars(preview)
        compact: dict[str, Any] = {
            "ok": bool(readback.get("ok")),
            "status": str(readback.get("status") or ""),
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": str(readback.get("artifact_type") or ref.get("artifact_type") or ""),
            "label": str(readback.get("label") or ref.get("label") or ""),
            "media_type": str(readback.get("media_type") or ref.get("media_type") or ""),
            "ref": DataFormatter.sanitize(dict(ref)),
            "value_preview": preview,
            "value_preview_meta": {
                "truncated": preview_chars < original_chars,
                "original_chars": original_chars,
                "preview_chars": preview_chars,
                "limit_chars": max_chars,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        meta = readback.get("meta")
        if isinstance(meta, Mapping):
            compact["meta"] = cls._compact_verifier_prompt_value(meta, max_chars=1200)
        return compact

    @classmethod
    def _compact_taskboard_workspace_file_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
        *,
        max_chars: int = _TASKBOARD_READBACK_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "readable": False,
                "status": "invalid_result",
                "error": f"Workspace file reader returned { type(readback).__name__ }.",
            }
        path = str(readback.get("path") or ref.get("path") or "")
        content = readback.get("content", readback.get("text", readback.get("value")))
        original_chars = cls._serialized_prompt_chars(content)
        preview = cls._compact_verifier_prompt_value(content, max_chars=max_chars)
        preview_chars = cls._serialized_prompt_chars(preview)
        ok = bool(readback.get("ok", readback.get("readable", False)))
        compact: dict[str, Any] = {
            "ok": ok,
            "status": str(readback.get("status") or ("completed" if ok else "error")),
            "path": path,
            "media_type": str(readback.get("media_type") or ref.get("media_type") or ""),
            "bytes": readback.get("bytes", ref.get("bytes", ref.get("size"))),
            "read_bytes": readback.get("read_bytes"),
            "sha256": str(readback.get("sha256") or ref.get("sha256") or ""),
            "truncated": bool(readback.get("truncated")),
            "handler_id": str(readback.get("handler_id") or ""),
            "content_kind": str(readback.get("content_kind") or ""),
            "extraction_method": str(readback.get("extraction_method") or ""),
            "ref": cls._compact_artifact_ref_for_verifier(ref),
            "content_preview": preview,
            "content_preview_meta": {
                "truncated": preview_chars < original_chars or bool(readback.get("truncated")),
                "original_chars": original_chars,
                "preview_chars": preview_chars,
                "limit_chars": max_chars,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        diagnostics = readback.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            compact["diagnostics"] = cls._compact_verifier_prompt_value(list(diagnostics), max_chars=1200)
        return compact

    @staticmethod
    def _serialized_prompt_chars(value: Any) -> int:
        try:
            return len(json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str))
        except Exception:
            return len(str(value or ""))

    @classmethod
    def _taskboard_available_readback(cls, evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        records = cls._taskboard_action_artifact_recall_records(evidence_view)
        refs = records[0]["artifact_refs"] if records else []
        file_refs = cls._taskboard_readback_file_refs(evidence_view)
        return {
            "schema_version": "agent_task_taskboard_readback/v1",
            "taskboard_readback_shape": {
                "available": bool(refs or file_refs),
                "allowed_execution_shape": "readback",
                "artifact_refs": DataFormatter.sanitize(refs),
                "file_refs": DataFormatter.sanitize(file_refs),
            },
            "action_artifact_readback": {
                "available": bool(refs),
                "action_id": "read_action_artifact",
                "artifact_refs": DataFormatter.sanitize(refs),
            },
            "workspace_file_readback": {
                "available": bool(file_refs),
                "file_refs": DataFormatter.sanitize(file_refs),
            },
            "policy": (
                "Use a TaskBoard readback card only when bounded previews are insufficient and the remaining "
                "work is scoped cold Action artifact or Workspace file readback. Mixed tool/readback work may "
                "still use the ActionRuntime read_action_artifact action or Workspace file actions."
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
                "Use search_surface='workspace_index' for Workspace SQLite/FTS records, 'workspace_files' for bounded file search, or 'workspace_index_and_files' when both bounded surfaces are justified; for workspace_files, query is content text or an exact phrase, path is the directory/file scope, and pattern is one file glob such as *.md, * or **. Do not put list/read/search commands in query.",
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
            "path",
            "sha256",
            "bytes",
            "size",
            "media_type",
            "role",
            "source",
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
                    "sha256",
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
