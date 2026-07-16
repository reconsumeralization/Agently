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

from .LifecycleState import AgentTaskLifecycleState
from .TaskShared import *


class AgentTaskResumeMixin(AgentTaskMixinBase):
    @staticmethod
    def _resume_run_id(task_id: str) -> str:
        # Namespaced so resume snapshots never mix with the task's per-step
        # observation checkpoints under the bare task_id.
        return f"{ task_id }::resume"

    def _resume_manifest(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
            "max_iterations": self.max_iterations,
            "verify": self.verify,
            "context_profile": self.context_profile,
            "context_budget": DataFormatter.sanitize(self.context_budget),
            "limits": DataFormatter.sanitize(self.limits),
            "options": DataFormatter.sanitize(self.options),
        }

    async def _write_resume_snapshot(self, iteration_index: int, verification: dict[str, Any]) -> None:
        """Persist a resumable snapshot keyed by task_id after an iteration.

        Stores the task manifest, the last completed iteration, the bounded
        iteration summaries, the cumulative satisfied-capability sets, and the
        last verification outcome so a crashed task can continue (or report its
        terminal result) from a fresh process.
        """
        # AgentTask process state is run-local by default. Hosts explicitly opt
        # into durable Workspace recovery when cross-process resume is needed.
        if not bool(self._agent_task_option("workspace_recovery", False)):
            return
        try:
            await self._task_reference_catalog.activate_persistence(self.workspace)
            await self.workspace.put_snapshot(
                self._resume_run_id(self.id),
                DataFormatter.sanitize(
                    {
                        "resume_version": 1,
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "manifest": self._resume_manifest(),
                        "iterations_summary": self._iteration_prompt_summaries(),
                        "reflection_summaries": self._reflection_prompt_summaries(),
                        "satisfied_required_actions": sorted(self._satisfied_required_actions),
                        "satisfied_required_skills": sorted(self._satisfied_required_skills),
                        "satisfied_capabilities": sorted(self._satisfied_capabilities),
                        "satisfied_succeeded_actions": sorted(self._satisfied_succeeded_actions),
                        "failed_execution_shapes": sorted(self._failed_execution_shapes),
                        "task_reference_catalog": self._task_reference_catalog.snapshot(),
                        "terminal_convergence": self._terminal_convergence_state.snapshot(),
                        "lifecycle_state": self._lifecycle_state.to_dict(),
                        "last_verification": {
                            "is_complete": bool(verification.get("is_complete")),
                            "requires_block": bool(verification.get("requires_block")),
                            "status": "completed" if bool(verification.get("is_complete")) else "",
                            "accepted": bool(verification.get("is_complete")),
                            "artifact_status": "accepted" if bool(verification.get("is_complete")) else "",
                            "reason": str(verification.get("reason") or ""),
                            "final_result": str(verification.get("final_result") or ""),
                            "final_response": str(verification.get("final_response") or ""),
                            "missing_criteria": self._normalize_string_list(verification.get("missing_criteria")),
                        },
                    }
                ),
                step_id=f"iteration-{ iteration_index }",
            )
        except Exception as error:
            # Snapshot persistence must never break the task loop.
            self.diagnostics.setdefault("resume_snapshot_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                }
            )

    async def _write_taskboard_resume_snapshot(
        self,
        *,
        stage: str,
        tick_index: int,
        revision: Any,
        evidence_view: Mapping[str, Any],
        runtime_topology: Mapping[str, Any],
        acceptance_index: Mapping[str, Any] | None = None,
        handoff_projection: Mapping[str, Any] | None = None,
        terminal_reason: str | None = None,
        final_result: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a TaskBoard resumable snapshot keyed by task_id.

        The public resume surface stays AgentExecution/AgentTask-owned; this
        snapshot gives the TaskBoard coordinator enough cold state to expose a
        terminal result or continue from a board revision without repeating
        completed cards.
        """
        # See _write_resume_snapshot: TaskBoard ticks persist only when the host
        # explicitly requests cross-process recovery.
        if not bool(self._agent_task_option("workspace_recovery", False)):
            return
        final_result = final_result if isinstance(final_result, Mapping) else {}
        status = str(final_result.get("status") or self.status or "").strip().lower()
        accepted = bool(final_result.get("accepted"))
        reason = str(final_result.get("reason") or terminal_reason or "")
        final_result_text = str(final_result.get("final_result") or "")
        is_complete = accepted and status in {"completed", "success", "accepted"}
        requires_block = (not is_complete) and status in {
            "blocked",
            "failed",
            "error",
            "timed_out",
            "max_iterations",
            "partial",
        }
        try:
            effective_revision = TaskBoardRevision.from_value(revision)
            await self._task_reference_catalog.activate_persistence(self.workspace)
            await self.workspace.put_snapshot(
                self._resume_run_id(self.id),
                DataFormatter.sanitize(
                    {
                        "resume_version": 2,
                        "task_id": self.id,
                        "iteration": int(tick_index),
                        "manifest": self._resume_manifest(),
                        "iterations_summary": self._iteration_prompt_summaries(),
                        "reflection_summaries": self._reflection_prompt_summaries(),
                        "satisfied_required_actions": sorted(self._satisfied_required_actions),
                        "satisfied_required_skills": sorted(self._satisfied_required_skills),
                        "satisfied_capabilities": sorted(self._satisfied_capabilities),
                        "satisfied_succeeded_actions": sorted(self._satisfied_succeeded_actions),
                        "failed_execution_shapes": sorted(self._failed_execution_shapes),
                        "task_reference_catalog": self._task_reference_catalog.snapshot(),
                        "terminal_convergence": self._terminal_convergence_state.snapshot(),
                        "lifecycle_state": self._lifecycle_state.to_dict(),
                        "taskboard_state": {
                            "schema_version": "agent_task_taskboard_resume/v1",
                            "stage": stage,
                            "tick_index": int(tick_index),
                            "status": status or self.status,
                            "terminal_reason": terminal_reason,
                            "revision": effective_revision.to_dict(),
                            "evidence_view": evidence_view,
                            "acceptance_index": dict(acceptance_index or {}),
                            "handoff_projection": dict(handoff_projection or {}),
                            "runtime_topology": dict(runtime_topology),
                            "workspace_refs": DataFormatter.sanitize(self.workspace_refs),
                            "final_result": dict(final_result),
                        },
                        "last_verification": {
                            "is_complete": is_complete,
                            "requires_block": requires_block,
                            "status": status,
                            "accepted": accepted,
                            "artifact_status": final_result.get("artifact_status"),
                            "reason": reason,
                            "final_result": final_result_text,
                            "final_response": str(final_result.get("final_response") or ""),
                            "missing_criteria": self._normalize_string_list(final_result.get("missing_criteria")),
                        },
                    }
                ),
                step_id=f"taskboard-{stage}-{tick_index}",
            )
        except Exception as error:
            self.diagnostics.setdefault("resume_snapshot_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                }
            )

    @classmethod
    async def async_resume(
        cls: type[_AgentTaskT],
        agent: "BaseAgent",
        task_id: str,
        *,
        workspace: str | os.PathLike[str] | None = None,
    ) -> _AgentTaskT:
        """Rebuild an AgentTask from its latest durable snapshot.

        The returned task continues from the iteration after the last completed
        one (or, when the last snapshot was already terminal, exposes that
        terminal result without re-running). Completed iterations are not
        re-executed, so their side effects are not repeated; an iteration that
        was in flight at crash time is re-planned.
        """
        agent_any = cast(Any, agent)
        if workspace is not None:
            agent_any.use_workspace(workspace)
        bound_workspace = getattr(agent, "workspace", None)
        if bound_workspace is None:
            raise RuntimeError(
                "AgentTask.async_resume requires a Workspace binding. Pass workspace=... "
                "or call agent.use_workspace(...) before resuming."
            )
        state = await bound_workspace.get_snapshot(cls._resume_run_id(str(task_id)))
        manifest = state.get("manifest") if isinstance(state, dict) else None
        if not isinstance(manifest, dict) or not manifest.get("goal"):
            raise ValueError(f"No resumable AgentTask snapshot was found for task_id '{ task_id }'.")
        task = cast(
            _AgentTaskT,
            cast(Any, cls)(
                agent,
                goal=str(manifest.get("goal") or ""),
                success_criteria=list(manifest.get("success_criteria") or []),
                execution=cast(Any, manifest.get("execution_strategy", "auto")),
                workspace=workspace,
                max_iterations=_normalize_agent_task_max_iterations(manifest.get("max_iterations")),
                verify=cast(Any, manifest.get("verify", "before_done")),
                context_profile=str(manifest.get("context_profile", "auto")),
                context_budget=cast(Any, manifest.get("context_budget")),
                limits=cast(Any, manifest.get("limits")),
                options=cast(Any, manifest.get("options")),
                task_id=str(task_id),
            ),
        )
        task_any = cast(Any, task)
        task_reference_catalog = state.get("task_reference_catalog")
        if isinstance(task_reference_catalog, Mapping):
            task_any._task_reference_catalog = TaskReferenceCatalog.from_snapshot(
                str(task_id),
                task_reference_catalog,
            )
        terminal_convergence = state.get("terminal_convergence")
        if isinstance(terminal_convergence, Mapping):
            task_any._terminal_convergence_state = TerminalConvergenceState.from_snapshot(
                str(task_id),
                terminal_convergence,
            )
        lifecycle_state = state.get("lifecycle_state")
        if isinstance(lifecycle_state, Mapping):
            restored_lifecycle_state = AgentTaskLifecycleState.from_dict(lifecycle_state)
            if restored_lifecycle_state.task_id != str(task_id):
                raise ValueError("AgentTask lifecycle snapshot task_id does not match the resume target.")
            task_any._lifecycle_state = restored_lifecycle_state
        task_any._resumed_from_iteration = int(state.get("iteration") or 0)
        effective_execution_strategy = manifest.get("effective_execution_strategy")
        if effective_execution_strategy in {"flat", "taskboard"}:
            task_any.effective_execution_strategy = cast(
                AgentTaskEffectiveExecutionStrategy, effective_execution_strategy
            )
        task_shape_analysis = manifest.get("task_shape_analysis")
        if isinstance(task_shape_analysis, dict):
            task_any.task_shape_analysis = DataFormatter.sanitize(task_shape_analysis)
        taskboard_state = state.get("taskboard_state")
        if isinstance(taskboard_state, dict):
            task_any._resumed_taskboard_state = DataFormatter.sanitize(taskboard_state)
            try:
                task_any._resumed_from_iteration = int(
                    taskboard_state.get("tick_index") or task_any._resumed_from_iteration or 0
                )
            except (TypeError, ValueError):
                pass
        summaries = state.get("iterations_summary")
        task_any._resumed_iteration_summaries = list(summaries) if isinstance(summaries, list) else []
        reflection_summaries = state.get("reflection_summaries")
        if isinstance(reflection_summaries, list):
            task_any.reflections = [
                DataFormatter.sanitize(item) for item in reflection_summaries if isinstance(item, dict)
            ]
        task_any._satisfied_required_actions = set(cls._normalize_string_list(state.get("satisfied_required_actions")))
        task_any._satisfied_required_skills = set(cls._normalize_string_list(state.get("satisfied_required_skills")))
        task_any._satisfied_capabilities = set(cls._normalize_string_list(state.get("satisfied_capabilities")))
        task_any._satisfied_succeeded_actions = set(
            cls._normalize_string_list(state.get("satisfied_succeeded_actions"))
        )
        task_any._failed_execution_shapes = set(cls._normalize_string_list(state.get("failed_execution_shapes")))
        last_verification = state.get("last_verification")
        if isinstance(last_verification, dict):
            taskboard_should_retry = (
                isinstance(taskboard_state, dict)
                and manifest.get("effective_execution_strategy") == "taskboard"
                and bool(last_verification.get("requires_block"))
            )
            if not taskboard_should_retry:
                task_any._resumed_prior_result = cls._terminal_result_from_resume(
                    task_id=str(task_id),
                    resumed_from_iteration=task_any._resumed_from_iteration,
                    last_verification=last_verification,
                )
        return task

    def resume(self, *args: Any, **kwargs: Any):
        raise TypeError("Use the async classmethod AgentTask.async_resume(agent, task_id, ...).")

    @classmethod
    def _terminal_result_from_resume(
        cls,
        *,
        task_id: str,
        resumed_from_iteration: int,
        last_verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        if bool(last_verification.get("is_complete")):
            final_result = last_verification.get("final_result") or ""
            artifact_status = last_verification.get("artifact_status") or "accepted"
            return {
                "status": str(last_verification.get("status") or "completed") or "completed",
                "accepted": bool(last_verification.get("accepted", True)),
                "artifact_status": artifact_status,
                "task_id": task_id,
                "final_result": final_result,
                "final_response": cls._agent_task_user_final_response(
                    final=last_verification,
                    accepted=True,
                    artifact_status=str(artifact_status),
                    status=str(last_verification.get("status") or "completed") or "completed",
                    reason=str(last_verification.get("reason") or ""),
                    missing_criteria=last_verification.get("missing_criteria", []),
                    final_result=final_result,
                ),
                "iterations": resumed_from_iteration,
                "resumed": True,
            }
        if bool(last_verification.get("requires_block")):
            artifact_status = last_verification.get("artifact_status") or "blocked"
            reason = last_verification.get("reason") or "Verifier blocked the task."
            return {
                "status": str(last_verification.get("status") or "blocked") or "blocked",
                "accepted": False,
                "artifact_status": artifact_status,
                "task_id": task_id,
                "reason": reason,
                "final_response": cls._agent_task_user_final_response(
                    final=last_verification,
                    accepted=False,
                    artifact_status=str(artifact_status),
                    status=str(last_verification.get("status") or "blocked") or "blocked",
                    reason=str(reason),
                    missing_criteria=last_verification.get("missing_criteria", []),
                    final_result=last_verification.get("final_result") or "",
                ),
                "iterations": resumed_from_iteration,
                "resumed": True,
            }
        return None


__all__ = ["AgentTaskResumeMixin"]
