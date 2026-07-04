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
from .TaskBoardSourceRefs import _TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION


class AgentTaskTaskBoardCardExecutionMixin(AgentTaskMixinBase):
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
        evidence_ledger = evidence_ledger_view(evidence_view, max_items=80, body_chars=1800)
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
                "evidence_ledger": evidence_ledger,
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
                "Execute exactly one TaskBoard card. "
                "Provide short card_intent and decision_basis fields before the card result fields to frame this "
                "card-local decision; do not include raw chain-of-thought or hidden reasoning. "
                "Use task_context_contract.current_time when the card needs current/latest/as-of evidence; label older "
                "or historical source material with its time boundary. "
                "taskboard_evidence_view is the compact evidence summary; request full content only through available "
                "Workspace or Action refs when needed. If previous_attempt_errors is non-empty, avoid repeating "
                "the same failing source or method when a bounded fallback can satisfy the card. dependency_readbacks "
                "contains bounded readback previews for dependency Action artifacts that were "
                "structurally truncated or marked full_value_available; inspect those before declaring dependency "
                "evidence missing. If available_readback lists Action artifact refs and the prefetched previews are "
                "still insufficient, call read_action_artifact with the artifact_id and action_call_id before blocking "
                "on missing evidence. If scoped_retrieval_results is present, those are already executed bounded "
                "Workspace search facts; use visible evidence_snippet content only within the excerpt, and treat "
                "locator_ref records as targets for later readback/search rather than source-content proof. "
                "Treat evidence_ledger as the authoritative grounding ledger for dependency evidence. Use ledger item "
                "ids in evidence_use for factual claims. failed/empty items support unavailable or missing-data claims "
                "only; ref_only items support only discovery/ref-pointer claims until readback evidence exists. "
                "Return card-local evidence and remaining work. If the card's original method fails but equivalent evidence or a bounded fallback "
                "is available, return status completed with diagnostics that explain the degraded source boundary. "
                "Only return failed or blocked when the card cannot produce the required outcome or the missing "
                "evidence is truly critical. If this card produces the user-facing deliverable, provide the complete "
                "bounded body in candidate_final_result, final_result, or artifact_markdown when it fits the bounded "
                "response. For a long, sectioned, or file-backed deliverable that cannot fit the bounded response, "
                "return artifact_manifest as a structured deliverable contract with path='final.md', section "
                "ids/titles, brief section intent, and source/evidence refs to use; artifact_manifest is not itself "
                "the deliverable body or proof of completion. Do not include full section content in "
                "artifact_manifest, and do not self-declare trusted file_refs for deliverables. Apply "
                "workspace_delivery_policy: when this card is authorized to write required "
                "final deliverable paths, use the required path in artifact_manifest.path instead of a working/evidence path. "
                "For file-backed deliverables, return acceptance_points with expected headings or exact anchors for "
                "critical verification points; do not invent line numbers or trusted file refs. "
                "If the task is source-grounded, include concrete source URLs, file paths, or "
                "evidence refs from source_refs/dependency_readbacks in the deliverable body; do not mention a "
                "source title or local downloaded filename without its verifier-visible URL/path when such a ref "
                f"exists. {_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION}Review or "
                "verification cards must not put review notes in those deliverable fields unless they include the "
                "full corrected deliverable body. After the main card result fields, include short self_check, "
                "short_summary, and progress_message for downstream card/finalizer context and human progress. "
                "These process fields are not evidence. Do not claim the whole task is complete; report only this "
                "card's local status."
            )
            card_output_schema = {
                "card_intent": (
                    str,
                    "One short sentence stating this card's local intent.",
                    False,
                ),
                "decision_basis": (
                    [str],
                    "Short card-local decision factors; no raw chain-of-thought.",
                    False,
                ),
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
                    "Existing evidence refs only; deliverable refs are trusted only when backed by verifier-visible Workspace/readback evidence",
                    False,
                ),
                "evidence": ([str], "Evidence produced or used by this card", False),
                "evidence_use": (
                    [dict],
                    "Claim bindings: [{claim, evidence_ids, support_type}], where support_type is content, unavailability, or ref_pointer. Cite each evidence id by its evidence-ledger cite_as (eN) or canonical id; for file/section claims cite the bounded readback evidence id, never a free-text locator label",
                    False,
                ),
                "acceptance_points": (
                    [dict],
                    "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}]",
                    False,
                ),
                "remaining_work": ([str], "Remaining work for this card, empty when complete", False),
                "self_check": (
                    str,
                    "Short post-card self check of uncertainty or residual risk.",
                    False,
                ),
                "short_summary": (
                    str,
                    "Short summary for downstream cards or finalization.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable card progress sentence; do not claim whole-task completion.",
                    False,
                ),
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
                child_meta = await self._read_child_execution_meta(execution)
                retry_diagnostic = self._taskboard_card_retry_diagnostic(
                    card_id=context.card.id,
                    error=error,
                    execution_id=execution_id,
                    attempt_index=attempt_index,
                    max_attempts=max_attempts,
                )
                if isinstance(child_meta, Mapping):
                    retry_diagnostic["evidence_summary"] = DataFormatter.sanitize(
                        self._execution_log_summary(cast(dict[str, Any], dict(child_meta)))
                    )
                    await self._emit_action_observation_events(
                        None,
                        execution_meta=child_meta,
                        owner_context=self._taskboard_card_action_event_owner_context(
                            context.card.id,
                            child_meta,
                        ),
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
                    child_meta=child_meta,
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
            self._append_execution_meta_evidence_items(
                cast(dict[str, Any], execution_meta),
                self._taskboard_dependency_readback_evidence_items(dependency_readbacks),
            )
            summary = self._execution_log_summary(cast(dict[str, Any], execution_meta))
            execution_evidence_ledger = self._evidence_ledger_from_execution_meta(cast(Mapping[str, Any], execution_meta))
            card_evidence_ledger = evidence_ledger_view(
                {
                    "evidence_items": [
                        *list(evidence_ledger.get("items", [])),
                        *list(execution_evidence_ledger.get("items", [])),
                    ]
                },
                max_items=120,
                body_chars=1800,
            )
            evidence_use_guard = validate_evidence_use(collect_evidence_use(card_output), card_evidence_ledger)
            evidence_repair_diagnostic: dict[str, Any] | None = None
            if isinstance(card_output, Mapping):
                card_output, evidence_use_guard, evidence_repair_diagnostic = (
                    self._repair_taskboard_card_evidence_use(
                        card_output,
                        evidence_use_guard,
                        card_evidence_ledger,
                    )
                )
                card_output = value_with_normalized_evidence_use(
                    card_output,
                    evidence_use_guard.get("normalized_evidence_use"),
                )
            await self._emit_action_observation_events(
                None,
                execution_meta=execution_meta,
                owner_context=self._taskboard_card_action_event_owner_context(
                    context.card.id,
                    execution_meta,
                ),
            )
            card_status = self._taskboard_card_status(
                card_output,
                execution_meta,
                evidence_use_guard=evidence_use_guard,
            )
            diagnostics = []
            if isinstance(card_output, Mapping):
                raw_diagnostics = card_output.get("diagnostics")
                if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                    diagnostics.extend(
                        dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics
                    )
            if evidence_repair_diagnostic is not None:
                diagnostics.append(evidence_repair_diagnostic)
            evidence_guard_blocking_count = self._taskboard_evidence_guard_blocking_count(evidence_use_guard)
            if evidence_guard_blocking_count > 0:
                diagnostics.append(
                    self._taskboard_card_evidence_use_guard_diagnostic(
                        evidence_use_guard,
                        blocking_count=evidence_guard_blocking_count,
                    )
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
                    "evidence_use_guard": evidence_use_guard,
                }
            )
            patch_proposal = (
                self._taskboard_scoped_retrieval_continuation_patch(context, card_output, diagnostics)
                if isinstance(card_output, Mapping)
                else None
            )
            process_summary = self._process_summary_from_value(
                card_output,
                stage="taskboard_card",
            )
            await self._emit_process_progress_from_output(
                card_output,
                stage="taskboard_card",
                card_id=context.card.id,
            )
            if attempt_index < max_attempts and self._taskboard_card_result_retryable(
                status=card_status,
                diagnostics=diagnostics,
            ):
                retry_diagnostic = self._taskboard_card_result_retry_diagnostic(
                    card_id=context.card.id,
                    status=card_status,
                    diagnostics=diagnostics,
                    attempt_index=attempt_index,
                    max_attempts=max_attempts,
                )
                previous_errors.append(retry_diagnostic)
                self.diagnostics.setdefault("taskboard_card_retries", []).append(retry_diagnostic)
                await self._emit(
                    f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.execution.retry",
                    retry_diagnostic,
                )
                continue
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
                    "evidence_ledger": card_evidence_ledger,
                    "evidence_use_guard": evidence_use_guard,
                    "process_summary": process_summary,
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
        evidence_ledger = evidence_ledger_view(evidence_view, max_items=80, body_chars=1800)
        preflight_diagnostics = task_board_preflight_diagnostics(
            context.revision,
            mounted_capabilities=self._planner_capabilities(),
            workspace_refs=self.workspace_refs.get("artifacts", []) if isinstance(self.workspace_refs, Mapping) else [],
        )
        acceptance_index = build_task_board_acceptance_index(
            context.revision,
            success_criteria=self.success_criteria,
            evidence_view=evidence_view,
            explicit_state_facts=task_board_explicit_state_facts(context.revision, evidence_view=evidence_view),
        )
        focus_payload = build_task_board_focus_payload(
            context.revision,
            acceptance_index=acceptance_index,
            schedule=TaskBoard(context.revision, handler=lambda _context: None).schedule(),
            preflight_diagnostics=preflight_diagnostics,
        )
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
            "evidence_ledger": evidence_ledger,
            "taskboard_acceptance_index": DataFormatter.sanitize(acceptance_index),
            "taskboard_focus_payload": DataFormatter.sanitize(focus_payload),
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
            "Complete one TaskBoard control card. "
            "This card is for synthesis, verification, finalization, or deciding the next board action; "
            "provide short card_intent and decision_basis fields before the control result fields; do not include raw "
            "chain-of-thought or hidden reasoning. "
            "Use task_context_contract.current_time when current/latest/as-of evidence matters, and label older "
            "or historical source material with its time boundary. "
            "do not plan or call tools from this request. taskboard_evidence_view is the compact evidence summary "
            "and preserve cold refs as pointers. Treat evidence_ledger as the authoritative grounding ledger and "
            "bind factual claims through evidence_use ids. failed/empty items support unavailability only; ref_only "
            "items support only discovery/ref-pointer claims until readback evidence exists. dependency_readbacks contains bounded "
            "readback previews for dependency Action artifacts that were structurally truncated or marked "
            "full_value_available; inspect those before declaring dependency evidence missing. If bounded previews "
            "and dependency_readbacks are insufficient, set next_board_action to 'readback' or 'repair' and explain "
            "the exact missing refs or gaps instead of inventing facts. If a concrete URL, path, or ref must be "
            "fetched or materialized before continuing, put it in target_refs; do not mention it only in gaps prose. "
            "When the card can produce the user-facing deliverable, provide the complete bounded body in "
            "artifact_markdown, candidate_final_result, or final_result when it fits the bounded output. For a long, "
            "sectioned, or file-backed deliverable that cannot fit the bounded response, return artifact_manifest as "
            "a structured deliverable contract with path='final.md', section ids/titles, brief section intent, and "
            "source/evidence refs to use; artifact_manifest is not itself the deliverable body or proof of completion. "
            "Do not include full section content in artifact_manifest, and do not self-declare trusted file_refs for "
            "deliverables. If the task is source-grounded, include "
            "the concrete source URLs, file paths, or evidence refs used by the deliverable in the deliverable body; "
            "do not mention a source title without its verifier-visible URL/path when such a ref exists. "
            "Apply workspace_delivery_policy: when this card is authorized to write required final deliverable paths, "
            "use the required path in artifact_manifest.path instead of a working/evidence path. "
            "For file-backed deliverables, return acceptance_points with expected headings or exact anchors for "
            "critical verification points; do not invent line numbers or trusted file refs. "
            "After the main control result fields, include short self_check, short_summary, and progress_message for "
            "downstream board context and human progress; these process fields are not evidence. "
            f"{_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION}Also return whether the card is sufficient "
            "and what continuation, if any, the board should consider."
        )
        control_output_schema = {
            "card_intent": (
                str,
                "One short sentence stating this control card's local intent.",
                False,
            ),
            "decision_basis": (
                [str],
                "Short control-card decision factors; no raw chain-of-thought.",
                False,
            ),
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
                "Existing evidence refs only; model-declared deliverable refs are untrusted without verifier-visible Workspace/readback evidence",
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
            "evidence_use": (
                [dict],
                "Claim bindings: [{claim, evidence_ids, support_type}], where support_type is content, unavailability, or ref_pointer. Cite each evidence id by its evidence-ledger cite_as (eN) or canonical id; for file/section claims cite the bounded readback evidence id, never a free-text locator label",
                False,
            ),
            "acceptance_points": (
                [dict],
                "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}]",
                False,
            ),
            "remaining_work": ([str], "Remaining work for this card, empty when complete", False),
            "self_check": (
                str,
                "Short post-control self check of uncertainty or residual risk.",
                False,
            ),
            "short_summary": (
                str,
                "Short summary for downstream board execution or finalization.",
                False,
            ),
            "progress_message": (
                str,
                "One safe human-readable control-card progress sentence; do not claim whole-task completion.",
                False,
            ),
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
        self._append_execution_meta_evidence_items(
            cast(dict[str, Any], execution_meta),
            self._taskboard_dependency_readback_evidence_items(dependency_readbacks),
        )
        execution_evidence_ledger = self._evidence_ledger_from_execution_meta(cast(Mapping[str, Any], execution_meta))
        card_evidence_ledger = evidence_ledger_view(
            {
                "evidence_items": [
                    *list(evidence_ledger.get("items", [])),
                    *list(execution_evidence_ledger.get("items", [])),
                ]
            },
            max_items=120,
            body_chars=1800,
        )
        evidence_use_guard = validate_evidence_use(collect_evidence_use(card_output), card_evidence_ledger)
        evidence_repair_diagnostic: dict[str, Any] | None = None
        if isinstance(card_output, Mapping):
            card_output, evidence_use_guard, evidence_repair_diagnostic = self._repair_taskboard_card_evidence_use(
                card_output,
                evidence_use_guard,
                card_evidence_ledger,
            )
            card_output = value_with_normalized_evidence_use(
                card_output,
                evidence_use_guard.get("normalized_evidence_use"),
            )
        diagnostics = []
        if isinstance(card_output, Mapping):
            raw_diagnostics = card_output.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(
                    dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics
                )
        if evidence_repair_diagnostic is not None:
            diagnostics.append(evidence_repair_diagnostic)
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
                "evidence_use_guard": evidence_use_guard,
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
        process_summary = self._process_summary_from_value(
            card_output,
            stage="taskboard_control",
        )
        await self._emit_process_progress_from_output(
            card_output,
            stage="taskboard_control",
            card_id=context.card.id,
        )
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
                "evidence_ledger": card_evidence_ledger,
                "evidence_use_guard": evidence_use_guard,
                "process_summary": process_summary,
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
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        delta = None if self._is_process_summary_stream_path(raw_path) else getattr(item, "delta", None)
        display_meta = self._taskboard_control_stream_display_meta(raw_path)
        return await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.control.{raw_path}",
            getattr(item, "value", None),
            event_type=event_type,
            delta=delta,
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            meta={
                "task_id": self.id,
                "status": self.status,
                "stage": "taskboard_card_control",
                "card_id": card_id,
                "stream_kind": "taskboard_control_request",
                "control_path": raw_path,
                **display_meta,
            },
        )

    @staticmethod
    def _taskboard_control_stream_display_meta(raw_path: str) -> dict[str, Any]:
        primary = str(raw_path or "").split(".", 1)[0].split("[", 1)[0].strip()
        natural_language_titles = {
            "answer": "Repair answer",
            "candidate_final_result": "Candidate final result",
            "final_result": "Final result",
            "progress_message": "Progress message",
            "self_check": "Self-check",
            "short_summary": "Short summary",
        }
        structured_titles = {
            "acceptance_points": ("[Acceptance: Criteria]", "acceptance"),
            "diagnostics": ("[Diagnostic: Execution diagnostics]", "diagnostic"),
            "evidence": ("[Evidence: Evidence summary]", "evidence"),
            "evidence_use": ("[Evidence: Evidence binding]", "evidence"),
            "file_refs": ("[Artifact: File references]", "artifact"),
            "gaps": ("[Diagnostic: Gaps]", "diagnostic"),
            "next_board_action": ("[Action: Next step]", "action"),
            "patch_proposal": ("[Action: Patch proposal]", "action"),
            "remaining_work": ("[Action: Remaining work]", "action"),
            "source_refs": ("[Evidence: Source references]", "evidence"),
            "status": ("[Status: Card status]", "status"),
            "sufficient": ("[Status: Evidence sufficiency]", "status"),
            "target_refs": ("[Action: Target refs]", "action"),
            "$status": ("[Status: Model request status]", "status"),
        }
        title_key = f"agent_task.taskboard.control.{primary or 'stream'}"
        if primary in natural_language_titles:
            title = natural_language_titles[primary]
            return {
                "display_title": title,
                "display_title_default": title,
                "display_title_key": title_key,
                "display_category": "model_natural_language",
                "display_is_intermediate": False,
            }
        if primary in structured_titles:
            title, category = structured_titles[primary]
            return {
                "display_title": title,
                "display_title_default": title,
                "display_title_key": title_key,
                "display_category": category,
                "display_is_intermediate": True,
            }
        title = f"[Intermediate: {primary or 'stream'}]"
        return {
            "display_title": title,
            "display_title_default": title,
            "display_title_key": title_key,
            "display_category": "intermediate",
            "display_is_intermediate": True,
        }

    async def _bridge_taskboard_card_execution_stream(self, card_id: str, execution: Any) -> None:
        try:
            async for stream_record in execution.get_async_generator(type="all"):
                if isinstance(stream_record, tuple) and len(stream_record) == 2:
                    _, item = stream_record
                else:
                    item = stream_record
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

    def _taskboard_card_action_event_owner_context(
        self,
        card_id: str,
        execution_meta: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner_context = self._action_event_owner_context(None, execution_meta)
        owner_context["origin"] = owner_context.get("origin") or "taskboard_card"
        owner_context["strategy"] = owner_context.get("strategy") or self.execution_strategy
        owner_context["card_id"] = owner_context.get("card_id") or card_id
        return owner_context

    async def _emit_taskboard_card_execution_stream_item(
        self,
        card_id: str,
        execution: Any,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        delta = None if self._is_process_summary_stream_path(raw_path) else getattr(item, "delta", None)
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
            delta=delta,
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
        no_progress_timeout = self._task_no_progress_timeout()
        if timeout is None and no_progress_timeout is None:
            return await awaitable
        task = asyncio.ensure_future(awaitable)
        try:
            timeout_at = time.monotonic() + timeout if timeout is not None else None
            while True:
                if task.done():
                    return await task
                wait_candidates: list[float] = []
                if timeout_at is not None:
                    remaining = timeout_at - time.monotonic()
                    if remaining <= 0:
                        task.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await task
                        raise TimeoutError(
                            f"TaskBoard card '{card_id}' {stage} request timed out after {timeout} seconds."
                        )
                    wait_candidates.append(remaining)
                if no_progress_timeout is not None:
                    quiet_for = time.monotonic() - self._last_stream_emit_monotonic
                    remaining = no_progress_timeout - quiet_for
                    if remaining <= 0:
                        task.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await task
                        raise TimeoutError(
                            f"TaskBoard card '{card_id}' {stage} request made no progress before idle deadline: "
                            f"max_no_progress_seconds={no_progress_timeout}."
                        )
                    wait_candidates.append(remaining)
                done, _pending = await asyncio.wait({task}, timeout=min(wait_candidates))
                if done:
                    return await task
        except (asyncio.TimeoutError, TimeoutError) as error:
            raise TimeoutError(
                _compact_agent_task_error_message(
                    error,
                    fallback=f"TaskBoard card '{card_id}' {stage} request timed out.",
                )
            ) from error

    def _failed_taskboard_card_result(
        self,
        *,
        card_id: str,
        error: Exception,
        execution_id: str | None = None,
        child_meta: Mapping[str, Any] | None = None,
    ) -> TaskBoardCardResult:
        message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
        is_timeout = self._is_timeout_error(error)
        if is_timeout and message == error.__class__.__name__:
            message = (
                f"TaskBoard card '{card_id}' execution timed out after " f"{self._task_request_timeout()} seconds."
            )
        diagnostics: list[dict[str, Any]] = []
        artifact_refs: tuple[Any, ...] = ()
        metadata: dict[str, Any] = {
            "execution_id": execution_id,
            "execution_strategy": self.execution_strategy,
            "status": "failed",
        }
        partial_evidence_diagnostic: dict[str, Any] | None = None
        if isinstance(child_meta, Mapping):
            child_summary = self._execution_log_summary(cast(dict[str, Any], dict(child_meta)))
            raw_artifact_refs = child_summary.get("artifact_refs")
            if isinstance(raw_artifact_refs, Sequence) and not isinstance(
                raw_artifact_refs, str | bytes | bytearray
            ):
                artifact_refs = tuple(DataFormatter.sanitize(ref) for ref in raw_artifact_refs)
            partial_evidence_diagnostic = {
                "type": "TaskBoardPartialChildEvidence",
                "code": "taskboard.card.partial_child_evidence",
                "status": "captured",
                "card_id": card_id,
                "execution_id": execution_id,
                "execution_strategy": self.execution_strategy,
                "stage": "taskboard_card",
                "evidence_summary": DataFormatter.sanitize(child_summary),
            }
            metadata["partial_child_evidence"] = True
            metadata["partial_child_status"] = str(child_meta.get("status") or "")
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
        diagnostics.append(diagnostic)
        if partial_evidence_diagnostic is not None:
            diagnostics.append(partial_evidence_diagnostic)
        self.diagnostics.setdefault("taskboard_card_errors", []).append(diagnostic)
        return TaskBoardCardResult(
            card_id=card_id,
            status="failed",
            preview=f"TaskBoard card execution failed: { error.__class__.__name__}: { message }",
            artifact_refs=artifact_refs,
            diagnostics=tuple(diagnostics),
            metadata={
                **metadata,
            },
        )

    @classmethod
    def _repair_taskboard_card_evidence_use(
        cls,
        card_output: Mapping[str, Any],
        evidence_use_guard: Mapping[str, Any],
        card_evidence_ledger: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any] | None]:
        original_blocking_count = cls._taskboard_evidence_guard_blocking_count(evidence_use_guard)
        if original_blocking_count <= 0:
            return card_output, evidence_use_guard, None
        repaired_evidence_use = cls._deterministic_evidence_binding_repair(evidence_use_guard, card_evidence_ledger)
        if not repaired_evidence_use:
            return card_output, evidence_use_guard, None
        repaired_output = value_with_normalized_evidence_use(card_output, repaired_evidence_use)
        repaired_guard = validate_evidence_use(collect_evidence_use(repaired_output), card_evidence_ledger)
        repaired_blocking_count = cls._taskboard_evidence_guard_blocking_count(repaired_guard)
        if repaired_blocking_count >= original_blocking_count:
            return card_output, evidence_use_guard, None
        diagnostic = {
            "code": "taskboard.card.evidence_binding_repair",
            "status": "completed" if repaired_blocking_count == 0 else "partial",
            "original_blocking_count": original_blocking_count,
            "repaired_blocking_count": repaired_blocking_count,
            "repaired_claim_count": len(repaired_evidence_use),
        }
        return repaired_output, repaired_guard, diagnostic

    @staticmethod
    def _taskboard_evidence_guard_blocking_count(evidence_use_guard: Mapping[str, Any]) -> int:
        try:
            return int(evidence_use_guard.get("blocking_count") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _taskboard_card_evidence_use_guard_diagnostic(
        evidence_use_guard: Mapping[str, Any],
        *,
        blocking_count: int,
    ) -> dict[str, Any]:
        guard_diagnostics: list[dict[str, Any]] = []
        raw_diagnostics = evidence_use_guard.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            for item in raw_diagnostics:
                if not isinstance(item, Mapping):
                    continue
                compact: dict[str, Any] = {}
                for key in ("code", "claim", "evidence_id", "support_type", "message"):
                    value = item.get(key)
                    if value in (None, "", [], {}):
                        continue
                    compact[key] = str(value)[:500] if key in {"claim", "message"} else DataFormatter.sanitize(value)
                if compact:
                    guard_diagnostics.append(compact)
                if len(guard_diagnostics) >= 6:
                    break
        return {
            "code": "taskboard.card.evidence_use_guard_blocking",
            "status": "blocked",
            "message": (
                "TaskBoard card evidence_use contains invalid or unbound evidence refs; retry using "
                "evidence_ledger item ids or cite_as values from the available evidence."
            ),
            "blocking_count": blocking_count,
            "guard_diagnostics": guard_diagnostics,
        }

    @staticmethod
    def _taskboard_card_status(
        card_output: Any,
        execution_meta: Mapping[str, Any],
        *,
        evidence_use_guard: Mapping[str, Any] | None = None,
    ) -> str:
        execution_status = str(execution_meta.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            return "failed"
        if isinstance(evidence_use_guard, Mapping):
            try:
                blocking_count = int(evidence_use_guard.get("blocking_count") or 0)
            except (TypeError, ValueError):
                blocking_count = 0
            if blocking_count > 0:
                return "blocked"
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



__all__ = ["AgentTaskTaskBoardCardExecutionMixin"]
