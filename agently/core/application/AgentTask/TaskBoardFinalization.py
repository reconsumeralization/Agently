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


class AgentTaskTaskBoardFinalizationMixin(AgentTaskMixinBase):
    """TaskBoard final synthesis, terminal verification, and final repair routing."""

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

    def _prioritize_taskboard_final_refs(self, refs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        required_paths = {str(path or "").strip() for path in self._required_workspace_deliverables()}

        def priority(item: tuple[int, Mapping[str, Any]]) -> tuple[int, int]:
            index, ref = item
            path = str(ref.get("path") or "").strip()
            if path and path in required_paths:
                return (0, index)
            if self._is_trusted_workspace_artifact_ref(ref):
                return (1, index)
            return (2, index)

        ordered = sorted(enumerate(refs), key=priority)
        return [dict(DataFormatter.sanitize(ref)) for _, ref in ordered]

    @classmethod
    def _taskboard_final_source_refs_from_evidence_view(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        ledger_refs = source_refs_from_ledger(evidence_view, max_refs=32)
        if ledger_refs:
            return ledger_refs
        return cls._collect_taskboard_source_refs(evidence_view, max_refs=32)

    async def _finalize_taskboard(self, revision: Any, *, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        schedule = TaskBoard(revision, handler=lambda _context: None).schedule()
        result_status = self._taskboard_terminal_status(revision, schedule)
        evidence_view = build_task_board_evidence_view(revision).to_dict()
        evidence_ledger = evidence_ledger_view(evidence_view, max_items=120, body_chars=2400)
        explicit_state_facts = task_board_explicit_state_facts(revision, evidence_view=evidence_view)
        blocking_state_facts = task_board_blocking_state_facts(explicit_state_facts)
        candidate_final_result = self._taskboard_candidate_final_result(revision)
        final_refs = self._prioritize_taskboard_final_refs(self._taskboard_final_refs_from_evidence_view(evidence_view))
        trusted_final_refs = [
            ref
            for ref in final_refs
            if isinstance(ref, Mapping) and self._is_trusted_workspace_artifact_ref(ref)
        ]
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

        final_artifact_evidence_items = await self._taskboard_final_artifact_verification_evidence_items(
            trusted_final_refs,
            final={},
        )
        if final_artifact_evidence_items:
            evidence_view = self._taskboard_evidence_view_with_additional_items(
                evidence_view,
                final_artifact_evidence_items,
            )
            evidence_ledger = evidence_ledger_view(evidence_view, max_items=120, body_chars=2400)

        finalization_source = "model_finalizer"
        final = self._promote_taskboard_final_candidate(
            revision,
            candidate_final_result=candidate_final_result,
            final_refs=final_refs,
            board_status=result_status,
        )
        if final is not None:
            promotion_guard = validate_evidence_use(collect_evidence_use(final), evidence_ledger)
            if promotion_guard.get("valid") is True:
                final = value_with_normalized_evidence_use(final, promotion_guard.get("normalized_evidence_use"))
                finalization_source = "candidate_promotion"
                await self._record_phase(
                    "taskboard_final_candidate_promoted",
                    diagnostics={
                        "reason": final.get("reason", ""),
                        "file_ref_count": len(final_refs),
                        "checked_claims": promotion_guard.get("checked_claims"),
                    },
                )
            else:
                self.diagnostics.setdefault("taskboard_final_candidate_promotion", []).append(
                    DataFormatter.sanitize({"accepted": False, "guard": promotion_guard})
                )
                final = None
        if final is None:
            final = await self._request_taskboard_final(
                revision,
                evidence_view,
                candidate_final_result=candidate_final_result,
                board_status=result_status,
                schedule=schedule,
                allow_degraded_final=result_status != "completed",
            )
            final = self._normalize_taskboard_final_result(
                final,
                candidate_final_result,
                fallback_final_result=self._workspace_artifact_final_result_from_refs(trusted_final_refs),
            )
        final_artifact_evidence_items = self._dedupe_taskboard_final_evidence_items(
            [
                *final_artifact_evidence_items,
                *await self._taskboard_final_artifact_verification_evidence_items(
                    trusted_final_refs,
                    final=final,
                ),
            ]
        )
        if final_artifact_evidence_items:
            evidence_view = self._taskboard_evidence_view_with_additional_items(
                evidence_view,
                final_artifact_evidence_items,
            )
            evidence_ledger = evidence_ledger_view(evidence_view, max_items=120, body_chars=2400)
        final_evidence_guard = validate_evidence_use(collect_evidence_use(final), evidence_ledger)
        final = value_with_normalized_evidence_use(final, final_evidence_guard.get("normalized_evidence_use"))
        accepted = self._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        final_verification: dict[str, Any] | None = None
        should_verify_final = (
            accepted
            or bool(str(final.get("final_result") or "").strip())
            or bool(str(candidate_final_result or "").strip())
            or bool(final_refs)
        )
        if should_verify_final:
            final_source_refs = self._taskboard_final_source_refs_from_evidence_view(evidence_view)
            verifier_final_result = str(final.get("final_result") or "").strip()
            if not verifier_final_result and trusted_final_refs:
                verifier_final_result = self._workspace_artifact_final_result_from_refs(trusted_final_refs)
            if not verifier_final_result:
                verifier_final_result = str(candidate_final_result or "").strip()
            final_execution_result = {
                "status": "completed",
                "accepted": accepted,
                "final_result": verifier_final_result,
                "reason": final.get("reason", ""),
                "missing_criteria": final.get("missing_criteria", []),
                "evidence_use": DataFormatter.sanitize(final.get("evidence_use", [])),
                "file_refs": final_refs,
                "artifact_refs": final_refs,
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_stream(evidence_view),
                "evidence_ledger": evidence_ledger,
            }
            final_execution_meta = {
                "status": "completed",
                "route": {
                    "selected_route": "agent_task",
                    "execution_strategy": self.execution_strategy,
                    "effective_execution_strategy": self.effective_execution_strategy,
                },
                "logs": {"artifact_refs": final_refs, "source_refs": final_source_refs},
                "workspace_refs": {"agent_task_artifacts": final_refs},
                "blocks": {
                    "evidence": {
                        "evidence_items": evidence_ledger.get("items", []),
                        "diagnostics": [],
                    }
                },
                "diagnostics": {
                    "taskboard_terminal_status": result_status,
                    "taskboard_explicit_state_facts": explicit_state_facts,
                    "taskboard_blocking_state_facts": blocking_state_facts,
                },
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
            if blocking_state_facts and final_verification is not None:
                reason = "; ".join(
                    str(fact.get("reason") or fact.get("code") or fact.get("status") or "explicit state fact")
                    for fact in blocking_state_facts
                )
                final_verification["is_complete"] = False
                final_verification["requires_block"] = True
                final_verification["reason"] = reason or "TaskBoard final gate blocked on explicit state facts."
                final_verification["missing_criteria"] = [
                    *list(final_verification.get("missing_criteria") or []),
                    "Resolve explicit task-scoped state facts before accepting the final result.",
                ]
                final_verification["guard_reasons"] = [
                    *list(final_verification.get("guard_reasons") or []),
                    "taskboard_explicit_state_fact_block",
                ]
            if final_verification is not None and bool(final_verification.get("is_complete")):
                accepted = True
                final = dict(final)
                final["accepted"] = True
                verification_final_result = final_verification.get("final_result")
                if not str(final.get("final_result") or "").strip():
                    if verification_final_result not in (None, "", [], {}):
                        final["final_result"] = str(verification_final_result).strip()
                    elif verifier_final_result:
                        final["final_result"] = verifier_final_result
                if not str(final.get("reason") or "").strip():
                    final["reason"] = final_verification.get("reason") or "TaskBoard final verification accepted."
                final["missing_criteria"] = []
            elif final_verification is not None and not bool(final_verification.get("is_complete")):
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
                    "explicit_state_facts": explicit_state_facts,
                    "blocking_state_facts": blocking_state_facts,
                    "terminal_status": result_status,
                    "degraded_finalization_attempted": result_status != "completed",
                    "finalization_source": finalization_source,
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

    @classmethod
    def _dedupe_taskboard_final_evidence_items(
        cls,
        items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            evidence_id = str(item.get("id") or "").strip()
            if evidence_id and evidence_id in seen:
                continue
            if evidence_id:
                seen.add(evidence_id)
            deduped.append(dict(DataFormatter.sanitize(item)))
        return deduped

    @classmethod
    def _taskboard_evidence_view_with_additional_items(
        cls,
        evidence_view: Mapping[str, Any],
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        additional = [item for item in evidence_items if isinstance(item, Mapping)]
        if not additional:
            return dict(DataFormatter.sanitize(evidence_view))
        updated = dict(DataFormatter.sanitize(evidence_view))
        raw_items = updated.get("evidence_items")
        existing = (
            [item for item in raw_items if isinstance(item, Mapping)]
            if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes | bytearray)
            else []
        )
        updated["evidence_items"] = cls._dedupe_taskboard_final_evidence_items([*additional, *existing])
        return DataFormatter.sanitize(updated)

    async def _taskboard_final_artifact_verification_evidence_items(
        self,
        refs: Sequence[Mapping[str, Any]],
        *,
        final: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for ref in self._dedupe_ref_records([dict(item) for item in refs if isinstance(item, Mapping)])[:4]:
            path = str(ref.get("path") or "").strip()
            if not path:
                continue
            source = str(ref.get("source") or "agent_task.taskboard.final_verification.workspace_artifact").strip()
            materialized_ref, content_for_locator, failure_item = await self._taskboard_materialize_final_artifact_ref(
                ref,
                source=source,
            )
            if failure_item is not None:
                items.append(failure_item)
                continue
            items.append(self._workspace_artifact_readback_evidence_item(materialized_ref))
            manifest = self._taskboard_final_artifact_manifest(
                materialized_ref,
                final=final,
                source=source,
            )
            locator_items = await self._workspace_artifact_acceptance_locator_evidence_items(
                ref=materialized_ref,
                result=final,
                manifest=manifest,
                source=source,
                content=content_for_locator,
            )
            targeted_readback_items = await self._taskboard_acceptance_locator_targeted_readback_items(locator_items)
            coverage_item = self._workspace_artifact_acceptance_coverage_evidence_item(
                path=path,
                source=source,
                locator_items=locator_items,
                targeted_readback_items=targeted_readback_items,
            )
            if coverage_item:
                items.append(coverage_item)
            items.extend(targeted_readback_items)
            items.extend(locator_items)
        return self._dedupe_taskboard_final_evidence_items(items)

    async def _taskboard_acceptance_locator_targeted_readback_items(
        self,
        locator_items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for locator in locator_items:
            if not isinstance(locator, Mapping):
                continue
            if str(locator.get("status") or "").strip().lower() != "ok":
                continue
            readback = await self._workspace_artifact_acceptance_locator_readback(locator)
            if readback is None:
                continue
            items.append(self._workspace_artifact_targeted_readback_evidence_item(locator, readback))
        return self._dedupe_taskboard_final_evidence_items(items)

    async def _taskboard_materialize_final_artifact_ref(
        self,
        ref: Mapping[str, Any],
        *,
        source: str,
    ) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        path = str(ref.get("path") or "").strip()
        materialized = dict(DataFormatter.sanitize(ref))
        materialized.setdefault("role", "workspace_artifact")
        materialized["source"] = source
        if not path:
            return materialized, "", None

        has_preview = bool(str(materialized.get("preview") or ""))
        needs_readback = not self._workspace_artifact_ref_has_trusted_readback(materialized) or not has_preview
        if not needs_readback:
            content = str(materialized.get("preview") or "")
            return materialized, "" if materialized.get("truncated") else content, None

        try:
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            return (
                materialized,
                "",
                self._workspace_artifact_failure_evidence_item(
                    path=path,
                    source=source,
                    code="agent_task.taskboard.final_artifact_readback_failed",
                    message="TaskBoard final artifact readback failed before final verification.",
                    readback={
                        "error": {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        }
                    },
                ),
            )

        content = str(read_result.get("content") or "")
        materialized.update(
            {
                "path": str(read_result.get("path") or path),
                "bytes": int(read_result.get("bytes") or materialized.get("bytes") or 0),
                "sha256": str(read_result.get("sha256") or materialized.get("sha256") or ""),
                "media_type": read_result.get("media_type") or materialized.get("media_type"),
                "content_kind": str(read_result.get("content_kind") or materialized.get("content_kind") or "text"),
                "preview": content,
                "truncated": bool(read_result.get("truncated")),
                "read_bytes": int(read_result.get("read_bytes") or 0),
                "handler_id": read_result.get("handler_id") or materialized.get("handler_id"),
            }
        )
        return materialized, "" if materialized.get("truncated") else content, None

    @staticmethod
    def _taskboard_final_artifact_manifest(
        ref: Mapping[str, Any],
        *,
        final: Mapping[str, Any],
        source: str,
    ) -> dict[str, Any]:
        path = str(ref.get("path") or "").strip()
        manifest: dict[str, Any] = {}
        raw_manifest = final.get("artifact_manifest") if isinstance(final, Mapping) else None
        if isinstance(raw_manifest, Mapping):
            manifest_path = str(raw_manifest.get("path") or "").strip()
            if not manifest_path or manifest_path == path:
                manifest.update(dict(DataFormatter.sanitize(raw_manifest)))
        if path:
            manifest["path"] = path
        manifest["source"] = source
        manifest["file_refs"] = [DataFormatter.sanitize(dict(ref))]
        for key in ("bytes", "sha256", "media_type", "content_kind"):
            if ref.get(key) not in (None, "", [], {}):
                manifest[key] = DataFormatter.sanitize(ref.get(key))
        return DataFormatter.sanitize(manifest)

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
        explicit_state_facts = task_board_explicit_state_facts(revision, evidence_view=evidence_view)
        acceptance_index = build_task_board_acceptance_index(
            revision,
            success_criteria=self.success_criteria,
            evidence_view=evidence_view,
            explicit_state_facts=explicit_state_facts,
        )
        focus_payload = build_task_board_focus_payload(
            revision,
            acceptance_index=acceptance_index,
            schedule=schedule,
            preflight_diagnostics=explicit_state_facts,
        )
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
                "evidence_ledger": evidence_ledger_view(evidence_view, max_items=120, body_chars=2400),
                "taskboard_acceptance_index": DataFormatter.sanitize(acceptance_index),
                "taskboard_focus_payload": DataFormatter.sanitize(focus_payload),
                "taskboard_explicit_state_facts": DataFormatter.sanitize(explicit_state_facts),
                "source_ref_policy": self._taskboard_source_ref_policy(),
                "source_refs": source_refs_from_ledger(evidence_view, max_refs=32)
                or self._taskboard_final_source_refs_from_evidence_view(evidence_view),
                "revision": self._compact_taskboard_revision_for_prompt(revision),
                "candidate_final_result": self._compact_verifier_prompt_value(candidate_final_result),
                "execution_prompt": self._execution_prompt_context(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Synthesize the final result for this TaskBoard task from completed card evidence. "
            "Verify every success criterion. Use evidence_ledger as the authoritative grounding ledger and bind "
            "factual claims through evidence_use ids. Use the hot evidence view for summaries and preserve cold refs "
            "as evidence pointers; do not invent unsupported facts. failed/empty ledger items support only missing "
            "or unavailable-data claims; ref_only items support only URL/path/ref discovery until readback exists. "
            "When candidate_final_result contains a "
            "complete non-file answer/report body that satisfies the criteria, preserve it as final_result "
            "instead of rewriting it into a shorter summary. For file-backed deliverables, do not copy the file body "
            "into final_result; return a concise path/ref pointer, or leave final_result empty when trusted refs already "
            "identify the delivered artifact. For source-grounded tasks, the final_result must include "
            "the concrete source URLs, file paths, or evidence refs that support the deliverable; source titles or "
            "general source names without verifier-visible URL/path refs are not enough when refs are available. "
            "For file-backed deliverables, return acceptance_points with expected headings or exact anchors for "
            "critical verification points; do not invent line numbers or trusted file refs. "
            "Apply source_ref_policy: source_refs with content_state='ref_only' are retrieval targets only, while "
            "source refs marked bounded_readback_available can support only the visible bounded preview/excerpt. "
            "If allow_degraded_final is true, the board has stopped with failed, blocked, skipped, or pending "
            "cards. You may still accept only when the completed/degraded evidence is enough to satisfy the "
            "user goal and success criteria with explicit missing-source or degraded-source boundaries in "
            "the final_result. If critical evidence is missing, set accepted=false and explain the missing criteria. "
            "After the final result fields, include short self_check, short_summary, and progress_message for "
            "downstream verification/repair context and human progress. These process fields are not evidence and "
            "must not include raw chain-of-thought or long evidence bodies."
        )
        request.output(
            {
                "accepted": (bool, "True only when all success criteria are satisfied", True),
                "reason": (str, "Concise final verification reason", True),
                "final_result": (
                    str,
                    "Final non-file business result or concise Workspace artifact path/ref pointer when accepted.",
                    False,
                ),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when accepted", False),
                "evidence_use": (
                    [dict],
                    "Claim bindings: [{claim, evidence_ids, support_type}], where support_type is content, unavailability, or ref_pointer",
                    False,
                ),
                "acceptance_points": (
                    [dict],
                    "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}]",
                    False,
                ),
                "self_check": (
                    str,
                    "Short finalization self check of uncertainty or residual risk.",
                    False,
                ),
                "short_summary": (
                    str,
                    "Short finalization summary for terminal verification or repair.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable finalization progress sentence; do not claim verifier acceptance.",
                    False,
                ),
            },
            format="json",
        )
        result = await self._await_task_request(request.async_get_data(), stage="taskboard_finalize")
        if isinstance(result, Mapping):
            await self._emit_process_progress_from_output(
                result,
                stage="taskboard_finalize",
            )
            return dict(result)
        return {"accepted": False, "reason": str(result), "final_result": "", "missing_criteria": self.success_criteria}

    def _promote_taskboard_final_candidate(
        self,
        revision: Any,
        *,
        candidate_final_result: str,
        final_refs: Sequence[Mapping[str, Any]],
        board_status: str,
    ) -> dict[str, Any] | None:
        if str(board_status or "") != "completed":
            return None
        sources = self._taskboard_promotable_deliverable_sources(revision)
        if len(sources) != 1:
            return None
        trusted_refs = [dict(DataFormatter.sanitize(ref)) for ref in final_refs if self._is_trusted_workspace_artifact_ref(ref)]
        final_result = candidate_final_result.strip()
        if not final_result and trusted_refs:
            final_result = self._workspace_artifact_final_result_from_refs(trusted_refs)
        if not final_result:
            return None
        source = sources[0]
        evidence_use = source.get("evidence_use") if isinstance(source, Mapping) else []
        return DataFormatter.sanitize(
            {
                "accepted": True,
                "reason": "Promoted a completed terminal TaskBoard candidate without redundant final synthesis.",
                "final_result": final_result,
                "missing_criteria": [],
                "evidence_use": evidence_use if isinstance(evidence_use, Sequence) and not isinstance(evidence_use, str | bytes | bytearray) else [],
                "taskboard_final_promotion": {
                    "source_card_id": source.get("card_id"),
                    "file_ref_count": len(trusted_refs),
                },
            }
        )

    def _taskboard_promotable_deliverable_sources(self, revision: Any) -> list[dict[str, Any]]:
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        card_results = getattr(revision, "card_results", {}) or {}
        depended_on: set[str] = set()
        for card in cards:
            depended_on.update(str(card_id) for card_id in getattr(card, "depends_on", ()) or ())
        leaf_ids = {str(getattr(card, "id", "")) for card in cards if str(getattr(card, "id", "")) not in depended_on}
        candidate_ids = leaf_ids or {str(card_id) for card_id in card_results.keys()}
        sources: list[dict[str, Any]] = []
        for card_id, result in card_results.items():
            card_id_text = str(card_id)
            if card_id_text not in candidate_ids:
                continue
            if str(getattr(result, "status", "")).strip().lower() != "completed":
                continue
            preview = getattr(result, "preview", None)
            candidate = self._candidate_final_result_from_execution_result(preview, include_answer=False)
            trusted_refs = self._trusted_taskboard_result_refs(result, preview)
            if not candidate and not trusted_refs:
                continue
            sources.append(
                {
                    "card_id": card_id_text,
                    "candidate_final_result": candidate,
                    "file_refs": trusted_refs,
                    "evidence_use": self._taskboard_result_evidence_use(preview),
                }
            )
        return DataFormatter.sanitize(sources)

    def _trusted_taskboard_result_refs(self, result: Any, preview: Any) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if self._is_trusted_workspace_artifact_ref(value):
                    refs.append(dict(DataFormatter.sanitize(value)))
                    return
                for key in ("file_refs", "artifact_refs"):
                    nested = value.get(key)
                    if isinstance(nested, Sequence) and not isinstance(nested, str | bytes | bytearray):
                        for item in nested:
                            collect(item)
                manifest = value.get("artifact_manifest")
                if isinstance(manifest, Mapping):
                    collect(manifest)
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    collect(item)

        collect(getattr(result, "file_refs", ()))
        collect(getattr(result, "artifact_refs", ()))
        collect(preview)
        return self._dedupe_ref_records(refs)

    @staticmethod
    def _taskboard_result_evidence_use(preview: Any) -> list[dict[str, Any]]:
        if not isinstance(preview, Mapping):
            return []
        evidence_use = preview.get("evidence_use")
        if isinstance(evidence_use, Mapping):
            return [dict(DataFormatter.sanitize(evidence_use))]
        if isinstance(evidence_use, Sequence) and not isinstance(evidence_use, str | bytes | bytearray):
            return [dict(DataFormatter.sanitize(item)) for item in evidence_use if isinstance(item, Mapping)]
        return []

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

    @classmethod
    def _normalize_taskboard_final_result(
        cls,
        final: dict[str, Any],
        candidate_final_result: str,
        *,
        fallback_final_result: str = "",
    ) -> dict[str, Any]:
        candidate = candidate_final_result.strip()
        fallback = fallback_final_result.strip()
        if not candidate and not fallback:
            return final
        accepted = cls._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        if not accepted:
            return final
        final_result = str(final.get("final_result") or "").strip()
        if not final_result and fallback:
            normalized = dict(final)
            normalized["final_result"] = fallback
            return normalized
        if candidate and (
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


__all__ = ["AgentTaskTaskBoardFinalizationMixin"]
