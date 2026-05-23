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

import re
import uuid
from typing import Any, cast

from agently.core import DynamicTaskContext, TaskDAGExecutor
from agently.types.data import (
    ActionResult,
    SkillExecutionDict,
    SkillExecutionPlan,
)
from agently.types.plugins import SkillsExecutionContext
from agently.utils.DataGuardian import (
    _copy_public,
    _ensure_dict,
    _ensure_dict_list,
    _ensure_list,
    _ensure_string_list,
)

from .errors import SkillExecutionError
from .helpers import _semantic_role_and_type
from .registry import SkillRegistry

_TEMPLATE_PATTERN = re.compile(r"^\$\{([^}]+)\}$")


class SkillExecution:
    def __init__(self, data: SkillExecutionDict):
        self.data = data
        self.execution_id = str(data.get("execution_id", ""))
        self.plan = data.get("plan", {})
        self.status = data.get("status", "created")
        self.output = data.get("output")
        self.result = data.get("result")
        self.runtime_stream = data.get("runtime_stream", [])
        self.skill_logs = data.get("skill_logs", [])
        self.action_logs = data.get("action_logs", [])
        self.approval_records = data.get("approval_records", [])
        self.intervention_records = data.get("intervention_records", [])
        self.close_snapshot = data.get("close_snapshot", {})

    def to_dict(self) -> SkillExecutionDict:
        return _copy_public(self.data)


class SkillExecutor:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def execute(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
    ) -> SkillExecution:
        execution_id = uuid.uuid4().hex
        action_logs: list[ActionResult] = []
        skill_logs: list[dict[str, Any]] = []
        runtime_stream: list[dict[str, Any]] = []
        state: dict[str, Any] = {"task": task}
        status = str(plan.get("status", "no_match"))
        if status in {"blocked", "rejected"}:
            user_message = self._blocked_user_message(plan)
            return self._build_execution(
                execution_id=execution_id,
                status="blocked",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output={
                    "error": "Skill execution plan is blocked.",
                    "user_message": user_message,
                    "rejected_skills": plan.get("rejected_skills", []),
                    "rejected_skills_packs": plan.get("rejected_skills_packs", []),
                    "resolution_suggestions": self._blocked_resolution_suggestions(plan),
                },
            )
        if not plan.get("selected_skills"):
            return self._build_execution(
                execution_id=execution_id,
                status="no_match",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output=None,
            )

        graph = self._build_dynamic_task_graph(plan)
        plan["dynamic_task_graph"] = graph
        if not graph.get("tasks"):
            return self._build_execution(
                execution_id=execution_id,
                status="success",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output=_copy_public(state),
                task_dag_close_snapshot={},
            )

        try:
            close_snapshot, dag_stream = await self._run_dynamic_task_graph(
                graph=graph,
                context=context,
                task=task,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
            )
        except Exception as error:
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output={"error": str(error), "state": _copy_public(state)},
                task_dag_close_snapshot={"error": str(error)},
            )

        for item in dag_stream:
            await self._emit_runtime_item(context=context, runtime_stream=runtime_stream, item=item)

        failed_stages = [log for log in skill_logs if log.get("status") in {"error", "blocked"}]
        dag_status = "error" if failed_stages else "success"
        return self._build_execution(
            execution_id=execution_id,
            status=dag_status,
            plan=plan,
            state=state,
            skill_logs=skill_logs,
            action_logs=action_logs,
            runtime_stream=runtime_stream,
            output=_copy_public(state),
            task_dag_close_snapshot=close_snapshot,
        )

    def _build_dynamic_task_graph(self, plan: SkillExecutionPlan) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        semantic_outputs: dict[str, Any] = {}
        composed = [_ensure_dict(item) for item in _ensure_list(plan.get("composed_stage_graph")) if _ensure_dict(item)]
        if composed and any(item.get("task_id") for item in composed):
            for index, stage_data in enumerate(composed, start=1):
                task_id = str(stage_data.get("task_id") or stage_data.get("stage_id") or f"stage_{ index }")
                stage_id = str(stage_data.get("stage_id") or task_id)
                tasks.append({
                    "id": task_id,
                    "kind": "skill_stage_handler",
                    "title": str(stage_data.get("title") or f"{ stage_data.get('skill_id', 'skill') }:{ stage_id }"),
                    "purpose": str(stage_data.get("purpose") or stage_data.get("title") or f"Execute composed skill stage { stage_id }."),
                    "depends_on": _ensure_string_list(stage_data.get("depends_on")),
                    "inputs": {
                        "selection": {
                            "skill_id": str(stage_data.get("skill_id") or ""),
                            "card": {},
                        },
                        "stage": {
                            **stage_data,
                            "stage_id": stage_id,
                            "kind": str(stage_data.get("kind") or "model_plan"),
                        },
                        "stage_id": stage_id,
                    },
                    "produces": _ensure_dict_list(stage_data.get("produces")) or [{"role": stage_id, "type": "plan"}],
                })
                for produced in _ensure_dict_list(stage_data.get("produces")):
                    role = str(produced.get("role") or "")
                    if role:
                        semantic_outputs[role] = {"task_id": task_id}
                if stage_id not in semantic_outputs:
                    semantic_outputs[stage_id] = {"task_id": task_id}
            for output_name in _ensure_string_list(plan.get("expected_outputs")):
                role, _ = _semantic_role_and_type(output_name)
                if role and role not in semantic_outputs and tasks:
                    semantic_outputs[role] = {"task_id": tasks[-1]["id"]}
            return {
                "graph_id": f"skill-execution-{ uuid.uuid4().hex[:12] }",
                "task_schema_version": "task_dag/v1",
                "tasks": tasks,
                "semantic_outputs": semantic_outputs,
                "policies": {"source": "skills_executor", "composed": True},
                "diagnostics": [],
            }
        previous_task_id = ""
        index = 0
        for selection in _ensure_list(plan.get("selected_skills")):
            selection_data = _ensure_dict(selection)
            skill_id = str(selection_data.get("skill_id", "skill"))
            for stage in _ensure_list(selection_data.get("stages")):
                stage_data = _ensure_dict(stage)
                if not stage_data:
                    continue
                index += 1
                stage_id = str(stage_data.get("stage_id") or stage_data.get("id") or f"stage_{ index }")
                task_id = self._task_id_for_stage(index=index, skill_id=skill_id, stage_id=stage_id)
                task_entry = {
                    "id": task_id,
                    "kind": "skill_stage_handler",
                    "title": f"{ skill_id }:{ stage_id }",
                    "purpose": f"Execute Skill '{ skill_id }' stage '{ stage_id }'.",
                    "depends_on": [previous_task_id] if previous_task_id else [],
                    "inputs": {
                        "selection": selection_data,
                        "stage": stage_data,
                        "stage_id": stage_id,
                    },
                    "produces": [{"role": stage_id, "type": str(stage_data.get("kind", "model"))}],
                }
                tasks.append(task_entry)
                semantic_outputs[stage_id] = {"task_id": task_id}
                previous_task_id = task_id
        return {
            "graph_id": f"skill-execution-{ uuid.uuid4().hex[:12] }",
            "task_schema_version": "task_dag/v1",
            "tasks": tasks,
            "semantic_outputs": semantic_outputs,
            "policies": {"source": "skills_executor"},
            "diagnostics": [],
        }

    def _task_id_for_stage(self, *, index: int, skill_id: str, stage_id: str) -> str:
        raw = f"s{ index }_{ skill_id }_{ stage_id }"
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_.-")
        if not normalized or not re.match(r"^[A-Za-z_]", normalized):
            normalized = f"s{ index }"
        return normalized

    def _blocked_user_message(self, plan: SkillExecutionPlan) -> str:
        rejected = [_ensure_dict(item) for item in _ensure_list(plan.get("rejected_skills"))]
        missing_actions = [
            str(item.get("reason") or item.get("skill_id") or "")
            for item in rejected
            if item.get("reason_code") == "missing_action"
        ]
        if missing_actions:
            return (
                "Skills Executor could not complete the requested Skill because one or more required "
                "capabilities are unavailable and no controlled substitute could be found. Third-party "
                "Skill scripts are installed as assets and are not executed directly. Please bind the "
                "missing capability as an Action, enable an appropriate execution environment, or choose "
                "another trusted provider."
            )
        return "Skills Executor could not complete the requested Skill. Review the rejected skills and required policies."

    def _blocked_resolution_suggestions(self, plan: SkillExecutionPlan) -> list[str]:
        suggestions = [
            "Bind a framework Action that provides the missing capability.",
            "Use a declarative stage backed by a sandboxed Bash/Python/Node action when that safely replaces the helper script.",
            "Install or configure an external provider/API key when the Skill depends on an external service.",
            "If no controlled substitute exists, ask the user to resolve the missing dependency before retrying.",
        ]
        rejected = [_ensure_dict(item) for item in _ensure_list(plan.get("rejected_skills"))]
        if any(item.get("reason_code") == "missing_action" for item in rejected):
            suggestions.insert(
                0,
                "For simple shell work, declare a Bash/shell action stage; Skills Executor can auto-bind a controlled Bash sandbox.",
            )
        return suggestions

    async def _run_dynamic_task_graph(
        self,
        *,
        graph: dict[str, Any],
        context: SkillsExecutionContext,
        task: str,
        state: dict[str, Any],
        skill_logs: list[dict[str, Any]],
        action_logs: list[ActionResult],
        runtime_stream: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async def run_skill_stage(dag_context: DynamicTaskContext):
            inputs = _ensure_dict(dag_context.task.inputs)
            selection = _ensure_dict(inputs.get("selection"))
            stage = _ensure_dict(inputs.get("stage"))
            stage_log = await self._execute_stage(
                context=context,
                task=task,
                selection=selection,
                stage=stage,
                state=state,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
            )
            skill_logs.append(stage_log)
            stage_id = str(stage_log.get("stage_id") or inputs.get("stage_id") or dag_context.task.id)
            return {
                "skill_id": stage_log.get("skill_id"),
                "stage_id": stage_id,
                "kind": stage_log.get("kind"),
                "status": stage_log.get("status"),
                "value": _copy_public(state.get(stage_id)),
            }

        stage_timeout = float(context.get_setting("skills.stage_execution_timeout", 600) or 600)
        executor = TaskDAGExecutor({"skill_stage_handler": run_skill_stage}, name="skill-execution")
        compiled = executor.compile(graph)
        execution = compiled.create_execution(auto_close=False)
        stream = execution.get_async_runtime_stream(timeout=0.1)
        await execution.async_start({"task": task, "plan": _copy_public(graph)})
        close_snapshot = await execution.async_close(timeout=stage_timeout)
        dag_stream = []
        async for item in stream:
            dag_stream.append(item)
        return close_snapshot, dag_stream

    async def _execute_stage(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        selection: dict[str, Any],
        stage: dict[str, Any],
        state: dict[str, Any],
        action_logs: list[ActionResult],
        runtime_stream: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage_id = str(stage.get("stage_id") or stage.get("id") or uuid.uuid4().hex)
        kind = str(stage.get("kind") or "model")
        log = {"skill_id": selection.get("skill_id"), "stage_id": stage_id, "kind": kind, "status": "success"}
        try:
            if kind == "action":
                action_id = str(stage.get("action") or "")
                if not action_id:
                    raise SkillExecutionError(f"Skill stage '{ stage_id }' is missing action.")
                if not context.action_available(action_id):
                    log["status"] = "blocked"
                    log["action_id"] = action_id
                    log["error"] = (
                        f"Action '{ action_id }' is not available and could not be resolved to a controlled substitute. "
                        "Third-party Skill scripts are not executed directly."
                    )
                    state[stage_id] = {
                        "blocked": True,
                        "user_message": log["error"],
                        "resolution_suggestions": [
                            "Bind the missing action before running the Skill.",
                            "Declare a Bash/shell stage when a controlled shell command can safely replace the helper.",
                            "Ask the user to install or configure the required provider when no substitute is available.",
                        ],
                    }
                    return log
                action_input = self._resolve_templates(stage.get("input", {}), task=task, state=state)
                result = await context.async_execute_action(
                    action_id,
                    action_input if isinstance(action_input, dict) else {},
                    purpose=f"Skill { selection.get('skill_id') } stage { stage_id }",
                    source_protocol="skill",
                )
                action_logs.append(result)
                state[stage_id] = result.get("data", result.get("result"))
                log["action_id"] = action_id
                log["action_status"] = result.get("status")
                if result.get("status") != "success":
                    log["status"] = result.get("status", "error")
                    log["error"] = result.get("error", "")
            elif kind == "model":
                result = await self._execute_model_stage(
                    context=context,
                    task=task,
                    selection=selection,
                    stage=stage,
                    state=state,
                    stage_id=stage_id,
                    runtime_stream=runtime_stream,
                    default_field="reply",
                )
                state[stage_id] = result
                if isinstance(result, dict):
                    log["output_keys"] = list(result.keys())
            elif kind == "model_plan":
                result = await self._execute_model_stage(
                    context=context,
                    task=task,
                    selection=selection,
                    stage=stage,
                    state=state,
                    stage_id=stage_id,
                    runtime_stream=runtime_stream,
                    default_field="plan",
                )
                state[stage_id] = result
                if isinstance(result, dict):
                    log["output_keys"] = list(result.keys())
            elif kind == "branch":
                result = await self._execute_branch_stage(
                    context=context,
                    task=task,
                    selection=selection,
                    stage=stage,
                    state=state,
                    stage_id=stage_id,
                    runtime_stream=runtime_stream,
                )
                state[stage_id] = result
                if isinstance(result, dict):
                    log["selected_branch"] = result.get("selected_branch")
            elif kind == "validate":
                self._validate_stage(stage, state)
                state[stage_id] = {"validated": True}
            elif kind == "emit":
                item = {
                    "type": "skills.stage_emit",
                    "action": "done",
                    "skill_id": selection.get("skill_id"),
                    "stage_id": stage_id,
                    "data": self._resolve_templates(stage.get("data", stage.get("emits", {})), task=task, state=state),
                }
                await self._emit_runtime_item(context=context, runtime_stream=runtime_stream, item=item)
                state[stage_id] = item
            elif kind in {"artifact_plan", "approval", "fallback", "qa_validation"}:
                state[stage_id] = {
                    "skill_id": selection.get("skill_id"),
                    "stage_id": stage_id,
                    "kind": kind,
                    "purpose": stage.get("purpose") or stage.get("title") or "",
                    "produces": _copy_public(stage.get("produces", [])),
                    "status": "planned",
                }
                log["status"] = "planned"
            else:
                state[stage_id] = {"skipped": True, "reason": f"Stage kind '{ kind }' is not implemented in V1."}
                log["status"] = "skipped"
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            log["status"] = "error"
            log["error"] = str(error)
        return log

    async def _execute_model_stage(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        selection: dict[str, Any],
        stage: dict[str, Any],
        state: dict[str, Any],
        stage_id: str,
        runtime_stream: list[dict[str, Any]],
        default_field: str,
    ) -> Any:
        prompt = self._build_model_stage_prompt(
            task=task,
            selection=selection,
            stage=stage,
            state=state,
            stage_id=stage_id,
        )
        output_schema = self._normalize_stage_output_schema(stage, default_field=default_field)
        ensure_keys = self._normalize_ensure_keys(stage, output_schema)
        max_retries = int(stage.get("max_retries", 3) or 3)

        async def stream_model_item(item: Any):
            await self._emit_runtime_item(
                context=context,
                runtime_stream=runtime_stream,
                item=self._model_stream_item_to_skill_stream(
                    item,
                    selection=selection,
                    stage=stage,
                    stage_id=stage_id,
                ),
            )

        result = await context.async_request_model(
            prompt=prompt,
            output_schema=output_schema,
            ensure_keys=ensure_keys,
            max_retries=max_retries,
            stream_handler=stream_model_item,
        )
        if ensure_keys and isinstance(result, dict):
            result = {key: result.get(key) for key in ensure_keys if key in result}
        return _copy_public(result)

    async def _execute_branch_stage(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        selection: dict[str, Any],
        stage: dict[str, Any],
        state: dict[str, Any],
        stage_id: str,
        runtime_stream: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if stage.get("prompt") or stage.get("purpose") or stage.get("model") is not None:
            result = await self._execute_model_stage(
                context=context,
                task=task,
                selection=selection,
                stage={
                    **stage,
                    "output_schema": stage.get("output_schema")
                    or stage.get("output")
                    or {
                        "selected_branch": (
                            str,
                            "The selected branch or case key from the declared branches.",
                        ),
                        "reason": (str, "Concise reason for the branch selection."),
                    },
                },
                state=state,
                stage_id=stage_id,
                runtime_stream=runtime_stream,
                default_field="selected_branch",
            )
            return dict(result) if isinstance(result, dict) else {"selected_branch": str(result), "reason": ""}

        condition = self._resolve_templates(stage.get("condition", stage.get("when")), task=task, state=state)
        branches = _ensure_dict(stage.get("branches") or stage.get("cases"))
        selected_branch = self._select_branch(condition, branches)
        return {
            "selected_branch": selected_branch,
            "condition": _copy_public(condition),
            "branches": _copy_public(branches),
        }

    def _build_model_stage_prompt(
        self,
        *,
        task: str,
        selection: dict[str, Any],
        stage: dict[str, Any],
        state: dict[str, Any],
        stage_id: str,
    ) -> dict[str, Any]:
        stage_prompt = stage.get("prompt") or stage.get("purpose") or stage.get("title") or f"Complete skill stage { stage_id }."
        return {
            "task": task,
            "skill": {
                "skill_id": selection.get("skill_id"),
                "card": _copy_public(selection.get("card", {})),
            },
            "stage": {
                "stage_id": stage_id,
                "kind": stage.get("kind", "model"),
                "title": stage.get("title", ""),
                "purpose": stage.get("purpose", ""),
                "prompt": self._resolve_templates(stage_prompt, task=task, state=state),
                "produces": _copy_public(stage.get("produces", [])),
            },
            "stage_input": self._resolve_templates(stage.get("input", {}), task=task, state=state),
            "prior_state": _copy_public(state),
        }

    def _normalize_stage_output_schema(self, stage: dict[str, Any], *, default_field: str) -> dict[str, Any]:
        raw_schema = stage.get("output_schema") or stage.get("output") or stage.get("outputs")
        if not raw_schema:
            field_name = self._default_output_field(stage, default_field=default_field)
            return {field_name: (str, f"Model-generated output for stage '{ stage.get('stage_id') or stage.get('id') or 'stage' }'.")}
        if isinstance(raw_schema, dict):
            normalized = {}
            for key, value in raw_schema.items():
                field_name = str(key)
                if isinstance(value, dict):
                    type_name = value.get("type") or value.get("kind") or "str"
                    desc = value.get("description") or value.get("desc") or value.get("purpose") or field_name
                    normalized[field_name] = (self._schema_type_from_name(type_name), str(desc))
                elif isinstance(value, list | tuple):
                    if value and isinstance(value[0], str):
                        normalized[field_name] = (self._schema_type_from_name(value[0]), *tuple(value[1:]))
                    else:
                        normalized[field_name] = tuple(value)
                elif isinstance(value, str):
                    normalized[field_name] = (str, value)
                else:
                    normalized[field_name] = value
            return normalized
        return {default_field: (str, str(raw_schema))}

    def _normalize_ensure_keys(self, stage: dict[str, Any], output_schema: Any) -> list[str] | None:
        configured = _ensure_string_list(stage.get("ensure_keys"))
        if configured:
            return configured
        if isinstance(output_schema, dict):
            return [str(key) for key in output_schema.keys()]
        return None

    def _default_output_field(self, stage: dict[str, Any], *, default_field: str) -> str:
        for produced in _ensure_dict_list(stage.get("produces")):
            role = str(produced.get("role") or "").strip()
            if role:
                normalized = re.sub(r"[^A-Za-z0-9_]+", "_", role).strip("_")
                if normalized:
                    return normalized
        return default_field

    def _schema_type_from_name(self, type_name: Any) -> type:
        normalized = str(type_name or "str").strip().lower()
        if normalized in {"str", "string", "text", "markdown", "md"}:
            return str
        if normalized in {"int", "integer"}:
            return int
        if normalized in {"float", "number"}:
            return float
        if normalized in {"bool", "boolean"}:
            return bool
        if normalized in {"dict", "object", "json", "structured"}:
            return dict
        if normalized in {"list", "array"}:
            return list
        return str

    def _model_stream_item_to_skill_stream(
        self,
        item: Any,
        *,
        selection: dict[str, Any],
        stage: dict[str, Any],
        stage_id: str,
    ) -> dict[str, Any]:
        event_type = str(getattr(item, "event_type", "done"))
        if event_type not in {"delta", "done"}:
            event_type = "done"
        field_path = str(getattr(item, "path", "") or "model")
        return {
            "type": "skills.stage_field",
            "action": event_type,
            "event_type": event_type,
            "skill_id": selection.get("skill_id"),
            "stage_id": stage_id,
            "field_path": field_path,
            "value": getattr(item, "value", None),
            "delta": getattr(item, "delta", None),
            "is_complete": bool(getattr(item, "is_complete", event_type == "done")),
            "payload": {
                "field_path": field_path,
                "wildcard_path": getattr(item, "wildcard_path", None),
                "indexes": getattr(item, "indexes", None),
                "kind": stage.get("kind", "model"),
            },
        }

    async def _emit_runtime_item(
        self,
        *,
        context: SkillsExecutionContext,
        runtime_stream: list[dict[str, Any]],
        item: dict[str, Any],
    ) -> None:
        runtime_stream.append(item)
        await context.async_emit_runtime_stream(item)

    def _select_branch(self, condition: Any, branches: dict[str, Any]) -> str:
        if isinstance(condition, bool):
            preferred = "true" if condition else "false"
            if preferred in branches:
                return preferred
        key = str(condition).strip()
        if key in branches:
            return key
        lowered = key.lower()
        for candidate in branches:
            if str(candidate).strip().lower() == lowered:
                return str(candidate)
        for fallback in ("default", "else", "fallback"):
            if fallback in branches:
                return fallback
        return key or "default"

    def _validate_stage(self, stage: dict[str, Any], state: dict[str, Any]):
        validation = _ensure_dict(stage.get("validation") or stage)
        required_state = [str(item) for item in _ensure_list(validation.get("required_state"))]
        missing = [key for key in required_state if key not in state]
        if missing:
            raise SkillExecutionError(f"Validation failed. Missing state keys: { ', '.join(missing) }")

    def _resolve_templates(self, value: Any, *, task: str, state: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve_templates(item, task=task, state=state) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_templates(item, task=task, state=state) for item in value]
        if not isinstance(value, str):
            return value
        match = _TEMPLATE_PATTERN.match(value.strip())
        if match is None:
            return value.replace("${task}", task)
        path = match.group(1)
        if path == "task":
            return task
        if path.startswith("state."):
            return self._read_path(state, path[len("state."):])
        return value

    def _read_path(self, source: Any, path: str):
        current = source
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
        return current

    def _build_execution(
        self,
        *,
        execution_id: str,
        status: str,
        plan: SkillExecutionPlan,
        state: dict[str, Any],
        skill_logs: list[dict[str, Any]],
        action_logs: list[ActionResult],
        runtime_stream: list[dict[str, Any]],
        output: Any,
        task_dag_close_snapshot: dict[str, Any] | None = None,
    ) -> SkillExecution:
        data = cast(SkillExecutionDict, {
            "execution_id": execution_id,
            "plan_id": str(plan.get("plan_id", "")),
            "status": status,
            "output": output,
            "result": output,
            "plan": _copy_public(plan),
            "runtime_stream": _copy_public(runtime_stream),
            "skill_logs": _copy_public(skill_logs),
            "action_logs": _copy_public(action_logs),
            "approval_records": _copy_public(plan.get("approval_requests", [])),
            "intervention_records": [],
            "close_snapshot": {
                "state": _copy_public(state),
                "status": status,
                "task_dag": _copy_public(task_dag_close_snapshot or {}),
            },
        })
        return SkillExecution(data)
