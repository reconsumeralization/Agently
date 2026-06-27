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
        cumulative_anchors = cls._cumulative_planner_evidence_anchors(previous_iterations)
        if cumulative_anchors:
            repair_context["available_evidence_anchors"] = cumulative_anchors
        return repair_context

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
        raw_execution_evidence_summary = self._execution_log_summary(execution_meta)
        raw_cumulative_evidence_summary = self._cumulative_execution_evidence_summary(execution_meta)
        trusted_workspace_artifacts = await self._trusted_workspace_artifacts_for_verifier(
            raw_cumulative_evidence_summary
        )
        evidence_summary = self._compact_verifier_evidence_summary(raw_execution_evidence_summary)
        cumulative_evidence_summary = self._compact_verifier_evidence_summary(raw_cumulative_evidence_summary)
        candidate_final_result = self._candidate_final_result_from_execution_result(execution_result)
        request = self.agent.create_temp_request()
        language_policy = self._language_policy()
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
                    execution_result,
                    max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
                ),
                "execution_meta": self._verification_execution_meta_summary(execution_meta, evidence_summary),
                "execution_evidence_summary": evidence_summary,
                "cumulative_execution_evidence_summary": cumulative_evidence_summary,
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
            "Use both execution_evidence_summary and cumulative_execution_evidence_summary; the final verification "
            "must account for evidence gathered in earlier iterations, not only the current write/finalize step. "
            "Use reflection_summaries as evaluator notes linked to evidence and verification; reflection records are not "
            "completion evidence by themselves. "
            "For source-grounded tasks, compare the candidate's factual claims, named sections, coverage mappings, "
            "quoted source titles, URLs, and artifact statements against verifier-visible evidence and bounded Action "
            "result previews. A citation, source URL, or file ref alone does not ground a mismatched claim; the claim "
            "must be supported by the referenced evidence content. When multiple same-site official sources are "
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
            "artifact body in trusted_workspace_artifacts.readback.content against visible source_refs, Action evidence, "
            "URLs, paths, and refs; a final_result path pointer alone is not enough to satisfy citation or provenance "
            "requirements. Do not ask a later step to read a Workspace artifact "
            "solely to paste its full content into final_result. If trusted artifact refs or readback are missing or "
            "too scoped to verify a material claim, keep is_complete=false and ask for scoped artifact readback or repair. "
            "When candidate_final_result contains a complete answer/report/artifact body that satisfies the criteria, "
            "use it as final_result. When the plan or success criteria require a Workspace artifact, accept only "
            "trusted Workspace write/readback refs from execution evidence; model-declared file_refs are diagnostics. "
            "If evidence is incomplete, set is_complete=false and explain failure_analysis and acceptance_delta: "
            "why the task is not accepted, which acceptance facts are missing or weak, and what evidence boundary "
            "blocked verification. The verifier does not choose tools, routes, execution shapes, or exact methods. "
            "repair_constraints and next_step_requirements are advisory compatibility fields only; keep them factual "
            "and do not turn them into a narrow tool script. Also include a short human-readable replan_instruction. "
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
        return self._normalize_verification(
            verification,
            execution_evidence_summary=raw_cumulative_evidence_summary,
            candidate_final_result=candidate_final_result,
        )

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
                        "bytes": self._coerce_non_negative_int(read_result.get("bytes")),
                        "sha256": str(read_result.get("sha256") or ""),
                        "read_bytes": self._coerce_non_negative_int(read_result.get("read_bytes")),
                        "truncated": bool(read_result.get("truncated")),
                        "content": (
                            self._truncate_prompt_text(content, _VERIFIER_PROMPT_VALUE_CHARS)
                            if isinstance(content, str)
                            else ""
                        ),
                    }
            artifacts.append(artifact)
            if len(artifacts) >= 4:
                break
        return DataFormatter.sanitize(artifacts)

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
    def _trusted_workspace_artifact_ref_summary(cls, ref: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            "path": str(ref.get("path") or ""),
            "bytes": cls._coerce_non_negative_int(ref.get("bytes")),
            "sha256": str(ref.get("sha256") or ""),
            "media_type": ref.get("media_type"),
            "content_kind": str(ref.get("content_kind") or ""),
            "role": str(ref.get("role") or ""),
            "source": str(ref.get("source") or ""),
            "truncated": bool(ref.get("truncated")),
            "read_bytes": cls._coerce_non_negative_int(ref.get("read_bytes")),
        }
        preview = ref.get("preview")
        if isinstance(preview, str) and preview:
            summary["preview"] = cls._truncate_prompt_text(preview, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        return DataFormatter.sanitize(summary)

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
        compact = cls._compact_verifier_prompt_value(context_pack, max_chars=_VERIFIER_PROMPT_VALUE_CHARS)
        return compact if isinstance(compact, dict) else {}

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
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=2400)
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
            "role",
            "label",
            "media_type",
            "size",
            "bytes",
            "sha256",
            "truncated",
            "available",
            "full_value_available",
            "path",
        )
        compact = {key: ref.get(key) for key in keep_keys if key in ref}
        if "preview" in ref:
            compact["preview"] = cls._compact_verifier_prompt_value(ref.get("preview"), max_chars=600)
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
            "result_preview_sha256",
        )
        compact: dict[str, Any] = {key: record.get(key) for key in keep_keys if key in record}
        if "result_preview" in record:
            compact["result_preview"] = cls._compact_action_preview_value(record.get("result_preview"), max_chars=5200)
        if isinstance(record.get("artifact_refs"), list):
            refs = record.get("artifact_refs") or []
            compact["artifact_refs"] = [cls._compact_artifact_ref_for_verifier(ref) for ref in refs[:4]]
            if len(refs) > 4:
                compact["artifact_refs"].append({"omitted": len(refs) - 4, "reason": "prompt_budget"})
        if record.get("file_refs"):
            compact["file_refs"] = cls._compact_verifier_prompt_value(record.get("file_refs"), max_chars=1000)
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
        if normalized["requires_block"]:
            normalized["is_complete"] = False
            guard_reasons.append("requires_block_true")
        if normalized["missing_criteria"]:
            normalized["is_complete"] = False
            guard_reasons.append("missing_criteria_present")
        execution_status = str(execution_evidence_summary.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            normalized["is_complete"] = False
            guard_reasons.append("execution_status_failed")
            execution_errors = execution_evidence_summary.get("errors", [])
            error_message = ""
            if isinstance(execution_errors, list) and execution_errors:
                first_error = execution_errors[0]
                if isinstance(first_error, dict):
                    error_message = str(first_error.get("message") or first_error.get("type") or "")
                else:
                    error_message = str(first_error)
            detail = f": {error_message}" if error_message else ""
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
        final_result_required = self._normalize_bool(verification.get("final_result_required"), default=False)
        normalized["final_result_required"] = final_result_required
        trusted_workspace_artifact_refs = self._trusted_workspace_artifact_refs_from_summary(execution_evidence_summary)
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
        risky_failed: list[str] = []
        non_blocking_failed: list[str] = []
        for action_id in failed_actions:
            if action_id in read_safe_actions and action_id not in required_actions:
                non_blocking_failed.append(action_id)
            else:
                risky_failed.append(action_id)
        risky_actions = self._merge_string_lists(
            risky_failed,
            [*blocked_actions, *approval_required_actions],
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
