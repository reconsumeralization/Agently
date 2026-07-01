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

import json
import re
from pathlib import PurePosixPath

from .TaskShared import *


class AgentTaskVerificationMixin(AgentTaskMixinBase):
    def _iteration_prompt_summaries(self) -> list[dict[str, Any]]:
        """Bounded, low-noise iteration history for plan/verify prompts.

        Full iteration records (including execution_meta) stay in self.iterations
        and the Workspace; prompts receive only step intent, the verification
        outcome, and Workspace refs so context does not grow unboundedly with the
        full execution metadata of every prior step.
        """
        limit = self._iterations_prompt_limit()
        records = self.iterations[-limit:] if isinstance(limit, int) and limit > 0 else self.iterations
        summaries: list[dict[str, Any]] = []
        for record in records:
            plan = record.get("plan")
            if not isinstance(plan, dict):
                plan = {}
            verification = record.get("verification")
            if not isinstance(verification, dict):
                verification = {}
            execution_meta = record.get("execution_meta")
            evidence_anchors = (
                self._planner_evidence_anchors_from_execution_meta(execution_meta)
                if isinstance(execution_meta, Mapping)
                else {}
            )
            summaries.append(
                {
                    "iteration": record.get("iteration"),
                    "step_instruction": plan.get("step_instruction", ""),
                    "effective_execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "")),
                    "process_summary": DataFormatter.sanitize(record.get("process_summary", {})),
                    "verification": {
                        "is_complete": verification.get("is_complete"),
                        "reason": verification.get("reason", ""),
                        "failure_analysis": verification.get("failure_analysis", ""),
                        "acceptance_delta": verification.get("acceptance_delta", []),
                        "missing_criteria": verification.get("missing_criteria", []),
                        "replan_instruction": verification.get("replan_instruction", ""),
                        "repair_constraints": verification.get("repair_constraints", []),
                        "next_step_requirements": verification.get("next_step_requirements", []),
                    },
                    "observation_ref": record.get("observation_ref"),
                    "verification_ref": record.get("verification_ref"),
                    "reflection_refs": DataFormatter.sanitize(record.get("reflection_refs", [])),
                    "evidence_anchors": evidence_anchors,
                }
            )
        # Prior-run summaries (from a resumed snapshot) come first so the model
        # sees the full task history; the recent-window limit applies to the
        # combined sequence.
        combined = [*self._resumed_iteration_summaries, *summaries]
        if isinstance(limit, int) and limit > 0 and len(combined) > limit:
            combined = combined[-limit:]
        return combined

    def _reflection_prompt_summaries(self) -> list[dict[str, Any]]:
        limit = self._iterations_prompt_limit()
        records = self.reflections[-limit:] if isinstance(limit, int) and limit > 0 else self.reflections
        summaries: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            summaries.append(
                {
                    "iteration": record.get("iteration"),
                    "phase": record.get("phase"),
                    "record_ref": record.get("record_ref"),
                    "summary": DataFormatter.sanitize(record.get("summary", {})),
                    "completion_evidence": False,
                }
            )
        return summaries

    @classmethod
    def _planner_repair_context(cls, previous_iterations: list[dict[str, Any]]) -> dict[str, Any]:
        if not previous_iterations:
            return {}
        latest = previous_iterations[-1]
        if not isinstance(latest, Mapping):
            return {}
        verification = latest.get("verification")
        if not isinstance(verification, Mapping):
            return {}
        if verification.get("is_complete") is True:
            return {}

        def normalize_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, tuple):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        missing_criteria = normalize_list(verification.get("missing_criteria"))
        acceptance_delta = normalize_list(verification.get("acceptance_delta"))
        repair_constraints = normalize_list(verification.get("repair_constraints"))
        next_step_requirements = normalize_list(verification.get("next_step_requirements"))
        replan_instruction = str(verification.get("replan_instruction") or "").strip()
        failure_analysis = str(verification.get("failure_analysis") or "").strip()
        if not any(
            [
                missing_criteria,
                acceptance_delta,
                repair_constraints,
                next_step_requirements,
                replan_instruction,
                failure_analysis,
            ]
        ):
            return {}
        repair_context = {
            "source_iteration": latest.get("iteration"),
            "verification_ref": latest.get("verification_ref"),
            "reason": str(verification.get("reason") or ""),
            "failure_analysis": failure_analysis,
            "acceptance_delta": acceptance_delta,
            "missing_criteria": missing_criteria,
            "advisory_repair_constraints": repair_constraints,
            "advisory_next_step_requirements": next_step_requirements,
            "replan_instruction": replan_instruction,
        }
        process_summary = latest.get("process_summary")
        if isinstance(process_summary, Mapping):
            compact_process = DataFormatter.sanitize(dict(process_summary))
            if compact_process:
                repair_context["process_summary"] = compact_process
        cumulative_anchors = cls._cumulative_planner_evidence_anchors(previous_iterations)
        if cumulative_anchors:
            repair_context["available_evidence_anchors"] = cumulative_anchors
        return repair_context

    def _active_repair_context(self) -> dict[str, Any]:
        """Latest verifier feedback for the next consumer work unit.

        Planner prompts already receive this shape. Execution and artifact
        carrier prompts need the same compact contract so a repair pass does not
        depend on the planner restating every verifier finding in prose.
        """

        return self._planner_repair_context(self._iteration_prompt_summaries())

    @classmethod
    def _planner_evidence_anchors_from_execution_meta(cls, execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        """Compact exact refs and previews that later planning can safely reuse.

        The planner should not receive full execution metadata on every turn, but
        repair steps need stable URLs, paths, and bounded action previews so they
        do not reconstruct source refs from prose verification feedback.
        """

        summary = cls._execution_log_summary(dict(execution_meta))
        return cls._planner_evidence_anchors_from_summary(summary)

    @classmethod
    def _planner_evidence_anchors_from_summary(cls, summary: Mapping[str, Any]) -> dict[str, Any]:
        source_refs = cls._compact_planner_source_refs(summary.get("source_refs", []), max_refs=24)
        action_previews = cls._compact_planner_action_previews(summary.get("actions", []), max_actions=8)
        artifact_refs = summary.get("artifact_refs")
        compact_artifact_refs: list[Any] = []
        if isinstance(artifact_refs, list):
            compact_artifact_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in artifact_refs[:8]]
        anchors: dict[str, Any] = {}
        if source_refs:
            anchors["source_refs"] = source_refs
        if action_previews:
            anchors["action_result_previews"] = action_previews
        if compact_artifact_refs:
            anchors["artifact_refs"] = compact_artifact_refs
        if summary.get("action_ids"):
            anchors["action_ids"] = DataFormatter.sanitize(summary.get("action_ids", []))
        status = str(summary.get("status") or "").strip()
        if status:
            anchors["status"] = status
        return anchors

    @classmethod
    def _cumulative_planner_evidence_anchors(cls, previous_iterations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        source_refs: list[Any] = []
        action_previews: list[Any] = []
        artifact_refs: list[Any] = []
        action_ids: list[str] = []
        for iteration in previous_iterations:
            anchors = iteration.get("evidence_anchors")
            if not isinstance(anchors, Mapping):
                continue
            raw_source_refs = anchors.get("source_refs")
            if isinstance(raw_source_refs, list):
                source_refs.extend(raw_source_refs)
            raw_action_previews = anchors.get("action_result_previews")
            if isinstance(raw_action_previews, list):
                action_previews.extend(raw_action_previews)
            raw_artifact_refs = anchors.get("artifact_refs")
            if isinstance(raw_artifact_refs, list):
                artifact_refs.extend(raw_artifact_refs)
            for action_id in cls._normalize_string_list(anchors.get("action_ids")):
                if action_id not in action_ids:
                    action_ids.append(action_id)

        cumulative: dict[str, Any] = {}
        compact_source_refs = cls._compact_planner_source_refs(source_refs, max_refs=32)
        if compact_source_refs:
            cumulative["source_refs"] = compact_source_refs
        compact_action_previews = cls._dedupe_planner_action_previews(action_previews)[:10]
        if compact_action_previews:
            cumulative["action_result_previews"] = compact_action_previews
        compact_artifact_refs = cls._dedupe_ref_records(artifact_refs)[:12]
        if compact_artifact_refs:
            cumulative["artifact_refs"] = compact_artifact_refs
        if action_ids:
            cumulative["action_ids"] = action_ids
        return DataFormatter.sanitize(cumulative)

    @classmethod
    def _compact_planner_source_refs(cls, refs: Any, *, max_refs: int) -> list[dict[str, Any]]:
        if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
            return []
        allowed_fields = {"source_url", "selected_url", "requested_url", "url", "href", "path"}
        compact: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            field = str(ref.get("field") or "").strip()
            value = str(ref.get("value") or "").strip()
            if not field and not value:
                path_value = str(ref.get("path") or "").strip()
                if path_value:
                    field = "path"
                    value = path_value
            action_call_id = str(ref.get("action_call_id") or "").strip()
            if field not in allowed_fields or not value:
                continue
            key = (field, value, action_call_id)
            if key in seen:
                continue
            seen.add(key)
            compact_ref = {
                "field": field,
                "value": value,
                "action_id": str(ref.get("action_id") or ""),
                "action_call_id": action_call_id,
                "path": str(ref.get("path") or ""),
            }
            content_state = str(ref.get("content_state") or "").strip()
            if content_state:
                compact_ref["content_state"] = content_state
            evidence_boundary = str(ref.get("evidence_boundary") or "").strip()
            if evidence_boundary:
                compact_ref["evidence_boundary"] = evidence_boundary
            compact.append(compact_ref)
            if len(compact) >= max_refs:
                break
        return compact

    @classmethod
    def _compact_planner_action_previews(cls, actions: Any, *, max_actions: int) -> list[dict[str, Any]]:
        if not isinstance(actions, Sequence) or isinstance(actions, str | bytes | bytearray):
            return []
        compact: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, Mapping):
                continue
            preview = action.get("result_preview")
            refs = cls._compact_planner_source_refs(cls._collect_source_refs_from_action_records([action]), max_refs=12)
            if preview is None and not refs:
                continue
            compact_action: dict[str, Any] = {
                "id": str(action.get("id") or action.get("name") or ""),
                "status": str(action.get("status") or ""),
            }
            action_call_id = str(action.get("action_call_id") or "").strip()
            if action_call_id:
                compact_action["action_call_id"] = action_call_id
            if action.get("input_preview"):
                compact_action["input_preview"] = cls._compact_verifier_prompt_value(
                    action.get("input_preview"),
                    max_chars=500,
                )
            if preview is not None:
                compact_action["result_preview"] = cls._compact_action_preview_value(preview, max_chars=1800)
            if refs:
                compact_action["source_refs"] = refs
            compact.append(compact_action)
            if len(compact) >= max_actions:
                break
        return cls._dedupe_planner_action_previews(compact)

    @staticmethod
    def _dedupe_planner_action_previews(actions: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for action in actions:
            if isinstance(action, Mapping):
                key = "|".join(
                    [
                        str(action.get("id") or ""),
                        str(action.get("action_call_id") or ""),
                        str(action.get("status") or ""),
                    ]
                )
            else:
                key = str(action)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(action)
        return deduped

    def _iterations_prompt_limit(self) -> int | None:
        configured = self._agent_task_option("iterations_prompt_limit", None)
        if configured is None:
            return None
        try:
            limit = int(configured)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    @staticmethod
    def _shape_for_route(route: str) -> str:
        return {
            "model_request": "direct",
            "skills": "skills",
            "dynamic_task": "dynamic_task",
            "agent_task": "dynamic_task",
        }.get(str(route or "").strip(), "direct")

    def _reconcile_effective_shape(self, plan: dict[str, Any], execution_meta: dict[str, Any]) -> None:
        """Write the route actually taken back into the plan's effective shape.

        Keeps effective_execution_shape consistent with the executed route so
        diagnostics never report a shape the run did not actually take.
        """
        route_info = execution_meta.get("route")
        if not isinstance(route_info, dict):
            route_info = {}
        actual_route = str(route_info.get("selected_route") or "")
        if not actual_route:
            return
        requested_shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct")
        # "actions" and "direct" both run the model_request route; treat them as
        # consistent so a normal actions step is not flagged as a mismatch.
        consistent = {"direct", "actions"} if actual_route == "model_request" else {self._shape_for_route(actual_route)}
        if requested_shape in consistent:
            return
        plan["effective_execution_shape"] = self._shape_for_route(actual_route)
        plan["route_shape_reconciled"] = {
            "requested_shape": requested_shape,
            "actual_route": actual_route,
            "effective_shape": plan["effective_execution_shape"],
        }
        self.diagnostics.setdefault("route_shape_reconciliations", []).append(plan["route_shape_reconciled"])

    async def _request_verification(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        execution_result: Any,
        execution_meta: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> dict[str, Any]:
        language_policy = self._language_policy()
        raw_execution_evidence_summary = self._execution_log_summary(execution_meta)
        raw_cumulative_evidence_summary = self._cumulative_execution_evidence_summary(execution_meta)
        current_evidence_ledger = self._evidence_ledger_from_execution_meta(execution_meta)
        evidence_ledger = self._cumulative_evidence_ledger(execution_meta)
        initial_evidence_use = collect_evidence_use(execution_result)
        initial_guard = validate_evidence_use(initial_evidence_use, evidence_ledger)
        await self._ensure_workspace_artifact_targeted_readback_evidence(
            execution_meta,
            evidence_ledger,
            evidence_use=initial_guard.get("normalized_evidence_use"),
        )
        raw_execution_evidence_summary = self._execution_log_summary(execution_meta)
        raw_cumulative_evidence_summary = self._cumulative_execution_evidence_summary(execution_meta)
        current_evidence_ledger = self._evidence_ledger_from_execution_meta(execution_meta)
        evidence_ledger = self._cumulative_evidence_ledger(execution_meta)
        grounding_guard = validate_evidence_use(initial_evidence_use, evidence_ledger)
        normalized_execution_result = value_with_normalized_evidence_use(
            execution_result,
            grounding_guard.get("normalized_evidence_use"),
        )
        if self._should_attempt_evidence_binding_repair(grounding_guard):
            repaired_evidence_use = self._deterministic_evidence_binding_repair(grounding_guard, evidence_ledger)
            if repaired_evidence_use:
                self.diagnostics.setdefault("evidence_binding_repair", []).append(
                    DataFormatter.sanitize(
                        {
                            "source": "deterministic_alias_resolver",
                            "repaired_count": len(repaired_evidence_use),
                        }
                    )
                )
            elif self._can_attempt_model_evidence_binding_repair():
                repaired_evidence_use = await self._request_evidence_binding_repair(
                    grounding_guard,
                    evidence_ledger,
                    language_policy=language_policy,
                )
            else:
                self.diagnostics.setdefault("evidence_binding_repair", []).append(
                    DataFormatter.sanitize(
                        {
                            "source": "model_repair_attempt_gate",
                            "skipped": True,
                            "attempt_count": self.diagnostics.get("evidence_binding_repair_attempt_count"),
                            "reason": "model evidence binding repair attempt limit reached; deterministic repair had no unique candidate",
                        }
                    )
                )
            if repaired_evidence_use:
                merged_evidence_use = self._merge_repaired_evidence_use(
                    grounding_guard.get("normalized_evidence_use"),
                    repaired_evidence_use,
                )
                candidate_execution_result = value_with_normalized_evidence_use(
                    normalized_execution_result,
                    merged_evidence_use,
                )
                candidate_guard = validate_evidence_use(collect_evidence_use(candidate_execution_result), evidence_ledger)
                self.diagnostics.setdefault("evidence_binding_repair", []).append(
                    DataFormatter.sanitize(
                        {
                            "accepted": candidate_guard.get("valid") is True,
                            "blocking_count": candidate_guard.get("blocking_count"),
                            "diagnostics": candidate_guard.get("diagnostics", []),
                        }
                    )
                )
                if candidate_guard.get("valid") is True:
                    normalized_execution_result = candidate_execution_result
                    grounding_guard = candidate_guard
        trusted_workspace_artifacts = workspace_artifacts_from_ledger(evidence_ledger)
        evidence_summary = self._compact_verifier_evidence_summary(raw_execution_evidence_summary)
        cumulative_evidence_summary = self._compact_verifier_evidence_summary(raw_cumulative_evidence_summary)
        verifier_execution_result = self._workspace_artifact_execution_result_for_verifier(normalized_execution_result)
        candidate_final_result = self._candidate_final_result_from_execution_result(normalized_execution_result)
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "plan": self._compact_verifier_prompt_value(plan, max_chars=_VERIFIER_PROMPT_ITEM_CHARS),
                "candidate_final_result": self._compact_verifier_prompt_value(
                    candidate_final_result,
                    max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
                ),
                "execution_result": self._compact_verifier_prompt_value(
                    verifier_execution_result,
                    max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
                ),
                "execution_meta": self._verification_execution_meta_summary(execution_meta, evidence_summary),
                "execution_evidence_summary": evidence_summary,
                "cumulative_execution_evidence_summary": cumulative_evidence_summary,
                "current_evidence_ledger": current_evidence_ledger,
                "evidence_ledger": evidence_ledger,
                "acceptance_locator_view": acceptance_locator_view_from_ledger(evidence_ledger),
                "grounding_guard": grounding_guard,
                "trusted_workspace_artifacts": trusted_workspace_artifacts,
                "capability_evidence_requirements": self._capability_evidence_requirements(),
                "context_pack": self._compact_context_pack_for_verifier(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "previous_iterations": self._iteration_prompt_summaries(),
                "reflection_summaries": self._reflection_prompt_summaries(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Verify the task against every success criterion. "
            "Also consider caller-provided execution_prompt constraints when they are present. "
            "Treat numeric criteria such as 'at least N' as exact counting rules and fail verification when the "
            "evidence does not meet the count. "
            "Require source/evidence references when the criteria ask for evidence. "
            "Treat evidence_ledger as the authoritative grounding ledger and use its item ids when judging claims. "
            "Use acceptance_locator_view as a verifier readback index for Workspace artifacts: locator items show "
            "where to inspect an artifact, not whether the content is semantically correct. Prefer bounded readback "
            "items produced from those locators when checking long artifact sections. A locator with "
            "requirement_level='required' comes from an output contract or success criterion; status=empty on that "
            "locator is a strong structural gap. A locator with requirement_level='advisory' comes from a model-suggested "
            "acceptance point; status=empty means that proposed anchor was not found, but it is not by itself proof that "
            "the deliverable lacks the required content when other required locators or targeted readbacks cover it. "
            "current_evidence_ledger is the current step view; evidence_ledger includes prior iteration evidence that "
            "is verifier-visible. Do not perform or assume extra readback outside this ledger. failed/empty ledger "
            "items are facts of unavailability only; ref_only items prove only a URL/path/ref was found; bounded or "
            "truncated content supports only the visible body. grounding_guard contains deterministic id/status/body "
            "state diagnostics that must block completion when blocking_count is non-zero. "
            "Use both execution_evidence_summary and cumulative_execution_evidence_summary; the final verification "
            "must account for evidence gathered in earlier iterations, not only the current write/finalize step. "
            "Use reflection_summaries as evaluator notes linked to evidence and verification; reflection records are not "
            "completion evidence by themselves. "
            "For source-grounded tasks, compare the candidate's factual claims, named sections, coverage mappings, "
            "quoted source titles, URLs, and artifact statements against verifier-visible evidence and bounded Action "
            "result previews. A citation, source URL, or file ref alone does not ground a mismatched claim; the claim "
            "must be supported by the referenced evidence content. source_refs with content_state='ref_only' prove "
            "only discovery/materialization and cannot support repository, document, or source-content claims until "
            "a bounded readback/content preview is verifier-visible. When multiple same-site official sources are "
            "available, prefer the most specific source that directly matches the task over broader announcement or "
            "summary pages. Reject candidates that ignore a more specific verifier-visible source and ground the "
            "deliverable only in a weaker source. Reject candidates that introduce unsupported source facts, syllabus "
            "headings, repository details, dates, numbers, or report conclusions. "
            "If bounded previews are enough to contradict the candidate, set is_complete=false. If the previews are "
            "too truncated to verify a material claim, set is_complete=false and ask for scoped evidence readback. "
            "If execution metadata, action records, diagnostics, command output, or verifier-visible evidence shows "
            "a failed required action or failed validation command, do not mark complete. "
            "If a criterion requires a script, command, test, or external validation to pass, require explicit "
            "successful evidence for that validation before completion. "
            "Decide final_result_required from the goal and success criteria: set it true when the task demands a "
            "concrete final deliverable (answer, file, report, artifact, or similar) and false when the work is "
            "purely an action or side effect with no expected returned deliverable. "
            "For non-file deliverables, final_result should contain the returned answer body. For trusted Workspace "
            "artifact deliverables, the body remains in Workspace and trusted_workspace_artifacts plus file_refs/readback "
            "are the completion evidence; final_result may be a concise path/ref summary and must not copy the full "
            "artifact body only to satisfy a structured field. For source-grounded Workspace artifacts, verify the "
            "artifact body in trusted_workspace_artifacts.readback.content and targeted readback evidence ledger items "
            "against visible source_refs, Action evidence, "
            "URLs, paths, and refs; a final_result path pointer alone is not enough to satisfy citation or provenance "
            "requirements. For long artifacts, also inspect workspace_artifact.targeted_readback ledger items for bounded "
            "section, tail, source-list, risk, reference, or coverage snippets before concluding a required section is "
            "missing. Do not ask a later step to read a Workspace artifact solely to paste its full content into "
            "final_result. If trusted artifact refs or readback are missing or too scoped to verify a material claim, "
            "keep is_complete=false and ask for scoped artifact readback or repair. "
            "When candidate_final_result contains a complete answer/report/artifact body that satisfies the criteria, "
            "use it as final_result. When the plan or success criteria require a Workspace artifact, accept only "
            "trusted Workspace write/readback refs from execution evidence; model-declared file_refs are diagnostics. "
            "If evidence is incomplete, set is_complete=false and explain failure_analysis and acceptance_delta: "
            "why the task is not accepted, which acceptance facts are missing or weak, and what evidence boundary "
            "blocked verification. The verifier does not choose tools, routes, execution shapes, or exact methods. "
            "repair_constraints and next_step_requirements are advisory compatibility fields only; keep them factual "
            "and do not turn them into a narrow tool script. Also include a short human-readable replan_instruction. "
            "After the judgment fields, include compact criterion_checks, verification_summary, and progress_message "
            "for downstream repair context and human progress. These fields are process summaries only; they are not "
            "completion evidence and must not contain raw chain-of-thought or long evidence bodies. "
            "Set requires_block=true only when the task cannot continue."
        )
        request.output(
            {
                "is_complete": (bool, "True only when all success criteria are satisfied", True),
                "requires_block": (bool, "True only when the task cannot continue", True),
                "reason": (str, "Concise verification reason", True),
                "failure_analysis": (
                    str,
                    "Why the current result is incomplete or unacceptable; empty when complete",
                    False,
                ),
                "acceptance_delta": (
                    [str],
                    "Specific acceptance facts or evidence gaps that remain unsatisfied",
                    False,
                ),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when none"),
                "replan_instruction": (str, "Instruction for the next planning round when incomplete"),
                "repair_constraints": (
                    [str],
                    "Advisory factual constraints; not a tool, route, or execution-shape plan",
                ),
                "next_step_requirements": (
                    [str],
                    "Advisory observable requirements for future evidence; not a hard method script",
                ),
                "final_result_required": (bool, "True when the goal expects a concrete returned final deliverable"),
                "final_result": (str, "Final business result when complete"),
                "criterion_checks": (
                    [dict],
                    "Compact per-criterion checks: [{criterion, status, summary, evidence_ids?, gaps?}].",
                    False,
                ),
                "verification_summary": (
                    str,
                    "Short verifier summary for repair context; no raw chain-of-thought.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable verification progress sentence.",
                    False,
                ),
            },
            format="json",
        )
        verification = await self._await_task_request(request.async_get_data(), stage="verify")
        if not isinstance(verification, dict):
            return {
                "is_complete": False,
                "requires_block": False,
                "reason": str(verification),
                "failure_analysis": str(verification),
                "acceptance_delta": self.success_criteria,
                "missing_criteria": self.success_criteria,
                "replan_instruction": "Run another bounded step with stronger evidence.",
                "repair_constraints": self.success_criteria,
                "next_step_requirements": ["Run another bounded step with stronger evidence."],
                "final_result_required": False,
                "final_result": "",
            }
        normalized = self._normalize_verification(
            verification,
            execution_evidence_summary=raw_cumulative_evidence_summary,
            candidate_final_result=candidate_final_result,
            grounding_guard=grounding_guard,
        )
        await self._emit_process_progress_from_output(
            normalized,
            stage="verification",
            iteration=iteration_index,
        )
        return normalized

    async def _ensure_workspace_artifact_targeted_readback_evidence(
        self,
        execution_meta: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any],
        *,
        evidence_use: Any = None,
    ) -> None:
        if not isinstance(execution_meta, dict):
            return
        existing_generic_paths: set[str] = set()
        existing_locator_readbacks: set[str] = set()
        for item in evidence_ledger.get("items", []) if isinstance(evidence_ledger, Mapping) else []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "") != "workspace_artifact.targeted_readback":
                continue
            path = str(item.get("path") or "").strip()
            provenance = item.get("provenance")
            source_evidence_id = (
                str(provenance.get("source_evidence_id") or "").strip() if isinstance(provenance, Mapping) else ""
            )
            if source_evidence_id:
                existing_locator_readbacks.add(source_evidence_id)
            elif path:
                existing_generic_paths.add(path)

        evidence_items: list[dict[str, Any]] = []
        for locator in acceptance_locator_view_from_ledger(evidence_ledger).get("items", []):
            if not isinstance(locator, Mapping):
                continue
            locator_id = str(locator.get("id") or "").strip()
            if not locator_id or locator_id in existing_locator_readbacks:
                continue
            if str(locator.get("status") or "") != "ok":
                continue
            readback = await self._workspace_artifact_acceptance_locator_readback(locator)
            if readback is not None:
                evidence_items.append(self._workspace_artifact_targeted_readback_evidence_item(locator, readback))
                existing_locator_readbacks.add(locator_id)

        claim_queries = self._evidence_use_verifier_target_queries(evidence_use)
        for artifact in workspace_artifacts_from_ledger(evidence_ledger):
            path = str(artifact.get("path") or "").strip()
            if not path or path in existing_generic_paths:
                continue
            if str(artifact.get("status") or "") != "ok":
                continue
            if str(artifact.get("body_state") or "") != "truncated":
                continue
            try:
                read_result = await self.workspace.read_file(
                    path,
                    max_bytes=self._workspace_artifact_verifier_readback_bytes(artifact),
                )
            except Exception as error:
                evidence_items.append(
                    self._workspace_artifact_targeted_readback_evidence_item(
                        artifact,
                        {
                            "kind": "verifier_readback",
                            "path": path,
                            "status": "failed",
                            "error": {
                                "type": error.__class__.__name__,
                                "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                            },
                        },
                    )
                )
                continue
            for readback in await self._trusted_workspace_artifact_targeted_readbacks(
                artifact,
                read_result,
                queries=claim_queries,
            ):
                evidence_items.append(self._workspace_artifact_targeted_readback_evidence_item(artifact, readback))
        self._append_execution_meta_evidence_items(execution_meta, evidence_items)

    @classmethod
    def _workspace_artifact_targeted_readback_evidence_item(
        cls,
        artifact: Mapping[str, Any],
        readback: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = str(readback.get("path") or artifact.get("path") or "").strip()
        raw_status = str(readback.get("status") or "read").strip()
        status = "failed" if raw_status == "failed" else "ok"
        kind = str(readback.get("kind") or "targeted_readback").strip()
        content = str(readback.get("content") or "")
        query = str(readback.get("query") or readback.get("matched_query") or "")
        locator = query or str(readback.get("offset") or readback.get("line_start") or kind)
        evidence_id = cls._workspace_artifact_evidence_id(
            "workspace_artifact_targeted_readback",
            path,
            f"{kind}:{locator}",
        )
        item: dict[str, Any] = {
            "id": evidence_id,
            "kind": "workspace_artifact.targeted_readback",
            "status": status,
            "raw_status": raw_status,
            "body_state": "ref_only" if status == "failed" else ("truncated" if readback.get("truncated") else "bounded"),
            "path": path,
            "aliases": cls._workspace_artifact_targeted_readback_aliases(
                path=path,
                query=query,
                readback=readback,
                artifact=artifact,
            ),
            "source": "agent_task.workspace_artifact.targeted_readback",
            "provenance": {
                "source": "agent_task.workspace_artifact.targeted_readback",
                "source_evidence_id": readback.get("source_evidence_id") or artifact.get("id"),
                "path": path,
                "kind": kind,
                "query": query,
                "matched_query": readback.get("matched_query"),
                "offset": readback.get("offset"),
                "line_start": readback.get("line_start"),
                "line_end": readback.get("line_end"),
            },
            "supports": {
                "content": status == "ok",
                "unavailability": status == "failed",
                "ref_pointer": False,
            },
        }
        if content:
            item["body"] = content
        if status == "failed":
            item["diagnostics"] = [
                {
                    "code": "agent_task.workspace_artifact.targeted_readback_failed",
                    "message": "Workspace artifact targeted readback failed before verifier request.",
                    "error": DataFormatter.sanitize(readback.get("error") or {}),
                }
            ]
        return DataFormatter.sanitize(item)

    @classmethod
    def _workspace_artifact_targeted_readback_aliases(
        cls,
        *,
        path: str,
        query: str,
        readback: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)
            slug = cls._workspace_artifact_readback_alias_slug(text)
            if slug and slug not in aliases:
                aliases.append(slug)

        add(path)
        add(PurePosixPath(path.replace("\\", "/")).name if path else "")
        add(query)
        add(readback.get("matched_query"))
        add(readback.get("source_evidence_id"))
        add(artifact.get("id"))
        return aliases[:24]

    @staticmethod
    def _workspace_artifact_readback_alias_slug(value: str) -> str:
        text = str(value or "").strip().lower().replace("_", " ")
        if not text:
            return ""
        slug = "-".join(re.findall(r"[a-z0-9]+", text))
        return slug[:160]

    async def _workspace_artifact_acceptance_locator_readback(
        self,
        locator: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        path = str(locator.get("path") or locator.get("artifact_path") or "").strip()
        locator_id = str(locator.get("id") or "").strip()
        if not path or not locator_id:
            return None
        offset = self._coerce_non_negative_int(locator.get("byte_offset"))
        byte_end = self._coerce_non_negative_int(locator.get("byte_end"))
        max_bytes = min(_VERIFIER_PROMPT_ITEM_CHARS, 2400)
        if byte_end > offset:
            max_bytes = min(max(byte_end - offset, 800), max_bytes)
        if offset > 0 or byte_end > 0:
            try:
                read_result = await self.workspace.read_file(path, max_bytes=max_bytes, offset=offset)
            except Exception as error:
                return {
                    "kind": "acceptance_locator_readback",
                    "path": path,
                    "status": "failed",
                    "source_evidence_id": locator_id,
                    "offset": offset,
                    "line_start": locator.get("line_start"),
                    "line_end": locator.get("line_end"),
                    "error": {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    },
                }
            return {
                "kind": "acceptance_locator_readback",
                "path": str(read_result.get("path") or path),
                "status": "read",
                "source_evidence_id": locator_id,
                "offset": int(read_result.get("offset") or offset),
                "line_start": locator.get("line_start"),
                "line_end": locator.get("line_end"),
                "truncated": bool(read_result.get("truncated")),
                "query": locator.get("heading") or locator.get("anchor_text") or locator.get("claim"),
                "content": self._truncate_prompt_text(str(read_result.get("content") or ""), max_bytes),
            }
        for query in self._acceptance_locator_search_queries(locator):
            match = await self._workspace_artifact_search_readback(
                path,
                query,
                max_file_bytes=5_000_000,
            )
            if match is not None:
                match["kind"] = "acceptance_locator_search"
                match["source_evidence_id"] = locator_id
                return match
        return {
            "kind": "acceptance_locator_readback",
            "path": path,
            "status": "failed",
            "source_evidence_id": locator_id,
            "error": {
                "type": "AcceptanceLocatorUnavailable",
                "message": "Acceptance locator did not include a byte offset and no anchor search matched.",
            },
        }

    @classmethod
    def _acceptance_locator_search_queries(cls, locator: Mapping[str, Any]) -> list[str]:
        queries: list[str] = []
        for key in ("heading", "anchor_text", "claim", "topic", "criterion_id"):
            text = str(locator.get(key) or "").strip()
            if text and len(text) <= 160 and text not in queries:
                queries.append(text)
        return queries[:4]

    def _should_attempt_evidence_binding_repair(self, grounding_guard: Mapping[str, Any]) -> bool:
        if not isinstance(grounding_guard, Mapping):
            return False
        if not grounding_guard.get("blocking_count"):
            return False
        blocking_codes = {
            str(item.get("code") or "")
            for item in grounding_guard.get("diagnostics", [])
            if isinstance(item, Mapping) and item.get("blocking") is True
        }
        if not blocking_codes:
            return False
        binding_codes = {
            "evidence_ledger.invalid_evidence_id",
            "evidence_ledger.ambiguous_evidence_alias",
            "evidence_ledger.missing_evidence_id",
            "evidence_ledger.ref_only_item_used_as_content_support",
        }
        if not blocking_codes.issubset(binding_codes):
            return False
        return True

    def _can_attempt_model_evidence_binding_repair(self) -> bool:
        try:
            count = int(self.diagnostics.get("evidence_binding_repair_attempt_count") or 0)
        except (TypeError, ValueError):
            count = 0
        return count < 2

    async def _request_evidence_binding_repair(
        self,
        grounding_guard: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any],
        *,
        language_policy: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            count = int(self.diagnostics.get("evidence_binding_repair_attempt_count") or 0)
        except (TypeError, ValueError):
            count = 0
        self.diagnostics["evidence_binding_repair_attempt_count"] = count + 1
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "blocking_evidence_use_diagnostics": self._evidence_binding_repair_diagnostics(grounding_guard),
                "current_evidence_use": grounding_guard.get("normalized_evidence_use", []),
                "available_evidence_refs": grounding_guard.get("available_evidence_refs", []),
                "grounding_rules": evidence_ledger.get("grounding_rules", {}) if isinstance(evidence_ledger, Mapping) else {},
            }
        )
        request.instruct(
            "Repair only the structured evidence_use bindings that failed deterministic id binding. "
            "Do not rewrite, summarize, or regenerate the candidate final result. "
            "Choose evidence_ids only from available_evidence_refs.id or available_evidence_refs.cite_as. "
            "If no listed evidence item can support a claim under the rules, return that claim with an empty evidence_ids "
            "list so the host can block precisely. Preserve each claim text and support_type."
        )
        request.output(
            {
                "evidence_use": (
                    [
                        {
                            "claim_index": (int, "Index of the claim in current_evidence_use when available", False),
                            "claim": (str, "Original claim text", True),
                            "evidence_ids": ([str], "Corrected evidence ids or cite_as handles from available_evidence_refs", True),
                            "support_type": (
                                str,
                                "content, unavailability, or ref_pointer; keep the original type unless it was structurally wrong",
                                True,
                            ),
                        }
                    ],
                    "Only corrected evidence_use entries for claims with binding diagnostics",
                    True,
                ),
                "repair_summary": (
                    str,
                    "Short summary of the evidence binding repair result; no candidate rewrite.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable repair progress sentence.",
                    False,
                ),
            },
            format="json",
        )
        repaired = await self._await_task_request(request.async_get_data(), stage="evidence_binding_repair")
        if not isinstance(repaired, Mapping):
            return []
        await self._emit_process_progress_from_output(
            repaired,
            stage="evidence_binding_repair",
        )
        evidence_use = repaired.get("evidence_use")
        if not isinstance(evidence_use, Sequence) or isinstance(evidence_use, str | bytes | bytearray):
            return []
        return [dict(DataFormatter.sanitize(item)) for item in evidence_use if isinstance(item, Mapping)]

    @classmethod
    def _deterministic_evidence_binding_repair(
        cls,
        grounding_guard: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        current = grounding_guard.get("normalized_evidence_use", [])
        if not isinstance(current, Sequence) or isinstance(current, str | bytes | bytearray):
            return []
        current_items = [dict(item) for item in current if isinstance(item, Mapping)]
        if not current_items:
            return []
        available_ref_records = cls._evidence_binding_available_ref_records(
            grounding_guard.get("available_evidence_refs", [])
        )
        if evidence_ledger is not None:
            available_ref_records = cls._merge_evidence_binding_ref_records(
                available_ref_records,
                cls._evidence_binding_available_ref_records_from_ledger(evidence_ledger),
            )
        available_refs = cls._evidence_binding_available_ref_index(available_ref_records)
        diagnostics = cls._evidence_binding_repair_diagnostics(grounding_guard)
        repaired: list[dict[str, Any]] = []
        seen_indexes: set[int] = set()
        for diagnostic in diagnostics:
            raw_index = diagnostic.get("claim_index")
            try:
                claim_index = int(raw_index) if raw_index is not None else -1
            except (TypeError, ValueError):
                claim_index = -1
            if claim_index < 0 or claim_index >= len(current_items) or claim_index in seen_indexes:
                continue
            item = current_items[claim_index]
            candidate_ids = cls._deterministic_evidence_id_candidates(
                diagnostic,
                available_refs,
                available_ref_records,
            )
            if len(candidate_ids) != 1:
                continue
            support_type = cls._deterministic_repaired_support_type(
                item,
                diagnostic,
                candidate_ids,
                available_ref_records,
            )
            repaired.append(
                DataFormatter.sanitize(
                    {
                        "claim_index": claim_index,
                        "claim": item.get("claim", diagnostic.get("claim", "")),
                        "evidence_ids": candidate_ids,
                        "support_type": support_type,
                    }
                )
            )
            seen_indexes.add(claim_index)
        return repaired

    @staticmethod
    def _evidence_binding_available_ref_records(value: Any) -> list[dict[str, Any]]:
        refs = value if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray) else []
        return [dict(DataFormatter.sanitize(ref)) for ref in refs if isinstance(ref, Mapping)]

    @staticmethod
    def _evidence_binding_available_ref_records_from_ledger(value: Any) -> list[dict[str, Any]]:
        ledger = value if isinstance(value, Mapping) and isinstance(value.get("items"), Sequence) else {}
        if not ledger and value not in (None, "", [], {}):
            ledger = evidence_ledger_view(value)
        raw_items = ledger.get("items") if isinstance(ledger, Mapping) else ()
        items = raw_items if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes | bytearray) else ()
        return [dict(DataFormatter.sanitize(item)) for item in items if isinstance(item, Mapping)]

    @staticmethod
    def _merge_evidence_binding_ref_records(
        refs: Sequence[Mapping[str, Any]],
        ledger_refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        merged_by_id: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        def merge(ref: Mapping[str, Any]) -> None:
            evidence_id = str(ref.get("id") or "").strip()
            if not evidence_id:
                return
            if evidence_id not in merged_by_id:
                merged_by_id[evidence_id] = {}
                order.append(evidence_id)
            existing = merged_by_id[evidence_id]
            aliases: list[str] = []
            for source in (existing.get("aliases"), ref.get("aliases")):
                if isinstance(source, Sequence) and not isinstance(source, str | bytes | bytearray):
                    for alias in source:
                        text = str(alias or "").strip()
                        if text and text not in aliases:
                            aliases.append(text)
            existing.update(dict(ref))
            if aliases:
                existing["aliases"] = aliases[:24]

        for ref in ledger_refs:
            merge(ref)
        for ref in refs:
            merge(ref)
        return [merged_by_id[evidence_id] for evidence_id in order]

    @staticmethod
    def _evidence_binding_available_ref_index(value: Any) -> dict[str, list[str]]:
        refs = value if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray) else []
        index: dict[str, list[str]] = {}
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("id") or "").strip()
            if not evidence_id:
                continue
            aliases: set[str] = {evidence_id}
            for field in (
                "cite_as",
                "path",
                "record_id",
                "source_url",
                "selected_url",
                "requested_url",
                "canonical_url",
                "url",
                "href",
                "artifact_id",
                "action_call_id",
            ):
                raw = ref.get(field)
                if raw not in (None, "", [], {}):
                    aliases.add(str(raw).strip())
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias).strip() for alias in raw_aliases if str(alias or "").strip())
            for alias in aliases:
                if alias:
                    index.setdefault(alias, []).append(evidence_id)
        return index

    @staticmethod
    def _deterministic_repaired_support_type(
        item: Mapping[str, Any],
        diagnostic: Mapping[str, Any],
        candidate_ids: Sequence[str],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> str:
        support_type = str(item.get("support_type", diagnostic.get("support_type", "")) or "").strip()
        if support_type != "unavailability":
            return support_type
        refs_by_id = {
            str(ref.get("id") or "").strip(): ref
            for ref in available_refs
            if isinstance(ref, Mapping) and str(ref.get("id") or "").strip()
        }
        candidate_refs = [refs_by_id.get(str(candidate_id or "").strip()) for candidate_id in candidate_ids]
        if not candidate_refs or any(ref is None for ref in candidate_refs):
            return support_type
        content_backed = all(
            str(ref.get("status") or "").strip().lower() == "ok"
            and str(ref.get("body_state") or "").strip().lower() in {"full", "bounded", "truncated"}
            for ref in candidate_refs
            if isinstance(ref, Mapping)
        )
        return "content" if content_backed else support_type

    @classmethod
    def _deterministic_evidence_id_candidates(
        cls,
        diagnostic: Mapping[str, Any],
        available_ref_index: Mapping[str, list[str]],
        available_refs: Sequence[Mapping[str, Any]] = (),
    ) -> list[str]:
        raw_candidates = diagnostic.get("candidates")
        if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, str | bytes | bytearray):
            candidates = [str(candidate).strip() for candidate in raw_candidates if str(candidate or "").strip()]
            unique = sorted(set(candidates))
            if len(unique) == 1:
                return unique
        evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        unique_matches: list[str] = []
        requires_content_replacement = False
        if evidence_id:
            matches = [
                str(item).strip()
                for item in available_ref_index.get(evidence_id, [])
                if str(item or "").strip()
            ]
            unique_matches = sorted(set(matches))
            requires_content_replacement = (
                str(diagnostic.get("code") or "") == "evidence_ledger.ref_only_item_used_as_content_support"
                and str(diagnostic.get("support_type") or "").strip().lower() == "content"
            )
            if len(unique_matches) == 1 and not requires_content_replacement:
                return unique_matches
            artifact_ref_matches = cls._deterministic_artifact_ref_candidates(diagnostic, available_refs)
            if len(artifact_ref_matches) == 1:
                return artifact_ref_matches
            action_result_matches = cls._deterministic_action_result_candidates(diagnostic, available_refs)
            if len(action_result_matches) == 1:
                return action_result_matches
        body_text_matches = cls._deterministic_body_text_candidates(diagnostic, available_refs)
        if len(body_text_matches) == 1:
            return body_text_matches
        if evidence_id:
            readback_matches = cls._deterministic_content_readback_candidates(diagnostic, available_refs)
            if len(readback_matches) == 1:
                return readback_matches
        if len(unique_matches) == 1:
            ref_by_id = {
                str(ref.get("id") or "").strip(): ref
                for ref in available_refs
                if isinstance(ref, Mapping) and str(ref.get("id") or "").strip()
            }
            match = ref_by_id.get(unique_matches[0], {})
            if not requires_content_replacement or str(match.get("body_state") or "").strip().lower() != "ref_only":
                return unique_matches
        return []

    @staticmethod
    def _deterministic_artifact_ref_candidates(
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        if not evidence_id:
            return []
        raw_candidates = diagnostic.get("candidates")
        candidates = {
            str(candidate).strip()
            for candidate in raw_candidates
            if str(candidate or "").strip()
        } if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, str | bytes | bytearray) else set()
        matches: list[str] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            ref_id = str(ref.get("id") or "").strip()
            if not ref_id or candidates and ref_id not in candidates:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            kind = str(ref.get("kind") or "").strip().lower()
            artifact_id = str(ref.get("artifact_id") or "").strip()
            if status != "ok" or body_state not in {"full", "bounded", "truncated"}:
                continue
            if kind == "artifact_ref" and (artifact_id == evidence_id or evidence_id in ref_id):
                matches.append(ref_id)
        return sorted(set(matches))

    @staticmethod
    def _deterministic_action_result_candidates(
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        support_type = str(diagnostic.get("support_type") or "").strip().lower()
        if support_type not in {"content", "unavailability"}:
            return []
        text = " ".join(str(diagnostic.get(key) or "") for key in ("claim", "evidence_id")).lower()
        action_refs: list[Mapping[str, Any]] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("id") or "").strip()
            if not evidence_id:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            kind = str(ref.get("kind") or "").strip().lower()
            if kind != "agent_task.action.result" or status != "ok":
                continue
            if body_state not in {"full", "bounded", "truncated"}:
                continue
            action_refs.append(ref)
        if not action_refs:
            return []
        diagnostic_evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        alias_matches: list[str] = []
        action_matches: list[str] = []
        for ref in action_refs:
            evidence_id = str(ref.get("id") or "").strip()
            aliases = {
                evidence_id,
                str(ref.get("cite_as") or "").strip(),
                str(ref.get("action_id") or "").strip(),
                str(ref.get("action_call_id") or "").strip(),
            }
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias or "").strip() for alias in raw_aliases)
            aliases.discard("")
            if diagnostic_evidence_id and diagnostic_evidence_id in aliases:
                alias_matches.append(evidence_id)
            action_id = str(ref.get("action_id") or "").strip().lower()
            action_tokens = {action_id, action_id.replace("_", " ")} if action_id else set()
            if action_tokens and any(token and token in text for token in action_tokens):
                action_matches.append(evidence_id)
        unique_alias_matches = sorted(set(alias_matches))
        if len(unique_alias_matches) == 1:
            return unique_alias_matches
        unique_action_matches = sorted(set(action_matches))
        if len(unique_action_matches) == 1:
            return unique_action_matches
        unique_action_refs = sorted({str(ref.get("id") or "").strip() for ref in action_refs if ref.get("id")})
        if len(unique_action_refs) != 1:
            return []
        evidence_id = str(diagnostic.get("evidence_id") or "").strip().lower()
        if evidence_id.startswith(("action result", "action_result", "action-result")):
            return unique_action_refs
        if support_type == "unavailability":
            return unique_action_refs
        return []

    @classmethod
    def _deterministic_body_text_candidates(
        cls,
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        support_type = str(diagnostic.get("support_type") or "").strip().lower()
        if support_type not in {"content", "unavailability"}:
            return []
        evidence_id_text = str(diagnostic.get("evidence_id") or "").strip()
        query_specs = [(query, False) for query in cls._evidence_binding_body_match_queries(evidence_id_text)]
        if not evidence_id_text or cls._evidence_binding_id_looks_like_workspace_locator(evidence_id_text):
            allow_partial_claim_match = not evidence_id_text
            query_specs.extend(
                (query, allow_partial_claim_match)
                for query in cls._evidence_binding_body_match_queries(str(diagnostic.get("claim") or ""))
            )
        query_specs = cls._dedupe_evidence_binding_query_specs(query_specs)
        if not query_specs:
            return []
        matches: list[Mapping[str, Any]] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            ref_id = str(ref.get("id") or "").strip()
            if not ref_id:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            status_supported = status == "ok" or (support_type == "unavailability" and status in {"failed", "empty"})
            if not status_supported or body_state not in {"full", "bounded", "truncated"}:
                continue
            body = cls._evidence_binding_ref_body(ref)
            if not body:
                continue
            if any(
                cls._evidence_binding_body_query_matches(query, body, allow_partial=allow_partial)
                for query, allow_partial in query_specs
            ):
                matches.append(ref)
        evidence_id_text = str(diagnostic.get("evidence_id") or "").strip()
        if cls._evidence_binding_id_looks_like_file_locator(evidence_id_text):
            # A file/path/locator reference may bind only to a ref whose own
            # path/anchor agrees. A body-text coincidence in another file must never
            # bind -- not even when it is the single body match. Anchor-gate every
            # file-locator case so cross-file binding is impossible; if no ref agrees,
            # return nothing and let the path-aware readback tier (or repair) decide.
            preferred_file_match = cls._preferred_file_locator_body_text_candidate(diagnostic, matches)
            return [preferred_file_match] if preferred_file_match else []
        unique_matches = cls._unique_evidence_binding_ref_ids(matches)
        if len(unique_matches) <= 1:
            return unique_matches
        coalesced_workspace_match = cls._coalesced_workspace_body_text_candidate(matches)
        if coalesced_workspace_match:
            return [coalesced_workspace_match]
        return unique_matches

    @staticmethod
    def _evidence_binding_ref_body(ref: Mapping[str, Any]) -> str:
        for key in ("body", "content", "text", "snippet", "preview", "result", "output", "value"):
            value = ref.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, str):
                return value
            try:
                return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                return str(value)
        return ""

    @classmethod
    def _evidence_binding_body_match_queries(cls, value: str) -> list[str]:
        raw = value.strip()
        if not raw:
            return []
        queries = [raw]
        lowered = raw.lower()
        for prefix in ("search result:", "search:", "result:", "source:", "evidence:"):
            if lowered.startswith(prefix):
                stripped = raw[len(prefix) :].strip()
                if stripped and stripped not in queries:
                    queries.append(stripped)
        for separator in (":", "/", "#"):
            if separator in raw:
                tail = raw.rsplit(separator, 1)[-1].strip()
                if tail and tail not in queries:
                    queries.append(tail)
        return [query for query in queries if cls._evidence_binding_body_query_is_informative(query)]

    @classmethod
    def _evidence_binding_body_query_matches(cls, query: str, body: str, *, allow_partial: bool = False) -> bool:
        normalized_query = cls._normalize_evidence_binding_text(query)
        if len(normalized_query) < 12:
            return False
        normalized_body = cls._normalize_evidence_binding_text(body)
        if normalized_query in normalized_body:
            return True
        query_tokens = cls._evidence_binding_body_match_tokens(query)
        if len(query_tokens) < 4:
            return False
        body_tokens = set(cls._evidence_binding_body_match_tokens(body))
        if not body_tokens:
            return False
        if all(token in body_tokens for token in query_tokens):
            return True
        if not allow_partial:
            return False
        overlap_count = sum(1 for token in query_tokens if token in body_tokens)
        required_overlap = min(len(query_tokens), max(3, (len(query_tokens) * 3 + 4) // 5))
        return overlap_count >= required_overlap

    @classmethod
    def _evidence_binding_body_query_is_informative(cls, query: str) -> bool:
        return len(cls._evidence_binding_body_match_tokens(query)) >= 3

    @staticmethod
    def _dedupe_evidence_binding_queries(queries: Sequence[str]) -> list[str]:
        deduped: list[str] = []
        for query in queries:
            text = str(query or "").strip()
            if text and text not in deduped:
                deduped.append(text)
        return deduped

    @staticmethod
    def _dedupe_evidence_binding_query_specs(queries: Sequence[tuple[str, bool]]) -> list[tuple[str, bool]]:
        deduped: list[tuple[str, bool]] = []
        seen: set[tuple[str, bool]] = set()
        for query, allow_partial in queries:
            text = str(query or "").strip()
            key = (text, bool(allow_partial))
            if text and key not in seen:
                seen.add(key)
                deduped.append(key)
        return deduped

    @classmethod
    def _evidence_binding_id_looks_like_workspace_locator(cls, evidence_id: str) -> bool:
        text = str(evidence_id or "").strip().lower()
        return (
            not text
            or "workspace_artifact" in text
            or "artifact_locator" in text
            or "acceptance_locator" in text
            or "readback" in text
            or cls._evidence_binding_id_looks_like_file_locator(text)
        )

    @staticmethod
    def _evidence_binding_id_looks_like_file_locator(evidence_id: str) -> bool:
        text = str(evidence_id or "").strip().lower().replace("\\", "/")
        if not text:
            return False
        if re.search(r"(?:^|[/\s'\"`])[\w.-]+\.[a-z0-9]{1,12}(?:\b|[:#/'\"`])", text):
            return True
        locator_terms = (" line ", " lines ", " row ", " rows ", " table ", " section ")
        padded = f" {text} "
        return any(term in padded for term in locator_terms)

    @classmethod
    def _normalize_evidence_binding_text(cls, value: str) -> str:
        return " ".join(cls._evidence_binding_body_match_tokens(value))

    @staticmethod
    def _evidence_binding_body_match_tokens(value: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", value.lower().replace("_", " "))
        ignored = {
            "a",
            "an",
            "and",
            "artifact",
            "claim",
            "content",
            "evidence",
            "final",
            "id",
            "locator",
            "md",
            "read",
            "readback",
            "section",
            "source",
            "targeted",
            "the",
            "workspace",
        }
        seen: set[str] = set()
        result: list[str] = []
        for token in tokens:
            if token in ignored:
                continue
            if not token.isdigit() and len(token) < 2:
                continue
            if token not in seen:
                seen.add(token)
                result.append(token)
        return result

    @staticmethod
    def _unique_evidence_binding_ref_ids(refs: Sequence[Mapping[str, Any]]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for ref in refs:
            evidence_id = str(ref.get("id") or "").strip()
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                ordered.append(evidence_id)
        return ordered

    @classmethod
    def _preferred_file_locator_body_text_candidate(
        cls,
        diagnostic: Mapping[str, Any],
        refs: Sequence[Mapping[str, Any]],
    ) -> str:
        evidence_id_text = str(diagnostic.get("evidence_id") or "").strip()
        if not cls._evidence_binding_id_looks_like_file_locator(evidence_id_text):
            return ""
        locator_text = evidence_id_text.lower().replace("\\", "/")
        locator_matches = [
            ref
            for ref in refs
            if cls._evidence_binding_ref_matches_locator_text(ref, locator_text)
        ]
        unique_locator_matches = cls._unique_evidence_binding_ref_ids(locator_matches)
        if len(unique_locator_matches) == 1:
            return unique_locator_matches[0]
        readable_matches = [
            ref
            for ref in locator_matches
            if cls._evidence_binding_ref_is_readback_like(ref)
        ]
        unique_readable_matches = cls._unique_evidence_binding_ref_ids(readable_matches)
        if len(unique_readable_matches) == 1:
            return unique_readable_matches[0]
        coalesced_workspace_match = cls._coalesced_workspace_body_text_candidate(readable_matches)
        if coalesced_workspace_match:
            return coalesced_workspace_match
        return ""

    @classmethod
    def _evidence_binding_ref_matches_locator_text(cls, ref: Mapping[str, Any], locator_text: str) -> bool:
        for alias in cls._evidence_binding_ref_locator_aliases(ref):
            normalized_alias = alias.lower().replace("\\", "/")
            if len(normalized_alias) < 3:
                continue
            if normalized_alias in locator_text:
                return True
        return False

    @staticmethod
    def _evidence_binding_ref_locator_aliases(ref: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)
            if text:
                basename = PurePosixPath(text.replace("\\", "/")).name
                if basename and basename not in aliases:
                    aliases.append(basename)

        for key in ("id", "cite_as", "path", "artifact_id", "action_call_id"):
            add(ref.get(key))
        raw_aliases = ref.get("aliases")
        if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
            for alias in raw_aliases:
                add(alias)
        return aliases

    @staticmethod
    def _evidence_binding_ref_is_readback_like(ref: Mapping[str, Any]) -> bool:
        kind = str(ref.get("kind") or "").strip().lower()
        action_id = str(ref.get("action_id") or "").strip().lower()
        return (
            "readback" in kind
            or kind == "workspace_artifact.readback"
            or kind == "workspace_artifact.targeted_readback"
            or action_id in {"read_file", "grep_files", "search_files"}
        )

    @classmethod
    def _coalesced_workspace_body_text_candidate(cls, refs: Sequence[Mapping[str, Any]]) -> str:
        workspace_refs: list[Mapping[str, Any]] = []
        for ref in refs:
            kind = str(ref.get("kind") or "").strip().lower()
            if kind not in {"workspace_artifact.readback", "workspace_artifact.targeted_readback"}:
                continue
            path = str(ref.get("path") or "").strip()
            evidence_id = str(ref.get("id") or "").strip()
            if path and evidence_id:
                workspace_refs.append(ref)
        if not workspace_refs:
            return ""
        paths = {str(ref.get("path") or "").strip() for ref in workspace_refs}
        if len(paths) != 1:
            return ""
        targeted_refs = [
            ref for ref in workspace_refs if str(ref.get("kind") or "").strip().lower() == "workspace_artifact.targeted_readback"
        ]
        selected_pool = targeted_refs or workspace_refs
        return str(selected_pool[-1].get("id") or "").strip()

    @classmethod
    def _deterministic_content_readback_candidates(
        cls,
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        if str(diagnostic.get("support_type") or "").strip() != "content":
            return []
        text = " ".join(
            str(diagnostic.get(key) or "")
            for key in ("claim", "evidence_id")
        ).lower()
        content_refs: list[Mapping[str, Any]] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("id") or "").strip()
            if not evidence_id:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            kind = str(ref.get("kind") or "").strip().lower()
            if status != "ok" or body_state not in {"full", "bounded", "truncated"}:
                continue
            if (
                "readback" not in kind
                and "workspace_artifact" not in kind
                and kind != "agent_task.action.result"
            ):
                continue
            content_refs.append(ref)
        if not content_refs:
            return []

        diagnostic_evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        alias_matches: list[str] = []
        path_matches: list[str] = []
        for ref in content_refs:
            evidence_id = str(ref.get("id") or "").strip()
            path = str(ref.get("path") or "").strip()
            basename = PurePosixPath(path.replace("\\", "/")).name if path else ""
            aliases = {
                evidence_id,
                str(ref.get("cite_as") or "").strip(),
                str(ref.get("artifact_id") or "").strip(),
                str(ref.get("action_id") or "").strip(),
                str(ref.get("action_call_id") or "").strip(),
                path,
                basename,
            }
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias or "").strip() for alias in raw_aliases)
            aliases.discard("")
            if diagnostic_evidence_id and diagnostic_evidence_id in aliases:
                alias_matches.append(evidence_id)
            path_tokens = [token.lower() for token in (path, basename) if token]
            if path_tokens and any(token in text for token in path_tokens):
                path_matches.append(evidence_id)
        unique_alias_matches = sorted(set(alias_matches))
        if len(unique_alias_matches) == 1:
            return unique_alias_matches
        unique_path_matches = sorted(set(path_matches))
        if len(unique_path_matches) == 1:
            return unique_path_matches

        unique_content_refs = sorted({str(ref.get("id") or "").strip() for ref in content_refs if ref.get("id")})
        if len(unique_content_refs) == 1:
            # "Only one content-bearing ref" must not cross files: a file/path/locator
            # reference may take this shortcut only when that ref's own path/anchor
            # agrees. Otherwise leave it unbound rather than binding to the wrong file.
            if cls._evidence_binding_id_looks_like_file_locator(diagnostic_evidence_id):
                only_ref = next(
                    (ref for ref in content_refs if str(ref.get("id") or "").strip() == unique_content_refs[0]),
                    None,
                )
                if only_ref is None or not cls._evidence_binding_ref_matches_locator_text(
                    only_ref, diagnostic_evidence_id.lower().replace("\\", "/")
                ):
                    return []
            return unique_content_refs
        return []

    @classmethod
    def _evidence_binding_repair_diagnostics(cls, grounding_guard: Mapping[str, Any]) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for item in grounding_guard.get("diagnostics", []) if isinstance(grounding_guard, Mapping) else []:
            if not isinstance(item, Mapping) or item.get("blocking") is not True:
                continue
            if str(item.get("code") or "") not in {
                "evidence_ledger.invalid_evidence_id",
                "evidence_ledger.ambiguous_evidence_alias",
                "evidence_ledger.missing_evidence_id",
                "evidence_ledger.ref_only_item_used_as_content_support",
            }:
                continue
            diagnostics.append(
                {
                    "code": item.get("code"),
                    "claim_index": item.get("index"),
                    "claim": item.get("claim"),
                    "evidence_id": item.get("evidence_id"),
                    "candidates": item.get("candidates"),
                    "support_type": item.get("support_type"),
                }
            )
        return DataFormatter.sanitize(diagnostics)

    @classmethod
    def _merge_repaired_evidence_use(cls, current_evidence_use: Any, repaired_evidence_use: Any) -> list[dict[str, Any]]:
        current = [dict(item) for item in current_evidence_use if isinstance(item, Mapping)] if isinstance(current_evidence_use, Sequence) and not isinstance(current_evidence_use, str | bytes | bytearray) else []
        repaired = [dict(item) for item in repaired_evidence_use if isinstance(item, Mapping)] if isinstance(repaired_evidence_use, Sequence) and not isinstance(repaired_evidence_use, str | bytes | bytearray) else []
        by_index: dict[int, dict[str, Any]] = {}
        by_claim: dict[str, dict[str, Any]] = {}
        for item in repaired:
            raw_claim_index = item.get("claim_index")
            try:
                claim_index = int(raw_claim_index) if raw_claim_index is not None else -1
            except (TypeError, ValueError):
                claim_index = -1
            if claim_index >= 0:
                by_index[claim_index] = item
            claim = str(item.get("claim") or "").strip()
            if claim:
                by_claim[claim] = item
        merged: list[dict[str, Any]] = []
        for index, item in enumerate(current):
            replacement = by_index.get(index)
            if replacement is None:
                replacement = by_claim.get(str(item.get("claim") or "").strip())
            merged.append(DataFormatter.sanitize(replacement if replacement is not None else item))
        return merged

    async def _trusted_workspace_artifacts_for_verifier(
        self,
        evidence_summary: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for ref in self._trusted_workspace_artifact_refs_from_summary(evidence_summary):
            artifact = self._trusted_workspace_artifact_ref_summary(ref)
            path = str(ref.get("path") or "").strip()
            if path:
                try:
                    read_result = await self.workspace.read_file(
                        path,
                        max_bytes=self._workspace_artifact_verifier_readback_bytes(ref),
                    )
                except Exception as error:
                    artifact["readback"] = {
                        "status": "failed",
                        "error": {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        },
                    }
                else:
                    content = read_result.get("content")
                    artifact["readback"] = {
                        "status": "read",
                        "path": str(read_result.get("path") or path),
                        "truncated": bool(read_result.get("truncated")),
                        "content": (
                            self._truncate_prompt_text(content, _VERIFIER_PROMPT_VALUE_CHARS)
                            if isinstance(content, str)
                            else ""
                        ),
                    }
                    targeted_readbacks = await self._trusted_workspace_artifact_targeted_readbacks(ref, read_result)
                    if targeted_readbacks:
                        artifact["targeted_readbacks"] = targeted_readbacks
            artifacts.append(artifact)
            if len(artifacts) >= 4:
                break
        return DataFormatter.sanitize(artifacts)

    async def _trusted_workspace_artifact_targeted_readbacks(
        self,
        ref: Mapping[str, Any],
        read_result: Mapping[str, Any],
        *,
        queries: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        path = str(ref.get("path") or read_result.get("path") or "").strip()
        if not path:
            return []
        byte_count = self._coerce_non_negative_int(ref.get("bytes") or read_result.get("bytes"))
        read_bytes = self._coerce_non_negative_int(read_result.get("read_bytes"))
        is_scoped_readback = bool(read_result.get("truncated")) or byte_count > read_bytes > 0
        if not is_scoped_readback:
            return []
        max_snippet_bytes = min(_VERIFIER_PROMPT_ITEM_CHARS, 2400)
        readbacks: list[dict[str, Any]] = []

        if byte_count > read_bytes > 0:
            offset = max(0, byte_count - max_snippet_bytes)
            try:
                tail = await self.workspace.read_file(path, max_bytes=max_snippet_bytes, offset=offset)
            except Exception as error:
                readbacks.append(
                    {
                        "kind": "tail_window",
                        "path": path,
                        "status": "failed",
                        "offset": offset,
                        "error": {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        },
                    }
                )
            else:
                readbacks.append(
                    {
                        "kind": "tail_window",
                        "path": str(tail.get("path") or path),
                        "status": "read",
                        "offset": int(tail.get("offset") or offset),
                        "truncated": bool(tail.get("truncated")),
                        "content": self._truncate_prompt_text(str(tail.get("content") or ""), max_snippet_bytes),
                    }
                )

        max_file_bytes = max(byte_count, _VERIFIER_PROMPT_VALUE_CHARS)
        for query in [*list(queries or ()), *self._workspace_artifact_verifier_target_queries()]:
            if len(readbacks) >= 8:
                break
            match = await self._workspace_artifact_search_readback(path, query, max_file_bytes=max_file_bytes)
            if match is not None:
                readbacks.append(match)
        return DataFormatter.sanitize(readbacks)

    async def _workspace_artifact_search_readback(
        self,
        path: str,
        query: str,
        *,
        max_file_bytes: int,
    ) -> dict[str, Any] | None:
        for search_query in self._workspace_artifact_query_variants(query):
            try:
                matches = await self.workspace.search_files(
                    search_query,
                    path=path,
                    max_results=1,
                    context_lines=4,
                    max_snippet_bytes=min(_VERIFIER_PROMPT_ITEM_CHARS, 2400),
                    max_file_bytes=max_file_bytes,
                )
            except Exception:
                continue
            if not matches:
                continue
            match = matches[0]
            return {
                "kind": "section_search",
                "path": str(match.get("path") or path),
                "query": query,
                "matched_query": search_query,
                "line_start": match.get("line_start"),
                "line_end": match.get("line_end"),
                "truncated": bool(match.get("truncated")),
                "content": self._truncate_prompt_text(str(match.get("snippet") or ""), _VERIFIER_PROMPT_ITEM_CHARS),
            }
        return None

    def _workspace_artifact_verifier_target_queries(self) -> list[str]:
        queries: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            text = " ".join(text.split())
            if len(text) > 120:
                return
            key = text.casefold()
            if key not in {item.casefold() for item in queries}:
                queries.append(text)

        def collect_section(value: Any) -> None:
            if isinstance(value, str):
                add(value)
                return
            if isinstance(value, Mapping):
                for key in ("title", "name", "heading", "id"):
                    add(value.get(key))
                return

        def collect_contract(value: Any) -> None:
            if not isinstance(value, Mapping):
                return
            sections = value.get("sections")
            if isinstance(sections, Sequence) and not isinstance(sections, str | bytes | bytearray):
                for section in sections:
                    collect_section(section)

        collect_contract(self._agent_task_option("output_contract", None))
        execution_prompt = self._execution_prompt_context()
        collect_contract(execution_prompt.get("output_contract"))
        prompt_input = execution_prompt.get("input")
        if isinstance(prompt_input, Mapping):
            collect_contract(prompt_input.get("output_contract"))
            case = prompt_input.get("case")
            if isinstance(case, Mapping):
                collect_contract(case.get("output_contract"))

        for query in (
            "source",
            "sources",
            "source list",
            "references",
            "citations",
            "risk",
            "risks",
            "uncertainty",
            "coverage",
        ):
            add(query)
        return queries[:12]

    @classmethod
    def _evidence_use_verifier_target_queries(cls, evidence_use: Any) -> list[str]:
        queries: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            text = " ".join(text.split())
            if len(text) > 160:
                return
            key = text.casefold()
            if key not in {item.casefold() for item in queries}:
                queries.append(text)

        for use in collect_evidence_use({"evidence_use": evidence_use}):
            if not isinstance(use, Mapping):
                continue
            add(use.get("claim"))
            for evidence_id in cls._normalize_string_list(use.get("evidence_ids")):
                if "/" in evidence_id or "." in evidence_id:
                    add(PurePosixPath(evidence_id).name)
        return queries[:12]

    @staticmethod
    def _workspace_artifact_query_variants(query: str) -> list[str]:
        variants: list[str] = []
        for value in (query, query.title(), query.lower(), query.upper()):
            text = str(value or "").strip()
            if text and text not in variants:
                variants.append(text)
        return variants

    @classmethod
    def _workspace_artifact_verifier_readback_bytes(cls, ref: Mapping[str, Any]) -> int:
        declared_bytes = cls._coerce_non_negative_int(ref.get("bytes"))
        if declared_bytes > 0 and declared_bytes < _VERIFIER_PROMPT_VALUE_CHARS:
            return declared_bytes + 1
        return max(_WORKSPACE_ARTIFACT_PREVIEW_BYTES, _VERIFIER_PROMPT_VALUE_CHARS)

    @classmethod
    def _trusted_workspace_artifact_refs_from_summary(cls, evidence_summary: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if cls._is_trusted_workspace_artifact_ref(value) and str(value.get("path") or "").strip():
                    refs.append(dict(value))
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    collect(item)

        collect(evidence_summary.get("artifact_refs"))
        workspace_refs = evidence_summary.get("workspace_refs")
        if isinstance(workspace_refs, Mapping):
            collect(workspace_refs)

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (str(ref.get("path") or ""), str(ref.get("sha256") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)
        return deduped

    @classmethod
    def _trusted_workspace_artifact_refs_have_readback(cls, refs: Sequence[Mapping[str, Any]]) -> bool:
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            readback = ref.get("readback")
            if isinstance(readback, Mapping):
                content = str(readback.get("content") or readback.get("preview") or readback.get("text") or "").strip()
                if content:
                    return True
                if cls._coerce_non_negative_int(readback.get("bytes")) > 0:
                    return True
                if cls._coerce_non_negative_int(readback.get("read_bytes")) > 0:
                    return True
            if cls._coerce_non_negative_int(ref.get("bytes")) > 0 and str(ref.get("sha256") or "").strip():
                return True
            file_refs = ref.get("file_refs")
            if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
                if cls._trusted_workspace_artifact_refs_have_readback(
                    [item for item in file_refs if isinstance(item, Mapping)]
                ):
                    return True
        return False

    @classmethod
    def _execution_status_liveness_diagnostic(
        cls,
        *,
        execution_status: str,
        execution_evidence_summary: Mapping[str, Any],
        verification: Mapping[str, Any],
        normalized: Mapping[str, Any],
        trusted_workspace_artifact_refs: Sequence[Mapping[str, Any]],
        grounding_guard: Mapping[str, Any] | None,
        final_result_required: bool,
    ) -> dict[str, Any] | None:
        if execution_status not in {"failed", "error", "timed_out", "blocked"}:
            return None
        if not final_result_required or not trusted_workspace_artifact_refs:
            return None
        if not cls._trusted_workspace_artifact_refs_have_readback(trusted_workspace_artifact_refs):
            return None
        if normalized.get("is_complete") is not True or normalized.get("requires_block") is True:
            return None
        if cls._normalize_string_list(normalized.get("missing_criteria")):
            return None
        if not isinstance(grounding_guard, Mapping):
            return None
        if grounding_guard.get("valid") is not True or int(grounding_guard.get("blocking_count") or 0) > 0:
            return None
        if not cls._verification_criteria_are_satisfied(verification.get("criterion_checks")):
            return None
        if cls._normalize_string_list(execution_evidence_summary.get("failed_actions")):
            return None
        if cls._normalize_string_list(execution_evidence_summary.get("blocked_actions")):
            return None
        if cls._normalize_string_list(execution_evidence_summary.get("approval_required_actions")):
            return None
        action_statuses = execution_evidence_summary.get("action_statuses")
        if isinstance(action_statuses, Mapping):
            for value in action_statuses.values():
                if str(value or "").strip().lower() in {"failed", "failure", "error", "timed_out", "timeout", "blocked"}:
                    return None
        for action in execution_evidence_summary.get("actions", []) or []:
            if not isinstance(action, Mapping):
                continue
            status = str(action.get("status") or "").strip().lower()
            if status in {"failed", "failure", "error", "timed_out", "timeout", "blocked"}:
                return None

        errors = execution_evidence_summary.get("errors")
        error_records = [dict(error) for error in errors if isinstance(error, Mapping)] if isinstance(errors, list) else []
        if not error_records:
            return None
        first_error = error_records[0]
        if not cls._is_liveness_stall_error(first_error):
            return None
        return {
            "status": execution_status,
            "error_type": str(first_error.get("error_type") or first_error.get("type") or ""),
            "stage": str(first_error.get("stage") or ""),
            "message": str(first_error.get("message") or ""),
            "last_progress_event": first_error.get("last_progress_event"),
            "idle_seconds": first_error.get("idle_seconds"),
            "elapsed_seconds": first_error.get("elapsed_seconds"),
            "diagnostic_only": True,
        }

    @classmethod
    def _verification_criteria_are_satisfied(cls, criterion_checks: Any) -> bool:
        if not isinstance(criterion_checks, Sequence) or isinstance(criterion_checks, str | bytes | bytearray):
            return False
        satisfied_statuses = {"satisfied", "passed", "pass", "ok", "complete", "completed", "accepted"}
        checked = False
        for check in criterion_checks:
            if not isinstance(check, Mapping):
                continue
            status = str(check.get("status") or "").strip().lower()
            if status not in satisfied_statuses:
                return False
            checked = True
        return checked

    @classmethod
    def _is_liveness_stall_error(cls, error: Mapping[str, Any]) -> bool:
        error_type = str(error.get("error_type") or error.get("type") or "")
        status = str(error.get("status") or "")
        message = str(error.get("message") or "")
        combined = f"{error_type} {status} {message}".lower()
        return (
            "runtimestagestallerror" in combined
            or "no progress" in combined
            or "idle deadline" in combined
            or "stalled" in combined
        )

    @classmethod
    def _completion_like_guard_text(cls, value: Any) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "task complete",
                "no replan needed",
                "verification successful",
                "deliverable complete",
                "complete and accepted",
                "all success criteria are satisfied",
            )
        )

    @classmethod
    def _align_guarded_verification_fields(
        cls,
        normalized: dict[str, Any],
        guard_reasons: Sequence[str],
        raw_verification: Mapping[str, Any] | None = None,
    ) -> None:
        if normalized.get("is_complete") is True or not guard_reasons:
            return
        raw_verification = raw_verification or {}
        missing = cls._normalize_string_list(normalized.get("missing_criteria"))
        guard_label = ", ".join(str(reason) for reason in guard_reasons if str(reason).strip()) or "verification_guard"
        summary = missing[0] if missing else f"Verification is blocked by {guard_label}."
        guarded_reason = f"Verification is not complete: {summary}"
        if cls._completion_like_guard_text(normalized.get("reason")):
            normalized["reason"] = guarded_reason
        if cls._completion_like_guard_text(normalized.get("failure_analysis")):
            normalized["failure_analysis"] = guarded_reason
        progress_message = normalized.get("progress_message", raw_verification.get("progress_message"))
        if cls._completion_like_guard_text(progress_message):
            normalized["progress_message"] = guarded_reason
        if (
            not str(normalized.get("replan_instruction") or "").strip()
            or cls._completion_like_guard_text(normalized.get("replan_instruction"))
        ):
            normalized["replan_instruction"] = (
                "Run another bounded step and produce explicit evidence for the guarded criteria."
            )
        filtered_requirements = [
            item
            for item in cls._normalize_string_list(normalized.get("next_step_requirements"))
            if not cls._completion_like_guard_text(item)
        ]
        normalized["next_step_requirements"] = cls._merge_string_lists(
            filtered_requirements,
            [normalized.get("replan_instruction")] if normalized.get("replan_instruction") else [],
        )

    @classmethod
    def _trusted_workspace_artifact_ref_summary(cls, ref: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            "path": str(ref.get("path") or ""),
            "role": str(ref.get("role") or ""),
            "source": str(ref.get("source") or ""),
            "truncated": bool(ref.get("truncated")),
        }
        file_refs = ref.get("file_refs")
        if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
            summary["file_refs"] = [
                cls._compact_artifact_ref_for_verifier(file_ref)
                for file_ref in list(file_refs)[:8]
                if isinstance(file_ref, Mapping)
            ]
        preview = ref.get("preview")
        if isinstance(preview, str) and preview:
            summary["preview"] = cls._truncate_prompt_text(preview, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        return DataFormatter.sanitize(summary)

    @classmethod
    def _workspace_artifact_execution_result_for_verifier(cls, execution_result: Any) -> Any:
        if not isinstance(execution_result, Mapping):
            return execution_result
        if not (
            isinstance(execution_result.get("artifact_manifest"), Mapping)
            or isinstance(execution_result.get("workspace_artifact_delivery"), Mapping)
            or (
                isinstance(execution_result.get("file_refs"), Sequence)
                and not isinstance(execution_result.get("file_refs"), str | bytes | bytearray)
            )
        ):
            return execution_result
        return cls._workspace_artifact_hot_value(execution_result)

    @classmethod
    def _workspace_artifact_hot_value(cls, value: Any, *, key_context: str = "") -> Any:
        if isinstance(value, Mapping):
            if key_context in {"file_refs", "artifact_refs"}:
                return cls._compact_artifact_ref_for_verifier(value)
            compact: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {"sha256", "bytes", "read_bytes", "size", "media_type", "content_kind", "handler_id"}:
                    continue
                if key_text == "artifact_manifest" and isinstance(item, Mapping):
                    compact[key_text] = cls._workspace_artifact_manifest_for_verifier(item)
                    continue
                if key_text == "workspace_artifact_delivery" and isinstance(item, Mapping):
                    compact[key_text] = cls._workspace_artifact_delivery_for_verifier(item)
                    continue
                compact[key_text] = cls._workspace_artifact_hot_value(item, key_context=key_text)
            return compact
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [cls._workspace_artifact_hot_value(item, key_context=key_context) for item in value]
        return value

    @classmethod
    def _workspace_artifact_manifest_for_verifier(cls, manifest: Mapping[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, item in manifest.items():
            key_text = str(key)
            if key_text in {"sha256", "bytes", "read_bytes", "size", "media_type", "content_kind", "handler_id"}:
                continue
            if key_text == "file_refs":
                compact[key_text] = cls._workspace_artifact_hot_value(item, key_context=key_text)
                continue
            compact[key_text] = cls._workspace_artifact_hot_value(item, key_context=key_text)
        return compact

    @classmethod
    def _workspace_artifact_delivery_for_verifier(cls, delivery: Mapping[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in ("source", "path", "status", "mode", "content_key"):
            if key in delivery:
                compact[key] = delivery.get(key)
        readback = delivery.get("readback")
        if isinstance(readback, Mapping):
            compact["readback"] = {
                key: readback.get(key)
                for key in ("path", "truncated")
                if key in readback
            }
        file_refs = delivery.get("file_refs")
        if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
            compact["file_refs"] = [
                cls._compact_artifact_ref_for_verifier(ref)
                for ref in list(file_refs)[:8]
                if isinstance(ref, Mapping)
            ]
            if len(file_refs) > 8:
                compact["file_refs"].append({"omitted": len(file_refs) - 8, "reason": "prompt_budget"})
        diagnostics = delivery.get("diagnostics")
        if diagnostics:
            compact["diagnostics"] = cls._workspace_artifact_hot_value(diagnostics, key_context="diagnostics")
        draft_meta = delivery.get("draft_meta")
        if isinstance(draft_meta, Mapping):
            compact["draft_meta"] = {
                key: draft_meta.get(key)
                for key in ("status", "route")
                if key in draft_meta
            }
        return compact

    @classmethod
    def _workspace_artifact_final_result_from_refs(cls, refs: Sequence[Mapping[str, Any]]) -> str:
        paths = [str(ref.get("path") or "").strip() for ref in refs if str(ref.get("path") or "").strip()]
        if not paths:
            return ""
        if len(paths) == 1:
            return f"Workspace artifact delivered at {paths[0]}; full content is available through file_refs/readback."
        return (
            "Workspace artifacts delivered at "
            + ", ".join(paths)
            + "; full content is available through file_refs/readback."
        )

    @classmethod
    def _final_result_is_workspace_artifact_pointer(
        cls,
        final_result: str,
        refs: Sequence[Mapping[str, Any]],
    ) -> bool:
        text = str(final_result or "").strip()
        if not text:
            return False
        if cls._looks_like_workspace_artifact_placeholder(text):
            return True
        for ref in refs:
            path = str(ref.get("path") or "").strip()
            if path and path in text:
                return True
        return False

    @classmethod
    def _candidate_final_result_from_execution_result(
        cls,
        execution_result: Any,
        *,
        include_answer: bool = True,
    ) -> str:
        if isinstance(execution_result, Mapping):
            candidates: list[str] = []
            for key in ("candidate_final_result", "final_result"):
                value = execution_result.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
            manifest = execution_result.get("artifact_manifest")
            if isinstance(manifest, Mapping):
                manifest_content = cls._workspace_artifact_manifest_content(manifest)
                if manifest_content.strip():
                    candidates.append(manifest_content.strip())
            keys: tuple[str, ...] = ("artifact_markdown", "artifact_html")
            if include_answer:
                keys = keys + ("answer", "result")
            for key in keys:
                value = execution_result.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
            if candidates:
                return max(candidates, key=len)
            if not include_answer:
                return ""
            step_result = execution_result.get("step_result")
            if isinstance(step_result, str) and len(step_result.strip()) > 200:
                return step_result.strip()
            remaining_work = execution_result.get("remaining_work")
            evidence = execution_result.get("evidence")
            if (
                not cls._has_remaining_work(remaining_work)
                and isinstance(evidence, Sequence)
                and not isinstance(evidence, str | bytes | bytearray)
            ):
                text_items = [item.strip() for item in evidence if isinstance(item, str) and item.strip()]
                if text_items:
                    longest = max(text_items, key=len)
                    if len(longest) > max(800, len(str(step_result or "")) * 3):
                        return longest
        if isinstance(execution_result, str) and execution_result.strip():
            return execution_result.strip()
        return ""

    @staticmethod
    def _has_remaining_work(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return any(bool(str(item).strip()) for item in value)
        return bool(value)

    @classmethod
    def _verification_execution_meta_summary(
        cls,
        execution_meta: Mapping[str, Any],
        evidence_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        route = execution_meta.get("route")
        diagnostics = execution_meta.get("diagnostics")
        return {
            "status": str(execution_meta.get("status") or ""),
            "route": cls._compact_verifier_prompt_value(route, max_chars=1200),
            "diagnostics": cls._compact_verifier_prompt_value(diagnostics, max_chars=1200),
            "evidence_summary": dict(evidence_summary),
        }

    @classmethod
    def _evidence_ledger_from_execution_meta(cls, execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        evidence_items: list[dict[str, Any]] = []
        blocks = execution_meta.get("blocks")
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                block_evidence = dict(evidence)
                raw_block_items = block_evidence.get("evidence_items")
                if isinstance(raw_block_items, Sequence) and not isinstance(
                    raw_block_items, str | bytes | bytearray
                ):
                    block_evidence["evidence_items"] = cls._prioritized_verifier_evidence_items(raw_block_items)
                block_ledger = evidence_ledger_view(block_evidence, max_items=80, body_chars=2400)
                evidence_items.extend(
                    dict(item)
                    for item in block_ledger.get("items", [])
                    if isinstance(item, Mapping)
                )
        evidence_items.extend(cls._action_result_evidence_items_from_execution_meta(execution_meta))
        return evidence_ledger_view(
            {"evidence_items": cls._prioritized_verifier_evidence_items(evidence_items)},
            max_items=120,
            body_chars=2400,
        )

    @classmethod
    def _prioritized_verifier_evidence_items(cls, items: Sequence[Any]) -> list[dict[str, Any]]:
        ordered: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            sanitized = dict(DataFormatter.sanitize(item))
            ordered.append((cls._verifier_evidence_item_priority(sanitized), index, sanitized))
        ordered.sort(key=lambda entry: (entry[0], entry[1]))
        return [item for _, _, item in ordered]

    @staticmethod
    def _verifier_evidence_item_priority(item: Mapping[str, Any]) -> int:
        kind = str(item.get("kind") or "").strip().lower()
        if kind == "workspace_artifact.targeted_readback":
            return 0
        if kind == "workspace_artifact.readback":
            return 1
        if kind == "workspace_artifact.acceptance_locator":
            return 2
        if kind.startswith("workspace_artifact."):
            return 3
        return 10

    @classmethod
    def _action_result_evidence_items_from_execution_meta(
        cls,
        execution_meta: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, record in enumerate(cls._collect_execution_action_records(execution_meta)):
            if not isinstance(record, Mapping):
                continue
            action_id = str(record.get("id") or record.get("name") or "").strip()
            if not action_id:
                continue
            status = cls._action_result_evidence_status(record)
            result_preview = record.get("result_preview")
            error = record.get("error")
            body_value = result_preview if result_preview not in (None, "", [], {}) else error
            body = cls._action_result_evidence_body(body_value)
            body_state = cls._action_result_evidence_body_state(record, status=status, body=body)
            action_call_id = str(record.get("action_call_id") or "").strip()
            evidence_id = cls._action_result_evidence_id(
                action_id=action_id,
                action_call_id=action_call_id,
                index=index,
                preview_sha=str(record.get("result_preview_sha256") or ""),
            )
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "agent_task.action.result",
                "status": status,
                "raw_status": record.get("status", status),
                "body_state": body_state,
                "action_id": action_id,
                "action_call_id": action_call_id,
                "aliases": cls._action_result_evidence_aliases(record),
                "provenance": {
                    "source": "agent_task.execution_meta.action_logs",
                    "action_id": action_id,
                    "action_call_id": action_call_id,
                },
                "supports": {
                    "content": status == "ok" and body_state in {"full", "bounded", "truncated"},
                    "unavailability": status in {"failed", "empty"},
                    "ref_pointer": bool(action_call_id),
                },
            }
            input_preview = record.get("input_preview")
            if input_preview not in (None, "", [], {}):
                item["input_preview"] = DataFormatter.sanitize(input_preview)
            if body:
                item["body"] = body
            for ref in cls._collect_source_refs_from_action_records([record]):
                if not isinstance(ref, Mapping):
                    continue
                field = str(ref.get("field") or "").strip()
                value = str(ref.get("value") or "").strip()
                if field and value and item.get(field) in (None, "", [], {}):
                    item[field] = value
            items.append(DataFormatter.sanitize(item))
        return items

    @staticmethod
    def _action_result_evidence_status(record: Mapping[str, Any]) -> str:
        status = str(record.get("status") or "").strip().lower()
        if status in {"failed", "failure", "error", "timed_out", "timeout", "blocked"} or record.get("error"):
            return "failed"
        if record.get("result_preview") in (None, "", [], {}):
            return "empty"
        return "ok"

    @classmethod
    def _action_result_evidence_body_state(
        cls,
        record: Mapping[str, Any],
        *,
        status: str,
        body: str,
    ) -> str:
        if not body:
            return "ref_only" if status == "failed" else "ref_only"
        preview_meta = record.get("result_preview_meta")
        if isinstance(preview_meta, Mapping) and preview_meta.get("truncated") is True:
            return "truncated"
        return "bounded"

    @staticmethod
    def _action_result_evidence_body(value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        sanitized = DataFormatter.sanitize(value)
        if isinstance(sanitized, str):
            return sanitized
        try:
            return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(sanitized)

    @staticmethod
    def _action_result_evidence_id(
        *,
        action_id: str,
        action_call_id: str,
        index: int,
        preview_sha: str,
    ) -> str:
        suffix = action_call_id or preview_sha or str(index)
        raw = f"agent_task_action_result:{action_id}:{suffix}"
        return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]

    @staticmethod
    def _action_result_evidence_aliases(record: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)

        action_id = str(record.get("id") or record.get("name") or "").strip()
        action_call_id = str(record.get("action_call_id") or "").strip()
        add(action_id)
        add(f"action_{action_id}")
        add(f"action_result_{action_id}")
        add(action_call_id)
        if action_call_id:
            add(f"action_{action_call_id}")
            add(f"action_result_{action_call_id}")
        for ref_key in ("artifact_refs", "file_refs"):
            refs = record.get(ref_key)
            if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
                continue
            for ref in refs:
                if not isinstance(ref, Mapping):
                    continue
                for field in ("artifact_id", "path", "record_id", "source_url", "selected_url", "url", "href"):
                    add(ref.get(field))
        return aliases[:24]

    def _cumulative_evidence_ledger(self, current_execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        current_ledger = self._evidence_ledger_from_execution_meta(current_execution_meta)
        for item in current_ledger.get("items", []):
            if isinstance(item, Mapping):
                items.append(dict(item))
        for iteration in reversed(self.iterations):
            if not isinstance(iteration, Mapping):
                continue
            previous_meta = iteration.get("execution_meta")
            if not isinstance(previous_meta, Mapping):
                continue
            previous_ledger = self._evidence_ledger_from_execution_meta(previous_meta)
            for item in previous_ledger.get("items", []):
                if isinstance(item, Mapping):
                    items.append(dict(item))
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            evidence_id = str(item.get("id") or "").strip()
            if evidence_id and evidence_id in seen:
                continue
            if evidence_id:
                seen.add(evidence_id)
            deduped.append(item)
        return evidence_ledger_view({"evidence_items": deduped}, max_items=120, body_chars=2400)

    def _cumulative_execution_evidence_summary(self, current_execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        for iteration in self.iterations:
            if not isinstance(iteration, Mapping):
                continue
            previous_meta = iteration.get("execution_meta")
            if isinstance(previous_meta, Mapping):
                summaries.append(self._execution_log_summary(dict(previous_meta)))
        current_summary = self._execution_log_summary(dict(current_execution_meta))
        summaries.append(current_summary)

        combined: dict[str, Any] = {
            "model_response_count": 0,
            "action_log_count": 0,
            "action_ids": [],
            "action_statuses": {},
            "actions": [],
            "source_refs": [],
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": [],
            "missing_required_actions": [],
            "selected_skill_ids": [],
            "required_skills": [],
            "missing_required_skills": [],
            "capabilities_used": [],
            "capability_evidence": {
                "actions": {"succeeded": [], "failed": []},
                "skills": {"selected": []},
                "artifacts": {"readback": []},
                "validations": {"passed": [], "failed": []},
            },
            "artifact_refs": [],
            "workspace_refs": {},
            "errors": [],
            "replan_signals": [],
            "current_replan_signals": [],
            "status": str(current_execution_meta.get("status") or ""),
        }

        for summary in summaries:
            combined["model_response_count"] += int(summary.get("model_response_count") or 0)
            actions = summary.get("actions")
            if isinstance(actions, list):
                combined["actions"].extend(actions)
            artifact_refs = summary.get("artifact_refs")
            if isinstance(artifact_refs, list):
                combined["artifact_refs"].extend(artifact_refs)
            source_refs = summary.get("source_refs")
            if isinstance(source_refs, list):
                combined["source_refs"].extend(source_refs)
            errors = summary.get("errors")
            if isinstance(errors, list):
                combined["errors"].extend(errors)
            replan_signals = summary.get("replan_signals")
            if isinstance(replan_signals, list):
                combined["replan_signals"].extend(replan_signals)
            workspace_refs = summary.get("workspace_refs")
            if isinstance(workspace_refs, Mapping):
                self._merge_workspace_ref_summary(combined["workspace_refs"], workspace_refs)
            for key in (
                "action_ids",
                "failed_actions",
                "blocked_actions",
                "approval_required_actions",
                "required_actions",
                "missing_required_actions",
                "selected_skill_ids",
                "required_skills",
                "missing_required_skills",
                "capabilities_used",
            ):
                combined[key] = self._merge_string_lists(combined.get(key), summary.get(key))
            action_statuses = summary.get("action_statuses")
            if isinstance(action_statuses, Mapping):
                combined["action_statuses"].update(DataFormatter.sanitize(action_statuses))
            capability_evidence = summary.get("capability_evidence")
            if isinstance(capability_evidence, Mapping):
                self._merge_capability_evidence_summary(combined["capability_evidence"], capability_evidence)

        combined["actions"] = self._dedupe_action_records(combined["actions"])
        combined["source_refs"] = self._dedupe_ref_records(combined["source_refs"])
        combined["action_log_count"] = len(combined["actions"])
        final_action_statuses = {
            str(action_id): str(status)
            for action_id, status in dict(combined.get("action_statuses") or {}).items()
            if str(action_id).strip()
        }
        combined["failed_actions"] = self._action_ids_by_final_status(
            final_action_statuses,
            {"failed", "failure", "error"},
        )
        combined["blocked_actions"] = self._action_ids_by_final_status(final_action_statuses, {"blocked"})
        combined["approval_required_actions"] = self._action_ids_by_final_status(
            final_action_statuses,
            {"approval_required"},
        )
        final_succeeded_actions = self._action_ids_by_final_status(
            final_action_statuses,
            {"success", "succeeded", "partial_success"},
        )
        capability_evidence_summary = combined.get("capability_evidence")
        capability_actions = (
            capability_evidence_summary.get("actions") if isinstance(capability_evidence_summary, dict) else None
        )
        if isinstance(capability_actions, dict):
            capability_actions["succeeded"] = final_succeeded_actions
            capability_actions["failed"] = combined["failed_actions"]
        combined["artifact_refs"] = self._dedupe_ref_records(combined["artifact_refs"])
        combined["errors"] = self._dedupe_jsonable_records(combined["errors"])
        combined["replan_signals"] = self._dedupe_jsonable_records(combined["replan_signals"])
        current_replan_signals = current_summary.get("replan_signals")
        if isinstance(current_replan_signals, list):
            combined["current_replan_signals"] = self._dedupe_jsonable_records(current_replan_signals)
        return DataFormatter.sanitize(combined)

    @staticmethod
    def _merge_workspace_ref_summary(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            key_text = str(key)
            if isinstance(value, list):
                bucket = target.setdefault(key_text, [])
                if isinstance(bucket, list):
                    for item in value:
                        if item not in bucket:
                            bucket.append(item)
                continue
            if key_text not in target:
                target[key_text] = value

    @classmethod
    def _merge_capability_evidence_summary(cls, target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for kind, value in source.items():
            if not isinstance(value, Mapping):
                continue
            bucket = target.setdefault(str(kind), {})
            if not isinstance(bucket, dict):
                continue
            for field, items in value.items():
                bucket[str(field)] = cls._merge_string_lists(bucket.get(str(field)), items)

    @classmethod
    def _compact_context_pack_for_verifier(cls, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        compact = cls._compact_verifier_prompt_value(
            cls._context_pack_for_verifier_hot_path(context_pack),
            max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
        )
        return compact if isinstance(compact, dict) else {}

    @classmethod
    def _context_pack_for_verifier_hot_path(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            compact: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {
                    "sha256",
                    "digest",
                    "bytes",
                    "read_bytes",
                    "size",
                    "media_type",
                    "content_kind",
                    "handler_id",
                }:
                    continue
                compact[key_text] = cls._context_pack_for_verifier_hot_path(item)
            return compact
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [cls._context_pack_for_verifier_hot_path(item) for item in value]
        return value

    @classmethod
    def _compact_verifier_evidence_summary(cls, summary: Mapping[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in summary.items():
            if key == "actions" and isinstance(value, list):
                compact[key] = [cls._compact_action_record_for_verifier(ref) for ref in value[:16]]
                if len(value) > 16:
                    compact[key].append({"omitted": len(value) - 16, "reason": "prompt_budget"})
                continue
            if key == "artifact_refs" and isinstance(value, list):
                compact[key] = [cls._compact_artifact_ref_for_verifier(ref) for ref in value[:24]]
                if len(value) > 24:
                    compact[key].append({"omitted": len(value) - 24, "reason": "prompt_budget"})
                continue
            if key == "workspace_refs" and isinstance(value, Mapping):
                compact[key] = cls._compact_verifier_prompt_value(
                    cls._workspace_artifact_hot_value(value),
                    max_chars=2400,
                )
                continue
            compact[key] = cls._compact_verifier_prompt_value(value, max_chars=_VERIFIER_PROMPT_ITEM_CHARS)
        return compact

    @classmethod
    def _compact_artifact_ref_for_verifier(cls, ref: Any) -> Any:
        if not isinstance(ref, Mapping):
            return cls._compact_verifier_prompt_value(ref, max_chars=600)
        keep_keys = (
            "artifact_id",
            "action_call_id",
            "path",
            "role",
            "label",
            "artifact_type",
            "source",
            "source_url",
            "selected_url",
            "requested_url",
            "canonical_url",
            "url",
            "href",
            "record_id",
            "collection",
            "kind",
            "content_state",
            "readback_mode",
            "truncated",
            "available",
        )
        compact = {key: ref.get(key) for key in keep_keys if key in ref}
        if "preview" in ref:
            compact["preview"] = cls._compact_verifier_prompt_value(ref.get("preview"), max_chars=600)
        if "content_preview" in ref:
            compact["content_preview"] = cls._compact_verifier_prompt_value(
                ref.get("content_preview"),
                max_chars=600,
            )
        return compact

    @classmethod
    def _compact_action_record_for_verifier(cls, record: Any) -> Any:
        if not isinstance(record, Mapping):
            return cls._compact_verifier_prompt_value(record, max_chars=900)
        keep_keys = (
            "id",
            "name",
            "status",
            "action_type",
            "kind",
            "action_call_id",
            "input_preview",
            "result_preview_meta",
        )
        compact: dict[str, Any] = {key: record.get(key) for key in keep_keys if key in record}
        result_preview_meta = compact.get("result_preview_meta")
        if isinstance(result_preview_meta, Mapping):
            compact["result_preview_meta"] = {
                key: result_preview_meta.get(key)
                for key in ("truncated", "omitted", "reason")
                if key in result_preview_meta
            }
        if "result_preview" in record:
            compact["result_preview"] = cls._compact_action_preview_value(record.get("result_preview"), max_chars=5200)
        if isinstance(record.get("artifact_refs"), list):
            refs = record.get("artifact_refs") or []
            compact["artifact_refs"] = [cls._compact_artifact_ref_for_verifier(ref) for ref in refs[:4]]
            if len(refs) > 4:
                compact["artifact_refs"].append({"omitted": len(refs) - 4, "reason": "prompt_budget"})
        if record.get("file_refs"):
            file_refs = record.get("file_refs")
            if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
                compact["file_refs"] = [
                    cls._compact_artifact_ref_for_verifier(ref)
                    for ref in list(file_refs)[:4]
                    if isinstance(ref, Mapping)
                ]
                if len(file_refs) > 4:
                    compact["file_refs"].append({"omitted": len(file_refs) - 4, "reason": "prompt_budget"})
            else:
                compact["file_refs"] = cls._compact_verifier_prompt_value(file_refs, max_chars=1000)
        return compact

    @classmethod
    def _compact_action_preview_value(
        cls,
        value: Any,
        *,
        max_chars: int,
        depth: int = 0,
    ) -> Any:
        value = _omit_agent_task_request_payloads_from_hot_path(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._truncate_prompt_text_middle(value, max_chars)
        if depth >= 4:
            return cls._truncate_prompt_text_middle(value, max_chars)
        if isinstance(value, list):
            limit = 12
            return [
                cls._compact_action_preview_value(item, max_chars=max(360, max_chars // 2), depth=depth + 1)
                for item in value[:limit]
            ] + ([{"omitted": len(value) - limit, "reason": "prompt_budget"}] if len(value) > limit else [])
        if isinstance(value, dict):
            compacted: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 36:
                    compacted["omitted"] = {"count": len(value) - 36, "reason": "prompt_budget"}
                    break
                key_text = str(key)
                item_chars = max_chars
                if key_text in {"content", "raw", "text", "output", "result", "data", "body", "preview"}:
                    item_chars = max(1600, max_chars)
                compacted[key_text] = cls._compact_action_preview_value(
                    item,
                    max_chars=item_chars,
                    depth=depth + 1,
                )
            return compacted
        return cls._truncate_prompt_text_middle(value, max_chars)

    @classmethod
    def _compact_verifier_prompt_value(
        cls,
        value: Any,
        *,
        max_chars: int = _VERIFIER_PROMPT_ITEM_CHARS,
        depth: int = 0,
    ) -> Any:
        value = _omit_agent_task_request_payloads_from_hot_path(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, bytes):
            text = value[:max_chars].decode("utf-8", "replace")
            return {"bytes": len(value), "preview": cls._truncate_prompt_text(text, max_chars)}
        if isinstance(value, str):
            return cls._truncate_prompt_text(value, max_chars)
        if depth >= 5:
            return cls._truncate_prompt_text(value, max_chars)
        if isinstance(value, list):
            limit = 24
            items = [
                cls._compact_verifier_prompt_value(
                    item,
                    max_chars=max(240, max_chars // 2),
                    depth=depth + 1,
                )
                for item in value[:limit]
            ]
            if len(value) > limit:
                items.append({"omitted": len(value) - limit, "reason": "prompt_budget"})
            return items
        if isinstance(value, dict):
            limit = 48
            compacted: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= limit:
                    compacted["omitted"] = {"count": len(value) - limit, "reason": "prompt_budget"}
                    break
                key_text = str(key)
                item_chars = max_chars
                if key_text in {"content", "raw", "text", "output", "result", "data", "body", "preview"}:
                    item_chars = max(240, max_chars // 3)
                compacted[key_text] = cls._compact_verifier_prompt_value(
                    item,
                    max_chars=item_chars,
                    depth=depth + 1,
                )
            return compacted
        return cls._truncate_prompt_text(value, max_chars)

    @staticmethod
    def _truncate_prompt_text(value: Any, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 32)].rstrip() + "\n[truncated for verifier prompt]"

    @staticmethod
    def _truncate_prompt_text_middle(value: Any, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        marker = "\n[truncated middle for verifier prompt]\n"
        available = max(0, max_chars - len(marker))
        head = max(1, available // 2)
        tail = max(1, available - head)
        return text[:head].rstrip() + marker + text[-tail:].lstrip()

    def _normalize_verification(
        self,
        verification: dict[str, Any],
        *,
        execution_evidence_summary: dict[str, Any],
        candidate_final_result: str = "",
        grounding_guard: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized: dict[str, Any] = {
            "is_complete": self._normalize_bool(verification.get("is_complete"), default=False),
            "requires_block": self._normalize_bool(verification.get("requires_block"), default=False),
            "reason": str(verification.get("reason") or ""),
            "failure_analysis": str(verification.get("failure_analysis") or verification.get("reason") or ""),
            "acceptance_delta": self._normalize_string_list(verification.get("acceptance_delta")),
            "missing_criteria": self._normalize_string_list(verification.get("missing_criteria")),
            "replan_instruction": str(verification.get("replan_instruction") or ""),
            "repair_constraints": self._normalize_string_list(verification.get("repair_constraints")),
            "next_step_requirements": self._normalize_string_list(verification.get("next_step_requirements")),
            "final_result": str(verification.get("final_result") or ""),
        }
        guard_reasons: list[str] = []
        if isinstance(grounding_guard, Mapping):
            normalized["grounding_guard"] = DataFormatter.sanitize(dict(grounding_guard))
            if int(grounding_guard.get("blocking_count") or 0) > 0 or grounding_guard.get("valid") is False:
                normalized["is_complete"] = False
                guard_reasons.append("evidence_ledger_grounding_guard_failed")
                guard_messages = [
                    str(item.get("message") or item.get("code") or "")
                    for item in grounding_guard.get("diagnostics", [])
                    if isinstance(item, Mapping) and item.get("blocking") is True
                ]
                if not guard_messages:
                    guard_messages = ["Evidence ledger grounding guard found invalid claim-to-evidence bindings."]
                normalized["missing_criteria"] = self._merge_string_lists(
                    normalized.get("missing_criteria"),
                    guard_messages,
                )
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    guard_messages,
                )
        if normalized["requires_block"]:
            normalized["is_complete"] = False
            guard_reasons.append("requires_block_true")
        if normalized["missing_criteria"]:
            normalized["is_complete"] = False
            guard_reasons.append("missing_criteria_present")
        final_result_required = self._normalize_bool(verification.get("final_result_required"), default=False)
        trusted_workspace_artifact_refs = self._trusted_workspace_artifact_refs_from_summary(execution_evidence_summary)
        execution_status = str(execution_evidence_summary.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            execution_errors = execution_evidence_summary.get("errors", [])
            error_message = ""
            if isinstance(execution_errors, list) and execution_errors:
                first_error = execution_errors[0]
                if isinstance(first_error, dict):
                    error_message = str(first_error.get("message") or first_error.get("type") or "")
                else:
                    error_message = str(first_error)
            detail = f": {error_message}" if error_message else ""
            liveness_diagnostic = self._execution_status_liveness_diagnostic(
                execution_status=execution_status,
                execution_evidence_summary=execution_evidence_summary,
                verification=verification,
                normalized=normalized,
                trusted_workspace_artifact_refs=trusted_workspace_artifact_refs,
                grounding_guard=grounding_guard,
                final_result_required=final_result_required,
            )
            if liveness_diagnostic is not None:
                normalized["non_blocking_execution_status"] = liveness_diagnostic
                self.diagnostics.setdefault("non_blocking_execution_status", []).append(
                    {"task_id": self.id, **liveness_diagnostic}
                )
            else:
                normalized["is_complete"] = False
                guard_reasons.append("execution_status_failed")
                normalized["missing_criteria"] = [
                    *normalized["missing_criteria"],
                    f"Execution step status is {execution_status}{detail}.",
                ]
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    [f"Execution step status is {execution_status}{detail}."],
                )
        raw_current_replan_signals = execution_evidence_summary.get("current_replan_signals")
        raw_replan_signals = (
            raw_current_replan_signals
            if isinstance(raw_current_replan_signals, list)
            else execution_evidence_summary.get("replan_signals", [])
        )
        replan_signals = [dict(signal) for signal in raw_replan_signals if isinstance(signal, dict)]
        if replan_signals:
            normalized["replan_signals"] = replan_signals
        blocking_signal = next(
            (signal for signal in replan_signals if str(signal.get("status") or "") == "blocked"),
            None,
        )
        if blocking_signal is not None:
            normalized["is_complete"] = False
            normalized["requires_block"] = True
            guard_reasons.append("structured_replan_signal_blocked")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                str(blocking_signal.get("reason") or "Execution emitted a blocked ReplanSignal."),
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [str(blocking_signal.get("reason") or "Execution emitted a blocked ReplanSignal.")],
            )
        actionable_signals = [
            signal
            for signal in replan_signals
            if str(signal.get("status") or "") in {"repair", "replan_segment", "replan_goal", "clarify"}
        ]
        if actionable_signals:
            normalized["is_complete"] = False
            guard_reasons.append("structured_replan_signal")
            if not normalized["replan_instruction"]:
                reasons = [
                    str(signal.get("reason") or signal.get("status") or "")
                    for signal in actionable_signals
                    if str(signal.get("reason") or signal.get("status") or "").strip()
                ]
                normalized["replan_instruction"] = "Handle structured ReplanSignal before accepting completion" + (
                    f": {'; '.join(reasons)}." if reasons else "."
                )
        risky_actions, non_blocking_failed_actions = self._execution_risk_actions(execution_evidence_summary)
        if non_blocking_failed_actions:
            normalized["non_blocking_failed_actions"] = non_blocking_failed_actions
        if risky_actions:
            normalized["is_complete"] = False
            guard_reasons.append("execution_risk_actions_present")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Unresolved execution risk actions: {', '.join(risky_actions)}",
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Unresolved execution risk actions: {', '.join(risky_actions)}"],
            )
        # Accumulate satisfied required capabilities across iterations, then guard
        # on what is still missing for the whole task rather than per-step.
        self._satisfied_required_actions.update(
            self._normalize_string_list(execution_evidence_summary.get("action_ids"))
        )
        self._satisfied_required_skills.update(
            self._normalize_string_list(execution_evidence_summary.get("selected_skill_ids"))
        )
        self._satisfied_capabilities.update(
            self._normalize_string_list(execution_evidence_summary.get("capabilities_used"))
        )
        capability_evidence = execution_evidence_summary.get("capability_evidence")
        if isinstance(capability_evidence, dict) and isinstance(capability_evidence.get("actions"), dict):
            self._satisfied_succeeded_actions.update(
                self._normalize_string_list(capability_evidence["actions"].get("succeeded"))
            )
        required_actions = self._normalize_string_list(execution_evidence_summary.get("required_actions"))
        required_skills = self._normalize_string_list(execution_evidence_summary.get("required_skills"))
        missing_required = [
            *[action_id for action_id in required_actions if action_id not in self._satisfied_required_actions],
            *[skill_id for skill_id in required_skills if skill_id not in self._satisfied_required_skills],
        ]
        if missing_required:
            normalized["is_complete"] = False
            guard_reasons.append("required_capability_evidence_missing")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Missing required capability evidence: {', '.join(missing_required)}",
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Missing required capability evidence: {', '.join(missing_required)}"],
            )
        # Load-bearing structured capability-evidence gate
        # (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC). A deterministic
        # correspondence check between authored, structured requirements (which
        # capabilities must appear in execution evidence) and the accumulated
        # capability/action evidence. It is independent of capability mode: a
        # model_decision skill (or any capability) the goal depends on can be
        # required as evidence here WITHOUT forcing routing. No prose or model
        # reading participates in the pass/fail decision. Only the kinds with a
        # deterministic check are enforced; reserved kinds are recorded as
        # unenforced diagnostics for the advisory model verifier.
        missing_capability_evidence, unenforced_requirements = self._evaluate_capability_evidence()
        if missing_capability_evidence:
            normalized["is_complete"] = False
            guard_reasons.append("capability_evidence_missing")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Missing required capability evidence: {', '.join(missing_capability_evidence)}",
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Missing required capability evidence: {', '.join(missing_capability_evidence)}"],
            )
            missing_required = [*missing_required, *missing_capability_evidence]
        if unenforced_requirements:
            self.diagnostics.setdefault("unenforced_evidence_requirements", []).extend(unenforced_requirements)
        normalized["missing_required_capabilities"] = missing_required
        normalized["missing_capability_evidence"] = missing_capability_evidence
        # Surface unenforced requirements so a reserved or not-yet-wired evidence
        # kind is visible rather than a silent no-op (it does not block).
        normalized["unenforced_evidence_requirements"] = unenforced_requirements
        normalized["final_result_required"] = final_result_required
        if normalized["is_complete"] and final_result_required and not normalized["final_result"].strip():
            if candidate_final_result.strip():
                normalized["final_result"] = candidate_final_result.strip()
            elif trusted_workspace_artifact_refs:
                normalized["final_result"] = self._workspace_artifact_final_result_from_refs(
                    trusted_workspace_artifact_refs
                )
                normalized["final_result_via_workspace_artifact"] = True
            else:
                normalized["is_complete"] = False
                guard_reasons.append("final_result_missing")
                normalized["missing_criteria"] = [
                    *normalized["missing_criteria"],
                    "Final result is missing.",
                ]
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    ["Final result is missing."],
                )
        if normalized["is_complete"]:
            final_result_text = normalized["final_result"].strip()
            final_result_is_artifact_pointer = self._final_result_is_workspace_artifact_pointer(
                final_result_text,
                trusted_workspace_artifact_refs,
            )
            if final_result_text and not final_result_is_artifact_pointer:
                output_contract = self._parse_final_result_output_contract(final_result_text)
                if output_contract is not None and output_contract["parse_success"] is not True:
                    normalized["is_complete"] = False
                    guard_reasons.append("final_result_output_parse_failed")
                    attempts = output_contract.get("attempts", [])
                    attempted_formats = [
                        str(item.get("format"))
                        for item in attempts
                        if isinstance(item, Mapping) and str(item.get("format") or "").strip()
                    ]
                    format_note = (
                        " -> ".join(attempted_formats) if attempted_formats else output_contract["declared_format"]
                    )
                    message = (
                        "Final result must parse as a dict for the declared execution output contract "
                        f"(tried {format_note})."
                    )
                    last_error = str(output_contract.get("last_error") or "").strip()
                    if last_error:
                        message = f"{message} Last parser error: {last_error}."
                    normalized["missing_criteria"] = self._merge_string_lists(
                        normalized.get("missing_criteria"),
                        [message],
                    )
                    normalized["acceptance_delta"] = self._merge_string_lists(
                        normalized.get("acceptance_delta"),
                        [message],
                    )
                    if not normalized["replan_instruction"]:
                        normalized["replan_instruction"] = (
                            "Run another bounded step and produce a final_result that can be parsed as a dict "
                            "for the declared execution output contract; use JSON if the requested format fails."
                        )
                elif output_contract is not None and output_contract.get("resolved_format") != output_contract.get(
                    "declared_format"
                ):
                    self.diagnostics.setdefault("final_result_output_contract", []).append(
                        {
                            "task_id": self.id,
                            "declared_format": output_contract.get("declared_format"),
                            "resolved_format": output_contract.get("resolved_format"),
                            "fallback": output_contract.get("fallback"),
                        }
                    )
            elif final_result_is_artifact_pointer and trusted_workspace_artifact_refs:
                normalized["final_result_via_workspace_artifact"] = True
        continuation = self._untried_read_action_continuation(execution_evidence_summary)
        if normalized["requires_block"] and continuation:
            normalized["requires_block"] = False
            guard_reasons = [reason for reason in guard_reasons if reason != "requires_block_true"]
            guard_reasons.append("untried_read_action_available")
            normalized["continuation_opportunities"] = continuation
            untried = ", ".join(continuation.get("untried_action_ids") or [])
            message = "Verifier requested blocking, but read-only evidence capabilities remain untried" + (
                f": {untried}." if untried else "."
            )
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [message],
            )
            normalized["missing_criteria"] = self._merge_string_lists(
                normalized.get("missing_criteria"),
                [message],
            )
            if not normalized["replan_instruction"]:
                normalized["replan_instruction"] = "Plan another bounded evidence-gathering step before blocking."
            self.diagnostics.setdefault("verification_continuations", []).append(
                {
                    "task_id": self.id,
                    "reason": continuation.get("reason"),
                    "untried_action_ids": continuation.get("untried_action_ids", []),
                    "failed_read_action_ids": continuation.get("failed_read_action_ids", []),
                }
            )
        if guard_reasons:
            self._align_guarded_verification_fields(normalized, guard_reasons, verification)
            normalized["guard_reasons"] = guard_reasons
            if not normalized["replan_instruction"]:
                normalized["replan_instruction"] = (
                    "Run another bounded step and produce explicit evidence for the guarded criteria."
                )
            self.diagnostics.setdefault("verification_guards", []).append(
                {
                    "task_id": self.id,
                    "guard_reasons": guard_reasons,
                    "missing_criteria": normalized["missing_criteria"],
                }
            )
        normalized["repair_constraints"] = self._merge_string_lists(
            normalized.get("repair_constraints"),
            normalized.get("missing_criteria"),
        )
        normalized["next_step_requirements"] = self._merge_string_lists(
            normalized.get("next_step_requirements"),
            [normalized.get("replan_instruction")] if normalized.get("replan_instruction") else [],
        )
        normalized["acceptance_delta"] = self._merge_string_lists(
            normalized.get("acceptance_delta"),
            normalized.get("missing_criteria"),
        )
        for key, value in verification.items():
            normalized.setdefault(key, DataFormatter.sanitize(value))
        return normalized

    def _execution_risk_actions(
        self,
        execution_evidence_summary: Mapping[str, Any],
    ) -> tuple[list[str], list[str]]:
        failed_actions = self._normalize_string_list(execution_evidence_summary.get("failed_actions"))
        blocked_actions = self._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
        approval_required_actions = self._normalize_string_list(
            execution_evidence_summary.get("approval_required_actions")
        )
        required_actions = set(self._normalize_string_list(execution_evidence_summary.get("required_actions")))
        for requirement in self._capability_evidence_requirements():
            if not requirement.get("required", True):
                continue
            if str(requirement.get("kind") or "capability_used") != "action_succeeded":
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if capability_id:
                required_actions.add(capability_id)

        read_safe_actions = {
            str(item.get("id") or "").strip()
            for item in self._planner_capabilities()
            if isinstance(item, Mapping)
            and str(item.get("kind") or "").strip() == "action"
            and str(item.get("id") or "").strip()
            and (
                str(item.get("side_effect_level") or "").strip().lower() == "read"
                or bool(item.get("replay_safe")) is True
            )
        }
        framework_diagnostic_actions = {"action_loop", "action_planning"}
        risky_failed: list[str] = []
        non_blocking_failed: list[str] = []
        for action_id in failed_actions:
            if (
                action_id in framework_diagnostic_actions
                or action_id in read_safe_actions
            ) and action_id not in required_actions:
                non_blocking_failed.append(action_id)
            else:
                risky_failed.append(action_id)
        risky_blocked: list[str] = []
        for action_id in blocked_actions:
            if action_id in framework_diagnostic_actions and action_id not in required_actions:
                non_blocking_failed.append(action_id)
            else:
                risky_blocked.append(action_id)
        risky_actions = self._merge_string_lists(
            risky_failed,
            [*risky_blocked, *approval_required_actions],
        )
        return risky_actions, non_blocking_failed

    def _parse_final_result_output_contract(self, final_result_text: str) -> dict[str, Any] | None:
        execution_prompt = self._execution_prompt_context()
        output_schema = execution_prompt.get("output")
        if not isinstance(output_schema, Mapping) or not output_schema:
            return None
        output_format = str(execution_prompt.get("output_format") or "").strip().lower()
        if output_format in {"", "json_object", "application/json"}:
            output_format = "json"
        if output_format == "auto":
            output_format = "json"
        if output_format not in {"json", "flat_markdown", "hybrid", "xml_field", "yaml_literal"}:
            return None

        attempts: list[dict[str, Any]] = []
        for format_name in [output_format, *([] if output_format == "json" else ["json"])]:
            parsed, error = self._parse_final_result_by_format(
                final_result_text,
                output_schema=dict(output_schema),
                output_format=format_name,
            )
            attempt = {"format": format_name, "success": parsed is not None}
            if error:
                attempt["error"] = error
            attempts.append(attempt)
            if parsed is not None:
                return {
                    "parse_success": True,
                    "declared_format": output_format,
                    "resolved_format": format_name,
                    "attempts": attempts,
                    "fallback": (
                        {
                            "from": output_format,
                            "to": format_name,
                            "reason": attempts[0].get("error"),
                        }
                        if format_name != output_format
                        else None
                    ),
                }

        last_error = ""
        for attempt in reversed(attempts):
            if attempt.get("error"):
                last_error = str(attempt["error"])
                break
        return {
            "parse_success": False,
            "declared_format": output_format,
            "resolved_format": output_format,
            "attempts": attempts,
            "last_error": last_error,
        }

    @staticmethod
    def _parse_final_result_by_format(
        final_result_text: str,
        *,
        output_schema: dict[str, Any],
        output_format: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return parse_output_contract_dict(
            final_result_text,
            output_schema=output_schema,
            output_format=output_format,
        )

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @classmethod
    def _merge_string_lists(cls, *values: Any) -> list[str]:
        merged: list[str] = []
        for value in values:
            for item in cls._normalize_string_list(value):
                if item not in merged:
                    merged.append(item)
        return merged


__all__ = ["AgentTaskVerificationMixin"]
