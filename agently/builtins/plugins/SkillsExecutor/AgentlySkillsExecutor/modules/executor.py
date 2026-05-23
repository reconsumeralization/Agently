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

import uuid
from typing import Any, Literal, cast

from agently.types.data import SkillExecutionDict, SkillExecutionPlan, SkillExecutionStatus
from agently.types.plugins import SkillsExecutionContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list

from .registry import SkillRegistry


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
        self.intervention_records = data.get("intervention_records", [])
        self.close_snapshot = data.get("close_snapshot", {})

    def to_dict(self) -> SkillExecutionDict:
        return _copy_public(self.data)

    def get_pending_waits(self) -> list[dict[str, Any]]:
        return []

    def save(self) -> SkillExecutionDict:
        return self.to_dict()

    @classmethod
    def load(cls, data: SkillExecutionDict | dict[str, Any]) -> "SkillExecution":
        return cls(cast(SkillExecutionDict, _copy_public(data)))

    async def async_resume_wait(self, wait_id: str, payload: Any = None) -> "SkillExecution":
        del payload
        raise KeyError(f"Skill wait '{ wait_id }' not found. Standard SKILL.md execution does not create framework waits.")


class SkillExecutor:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def execute(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None = None,
    ) -> SkillExecution:
        execution_id = uuid.uuid4().hex
        runtime_stream: list[dict[str, Any]] = []
        skill_logs: list[dict[str, Any]] = []
        status = str(plan.get("status", "no_match"))
        if status in {"blocked", "rejected"}:
            return self._build_execution(
                execution_id=execution_id,
                status="blocked",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output={
                    "error": "Skill execution plan is blocked.",
                    "rejected_skills": plan.get("rejected_skills", []),
                    "rejected_skills_packs": plan.get("rejected_skills_packs", []),
                },
            )
        if not plan.get("selected_skills"):
            return self._build_execution(
                execution_id=execution_id,
                status="no_match",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output=None,
            )

        prompt = self._build_prompt(task=task, plan=plan)
        await self._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.prompt_only.start",
                "action": "start",
                "skill_ids": [str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills"))],
            },
        )

        async def stream_model_item(item: Any):
            await self._emit_runtime_item(
                context=context,
                runtime_stream=runtime_stream,
                item={
                    "type": "skills.model_stream",
                    "action": str(getattr(item, "event_type", "delta") or "delta"),
                    "path": getattr(item, "path", None),
                    "value": getattr(item, "value", None),
                    "delta": getattr(item, "delta", None),
                    "is_complete": bool(getattr(item, "is_complete", False)),
                },
            )

        try:
            effective_output_format = self._resolve_output_format(plan, output_format)
            result = await context.async_request_model(
                prompt=prompt,
                output_schema=self._output_schema(plan),
                output_format=effective_output_format,
                ensure_keys=None,
                max_retries=3,
                stream_handler=stream_model_item,
            )
        except Exception as error:
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output={"error": str(error)},
            )

        for selection in _ensure_list(plan.get("selected_skills")):
            selection_data = _ensure_dict(selection)
            skill_logs.append({
                "skill_id": selection_data.get("skill_id"),
                "status": "success",
                "execution_mode": "prompt_only",
                "guidance_path": _ensure_dict(selection_data.get("guidance")).get("path", "SKILL.md"),
                "decision_card_checksum": _ensure_dict(selection_data.get("decision_card")).get("checksum", ""),
            })
        await self._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.prompt_only.done",
                "action": "done",
                "skill_ids": [str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills"))],
            },
        )
        return self._build_execution(
            execution_id=execution_id,
            status="success",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=skill_logs,
            output=_copy_public(result),
        )

    def _build_prompt(self, *, task: str, plan: SkillExecutionPlan) -> dict[str, Any]:
        selected = [_ensure_dict(item) for item in _ensure_list(plan.get("selected_skills"))]
        return {
            "task": task,
            "skills_execution_policy": [
                "Use the selected Skills as model-readable SKILL.md instructions.",
                "Use the full guidance content as the source of behavior, not Agently decision-card summaries.",
                "Synthesize all relevant selected Skills in one response.",
                "Do not treat a selected Skill as disabled or unavailable because of Agently metadata.",
                "Bundled scripts, references, and assets are listed in resource_indexes with path, kind, and summary. They may be read on demand when the execution strategy supports it. Do not claim bundled resources were executed unless an explicit Action or environment did so.",
            ],
            "selected_skill_cards": [_copy_public(item.get("decision_card", {})) for item in selected],
            "selected_skill_guidance": [
                {
                    "skill_id": item.get("skill_id"),
                    "display_name": item.get("display_name"),
                    "path": _ensure_dict(item.get("guidance")).get("path", "SKILL.md"),
                    "content": _ensure_dict(item.get("guidance")).get("content", ""),
                }
                for item in selected
            ],
            "resource_indexes": [_copy_public(item.get("resource_index", {})) for item in selected],
            "expected_result_shape": _copy_public(plan.get("expected_result_shape", {})),
        }

    def _output_schema(self, plan: SkillExecutionPlan) -> Any:
        configured = _ensure_dict(plan.get("expected_result_shape"))
        if configured:
            return configured
        return {
            "response": (str, "The final response produced by applying the selected SKILL.md guidance."),
            "skill_trace": (list, "Skill ids used and concise notes about how each was applied."),
        }

    def _resolve_output_format(
        self,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None,
    ) -> Literal["json", "flat_markdown", "hybrid", "auto"]:
        candidate = str(output_format or plan.get("expected_result_format") or "auto")
        if candidate not in {"json", "flat_markdown", "hybrid", "auto"}:
            raise ValueError(
                "Skill execution output_format must be one of: json, flat_markdown, hybrid, auto."
            )
        return cast(Literal["json", "flat_markdown", "hybrid", "auto"], candidate)

    async def _emit_runtime_item(
        self,
        *,
        context: SkillsExecutionContext,
        runtime_stream: list[dict[str, Any]],
        item: dict[str, Any],
    ) -> None:
        runtime_stream.append(item)
        await context.async_emit_runtime_stream(item)

    def _build_execution(
        self,
        *,
        execution_id: str,
        status: SkillExecutionStatus,
        plan: SkillExecutionPlan,
        runtime_stream: list[dict[str, Any]],
        skill_logs: list[dict[str, Any]],
        output: Any,
    ) -> SkillExecution:
        close_snapshot = {
            "status": status,
            "execution_mode": "prompt_only",
            "skill_count": len(_ensure_list(plan.get("selected_skills"))),
        }
        data = SkillExecutionDict({
            "execution_id": execution_id,
            "plan_id": str(plan.get("plan_id", "")),
            "status": status,
            "output": _copy_public(output),
            "result": _copy_public(output),
            "plan": _copy_public(plan),
            "runtime_stream": _copy_public(runtime_stream),
            "skill_logs": _copy_public(skill_logs),
            "action_logs": [],
            "intervention_records": [],
            "close_snapshot": close_snapshot,
        })
        return SkillExecution(data)
