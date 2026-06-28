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


class AgentTaskFlatStrategyMixin(AgentTaskMixinBase):
    async def _run_iteration(self, iteration_index: int) -> dict[str, Any]:
        try:
            if self._task_deadline_exceeded():
                return await self._terminate_timed_out(iteration_index, stage="plan")
            await self._emit_progress(
                iteration_index,
                "context",
                f"Iteration {iteration_index}: building a Workspace context pack for the task goal.",
            )
            await self._emit(f"agent_task.iteration.{iteration_index}.started", {"iteration": iteration_index})
            context_pack = await self._await_task_deadline(
                self._build_context(),
                stage="context",
            )
            await self._emit(f"agent_task.iteration.{iteration_index}.context", context_pack)
            await self._emit_snapshot(
                iteration_index,
                "context",
                {
                    "context_item_count": len(context_pack.get("items", [])),
                    "diagnostics": context_pack.get("diagnostics", {}),
                },
                message=(
                    f"Iteration {iteration_index}: context pack ready with "
                    f"{len(context_pack.get('items', []))} item(s)."
                ),
            )

            await self._emit_progress(
                iteration_index,
                "plan",
                f"Iteration {iteration_index}: asking the model to plan one bounded execution step.",
            )
            plan = self._normalize_step_plan(
                await self._await_task_deadline(
                    self._request_plan(iteration_index, context_pack),
                    stage="plan",
                )
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
        await self._record_phase(
            "planned",
            iteration=iteration_index,
            diagnostics={
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get(
                    "effective_execution_shape", plan.get("execution_shape", "direct")
                ),
                "step_instruction": plan.get("step_instruction", ""),
                "expected_evidence": plan.get("expected_evidence", ""),
                "rationale": plan.get("rationale", ""),
            },
        )
        await self._emit(f"agent_task.iteration.{iteration_index}.plan", plan)
        await self._emit_snapshot(
            iteration_index,
            "plan",
            {
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get(
                    "effective_execution_shape", plan.get("execution_shape", "direct")
                ),
                "step_instruction": plan.get("step_instruction", ""),
                "expected_evidence": plan.get("expected_evidence", ""),
                "rationale": plan.get("rationale", ""),
            },
            message=f"Iteration {iteration_index}: plan ready; next bounded step is selected.",
        )
        decision_ref = await self._record_decision(iteration_index, plan, context_pack)
        await self._emit(f"agent_task.iteration.{iteration_index}.decision", {"record": decision_ref})

        await self._emit_progress(
            iteration_index,
            "execute",
            f"Iteration {iteration_index}: executing the bounded step and collecting evidence.",
        )
        await self._record_phase(
            "executing",
            iteration=iteration_index,
            diagnostics={
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get(
                    "effective_execution_shape", plan.get("execution_shape", "direct")
                ),
                "step_instruction": plan.get("step_instruction", ""),
            },
        )
        try:
            execution_result, execution_meta = await self._await_task_deadline(
                self._execute_step(iteration_index, plan, context_pack),
                stage="execute",
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
        execution_failed = str(execution_meta.get("status") or "").strip().lower() in {
            "failed",
            "error",
            "timed_out",
            "blocked",
        }
        if execution_failed:
            self._record_failed_execution_shape(plan, execution_meta)
            execution_result, execution_meta = await self._maybe_run_acp_recovery(
                iteration_index,
                plan=plan,
                execution_result=execution_result,
                execution_meta=execution_meta,
            )
            execution_failed = str(execution_meta.get("status") or "").strip().lower() in {
                "failed",
                "error",
                "timed_out",
                "blocked",
            }
        execution_result = await self._deliver_workspace_artifact(
            execution_result,
            plan=plan,
            execution_meta=execution_meta,
            source=f"agent_task.iteration.{iteration_index}.workspace_artifact",
            context_pack=context_pack,
            iteration_index=iteration_index,
            allow_stream_draft=not execution_failed,
        )
        await self._emit_snapshot(
            iteration_index,
            "execution",
            {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
                "logs": self._execution_log_summary(execution_meta),
            },
            message=(
                f"Iteration {iteration_index}: bounded step failed; failure evidence was captured."
                if execution_failed
                else f"Iteration {iteration_index}: bounded step finished; execution evidence was captured."
            ),
        )
        observation_ref, checkpoint_ref = await self._record_observation(
            iteration_index,
            plan=plan,
            decision_ref=decision_ref,
            execution_result=execution_result,
            execution_meta=execution_meta,
        )
        step_reflection_ref = None
        if self._should_record_process_reflection("bounded_step", plan=plan):
            step_reflection_ref = await self._record_reflection(
                iteration_index,
                phase="bounded_step",
                subject_ref=observation_ref,
                summary=self._bounded_step_reflection_summary(
                    plan=plan,
                    execution_meta=execution_meta,
                    execution_failed=execution_failed,
                ),
            )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.observation",
            {"record": observation_ref, "checkpoint": checkpoint_ref},
        )
        await self._record_phase(
            "evidence_recorded",
            iteration=iteration_index,
            diagnostics={
                "observation_ref": observation_ref,
                "checkpoint_ref": checkpoint_ref,
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
            },
        )

        should_verify, verification_decision = self._should_request_flat_final_verification(
            execution_result,
            execution_meta,
        )
        if should_verify:
            await self._emit_progress(
                iteration_index,
                "verify",
                f"Iteration {iteration_index}: verifying the evidence against every success criterion.",
            )
            try:
                verification = await self._await_task_deadline(
                    self._request_verification(
                        iteration_index,
                        plan=plan,
                        execution_result=execution_result,
                        execution_meta=execution_meta,
                        context_pack=context_pack,
                    ),
                    stage="verify",
                )
            except _AgentTaskDeadlineExceeded as error:
                return await self._terminate_timed_out(
                    iteration_index,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
            verification_source = "independent_verifier"
        else:
            verification = self._flat_consumer_continuation_verification(
                execution_result,
                execution_meta,
                decision=verification_decision,
            )
            verification_source = "consumer_driven_continuation"
            await self._emit_progress(
                iteration_index,
                "continue",
                f"Iteration {iteration_index}: bounded step reported remaining work; the next iteration will consume its evidence.",
            )
        if bool(verification.get("is_complete")):
            missing_deliverables = await self._missing_required_workspace_deliverables()
            if missing_deliverables:
                self._guard_missing_required_deliverables(verification, missing_deliverables)
        await self._record_phase(
            "verified",
            iteration=iteration_index,
            diagnostics={
                "verification_source": verification_source,
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
                "missing_criteria": verification.get("missing_criteria", []),
                "final_result_present": bool(str(verification.get("final_result") or "").strip()),
            },
        )
        await self._record_phase(
            "guarded",
            iteration=iteration_index,
            diagnostics={
                "verification_source": verification_source,
                "guard_reasons": verification.get("guard_reasons", []),
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
            },
        )
        verification_ref = await self._record_verification(iteration_index, verification, observation_ref)
        verification_reflection_ref = None
        if self._should_record_process_reflection("major_node", plan=plan):
            verification_reflection_ref = await self._record_reflection(
                iteration_index,
                phase="major_node",
                subject_ref=verification_ref,
                summary=self._major_node_reflection_summary(verification=verification),
            )
        await self._emit_snapshot(
            iteration_index,
            "verification",
            {
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
                "verification_source": verification_source,
                "reason": verification.get("reason"),
                "missing_criteria": verification.get("missing_criteria", []),
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
                "replan_instruction": verification.get("replan_instruction", ""),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
            },
            message=(
                f"Iteration {iteration_index}: verification "
                f"{'passed' if verification.get('is_complete') else 'requires another step'}."
            ),
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.verification",
            {"verification": verification, "record": verification_ref},
        )

        iteration_record = {
            "iteration": iteration_index,
            "plan": plan,
            "decision_ref": decision_ref,
            "execution_meta": execution_meta,
            "observation_ref": observation_ref,
            "verification": verification,
            "verification_ref": verification_ref,
            "verification_source": verification_source,
            "reflection_refs": [ref for ref in (step_reflection_ref, verification_reflection_ref) if ref is not None],
            "context_item_count": len(context_pack.get("items", [])),
        }
        self.iterations.append(DataFormatter.sanitize(iteration_record))
        # The cumulative satisfied-capability sets are updated inside
        # _normalize_verification; persist a resumable snapshot for this
        # iteration so a crashed task can continue from the next iteration.
        await self._write_resume_snapshot(iteration_index, verification)

        if bool(verification.get("is_complete")):
            self.status = "completed"
            self.result = {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "task_id": self.id,
                "final_result": verification.get("final_result") or execution_result,
                "iterations": iteration_index,
                "verification": verification,
            }
            await self._emit_progress(
                iteration_index,
                "completed",
                f"Iteration {iteration_index}: all success criteria are satisfied; the task is complete.",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": True, "artifact_status": "accepted"},
            )
            await self._emit("agent_task.completed", self.result)
            return {"terminal": True, "status": self.status}

        if verification.get("requires_block"):
            self.status = "blocked"
            self.result = {
                "status": "blocked",
                "accepted": False,
                "artifact_status": "blocked",
                "task_id": self.id,
                "reason": verification.get("reason") or "Verifier blocked the task.",
                "iterations": iteration_index,
                "verification": verification,
            }
            await self._emit_progress(
                iteration_index,
                "blocked",
                f"Iteration {iteration_index}: verifier blocked the task because it cannot continue safely.",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": False, "artifact_status": "blocked"},
            )
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        if self.max_iterations is not None and iteration_index >= self.max_iterations:
            missing_capabilities = self._normalize_string_list(verification.get("missing_required_capabilities"))
            if missing_capabilities:
                self.status = "capability_unavailable"
                reason = (
                    "Task could not satisfy required capabilities before max_iterations: "
                    f"{', '.join(missing_capabilities)}."
                )
            else:
                self.status = "max_iterations"
                reason = verification.get("reason") or "Task did not pass verification before max_iterations."
            self.result = {
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "reason": reason,
                "iterations": iteration_index,
                "verification": verification,
            }
            if missing_capabilities:
                self.result["missing_required_capabilities"] = missing_capabilities
            await self._emit_progress(
                iteration_index,
                self.status,
                f"Iteration {iteration_index}: { reason }",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": False, "artifact_status": "partial"},
            )
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        await self._emit_progress(
            iteration_index,
            "replan",
            f"Iteration {iteration_index}: verifier found gaps; the next iteration will replan.",
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.replan",
            {
                "reason": verification.get("reason"),
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
                "replan_instruction": verification.get("replan_instruction"),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
                "replan_signals": verification.get("replan_signals", []),
                "verification_source": verification_source,
            },
        )
        await self._record_phase(
            "replanned",
            iteration=iteration_index,
            diagnostics={
                "verification_source": verification_source,
                "reason": verification.get("reason"),
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
                "replan_instruction": verification.get("replan_instruction"),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
                "replan_signals": verification.get("replan_signals", []),
            },
        )
        return {"terminal": False, "status": "continue"}

    def _should_request_flat_final_verification(
        self,
        execution_result: Any,
        execution_meta: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        status = str(execution_meta.get("status") or "").strip().lower()
        if status in {"failed", "error", "timed_out", "blocked"}:
            return True, {"reason": "execution_status_requires_verification", "status": status}
        if not isinstance(execution_result, Mapping):
            return True, {"reason": "non_mapping_execution_result"}
        remaining_work = self._normalize_string_list(execution_result.get("remaining_work"))
        ready_for_final_verification = execution_result.get("ready_for_final_verification")
        ready_is_explicit = "ready_for_final_verification" in execution_result
        ready_is_true = self._normalize_bool(ready_for_final_verification, default=True) is True
        if ready_is_explicit and not ready_is_true:
            return False, {
                "reason": "work_unit_not_ready_for_final_verification",
                "remaining_work": remaining_work,
            }
        if remaining_work:
            if ready_is_explicit and ready_is_true:
                return True, {
                    "reason": "explicit_ready_for_final_verification",
                    "remaining_work": remaining_work,
                }
            return False, {
                "reason": "work_unit_reports_remaining_work",
                "remaining_work": remaining_work,
            }
        return True, {"reason": "ready_for_final_verification"}

    def _flat_consumer_continuation_verification(
        self,
        execution_result: Any,
        execution_meta: Mapping[str, Any],
        *,
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_summary = self._cumulative_execution_evidence_summary(dict(execution_meta))
        remaining_work = []
        if isinstance(execution_result, Mapping):
            remaining_work = self._normalize_string_list(execution_result.get("remaining_work"))
        if not remaining_work:
            remaining_work = self._normalize_string_list(decision.get("remaining_work"))
        reason = "Current work unit produced intermediate evidence for the next Flat iteration to consume."
        if remaining_work:
            reason = "Current work unit reported remaining work for the next Flat iteration."
        raw_verification = {
            "is_complete": False,
            "requires_block": False,
            "reason": reason,
            "failure_analysis": reason,
            "acceptance_delta": remaining_work or ["A downstream Flat iteration must consume the new evidence."],
            "missing_criteria": [],
            "replan_instruction": "Plan the next bounded work unit using the previous observation evidence.",
            "repair_constraints": [],
            "next_step_requirements": remaining_work,
            "final_result_required": False,
            "final_result": "",
        }
        normalized = self._normalize_verification(
            raw_verification,
            execution_evidence_summary=raw_summary,
            candidate_final_result="",
        )
        normalized["verification_source"] = "consumer_driven_continuation"
        normalized["consumer_driven_sufficiency"] = {
            "consumer": "next_flat_iteration",
            "decision": DataFormatter.sanitize(dict(decision)),
        }
        return normalized

    async def _build_context(self) -> "WorkspaceContextPackage":
        try:
            return await self.workspace.build_context(
                goal=self.goal,
                scope={"task_id": self.id},
                budget=self.context_budget,
                profile=self.context_profile,
            )
        except Exception as error:
            fallback_reason: dict[str, Any] = {
                "type": error.__class__.__name__,
                "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "stage": "workspace.build_context",
            }
            self.diagnostics.setdefault("recall_fallbacks", []).append(fallback_reason)
            try:
                fallback = await self.workspace.build_context(
                    goal="",
                    scope={"task_id": self.id},
                    budget=self.context_budget,
                    profile=self.context_profile,
                )
            except Exception as fallback_error:
                # A failing recall backend must not break the task loop. Return an
                # empty context pack so planning continues with no recalled context.
                fallback_reason["fallback_error"] = {
                    "type": fallback_error.__class__.__name__,
                    "message": _compact_agent_task_error_message(
                        fallback_error, fallback=fallback_error.__class__.__name__
                    ),
                }
                return cast(
                    "WorkspaceContextPackage",
                    {
                        "goal": self.goal,
                        "profile": self.context_profile,
                        "items": [],
                        "omitted": [],
                        "diagnostics": {"fallback_reason": fallback_reason},
                    },
                )
            diagnostics = fallback.setdefault("diagnostics", {})
            diagnostics["fallback_reason"] = fallback_reason
            return fallback

    def _step_execution_policy(self) -> dict[str, Any]:
        agent_task_options = self.options.get("agent_task")
        effort = agent_task_options.get("effort") if isinstance(agent_task_options, dict) else None
        effort = effort if isinstance(effort, dict) else {}
        execution_policy = effort.get("execution")
        policy = dict(execution_policy) if isinstance(execution_policy, dict) else {}
        raw_step_plan = (
            str(policy.get("step_plan") or policy.get("step_execution") or policy.get("execution_shape") or "direct")
            .strip()
            .lower()
        )
        requested_step_plan = raw_step_plan
        if raw_step_plan in {"dynamic_task", "task_dag", "execution_dag", "dag"}:
            raw_step_plan = "direct"
            policy["step_plan_degraded_from"] = requested_step_plan
            policy["step_plan_degradation_reason"] = "task_dag_not_agent_execution_strategy"
        if raw_step_plan not in {"direct", "auto"}:
            raw_step_plan = "direct"
        effective_execution_strategy = self.effective_execution_strategy or (
            "flat" if self.execution_strategy == "flat" else self.execution_strategy
        )
        explicit_flat_strategy = self.execution_strategy == "flat"
        if explicit_flat_strategy:
            raw_step_plan = "direct"
        policy["step_plan"] = raw_step_plan
        policy["execution_strategy"] = self.execution_strategy
        policy["effective_execution_strategy"] = effective_execution_strategy
        if "max_tasks" not in policy and "max_plan_items" in policy:
            policy["max_tasks"] = policy.get("max_plan_items")
        policy["allow_dag_steps"] = False
        failed_dag_shapes = sorted(self._failed_execution_shapes.intersection(_DEGRADED_DAG_STEP_EXECUTION_SHAPES))
        if failed_dag_shapes:
            policy["suppressed_execution_shapes"] = failed_dag_shapes
        return policy

    def _execution_prompt_context(self) -> dict[str, Any]:
        raw = self.options.get("execution_prompt_snapshot")
        if not isinstance(raw, Mapping):
            return {}
        return cast(dict[str, Any], DataFormatter.sanitize(dict(raw)))

    def _language_policy(self) -> dict[str, Any]:
        raw_policy = self._agent_task_option("language_policy", None)
        if raw_policy is None:
            raw_policy = self._agent_task_option("language", None)
        if raw_policy is None:
            raw_policy = language_policy_from_prompt_snapshot(self.options.get("execution_prompt_snapshot"))
        if raw_policy is None:
            getter = getattr(getattr(self.agent, "settings", None), "get", None)
            if callable(getter):
                raw_policy = getter("agent.language_policy", None)
        progress_language = self._agent_task_option("progress_language", None) or self._agent_task_option(
            "stream_progress_language", None
        )
        if isinstance(raw_policy, Mapping):
            return dict(resolve_language_policy(base=raw_policy, progress_language=progress_language))
        return dict(resolve_language_policy(raw_policy or "auto", progress_language=progress_language))

    def _apply_language_policy_to_request(self, request: Any, policy: Mapping[str, Any] | None = None) -> None:
        apply_language_policy_to_prompt(getattr(request, "prompt", request), policy or self._language_policy())

    def _required_workspace_deliverables(self) -> list[str]:
        paths: list[str] = []

        def add_path(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in paths:
                paths.append(text)

        def add_deliverables(value: Any) -> None:
            if isinstance(value, str):
                add_path(value)
                return
            if isinstance(value, Mapping):
                path = value.get("path") or value.get("file") or value.get("name")
                if path is not None:
                    add_path(path)
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    add_deliverables(item)

        def add_contract(value: Any) -> None:
            if not isinstance(value, Mapping):
                add_deliverables(value)
                return
            add_deliverables(value.get("deliverables"))
            add_deliverables(value.get("required_deliverables"))

        add_contract(self._agent_task_option("output_contract", None))
        add_deliverables(self._agent_task_option("required_deliverables", None))
        add_deliverables(self._agent_task_option("deliverables", None))

        execution_prompt = self._execution_prompt_context()
        add_contract(execution_prompt.get("output_contract"))
        prompt_input = execution_prompt.get("input")
        if isinstance(prompt_input, Mapping):
            add_contract(prompt_input.get("output_contract"))
            add_deliverables(prompt_input.get("required_deliverables"))
            case = prompt_input.get("case")
            if isinstance(case, Mapping):
                add_contract(case.get("output_contract"))
        return paths

    async def _missing_required_workspace_deliverables(self) -> list[str]:
        missing: list[str] = []
        for path in self._required_workspace_deliverables():
            try:
                read_result = await self.workspace.read_file(path, max_bytes=1)
            except Exception:
                missing.append(path)
                continue
            try:
                size = int(read_result.get("bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            if size <= 0:
                missing.append(path)
        return missing

    def _guard_missing_required_deliverables(
        self,
        verification: dict[str, Any],
        missing_deliverables: Sequence[str],
    ) -> None:
        if not missing_deliverables:
            return
        message = "Missing required Workspace deliverable(s): " + ", ".join(str(item) for item in missing_deliverables)
        verification["is_complete"] = False
        verification["final_result_required"] = True
        verification["missing_criteria"] = self._merge_string_lists(
            verification.get("missing_criteria"),
            [message],
        )
        verification["acceptance_delta"] = self._merge_string_lists(
            verification.get("acceptance_delta"),
            [message],
        )
        verification["guard_reasons"] = self._merge_string_lists(
            verification.get("guard_reasons"),
            ["required_workspace_deliverable_missing"],
        )
        if not str(verification.get("failure_analysis") or "").strip():
            verification["failure_analysis"] = message
        if not str(verification.get("replan_instruction") or "").strip():
            verification["replan_instruction"] = (
                "Write and read back the required Workspace deliverable before accepting completion."
            )

    def _normalize_step_plan(self, plan: Any) -> dict[str, Any]:
        normalized: dict[str, Any]
        if isinstance(plan, dict):
            normalized = plan
        else:
            normalized = {"step_instruction": str(plan), "expected_evidence": "", "rationale": ""}
        raw_shape = (
            normalized.get("execution_shape")
            or normalized.get("step_kind")
            or normalized.get("route")
            or normalized.get("route_hint")
            or "direct"
        )
        shape = self._normalize_step_execution_shape(raw_shape)
        normalized["execution_shape"] = shape
        normalized.setdefault("effective_execution_shape", shape)
        normalized.setdefault("step_instruction", "")
        normalized.setdefault("expected_evidence", "")
        normalized.setdefault("rationale", "")
        # Structured step scope (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # scope comes from explicit capability lists, never from parsing the
        # natural-language step_instruction. `allowed_action_ids` is retained as an
        # internal alias for the action-id enforcement seam.
        raw_scope = normalized.get("step_scope")
        if not isinstance(raw_scope, dict):
            raw_scope = {}
        allowed_capability_ids = self._normalize_string_list(
            raw_scope.get("allowed_capability_ids") or normalized.get("allowed_action_ids")
        )
        normalized["step_scope"] = {"allowed_capability_ids": allowed_capability_ids}
        normalized["allowed_action_ids"] = allowed_capability_ids
        scoped_retrieval = self._normalize_scoped_retrieval_plan(normalized.get("scoped_retrieval"))
        if scoped_retrieval:
            normalized["scoped_retrieval"] = scoped_retrieval
        else:
            normalized.pop("scoped_retrieval", None)
        self._normalize_step_deliverable_mode(normalized)
        return normalized

    @classmethod
    def _normalize_scoped_retrieval_plan(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return {}
        raw_queries = raw.get("query_groups", raw.get("queries", raw.get("query")))
        if raw_queries is None:
            query_values: list[Any] = []
        elif isinstance(raw_queries, str):
            query_values = [raw_queries]
        elif isinstance(raw_queries, Sequence) and not isinstance(raw_queries, (bytes, bytearray)):
            query_values = list(raw_queries)
        else:
            query_values = [raw_queries]
        query_groups: list[dict[str, Any]] = []
        for item in query_values:
            if isinstance(item, Mapping):
                query = str(item.get("query") or item.get("text") or item.get("keyword") or "").strip()
                expected_role = str(item.get("expected_role") or item.get("role") or "").strip()
                candidate: dict[str, Any] = {
                    "query": query,
                    "expected_role": expected_role if expected_role in {"evidence_snippet", "locator_ref"} else "",
                }
                for key in ("path", "pattern", "collection", "kind"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        candidate[key] = cls._normalize_scoped_retrieval_pattern(value) if key == "pattern" else value
                surface = str(item.get("search_surface") or item.get("surface") or "").strip()
                if surface in {"workspace_index", "workspace_files", "workspace_index_and_files", "files"}:
                    candidate["search_surface"] = "workspace_files" if surface == "files" else surface
                for key in ("max_results", "snippet_limit", "snippet_offset", "max_file_bytes", "context_lines"):
                    value = item.get(key)
                    if value is not None:
                        candidate[key] = DataFormatter.sanitize(value)
                if item.get("include_hidden") is not None:
                    candidate["include_hidden"] = bool(item.get("include_hidden"))
                filters = item.get("filters")
                if isinstance(filters, Mapping):
                    candidate["filters"] = DataFormatter.sanitize(dict(filters))
                content_queries = cls._scoped_retrieval_content_queries(candidate.get("filters"))
            else:
                query = str(item or "").strip()
                candidate = {"query": query, "expected_role": ""}
                content_queries = []
            if not query and not content_queries:
                continue
            if not candidate.get("expected_role"):
                candidate.pop("expected_role", None)
            if content_queries:
                template = dict(candidate)
                filters = template.get("filters")
                if isinstance(filters, Mapping):
                    filtered = dict(filters)
                    filtered.pop("content_contains", None)
                    if filtered:
                        template["filters"] = filtered
                    else:
                        template.pop("filters", None)
                for content_query in content_queries:
                    expanded = dict(template)
                    expanded["query"] = content_query
                    query_groups.append(expanded)
                    if len(query_groups) >= 8:
                        break
                if len(query_groups) >= 8:
                    break
                continue
            query_groups.append(candidate)
            if len(query_groups) >= 8:
                break
        if not query_groups:
            return {}
        raw_fallback_order = raw.get("fallback_order") or raw.get("fallbacks")
        if isinstance(raw_fallback_order, str):
            fallback_values = [raw_fallback_order]
        elif isinstance(raw_fallback_order, Sequence) and not isinstance(
            raw_fallback_order, (bytes, bytearray)
        ):
            fallback_values = list(raw_fallback_order)
        else:
            fallback_values = []
        fallback_order: list[str] = []
        for item in fallback_values:
            text = str(item or "").strip()
            if text:
                fallback_order.append(text)
        normalized: dict[str, Any] = {"query_groups": query_groups}
        if fallback_order:
            normalized["fallback_order"] = fallback_order[:8]
        return normalized

    @staticmethod
    def _normalize_scoped_retrieval_pattern(value: str) -> str:
        text = str(value or "").strip()
        if "," in text:
            return "**"
        return text

    @staticmethod
    def _scoped_retrieval_content_queries(filters: Any) -> list[str]:
        if not isinstance(filters, Mapping):
            return []
        raw_terms = filters.get("content_contains")
        if isinstance(raw_terms, str):
            values = [raw_terms]
        elif isinstance(raw_terms, Sequence) and not isinstance(raw_terms, (bytes, bytearray)):
            values = list(raw_terms)
        else:
            values = []
        queries: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in queries:
                queries.append(text)
        return queries[:8]

    def _normalize_step_deliverable_mode(self, plan: dict[str, Any]) -> None:
        raw_mode = str(plan.get("deliverable_mode") or "").strip().lower().replace("-", "_")
        mode_aliases = {
            "": "",
            "none": "",
            "inline": "inline_final",
            "inline_answer": "inline_final",
            "final_answer": "inline_final",
            "file": "workspace_artifact",
            "file_backed": "workspace_artifact",
            "artifact": "workspace_artifact",
            "workspace": "workspace_artifact",
            "sectioned": "sectioned_workspace_artifact",
            "sectioned_artifact": "sectioned_workspace_artifact",
            "sectioned_workspace": "sectioned_workspace_artifact",
        }
        normalized_mode = mode_aliases.get(raw_mode, raw_mode)
        if normalized_mode not in {"", "inline_final", "workspace_artifact", "sectioned_workspace_artifact"}:
            normalized_mode = ""

        required_deliverables = self._required_workspace_deliverables()
        if required_deliverables and normalized_mode in {"", "inline_final"}:
            plan["deliverable_mode"] = "sectioned_workspace_artifact"
            plan["deliverable_mode_source"] = "required_workspace_deliverables"
            plan.setdefault("required_workspace_deliverables", required_deliverables)
            plan.setdefault("prefer_stream_draft", True)
            return
        if normalized_mode:
            plan["deliverable_mode"] = normalized_mode
            plan.setdefault("deliverable_mode_source", "planner")

    @staticmethod
    def _normalize_step_execution_shape(value: Any) -> str:
        text = str(value or "direct").strip().lower().replace("-", "_")
        aliases = {
            "model": "direct",
            "model_request": "direct",
            "direct_request": "direct",
            "flat": "direct",
            "flat_react": "direct",
            "action": "actions",
            "tool": "actions",
            "tools": "actions",
            "skill": "skills",
            "dag": "dynamic_task",
            "task_dag": "dynamic_task",
            "dynamic_task_dag": "dynamic_task",
            "agent_execution_dag": "execution_dag",
        }
        normalized = aliases.get(text, text)
        return normalized if normalized in _STEP_EXECUTION_SHAPES else "direct"

    def _configure_step_execution(self, execution: Any, plan: dict[str, Any]) -> dict[str, Any]:
        policy = self._step_execution_policy()
        requested_shape = str(plan.get("execution_shape") or "direct")
        effective_shape = requested_shape
        dag_allowed = False
        warning: str | None = None

        dag_shape_degraded = requested_shape in _DEGRADED_DAG_STEP_EXECUTION_SHAPES
        if dag_shape_degraded:
            effective_shape = "direct"
            warning = "dag_shape_not_agent_execution_strategy"

        plan["effective_execution_shape"] = effective_shape
        # Structured step scope (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # when the plan names an explicit capability allowlist, narrow this step's
        # action candidates to it via the execution-local action-id seam. Scope
        # comes from the structured step_scope field, never from parsing the
        # step_instruction prose. The hard guarantee remains the verifier evidence
        # gate; this only prevents an evidence-gathering step from silently
        # completing the whole task with unrelated capabilities.
        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        allowed_capability_ids = self._normalize_string_list(step_scope.get("allowed_capability_ids"))
        if allowed_capability_ids and effective_shape in {"direct", "actions"}:
            local_action_ids = getattr(execution, "local_action_ids", None)
            if isinstance(local_action_ids, list):
                for capability_id in allowed_capability_ids:
                    if capability_id not in local_action_ids:
                        local_action_ids.append(capability_id)
            sync_action_scope = getattr(execution, "_sync_action_scope", None)
            if callable(sync_action_scope):
                sync_action_scope(source="AgentTaskLoop.step_scope")
        action_scope_source = "step_scope" if allowed_capability_ids else ""
        if effective_shape == "actions" and not allowed_capability_ids:
            action_capability_ids = [
                str(item.get("id") or "").strip()
                for item in self._planner_capabilities()
                if isinstance(item, Mapping)
                and str(item.get("kind") or "").strip() == "action"
                and str(item.get("id") or "").strip()
            ]
            if action_capability_ids:
                use_actions = getattr(execution, "use_actions", None)
                if callable(use_actions):
                    use_actions(action_capability_ids)
                    action_scope_source = "planner_capabilities"
        step_execution = {
            "requested_shape": requested_shape,
            "effective_shape": effective_shape,
            "dag_allowed": dag_allowed,
            "dag_shape_degraded": dag_shape_degraded,
            "step_scope": DataFormatter.sanitize(step_scope),
            "action_scope_source": action_scope_source,
            "policy": DataFormatter.sanitize(policy),
        }
        route_policy = self._route_policy_for_step_execution(effective_shape)
        if route_policy:
            apply_route_policy = getattr(execution, "route_policy", None)
            if callable(apply_route_policy):
                apply_route_policy(route_policy)
            step_execution["route_policy"] = DataFormatter.sanitize(route_policy)
        if warning is not None:
            step_execution["warning"] = warning
            execution_warnings = plan.get("execution_warnings")
            if not isinstance(execution_warnings, list):
                execution_warnings = []
            execution_warnings.append(warning)
            plan["execution_warnings"] = execution_warnings
            self.diagnostics.setdefault("step_execution_warnings", []).append(
                {"iteration_shape": requested_shape, "warning": warning}
            )
        plan["step_execution"] = step_execution
        record_option = getattr(execution, "record_consumed_option", None)
        if callable(record_option):
            record_option("agent_task.step.execution_shape", effective_shape, owner="AgentTaskLoop")
            if route_policy:
                record_option("agent_task.step.route_policy", route_policy, owner="AgentTaskLoop")
            if policy.get("step_plan") != "direct":
                record_option("effort.execution.step_plan", policy.get("step_plan"), owner="AgentTaskLoop")
        return step_execution

    @staticmethod
    def _route_policy_for_step_execution(effective_shape: str) -> dict[str, Any]:
        route_by_shape = {
            "direct": "model_request",
            "actions": "model_request",
            "skills": "skills",
        }
        route = route_by_shape.get(str(effective_shape or "").strip())
        if route is None:
            return {}
        return {
            "allowed_routes": [route],
            # A bounded step that cannot honor its selected shape must not silently
            # run model_request: block so the loop sees the mismatch and replans.
            "on_violation": "block",
            "owner": "AgentTaskLoop",
            "step_execution_shape": effective_shape,
        }

    def _planner_capabilities(self) -> list[dict[str, Any]]:
        """Planner-facing capability candidate snapshot (inert data only).

        Read from the typed snapshot the orchestrator route injected into options
        at task construction (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC).
        Covers AgentTaskLoop executable actions, skills, and skill packs as one
        capability list. AgentTask consumes only this snapshot; it does not reach
        back into the routing plugin.
        """
        raw = self.options.get("planner_capabilities")
        if not isinstance(raw, list):
            return []
        capabilities: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            capability_id = str(item.get("id") or item.get("capability_id") or "").strip()
            if not capability_id:
                continue
            kind = str(item.get("kind") or "action")
            if kind == "dynamic_task":
                continue
            entry: dict[str, Any] = {
                "id": capability_id,
                "kind": kind,
                "route": str(item.get("route") or "model_request"),
                "guidance_access": str(item.get("guidance_access") or "none"),
                "description": str(item.get("description") or ""),
            }
            if item.get("mode"):
                entry["mode"] = str(item.get("mode"))
            if "side_effect_level" in item:
                entry["side_effect_level"] = str(item.get("side_effect_level") or "")
            if "replay_safe" in item:
                entry["replay_safe"] = self._normalize_bool(item.get("replay_safe"), default=False)
            capabilities.append(entry)
        return capabilities

    def _capability_evidence_requirements(self) -> list[dict[str, Any]]:
        """Structured, authored completion-evidence requirements (inert data).

        The load-bearing gate's trigger
        (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC): which capabilities
        must appear in execution evidence for the task to be acceptable. Authored
        as a structured option, independent of capability mode; never inferred
        from free-text criteria. Accepts either a list of capability-id strings
        (treated as `capability_used`) or a list of EvidenceRequirement dicts. The
        legacy `skill_evidence_requirements` option is read as a fallback alias.
        """
        raw = self.options.get("capability_evidence_requirements")
        if raw is None:
            raw = self.options.get("skill_evidence_requirements")
        if isinstance(raw, dict):
            raw = raw.get("capabilities") or raw.get("skills")
        if not isinstance(raw, (list, tuple)):
            return []
        requirements: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                capability_id = item.strip()
                if capability_id:
                    requirements.append(
                        {
                            "capability_id": capability_id,
                            "kind": "capability_used",
                            "required": True,
                            "source": "criterion",
                        }
                    )
                continue
            if not isinstance(item, dict):
                continue
            capability_id = str(item.get("capability_id") or item.get("id") or "").strip()
            if not capability_id:
                continue
            requirement: dict[str, Any] = {
                "capability_id": capability_id,
                "kind": str(item.get("kind") or "capability_used"),
                "required": bool(item.get("required", True)),
                "source": str(item.get("source") or "criterion"),
            }
            if item.get("capability_kind"):
                requirement["capability_kind"] = str(item.get("capability_kind"))
            if item.get("criterion_id"):
                requirement["criterion_id"] = str(item.get("criterion_id"))
            requirements.append(requirement)
        return requirements

    def _untried_read_action_continuation(
        self,
        execution_evidence_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        read_action_ids = {
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
        if not read_action_ids:
            return {}
        used_action_ids = set(self._normalize_string_list(execution_evidence_summary.get("action_ids")))
        used_action_ids.update(self._satisfied_capabilities)
        untried_action_ids = sorted(read_action_ids - used_action_ids)
        if not untried_action_ids:
            return {}
        blocked_actions = self._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
        approval_required_actions = self._normalize_string_list(
            execution_evidence_summary.get("approval_required_actions")
        )
        if blocked_actions or approval_required_actions:
            return {}
        failed_actions = self._normalize_string_list(execution_evidence_summary.get("failed_actions"))
        unsafe_failed_actions = [action_id for action_id in failed_actions if action_id not in read_action_ids]
        if unsafe_failed_actions:
            return {}
        return {
            "reason": "read_action_continuation_available",
            "untried_action_ids": untried_action_ids,
            "failed_read_action_ids": sorted(action_id for action_id in failed_actions if action_id in read_action_ids),
        }

    def _evaluate_capability_evidence(self) -> tuple[list[str], list[dict[str, Any]]]:
        """Deterministically check structured evidence requirements.

        Returns (missing_capability_ids, unenforced_requirements). Checks run
        against capability evidence accumulated across iterations
        (`_satisfied_capabilities` for `capability_used`,
        `_satisfied_succeeded_actions` for `action_succeeded`). Only the wired
        combinations are enforced; anything without a structural producer
        (the reserved evidence kinds, and `capability_used` for a
        `dynamic_task` capability whose usage is not recorded in evidence) is
        returned as an unenforced diagnostic rather than silently passing or
        false-failing.
        """
        requirements = self._capability_evidence_requirements()
        if not requirements:
            return [], []

        missing: list[str] = []
        unenforced: list[dict[str, Any]] = []
        for requirement in requirements:
            if not requirement.get("required", True):
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if not capability_id:
                continue
            kind = str(requirement.get("kind") or "capability_used")
            capability_kind = str(requirement.get("capability_kind") or "")
            if kind == "capability_used" and capability_kind != "dynamic_task":
                if capability_id not in self._satisfied_capabilities:
                    missing.append(capability_id)
            elif kind == "action_succeeded":
                if capability_id not in self._satisfied_succeeded_actions:
                    missing.append(capability_id)
            else:
                unenforced.append(
                    {
                        "task_id": self.id,
                        "capability_id": capability_id,
                        "kind": kind,
                        "capability_kind": capability_kind,
                    }
                )
        # De-duplicate while preserving order.
        deduped: list[str] = []
        for capability_id in missing:
            if capability_id not in deduped:
                deduped.append(capability_id)
        return deduped, unenforced

    async def _request_plan(self, iteration_index: int, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        request = self.agent.create_temp_request()
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
        planner_capabilities = self._planner_capabilities()
        execution_prompt = self._execution_prompt_context()
        previous_iterations = self._iteration_prompt_summaries()
        repair_context = self._planner_repair_context(previous_iterations)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract(),
                "iteration": iteration_index,
                "previous_iterations": previous_iterations,
                "repair_context": repair_context,
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": execution_prompt,
                "execution_policy": self._step_execution_policy(),
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
                "planner_capabilities": planner_capabilities,
                "retrieval_policy": scoped_retrieval_policy(),
                "language_policy": language_policy,
            }
        )
        # Explanatory note only (not a guarantee): the hard guarantee is the
        # verifier evidence gate, not this prompt text. It tells the planner which
        # capabilities exist and that some kinds (e.g. a Skill's guidance) load
        # only on their own route.
        capability_note = (
            " Available capabilities are listed in planner_capabilities, each with a kind "
            "(action/skill/skill_pack), the execution_shape route that exposes it, and "
            "guidance_access. A capability whose guidance_access is route_context (such as a Skill's "
            "guidance) only reaches the model on its own route, so choose that execution_shape when such "
            "a capability is the intended way to satisfy a criterion."
            if planner_capabilities
            else ""
        )
        allowed_shapes = "direct or actions" if self.execution_strategy == "flat" else "direct, actions, or skills"
        strategy_note = (
            " The selected execution_strategy is flat: keep the task in a linear AgentTask loop and do not plan DAG or TaskBoard steps."
            if self.execution_strategy == "flat"
            else ""
        )
        request.instruct(
            "Plan the next bounded AgentExecution step for this AgentTask. "
            "Treat execution_prompt as caller-provided task context, including any input, instructions, and output contract. "
            "Use task_context_contract.current_time for current-time facts and current/latest/as-of source boundaries, "
            "and use task_context_contract for ref-backed "
            "intermediate-resource handling. It is not a resource cap. "
            "Use prior verification evidence when present. Do not finalize unless all success criteria can be verified. "
            "When repair_context is present, use it as verification feedback: understand why prior work was incomplete, "
            "compare the acceptance delta, and then choose the next bounded step. The verifier does not choose tools, "
            "routes, execution shapes, or exact methods; the planner owns the next action while respecting grounded "
            "acceptance facts and deterministic guards. When repair_context.available_evidence_anchors is present, "
            "use its exact source_refs values and action_result_previews as the bounded evidence anchor set for repair; "
            "do not shorten, infer, or reconstruct URLs, file paths, or source refs from source titles or prose feedback. "
            "source_refs with content_state='ref_only' prove only discovery or materialization; read the referenced "
            "file/ref before using its content for repository, document, or source-grounded claims. "
            "For web discovery tasks, if the task context already names an official domain, homepage, or URL and "
            "search results are empty, unstable, or inconclusive, plan a Browse step for that known entry point and "
            "follow same-site navigation links before concluding that the required source is unavailable. Search "
            "result snippets are discovery hints, not source evidence. Before using a search result snippet or a broad "
            "announcement page as the source boundary, plan Browse/readback for the candidate page and relevant same-site "
            "index/list/download/navigation pages so a more specific official source can be discovered. "
            f"Set execution_shape to {allowed_shapes}. "
            "Do not plan TaskDAG, DynamicTask, or DAG-shaped execution here; TaskDAG is an independent "
            "manual/configured or visual-automation orchestration surface, not an AgentTaskLoop strategy."
            + strategy_note
            + capability_note
            + " Optionally set step_scope.allowed_capability_ids to limit this bounded step to specific capability "
            "ids when it is only meant to gather evidence; leave it empty when the step may use any available capability."
            " For Workspace, repository, or file-backed evidence, prefer scoped retrieval before bulk reads when it can "
            "reduce prompt input. If useful, return scoped_retrieval.query_groups with prioritized exact phrases or "
            "natural search text plus expected_role='evidence_snippet' or 'locator_ref'. Search/read executors only "
            "record bounded facts; the planner/verifier must judge semantic usefulness after seeing snippets or readbacks. "
            "Set query_group.search_surface to 'workspace_index' for SQLite/FTS records, 'workspace_files' for bounded "
            "file grep-style search, or 'workspace_index_and_files' when both surfaces are worth the bounded cost. For "
            "workspace_files, query is the content text to search, path is the directory or file scope, and pattern is a "
            "file glob such as '*.md' or '*' rather than another content keyword. "
            "When the task context names a concrete Workspace collection, kind, path, or scope for the relevant records, "
            "carry record collections as filters.collection; carry record kinds as filters.kind only when the exact kind "
            "is provided, never by guessing a generic kind such as 'note'; carry file scopes as path/pattern so scoped "
            "search targets task evidence "
            "instead of framework planning, checkpoint, verification, or reflection records."
            " For long, sectioned, or prose-heavy deliverables, separate the content-carrier decision from the "
            "control/evidence contract. A single freeform document can be drafted as natural Markdown/plain text. When "
            "field boundaries are required, preserve the caller's declared .output(..., format=...) contract such as "
            "xml_field, hybrid, or yaml_literal instead of forcing the long body into compact JSON fields. Keep status, "
            "evidence, and verification as separate compact judgment/readback contracts. If this AgentTask step must "
            "deliver through Workspace, choose deliverable_mode='workspace_artifact' or 'sectioned_workspace_artifact' and "
            "instruct the execution step to return only a Workspace artifact_manifest path plus a section outline. Do not ask "
            "the execution step to put complete deliverable bodies inside artifact_markdown, answer, final_result, "
            "candidate_final_result, or artifact_manifest section content; AgentTask will stream the long body into Workspace "
            "and read it back. The model must not self-declare trusted file_refs for a deliverable."
        )
        request.output(
            {
                "execution_shape": (
                    str,
                    "Execution shape for this bounded step: direct, actions, or skills",
                    False,
                ),
                "step_instruction": (str, "Instruction for one bounded AgentExecution step", True),
                "expected_evidence": (str, "Evidence this step should produce", True),
                "rationale": (str, "Why this is the next step", True),
                "deliverable_mode": (
                    str,
                    "inline_final, workspace_artifact, or sectioned_workspace_artifact for expected deliverables",
                    False,
                ),
                "step_scope": (
                    dict,
                    "Optional structured scope: {allowed_capability_ids: [...]}; empty means no restriction",
                    False,
                ),
                "scoped_retrieval": (
                    dict,
                    "Optional retrieval plan: {query_groups: [{query, expected_role, search_surface?, path?, pattern?, filters?, max_results?, snippet_limit?, max_file_bytes?}], fallback_order?: [...]}; executors return facts only",
                    False,
                ),
            },
            format="json",
        )
        plan = await self._await_task_request(request.async_get_data(), stage="plan")
        return self._normalize_step_plan(plan)

    async def _execute_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> tuple[Any, dict[str, Any]]:
        override = self._step_stage_override("_execute_step")
        if override is not None:
            result = override(iteration_index, plan, context_pack)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
            return cast(tuple[Any, dict[str, Any]], result)

        plan = self._normalize_step_plan(plan)
        work_unit = self._build_flat_work_unit_intent(iteration_index, plan, context_pack)

        async def run_agent_step(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            scoped_retrieval_results = self._scoped_retrieval_results_from_block_context(_context)
            execution_result, execution_meta = await self._run_bounded_agent_execution_step(
                iteration_index,
                plan,
                context_pack,
                carrier_output_policy=self._carrier_output_policy_from_block_context(_context),
                scoped_retrieval_results=scoped_retrieval_results,
            )
            return {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta": DataFormatter.sanitize(execution_meta),
                "scoped_retrieval_results": DataFormatter.sanitize(scoped_retrieval_results),
            }

        try:
            execution_result, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:iter-{iteration_index}",
                handler=run_agent_step,
                start_payload={
                    "task_id": self.id,
                    "iteration": iteration_index,
                    "plan": DataFormatter.sanitize(plan),
                    "context_pack": DataFormatter.sanitize(context_pack),
                },
            )
        except Exception as error:
            result, failed_meta = self._failed_execution_result(
                iteration_index,
                plan=plan,
                error=error,
                execution_id=f"{self.id}:iter-{iteration_index}:blocks-step",
            )
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.failed",
                {"execution_meta": failed_meta},
            )
            await self._record_phase(
                "execution_failed",
                iteration=iteration_index,
                diagnostics={
                    "execution_id": failed_meta.get("execution_id"),
                    "route": failed_meta.get("route"),
                    "error": failed_meta.get("diagnostics", {}).get("execution_error"),
                    "work_unit": work_unit.to_dict(),
                },
            )
            return result, failed_meta

        self._reconcile_effective_shape(plan, execution_meta)
        status = str(execution_meta.get("status") or "").strip().lower()
        if status not in {"failed", "error", "timed_out", "blocked"}:
            await self._emit(f"agent_task.iteration.{iteration_index}.execution.completed", execution_meta)
        return execution_result, cast(dict[str, Any], execution_meta)

    async def _run_bounded_agent_execution_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
        *,
        carrier_output_policy: Mapping[str, Any] | None = None,
        scoped_retrieval_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        plan = self._normalize_step_plan(plan)
        execution = self._create_bounded_child_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}",
                "step_id": "execute",
                "scope": {"strategy_phase": "agent_task_execution_step"},
            },
        )
        step_execution = self._configure_step_execution(execution, plan)
        language_policy = self._language_policy()
        try:
            result, meta = await self._run_bounded_child_execution(
                execution=execution,
                language_policy=language_policy,
                input_payload={
                    "task_id": self.id,
                    "goal": self.goal,
                    "success_criteria": self.success_criteria,
                    "task_context_contract": self._task_context_contract(),
                    "iteration": iteration_index,
                    "plan": DataFormatter.sanitize(plan),
                    "step_execution": step_execution,
                    "execution_strategy": self.execution_strategy,
                    "effective_execution_strategy": self.effective_execution_strategy,
                    "context_pack": DataFormatter.sanitize(context_pack),
                    "execution_prompt": self._execution_prompt_context(),
                    "retrieval_policy": scoped_retrieval_policy(),
                    "scoped_retrieval": DataFormatter.sanitize(plan.get("scoped_retrieval", {})),
                    "scoped_retrieval_results": DataFormatter.sanitize(list(scoped_retrieval_results or ())),
                    "language_policy": language_policy,
                },
                instruction=(
                    "Execute exactly one bounded step for the AgentTask. "
                    f"Use the selected execution shape: {step_execution.get('effective_shape', 'direct')}. "
                    f"The AgentTask requested execution_strategy is {self.execution_strategy}; "
                    f"the effective execution_strategy is {self.effective_execution_strategy or self.execution_strategy}. "
                    "Respect the caller-provided execution_prompt context and output contract when present. "
                    "Use task_context_contract.current_time when the task asks for current/latest/as-of evidence, and "
                    "keep downloads, web snapshots, notes, generated code, and large extracted text as refs until scoped "
                    "readback is needed. "
                    "Return concrete evidence for the verifier. If this step produces the requested final answer, report, "
                    "file body, or artifact body, put the complete candidate deliverable in candidate_final_result instead "
                    "of burying the only copy inside evidence when it fits the bounded output. If the plan deliverable_mode "
                    "is workspace_artifact or sectioned_workspace_artifact, return only an artifact_manifest with path and "
                    "section outline for long or multi-section deliverables; use artifact_markdown only for bounded short "
                    "bodies. Do not put the full long body in artifact_manifest section content, answer, "
                    "candidate_final_result, or final_result. AgentTask will stream/write/read back Workspace files and "
                    "produce trusted file_refs. "
                    "Do not invent file_refs for deliverables. "
                    "For web-source steps, treat Search results as discovery hints only. Browse official pages and follow "
                    "same-site index/list/download/navigation links before relying on a broad announcement page as the "
                    "source boundary. "
                    "For repository or file-source steps, a clone/list manifest path is ref_only; read the specific "
                    "file or artifact before making claims about its content. "
                    "When scoped_retrieval.query_groups is present, try the prioritized scoped search before broad "
                    "reads; use evidence_snippet results as bounded source text and locator_ref results only as targets "
                    "for later bounded readback. If scoped_retrieval_results is present, those are already executed "
                    "Blocks/Workspace search facts for the current step; inspect them before choosing broader reads. "
                    "Do not treat a local search hit as semantic acceptance by itself. "
                    "Do not claim final completion unless evidence supports it. "
                    "Use remaining_work for task-level work that the next Flat iteration should consume or perform. "
                    "Non-empty remaining_work defaults to intermediate and skips terminal verification for this work "
                    "unit. Set ready_for_final_verification=false to make that explicit, or true only when this work "
                    "unit intentionally needs terminal, blocking, or risk verification now."
                    + self._bounded_step_carrier_instruction(carrier_output_policy)
                ),
                output_schema=self._bounded_step_output_schema(carrier_output_policy),
                output_format=self._carrier_control_output_format(carrier_output_policy),
                use_output=self._carrier_uses_control_output(carrier_output_policy),
                carrier_output_policy=carrier_output_policy,
                started_event=f"agent_task.iteration.{iteration_index}.execution.started",
                started_payload={"step_execution": step_execution},
                stream_bridge=lambda child_execution: self._bridge_step_execution_stream(
                    iteration_index, child_execution
                ),
            )
        except Exception as error:
            child_meta = await self._read_child_execution_meta(execution)
            result, failed_meta = self._failed_execution_result(
                iteration_index,
                plan=plan,
                error=error,
                execution_id=str(getattr(execution, "id", "") or "") or None,
                child_meta=child_meta,
            )
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.failed",
                {"execution_meta": failed_meta},
            )
            await self._record_phase(
                "execution_failed",
                iteration=iteration_index,
                diagnostics={
                    "execution_id": failed_meta.get("execution_id"),
                    "route": failed_meta.get("route"),
                    "error": failed_meta.get("diagnostics", {}).get("execution_error"),
                },
            )
            return result, failed_meta
        return result, cast(dict[str, Any], meta)

    @staticmethod
    def _bounded_step_output_schema(carrier_output_policy: Mapping[str, Any] | None) -> dict[str, Any]:
        if (
            isinstance(carrier_output_policy, Mapping)
            and str(carrier_output_policy.get("body_transport") or "") == "workspace_artifact"
            and carrier_output_policy.get("body_uses_output") is False
        ):
            return {
                "step_result": (
                    str,
                    "Concise status and evidence summary for this bounded step; do not include the artifact body",
                    True,
                ),
                "artifact_manifest": (
                    dict,
                    "Optional Workspace artifact manifest with path and section outline only; no full body content and no file_refs",
                    False,
                ),
                "evidence": ([str], "Evidence produced by the step", True),
                "remaining_work": (
                    [str],
                    "Task-level remaining work for the next Flat iteration; non-empty values skip terminal verification unless ready_for_final_verification is explicitly true",
                ),
                "ready_for_final_verification": (
                    bool,
                    "False when the next Flat iteration should consume this output before terminal verification; true only for terminal, blocking, or risk verification now",
                    False,
                ),
            }
        return {
            "step_result": (str, "Concrete result of this bounded step", True),
            "candidate_final_result": (
                str,
                "Complete answer/report/artifact body produced by this step when it may satisfy the final task",
                False,
            ),
            "artifact_markdown": (
                str,
                "Short markdown deliverable body when this step creates one and it fits bounded output",
                False,
            ),
            "artifact_manifest": (
                dict,
                "Workspace artifact manifest for file-backed or sectioned deliverables",
                False,
            ),
            "file_refs": (
                [dict],
                "Existing evidence refs only; deliverable refs become trusted only after AgentTask Workspace write/readback",
                False,
            ),
            "evidence": ([str], "Evidence produced by the step", True),
            "remaining_work": (
                [str],
                "Task-level remaining work for the next Flat iteration; non-empty values skip terminal verification unless ready_for_final_verification is explicitly true",
            ),
            "ready_for_final_verification": (
                bool,
                "False when the next Flat iteration should consume this output before terminal verification; true only for terminal, blocking, or risk verification now",
                False,
            ),
        }

    @staticmethod
    def _bounded_step_carrier_instruction(carrier_output_policy: Mapping[str, Any] | None) -> str:
        if (
            isinstance(carrier_output_policy, Mapping)
            and str(carrier_output_policy.get("body_transport") or "") == "workspace_artifact"
            and carrier_output_policy.get("body_uses_output") is False
        ):
            return (
                " This work unit uses a Workspace artifact carrier: return compact control data only. "
                "Use artifact_manifest for the target path and section outline when the artifact is ready, "
                "and keep the full prose body out of structured output fields."
            )
        return ""

    async def _bridge_step_execution_stream(self, iteration_index: int, execution: Any) -> None:
        try:
            async for item in execution.get_async_generator(type="instant"):
                await self._emit_step_execution_stream_item(iteration_index, execution, item)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.diagnostics.setdefault("stream_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "iteration": iteration_index,
                    "stage": "execution",
                    "child_execution_id": str(getattr(execution, "id", "") or ""),
                }
            )

    async def _emit_step_execution_stream_item(
        self,
        iteration_index: int,
        execution: Any,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        item_meta = getattr(item, "meta", None)
        meta: dict[str, Any] = {
            "task_id": self.id,
            "status": self.status,
            "iteration": iteration_index,
            "stage": "execution",
            "stream_kind": "child_execution",
            "child_execution_id": str(getattr(execution, "id", "") or ""),
            "child_path": raw_path,
            "child_source": str(getattr(item, "source", "") or ""),
            "child_route": str(getattr(item, "route", "") or ""),
        }
        if isinstance(item_meta, Mapping):
            meta["child_meta"] = DataFormatter.sanitize(dict(item_meta))
        return await self._emit(
            f"agent_task.iteration.{iteration_index}.execution.{raw_path}",
            getattr(item, "value", None),
            event_type=event_type,
            delta=getattr(item, "delta", None),
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            meta=meta,
        )

    def _step_stage_override(self, stage_name: str):
        overrides = getattr(self, "_agent_task_step_overrides", None)
        if not isinstance(overrides, dict):
            return None
        handler = overrides.get(stage_name)
        return handler if callable(handler) else None

    def _record_failed_execution_shape(self, plan: dict[str, Any], execution_meta: dict[str, Any]) -> None:
        route = execution_meta.get("route", {})
        route_name = ""
        if isinstance(route, dict):
            route_name = str(route.get("selected_route") or "")
        shape = self._shape_for_route(route_name) if route_name else ""
        if not shape:
            shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "")
        shape = self._normalize_step_execution_shape(shape)
        if shape:
            self._failed_execution_shapes.add(shape)


__all__ = ["AgentTaskFlatStrategyMixin"]
