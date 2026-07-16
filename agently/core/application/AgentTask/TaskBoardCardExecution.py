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
    async def _try_taskboard_preplanned_action_calls(
        self,
        context: Any,
        *,
        raw_commands_override: Any = None,
        command_source: str = "taskboard_plan",
        action_planning_model_requests: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        card = getattr(context, "card", None)
        if self._taskboard_card_execution_shape(card) != "actions":
            return None
        metadata = getattr(card, "metadata", None)
        raw_commands = (
            raw_commands_override
            if raw_commands_override is not None
            else metadata.get("action_commands")
            if isinstance(metadata, Mapping)
            else None
        )
        if raw_commands in (None, [], ()):
            return None
        card_id = str(getattr(card, "id", "") or "")
        return await self._execute_bounded_action_commands(
            raw_commands=raw_commands,
            required_action_ids=self._taskboard_card_required_action_ids(card),
            execution_id=f"{self.id}:taskboard:{card_id or 'card'}:action-call",
            code_prefix="taskboard.action_commands",
            execution_kind="taskboard_preplanned_action_calls",
            command_source=command_source,
            action_planning_model_requests=action_planning_model_requests,
            unit_label="TaskBoard",
            todo_suggestion="Finish this bounded TaskBoard action card after execution.",
        )

    def _taskboard_preplanned_action_failure(
        self,
        *,
        card_id: str,
        code: str,
        message: str,
        command_source: str = "taskboard_plan",
        action_planning_model_requests: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        diagnostic = {
            "code": code,
            "message": message,
            "execution_kind": "taskboard_preplanned_action_calls",
            "command_source": command_source,
            "action_planning_model_requests": action_planning_model_requests,
        }
        return (
            {
                "status": "failed",
                "answer": "",
                "remaining_work": [message],
                "diagnostics": [diagnostic],
            },
            {
                "execution_id": f"{self.id}:taskboard:{card_id or 'card'}:action-call",
                "status": "failed",
                "route": {"selected_route": "action_call", "status": "failed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": [diagnostic]},
                "diagnostics": [diagnostic],
            },
        )

    async def _try_taskboard_narrow_action_command_request(
        self,
        context: Any,
        *,
        card_input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Resolve unknown kwargs once, then dispatch through ActionRuntime directly."""

        card = getattr(context, "card", None)
        if self._taskboard_card_execution_shape(card) != "actions":
            return None
        metadata = getattr(card, "metadata", None)
        if isinstance(metadata, Mapping) and metadata.get("action_commands") not in (None, [], ()):
            return None

        required_action_ids = self._taskboard_card_required_action_ids(card)
        if not required_action_ids:
            return None

        action_contracts, unavailable_action_id = self._bounded_action_contracts(
            required_action_ids
        )
        if unavailable_action_id is not None:
            return self._taskboard_preplanned_action_failure(
                card_id=str(getattr(card, "id", "") or ""),
                code="taskboard.action_commands.required_action_unavailable",
                message=f"Required TaskBoard Action '{unavailable_action_id}' is unavailable.",
                command_source="taskboard_action_command_request",
                action_planning_model_requests=0,
            )

        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "card": {
                    "id": str(getattr(card, "id", "") or ""),
                    "objective": str(getattr(card, "objective", "") or ""),
                    "done_when": list(getattr(card, "done_when", ()) or ()),
                    "required_outputs": list(getattr(card, "required_outputs", ()) or ()),
                },
                "dependency_results": DataFormatter.sanitize(
                    card_input_payload.get("dependency_results", {})
                ),
            }
        )
        request.info(
            {
                "available_actions": action_contracts,
                "required_action_ids": required_action_ids,
            }
        )
        request.instruct(
            "Produce the complete bounded Action command batch for this one TaskBoard card. "
            "Use only offered action_id values and exact kwargs defined by each Action contract. "
            "Use dependency_results for arguments that became available from upstream cards. "
            "Do not execute Actions, synthesize the whole task, invent placeholders, or request another planning round. "
            "Include every required_action_id at least once; repeated calls are allowed only when distinct inputs are "
            "required by this card."
        )
        request.output(
            {
                "action_commands": (
                    [
                        {
                            "purpose": (str, "Bounded purpose for this exact Action call.", True),
                            "action_id": (str, "Exact offered Action id.", True),
                            "action_input": (dict, "Complete kwargs for the Action contract.", True),
                        }
                    ],
                    "Complete Action command batch for this card.",
                    True,
                )
            },
            format="json",
        )
        await self._emit(
            f"agent_task.taskboard.card.{self._stream_path_token(str(getattr(card, 'id', '') or 'card'))}.action_commands.started",
            {
                "card_id": str(getattr(card, "id", "") or ""),
                "required_action_ids": required_action_ids,
            },
        )
        raw = await self._await_taskboard_card_execution(
            request.async_get_data(),
            card_id=str(getattr(card, "id", "") or ""),
            stage="action_commands",
        )
        raw_commands = raw.get("action_commands") if isinstance(raw, Mapping) else None
        if raw_commands in (None, [], ()):
            return self._taskboard_preplanned_action_failure(
                card_id=str(getattr(card, "id", "") or ""),
                code="taskboard.action_commands.empty_model_result",
                message="The TaskBoard Action command request returned no commands.",
                command_source="taskboard_action_command_request",
                action_planning_model_requests=1,
            )
        return await self._try_taskboard_preplanned_action_calls(
            context,
            raw_commands_override=raw_commands,
            command_source="taskboard_action_command_request",
            action_planning_model_requests=1,
        )

    async def _try_taskboard_workspace_artifact_action_transfer(
        self,
        context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Lower an exact Workspace artifact handoff to direct Action calls.

        This path is intentionally structural: one upstream Workspace file, one
        exact final path, and registered Workspace write/read Actions. It does
        not infer paths or choose among competing source artifacts.
        """

        card = getattr(context, "card", None)
        if self._taskboard_card_execution_shape(card) != "actions":
            return None
        metadata = getattr(card, "metadata", None)
        if not isinstance(metadata, Mapping):
            return None
        target_paths = self._normalize_string_list(metadata.get("final_workspace_deliverables"))
        if len(target_paths) != 1:
            return None
        target_path = target_paths[0]
        try:
            self.workspace.resolve_file_path(target_path)
        except Exception:
            return None

        action_requirements = self._taskboard_card_action_requirements(card)
        required_card_action_ids = self._taskboard_card_required_action_ids(card)
        registry = getattr(getattr(self.agent, "action", None), "action_registry", None)
        get_spec = getattr(registry, "get_spec", None)
        if not callable(get_spec):
            return None

        def action_spec(action_id: str) -> Mapping[str, Any] | None:
            spec = get_spec(action_id)
            return spec if isinstance(spec, Mapping) else None

        def is_workspace_write(action_id: str) -> bool:
            spec = action_spec(action_id)
            meta = spec.get("meta") if isinstance(spec, Mapping) else None
            return bool(
                isinstance(meta, Mapping)
                and str(meta.get("component") or "") == "workspace"
                and meta.get("write") is True
                and isinstance(spec.get("kwargs"), Mapping)
                and {"path", "content"}.issubset(spec["kwargs"])
            )

        write_action_ids = [action_id for action_id in required_card_action_ids if is_workspace_write(action_id)]
        if len(write_action_ids) != 1:
            return None

        source_paths: list[str] = []
        dependency_results = getattr(context, "dependency_results", None)
        if not isinstance(dependency_results, Mapping):
            return None
        for dependency_result in dependency_results.values():
            for collection_name in ("file_refs", "artifact_refs"):
                refs = getattr(dependency_result, collection_name, ())
                if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
                    continue
                for ref in refs:
                    if not isinstance(ref, Mapping) or ref.get("available", True) is False:
                        continue
                    path = str(ref.get("path") or "").strip()
                    if path and path != target_path and path not in source_paths:
                        source_paths.append(path)
        if len(source_paths) != 1:
            return None
        source_path = source_paths[0]

        try:
            source_info = self.workspace.inspect_file(source_path)
            source_size = int(source_info.get("size") or source_info.get("bytes") or 0)
            source_read = await self.workspace.read_file(
                source_path,
                max_bytes=max(source_size + 1, 1),
            )
        except Exception as error:
            return self._taskboard_direct_artifact_action_failure(
                card_id=str(getattr(card, "id", "") or ""),
                source_path=source_path,
                target_path=target_path,
                action_logs=[],
                error=error,
            )
        source_content = source_read.get("content")
        if not isinstance(source_content, str) or source_read.get("truncated") is True:
            return self._taskboard_direct_artifact_action_failure(
                card_id=str(getattr(card, "id", "") or ""),
                source_path=source_path,
                target_path=target_path,
                action_logs=[],
                error=RuntimeError("The source Workspace artifact is not a complete text body."),
            )

        write_action_id = write_action_ids[0]
        write_records = await self.agent.action._async_execute_action_calls(
            action_calls=[
                {
                    "action_id": write_action_id,
                    "action_input": {"path": target_path, "content": source_content, "append": False},
                    "purpose": f"Materialize {target_path} from the completed TaskBoard artifact.",
                    "source_protocol": "taskboard_action_call",
                }
            ],
            settings=self.agent.settings,
            agent_name=self.agent.name,
        )
        action_logs = [dict(record) for record in write_records if isinstance(record, Mapping)]
        write_result = action_logs[0] if action_logs else None

        task_required_action_ids = self._task_contract_required_action_ids()

        def is_workspace_read(action_id: str) -> bool:
            spec = action_spec(action_id)
            meta = spec.get("meta") if isinstance(spec, Mapping) else None
            return bool(
                isinstance(meta, Mapping)
                and str(meta.get("component") or "") == "workspace"
                and str(spec.get("side_effect_level") or "").strip().lower() == "read"
                and isinstance(spec.get("kwargs"), Mapping)
                and "path" in spec["kwargs"]
            )

        read_action_ids = sorted(
            action_id for action_id in task_required_action_ids if is_workspace_read(action_id)
        )
        write_succeeded = self._taskboard_direct_action_succeeded(write_result)
        if write_succeeded:
            read_records = await self.agent.action._async_execute_action_calls(
                action_calls=[
                    {
                        "action_id": read_action_id,
                        "action_input": {
                            "path": target_path,
                            "max_bytes": max(source_size + 1, 1),
                            "offset": 0,
                        },
                        "purpose": f"Read back {target_path} after TaskBoard materialization.",
                        "source_protocol": "taskboard_action_call",
                    }
                    for read_action_id in read_action_ids
                ],
                settings=self.agent.settings,
                agent_name=self.agent.name,
            )
            for read_result in read_records:
                if isinstance(read_result, Mapping):
                    action_logs.append(dict(read_result))

        all_succeeded = bool(action_logs) and all(
            self._taskboard_direct_action_succeeded(record) for record in action_logs
        )
        if not all_succeeded:
            return self._taskboard_direct_artifact_action_failure(
                card_id=str(getattr(card, "id", "") or ""),
                source_path=source_path,
                target_path=target_path,
                action_logs=action_logs,
                error=RuntimeError("A required Workspace artifact Action did not succeed."),
            )

        target_info = dict(self.workspace.inspect_file(target_path))
        target_info.update({"path": target_path, "role": "workspace_artifact", "available": True})
        execution_id = f"{self.id}:taskboard:{getattr(card, 'id', 'card')}:action-call"
        return (
            {
                "status": "completed",
                "answer": f"Workspace artifact materialized at {target_path} and read back.",
                "artifact_manifest": {"path": target_path},
                "file_refs": [DataFormatter.sanitize(target_info)],
                "evidence": [f"Action {record.get('action_id')} succeeded." for record in action_logs],
                "remaining_work": [],
                "diagnostics": [],
            },
            {
                "execution_id": execution_id,
                "status": "success",
                "route": {"selected_route": "action_call", "status": "completed"},
                "logs": {"action_logs": action_logs, "route_logs": {}, "errors": []},
                "diagnostics": [
                    {
                        "execution_kind": "taskboard_workspace_artifact_action_transfer",
                        "source_path": source_path,
                        "target_path": target_path,
                        "action_planning_model_requests": 0,
                    }
                ],
            },
        )

    @staticmethod
    def _taskboard_direct_action_succeeded(record: Any) -> bool:
        if not isinstance(record, Mapping):
            return False
        return record.get("ok") is True or str(record.get("status") or "").strip().lower() in {
            "success",
            "succeeded",
            "ok",
            "completed",
            "partial_success",
        }

    def _taskboard_direct_artifact_action_failure(
        self,
        *,
        card_id: str,
        source_path: str,
        target_path: str,
        action_logs: Sequence[Mapping[str, Any]],
        error: BaseException,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        execution_id = f"{self.id}:taskboard:{card_id or 'card'}:action-call"
        diagnostic = {
            "execution_kind": "taskboard_workspace_artifact_action_transfer",
            "source_path": source_path,
            "target_path": target_path,
            "action_planning_model_requests": 0,
            "error": str(error),
        }
        return (
            {
                "status": "failed",
                "answer": "",
                "remaining_work": [str(error)],
                "diagnostics": [diagnostic],
            },
            {
                "execution_id": execution_id,
                "status": "failed",
                "route": {"selected_route": "action_call", "status": "failed"},
                "logs": {"action_logs": [dict(record) for record in action_logs], "route_logs": {}, "errors": [diagnostic]},
                "diagnostics": [diagnostic],
            },
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
        skill_context_readbacks = self._taskboard_skill_context_readbacks(
            context.card,
            context_pack,
        )
        skill_readback_evidence_items = self._taskboard_skill_context_readback_evidence_items(
            skill_context_readbacks,
            card_id=str(getattr(context.card, "id", "") or ""),
        )
        raw_evidence_items = evidence_view.get("evidence_items", [])
        combined_evidence_view = {
            "evidence_items": [
                *(
                    list(raw_evidence_items)
                    if isinstance(raw_evidence_items, Sequence)
                    and not isinstance(raw_evidence_items, str | bytes | bytearray)
                    else []
                ),
                *skill_readback_evidence_items,
            ]
        }
        evidence_ledger = self._stable_evidence_ledger_view(combined_evidence_view, max_items=80, body_chars=1800)
        prompt_evidence_ledger = self._model_evidence_ledger_projection(
            self._stable_evidence_ledger_view(
                combined_evidence_view,
                max_items=64,
                body_chars=1200,
                budget_selection="content_first",
                max_overflow_refs=64,
            ),
            max_items=64,
        )
        readback_records = self._taskboard_action_artifact_recall_records(evidence_view)
        dependency_readbacks = await self._taskboard_dependency_action_artifact_readbacks(
            evidence_view,
            card_id=str(getattr(context.card, "id", "") or ""),
            context_pack=context_pack,
        )
        card_value = context.card.to_dict()
        card_metadata = (
            card_value.get("metadata")
            if isinstance(card_value.get("metadata"), Mapping)
            else {}
        )
        done_when = card_value.get("done_when") or card_metadata.get("done_when") or []
        if isinstance(done_when, str):
            done_when = [done_when]
        work_unit_boundary = {
            "card_id": str(card_value.get("id") or ""),
            "objective": str(
                card_value.get("objective") or card_value.get("title") or ""
            ),
            "done_when": [
                str(item)
                for item in done_when
                if str(item or "").strip()
            ],
            "whole_task_completion_out_of_scope": True,
        }
        task_orientation_payload = {
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "context_pack": DataFormatter.sanitize(context_pack),
            "execution_prompt": DataFormatter.sanitize(
                self._execution_prompt_context()
            ),
        }
        max_attempts = self._taskboard_card_max_attempts()
        previous_errors: list[dict[str, Any]] = []
        language_policy = self._language_policy()
        for attempt_index in range(1, max_attempts + 1):
            carrier_plan = self._taskboard_card_carrier_plan(context.card)
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
                    "step_execution_shape": str(
                        carrier_plan.get("effective_execution_shape")
                        or carrier_plan.get("execution_shape")
                        or "auto"
                    ),
                },
                recall_records=cast(Sequence[Mapping[str, Any]], readback_records),
                recall_source="AgentTaskTaskBoard.evidence_view",
            )
            self._configure_step_execution(execution, carrier_plan)
            if self._taskboard_card_execution_shape(context.card) == "actions":
                # A TaskBoard action card already owns the bounded work unit.
                # Plan its Action commands once, execute them, and let the same
                # child request produce the card result; do not ask ActionLoop
                # for a second "response or execute" decision after success.
                self._apply_child_execution_action_loop_guard(execution, max_rounds=1)
            source_refs = self._collect_taskboard_source_refs(
                evidence_ledger,
                evidence_view,
                dependency_readbacks,
                context.dependency_results,
                max_refs=_TASKBOARD_SOURCE_REFS_MAX,
            )
            card_input_payload = {
                "task_id": self.id,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "card": card_value,
                "work_unit_boundary": work_unit_boundary,
                "dependency_results": self._compact_taskboard_dependency_results(context.dependency_results),
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_prompt(evidence_view),
                "evidence_ledger": prompt_evidence_ledger,
                "dependency_readbacks": dependency_readbacks,
                "skill_context_readbacks": DataFormatter.sanitize(skill_context_readbacks),
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
                "language_policy": language_policy,
            }
            card_instruction = (
                "Execute exactly one TaskBoard card. "
                "work_unit_boundary is authoritative for this Action-planning run. "
                "Do not execute sibling objectives merely because task orientation describes the whole task. "
                "Finish this run when the card objective and done_when facts are satisfied. "
                "Provide short card_intent and decision_basis fields before the card result fields to frame this "
                "card-local decision; do not include raw chain-of-thought or hidden reasoning. "
                "Use task_context_contract.current_time only when the card needs current/latest/as-of evidence; label older "
                "or historical source material with its time boundary. Do not treat the runtime/current date as a "
                "business fact, incident date, deployment date, publication date, approval date, or validation date "
                "unless the goal or verifier-visible evidence explicitly provides it. "
                "taskboard_evidence_view is the compact evidence summary; request full content only through available "
                "Workspace or Action refs when needed. If previous_attempt_errors is non-empty, avoid repeating "
                "the same failing source or method when a bounded fallback can satisfy the card. dependency_readbacks "
                "contains bounded readback previews for dependency Action artifacts that were "
                "structurally truncated or marked full_value_available; inspect those before declaring dependency "
                "evidence missing. skill_context_readbacks contains only the already-loaded Skill guidance and "
                "resources selected for this card; apply that context directly without treating its citations as "
                "Workspace paths. If available_readback lists Action artifact refs and the prefetched previews are "
                "still insufficient, call read_action_artifact with the host-issued selection_key before blocking "
                "on missing evidence. If scoped_retrieval_results is present, those are already executed bounded "
                "Workspace search facts; use visible evidence_snippet content only within the excerpt, and treat "
                "locator_ref records as targets for later readback/search rather than source-content proof. "
                "Treat evidence_ledger as the authoritative grounding ledger for dependency evidence. Use only an "
                "exact offered evidence_ledger.items[].reference_id in evidence_use.evidence_ids; no other prompt "
                "field is an evidence identity. failed/empty items support unavailable or missing-data claims "
                "only; ref_only items support only discovery/ref-pointer claims until readback evidence exists. "
                "Return card-local evidence and remaining work. If the card's original method fails but equivalent evidence or a bounded fallback "
                "is available, return status completed with diagnostics that explain the degraded source boundary. "
                "For a final-verification repair card, preserve valid stable bindings from "
                "card.evidence_contract.prior_final_evidence_use and change only bindings or claims implicated by the repair contract. "
                "Only return failed or blocked when the card cannot produce the required outcome or the missing "
                "evidence is truly critical. If this card produces the user-facing deliverable, provide the complete "
                "bounded body in candidate_final_result, final_result, or artifact_markdown when it fits the bounded "
                "response. Preserve task-provided facts exactly. Do not add concrete times, dates, publication states, "
                "validation states, numbers, source headings, or status details unless they are visible in the goal, "
                "dependency evidence, or evidence_ledger, or are explicitly derived from those facts and labeled as "
                "derived. Preserve uncertainty and evidence strength exactly: statements such as 'no known data loss', "
                "'audit still running', 'not yet published', or 'needs sign-off' must not be rewritten into confirmed "
                "absence, completed validation, publication, approval, or resolution. When evidence says no data loss "
                "is known and an audit is still running, do not state or imply that data is intact, complete, safe, "
                "fully verified, or that no data was lost. Keep the response bounded. "
                "Unless the user explicitly requests a fill-in template, do not leave unresolved placeholders such as "
                "[date], [time], [name], [Your Name], [Title], TODO, or TBD in a final deliverable; omit unknown "
                "details or write a role-generic sentence grounded in available facts. "
                "For a long, sectioned, or file-backed deliverable that cannot fit the bounded response, "
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
                    "Claim bindings: [{claim, evidence_ids, support_type}], where evidence_ids contains only offered stable reference_id values and support_type is content, unavailability, or ref_pointer; for file/section claims select the bounded readback reference, never a free-text locator label",
                    False,
                ),
                "acceptance_points": (
                    [dict],
                    "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}], where evidence_ids contains only exact offered evidence_ledger.items[].reference_id values",
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
            action_requirements = self._taskboard_card_action_requirements(context.card)
            required_action_ids = self._taskboard_card_required_action_ids(context.card)
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
                    [
                        *(
                            {"required_output": str(item), "source": "taskboard_card"}
                            for item in list(
                                getattr(context.card, "required_outputs", ()) or ()
                            )
                        ),
                        *action_requirements,
                    ]
                ),
                capability_scope=tuple(
                    {
                        "capability_id": capability_id,
                        "capability_kind": "action",
                        "source": "taskboard_card",
                    }
                    for capability_id in required_action_ids
                ),
                delivery_contract={
                    "card": DataFormatter.sanitize(context.card.to_dict()),
                    "execution_prompt": DataFormatter.sanitize(self._execution_prompt_context()),
                    "task_context_contract": self._task_context_contract_for_model_prompt(),
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
                    "plan_block_kind": (
                        "action_call"
                        if required_action_ids
                        else "agent_step"
                    ),
                    "preferred_execution_shape": str(
                        carrier_plan.get("effective_execution_shape")
                        or carrier_plan.get("execution_shape")
                        or "auto"
                    ),
                    "strategy": "taskboard",
                    "card_id": context.card.id,
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                },
            )

            async def run_card_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
                carrier_output_policy = self._carrier_output_policy_from_block_context(_context)
                direct_artifact_transfer = await self._try_taskboard_workspace_artifact_action_transfer(context)
                if direct_artifact_transfer is not None:
                    direct_result, direct_meta = direct_artifact_transfer
                    return {
                        "execution_result": DataFormatter.sanitize(direct_result),
                        "execution_meta": DataFormatter.sanitize(direct_meta),
                    }
                preplanned_action_calls = await self._try_taskboard_preplanned_action_calls(context)
                if preplanned_action_calls is not None:
                    direct_result, direct_meta = preplanned_action_calls
                    return {
                        "execution_result": DataFormatter.sanitize(direct_result),
                        "execution_meta": DataFormatter.sanitize(direct_meta),
                    }
                narrow_action_commands = await self._try_taskboard_narrow_action_command_request(
                    context,
                    card_input_payload=card_input_payload,
                )
                if narrow_action_commands is not None:
                    direct_result, direct_meta = narrow_action_commands
                    return {
                        "execution_result": DataFormatter.sanitize(direct_result),
                        "execution_meta": DataFormatter.sanitize(direct_meta),
                    }
                effective_card_input_payload = self._taskboard_card_payload_with_scoped_retrieval_results(
                    card_input_payload,
                    _context,
                )
                card_result, card_meta = await self._run_bounded_child_execution(
                    execution=execution,
                    language_policy=language_policy,
                    input_payload=effective_card_input_payload,
                    info_payload=task_orientation_payload,
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
            card_evidence_ledger = self._taskboard_card_binding_evidence_ledger(
                evidence_ledger,
                execution_evidence_ledger,
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
                if self._should_attempt_evidence_binding_repair(evidence_use_guard):
                    card_output, evidence_use_guard, model_repair_diagnostic = (
                        await self._repair_taskboard_card_evidence_use_with_model(
                            card_output,
                            evidence_use_guard,
                            card_evidence_ledger,
                            language_policy=language_policy,
                        )
                    )
                    if model_repair_diagnostic is not None:
                        if evidence_repair_diagnostic is not None:
                            model_repair_diagnostic["prior_repair"] = evidence_repair_diagnostic
                        evidence_repair_diagnostic = model_repair_diagnostic
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

    def _taskboard_card_binding_evidence_ledger(
        self,
        historical_evidence_ledger: Mapping[str, Any],
        current_execution_evidence_ledger: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build the card-local binding view with current evidence first."""
        current_items = current_execution_evidence_ledger.get("items", [])
        historical_items = historical_evidence_ledger.get("items", [])
        return self._stable_evidence_ledger_view(
            {
                "evidence_items": [
                    *(
                        list(current_items)
                        if isinstance(current_items, Sequence)
                        and not isinstance(current_items, str | bytes | bytearray)
                        else []
                    ),
                    *(
                        list(historical_items)
                        if isinstance(historical_items, Sequence)
                        and not isinstance(historical_items, str | bytes | bytearray)
                        else []
                    ),
                ]
            },
            max_items=120,
            body_chars=1800,
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
        evidence_ledger = self._stable_evidence_ledger_view(evidence_view, max_items=80, body_chars=1800)
        prompt_evidence_ledger = self._model_evidence_ledger_projection(
            self._stable_evidence_ledger_view(
                evidence_view,
                max_items=64,
                body_chars=1200,
                budget_selection="content_first",
                max_overflow_refs=64,
            ),
            max_items=64,
        )
        preflight_diagnostics = task_board_preflight_diagnostics(
            context.revision,
            mounted_capabilities=self._planner_capabilities(),
            workspace_refs=self.workspace_refs.get("artifacts", []) if isinstance(self.workspace_refs, Mapping) else [],
        )
        acceptance_index = build_task_board_acceptance_index(
            context.revision,
            success_criteria=self.success_criteria,
            evidence_view=evidence_view,
            evidence_ledger=evidence_ledger,
            explicit_state_facts=task_board_explicit_state_facts(context.revision, evidence_view=evidence_view),
            previous_acceptance_index=(
                getattr(self, "_latest_taskboard_acceptance_index", None)
                if isinstance(getattr(self, "_latest_taskboard_acceptance_index", None), Mapping)
                else None
            ),
        )
        acceptance_verification_plan = build_task_board_incremental_verification_plan(acceptance_index)
        scoped_evidence_view = build_task_board_scoped_evidence_view(
            acceptance_index,
            evidence_view=evidence_view,
            evidence_ledger=evidence_ledger,
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
            evidence_ledger,
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
            "task_context_contract": self._task_context_contract_for_model_prompt(),
            "card": context.card.to_dict(),
            "dependency_results": self._compact_taskboard_dependency_results(context.dependency_results),
            "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_prompt(evidence_view),
            "evidence_ledger": prompt_evidence_ledger,
            "taskboard_acceptance_index": DataFormatter.sanitize(acceptance_index),
            "taskboard_acceptance_verification_plan": DataFormatter.sanitize(acceptance_verification_plan),
            "taskboard_scoped_evidence_view": DataFormatter.sanitize(scoped_evidence_view),
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
            "Use task_context_contract.current_time only when current/latest/as-of evidence matters, and label older "
            "or historical source material with its time boundary. Do not treat the runtime/current date as a "
            "business fact, incident date, deployment date, publication date, approval date, or validation date "
            "unless the goal or verifier-visible evidence explicitly provides it. "
            "do not plan or call tools from this request. taskboard_evidence_view is the compact evidence summary "
            "and preserve cold refs as pointers. Treat evidence_ledger as the authoritative grounding ledger and "
            "bind factual claims through only exact offered evidence_ledger.items[].reference_id values in "
            "evidence_use.evidence_ids; no other prompt field is an evidence identity. failed/empty items support "
            "unavailability only; ref_only "
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
            "Preserve task-provided facts exactly. Do not add concrete times, dates, publication states, validation "
            "states, numbers, source headings, or status details unless they are visible in the goal, dependency "
            "evidence, or evidence_ledger, or are explicitly derived from those facts and labeled as derived. "
            "Preserve uncertainty and evidence strength exactly: no-known-loss, still-running audit, unpublished "
            "manifest, missing sign-off, and unresolved warning states must not become confirmed absence, complete "
            "validation, publication, approval, or fix. When evidence says no data loss is known and an audit is "
            "still running, do not state or imply that data is intact, complete, safe, fully verified, or that no data was lost. "
            "Unless the user explicitly requests a fill-in template, do not leave unresolved placeholders such as "
            "[date], [time], [name], [Your Name], [Title], TODO, or TBD in a final deliverable; omit unknown "
            "details or write a role-generic sentence grounded in available facts. "
            "For file-backed deliverables, return acceptance_points with expected headings or exact anchors for "
            "critical verification points; do not invent line numbers or trusted file refs. "
            "After the main control result fields, include short self_check, short_summary, and progress_message for "
            "downstream board context and human progress; these process fields are not evidence. "
            "Judge status, sufficient, gaps, and remaining_work only against this card's objective and done_when; "
            "work already assigned to a downstream card is not remaining work for the current card. "
            f"{_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION}Also return whether the card is sufficient "
            "and what continuation, if any, the board should consider."
        )
        grounding_repair_contract = self._taskboard_grounding_repair_contract(context)
        grounding_patch_mode = bool(
            grounding_repair_contract
            and self._taskboard_grounding_patch_paths(context)
        )
        if grounding_patch_mode:
            control_instruction += (
                " This is a grounding-only Workspace repair. Do not return candidate_final_result, final_result, "
                "artifact_markdown, or a complete artifact body. Set next_board_action='patch' and return only a "
                "Workspace patch_proposal for an authorized final deliverable path. Return exactly one replace operation "
                "for every material_claim_repair_contract requirement and copy that requirement's claim_key into the operation. "
                "Each requirement's artifact_quote is a host-validated exact span from its immutable segment_id; treat it "
                "as present in the contracted content version and use it to construct old_string without guessing from claim prose. "
                "Each operation must use op='replace', old_string for exact artifact text wholly within the implicated "
                "artifact_quote, and new_string for its bounded replacement; "
                "do not use write, append, insert, full-file replacement, or unrelated edits."
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
            "status": (str, "completed, setback, blocked, failed, or skipped for this card", False),
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
                "finalize, continue, readback, repair, patch, block, or stop; continue advances the board without overriding this card's explicit status",
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
                "Claim bindings: [{claim, evidence_ids, support_type}], where evidence_ids contains only offered stable reference_id values and support_type is content, unavailability, or ref_pointer; for file/section claims select the bounded readback reference, never a free-text locator label",
                False,
            ),
            "acceptance_points": (
                [dict],
                "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}], where evidence_ids contains only exact offered evidence_ledger.items[].reference_id values",
                False,
            ),
            "remaining_work": (
                [str],
                "Remaining work inside this card's objective/done_when only; exclude work owned by downstream cards",
                False,
            ),
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
                {
                    "path": (str, "Authorized Workspace file path to patch", True),
                    "operations": (
                        [
                            {
                                "claim_key": (
                                    str,
                                    "Exact host-issued claim_key for the one grounding requirement repaired by this operation",
                                    True,
                                ),
                                "op": (Literal["replace"], "Only replace is allowed", True),
                                "old_string": (
                                    str,
                                    "Exact existing artifact text wholly within one host-validated artifact_quote",
                                    True,
                                ),
                                "new_string": (
                                    str,
                                    "Bounded replacement text for that implicated claim",
                                    True,
                                ),
                            }
                        ],
                        "Exactly one replace operation per grounding repair requirement",
                        True,
                    ),
                }
                if grounding_patch_mode
                else dict,
                (
                    "Grounding-only Workspace replace patch using path and operations with exact "
                    "old_string/new_string fields"
                    if grounding_patch_mode
                    else "Optional TaskBoardPatch or Workspace text patch proposal when next_board_action is patch"
                ),
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
                "task_context_contract": self._task_context_contract_for_model_prompt(),
            },
            quality_gates=(
                {
                    "kind": "taskboard_control_card_status",
                    "allowed_statuses": ["completed", "setback", "blocked", "failed", "skipped"],
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
        allow_workspace_delivery = (
            not grounding_patch_mode
            and self._taskboard_control_output_allows_workspace_delivery(card_output)
        )
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
        card_evidence_ledger = self._stable_evidence_ledger_view(
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
            if self._should_attempt_evidence_binding_repair(evidence_use_guard):
                card_output, evidence_use_guard, model_repair_diagnostic = (
                    await self._repair_taskboard_card_evidence_use_with_model(
                        card_output,
                        evidence_use_guard,
                        card_evidence_ledger,
                        language_policy=language_policy,
                    )
                )
                if model_repair_diagnostic is not None:
                    if evidence_repair_diagnostic is not None:
                        model_repair_diagnostic["prior_repair"] = evidence_repair_diagnostic
                    evidence_repair_diagnostic = model_repair_diagnostic
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

    async def _repair_taskboard_card_evidence_use_with_model(
        self,
        card_output: Mapping[str, Any],
        evidence_use_guard: Mapping[str, Any],
        card_evidence_ledger: Mapping[str, Any],
        *,
        language_policy: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], dict[str, Any] | None]:
        original_blocking_count = self._taskboard_evidence_guard_blocking_count(evidence_use_guard)
        if original_blocking_count <= 0:
            return card_output, evidence_use_guard, None
        try:
            repaired_evidence_use = await self._request_evidence_binding_repair(
                evidence_use_guard,
                card_evidence_ledger,
                language_policy=language_policy,
            )
        except Exception as error:
            diagnostic = {
                "code": "taskboard.card.model_evidence_binding_repair",
                "status": "failed",
                "original_blocking_count": original_blocking_count,
                "repaired_blocking_count": original_blocking_count,
                "error": {
                    "type": error.__class__.__name__,
                    "message": str(error),
                },
            }
            return card_output, evidence_use_guard, DataFormatter.sanitize(diagnostic)
        if not repaired_evidence_use:
            return card_output, evidence_use_guard, {
                "code": "taskboard.card.model_evidence_binding_repair",
                "status": "no_match",
                "original_blocking_count": original_blocking_count,
                "repaired_blocking_count": original_blocking_count,
            }
        merged_evidence_use = self._merge_repaired_evidence_use(
            evidence_use_guard.get("normalized_evidence_use"),
            repaired_evidence_use,
        )
        repaired_output = value_with_normalized_evidence_use(card_output, merged_evidence_use)
        repaired_guard = validate_evidence_use(collect_evidence_use(repaired_output), card_evidence_ledger)
        repaired_blocking_count = self._taskboard_evidence_guard_blocking_count(repaired_guard)
        diagnostic = {
            "code": "taskboard.card.model_evidence_binding_repair",
            "status": "completed" if repaired_blocking_count == 0 else "partial",
            "original_blocking_count": original_blocking_count,
            "repaired_blocking_count": repaired_blocking_count,
            "repaired_claim_count": len(repaired_evidence_use),
        }
        if repaired_blocking_count >= original_blocking_count:
            diagnostic["status"] = "rejected"
            return card_output, evidence_use_guard, diagnostic
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
                "offered reference_id values from the available evidence."
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
        # A card owns execution, not terminal semantic acceptance. Invalid
        # model-authored evidence bindings stay diagnostic/untrusted and are
        # excluded by the evidence guard, but they must not reverse a canonical
        # successful Action execution into a failed business card. The outer
        # terminal verifier owns acceptance against the canonical ledger.
        if isinstance(card_output, Mapping):
            status = str(card_output.get("status") or "completed").strip().lower()
            if status in {"completed", "setback", "blocked", "failed", "skipped"}:
                return status
            remaining = card_output.get("remaining_work")
            if isinstance(remaining, Sequence) and not isinstance(remaining, str | bytes | bytearray) and remaining:
                return "blocked"
        return "completed"

    @classmethod
    def _taskboard_control_card_status(cls, card_output: Any) -> str:
        if isinstance(card_output, Mapping):
            status = str(card_output.get("status") or "completed").strip().lower()
            next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
            workspace_patch_delivery = card_output.get("workspace_patch_delivery")
            if (
                next_action == "patch"
                and status in {"completed", "skipped"}
                and card_output.get("sufficient") is not False
                and isinstance(workspace_patch_delivery, Mapping)
                and str(workspace_patch_delivery.get("status") or "").strip().lower() == "completed"
            ):
                return status
            if next_action in {"readback", "needs_readback", "repair", "patch"}:
                return "setback"
            if next_action == "block":
                return "blocked"
            if status in {"completed", "setback", "blocked", "failed", "skipped"}:
                return status
            remaining = card_output.get("remaining_work")
            gaps = card_output.get("gaps")
            if cls._has_remaining_work(remaining) or cls._has_remaining_work(gaps):
                return "setback"
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
