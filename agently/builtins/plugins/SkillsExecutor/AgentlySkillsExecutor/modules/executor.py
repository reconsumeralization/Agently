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
from .strategies import run_staged_execution, run_react_execution


def _to_int(value: Any, default: int) -> int:
    """Safely coerce a settings value to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        self.effort = data.get("effort")

    def to_dict(self) -> SkillExecutionDict:
        return _copy_public(self.data)

    # ── Snapshot durability (E5) ──

    def save_snapshot(self, path: str) -> None:
        """Persist execution snapshot to a JSON file for later resume."""
        import json as _json
        snapshot = self.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

    @classmethod
    def load_snapshot(cls, path: str) -> "SkillExecution":
        """Load a previously saved execution snapshot from a JSON file."""
        import json as _json
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        return cls(cast(SkillExecutionDict, data))

    def get_pending_waits(self) -> list[dict[str, Any]]:
        """Return pending intervention records that need human input."""
        return [
            r for r in self.intervention_records
            if r.get("status") == "pending"
        ]

    def save(self) -> SkillExecutionDict:
        return self.to_dict()

    @classmethod
    def load(cls, data: SkillExecutionDict | dict[str, Any]) -> "SkillExecution":
        return cls(cast(SkillExecutionDict, _copy_public(data)))

    async def async_resume_wait(self, wait_id: str, payload: Any = None) -> "SkillExecution":
        del payload
        raise KeyError(
            f"Skill wait '{wait_id}' is not resumable from a closed SkillExecution snapshot. "
            "Use the underlying TriggerFlow execution continue_with(...) lifecycle for active waits."
        )


class _RuntimeStreamCaptureContext:
    def __init__(self, context: SkillsExecutionContext, runtime_stream: list[dict[str, Any]]):
        self._context = context
        self._runtime_stream = runtime_stream

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None:
        self._runtime_stream.append(_copy_public(item))
        await self._context.async_emit_runtime_stream(item)


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
        effort: str | None = None,
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
                effort=effort,
            )
        if not plan.get("selected_skills"):
            return self._build_execution(
                execution_id=execution_id,
                status="no_match",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output=None,
                effort=effort,
            )

        # Resolve effort preset overrides
        effort_config = self._resolve_effort(context, effort)
        strategy = effort_config.get("strategy") or plan.get("execution_strategy", "single_shot")
        if strategy == "staged":
            return await self._execute_staged(
                context=context,
                task=task,
                plan=plan,
                execution_id=execution_id,
                effort_config=effort_config,
                effort=effort,
            )
        if strategy == "react":
            return await self._execute_react(
                context=context,
                task=task,
                plan=plan,
                execution_id=execution_id,
                effort_config=effort_config,
                effort=effort,
            )

        # ── single_shot (existing prompt-only path) ──
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
                effort=effort,
                execution_mode="single_shot",
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
            effort=effort,
            execution_mode="single_shot",
        )

    async def _execute_staged(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        execution_id: str,
        effort_config: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> SkillExecution:
        ec = effort_config or {}
        step_budget = _to_int(ec.get("step_budget") or self.registry.settings.get("skills.staged_max_steps", 12), 12)
        model_key = str(ec.get("reason_key") or plan.get("model_key") or "reason")
        artifact_inline_limit = _to_int(ec.get("artifact_inline_limit") or self.registry.settings.get("skills.artifact_inline_limit", 4096), 4096)
        runtime_stream: list[dict[str, Any]] = []
        capture_context = _RuntimeStreamCaptureContext(context, runtime_stream)

        try:
            result = await run_staged_execution(
                task=task,
                plan=dict(plan),
                context=capture_context,
                settings=self.registry.settings,
                step_budget=step_budget,
                model_key=model_key,
                semantic_outputs=_ensure_dict(plan.get("expected_result_shape")),
                artifact_inline_limit=artifact_inline_limit,
            )
        except Exception as error:
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=[],
                output={"error": str(error)},
                effort=effort,
                execution_mode="staged",
            )

        return self._build_execution(
            execution_id=execution_id,
            status="success",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=[],
            output=_copy_public(result),
            effort=effort,
            execution_mode="staged",
        )

    async def _execute_react(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        execution_id: str,
        effort_config: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> SkillExecution:
        ec = effort_config or {}
        step_budget = _to_int(ec.get("step_budget") or self.registry.settings.get("skills.react_max_steps", 30), 30)
        model_key = str(ec.get("reason_key") or plan.get("model_key") or "reason")
        artifact_inline_limit = _to_int(ec.get("artifact_inline_limit") or self.registry.settings.get("skills.artifact_inline_limit", 4096), 4096)
        runtime_stream: list[dict[str, Any]] = []
        capture_context = _RuntimeStreamCaptureContext(context, runtime_stream)

        allowed_tools, allowed_actions, allow_scripts = self._extract_react_affordances(plan)

        try:
            result = await run_react_execution(
                task=task,
                plan=dict(plan),
                context=capture_context,
                settings=self.registry.settings,
                step_budget=step_budget,
                model_key=model_key,
                allowed_tools=allowed_tools,
                allowed_actions=allowed_actions,
                allow_scripts=allow_scripts,
                artifact_inline_limit=artifact_inline_limit,
            )
        except Exception as error:
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=[],
                output={"error": str(error)},
                effort=effort,
                execution_mode="react",
            )

        return self._build_execution(
            execution_id=execution_id,
            status="success",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=[],
            output=_copy_public(result),
            effort=effort,
            execution_mode="react",
        )

    def _resolve_effort(
        self,
        context: SkillsExecutionContext,
        effort: str | None,
    ) -> dict[str, Any]:
        """Resolve an effort preset name into concrete execution overrides.

        Returns a dict with optional keys: strategy, reason_key, step_budget,
        artifact_inline_limit. Empty dict means no overrides (use plan defaults).
        """
        if not effort:
            return {}
        presets = context.get_setting("effort_presets", None)
        if presets is None:
            presets = self.registry.settings.get("effort_presets") or {}
        if not isinstance(presets, dict):
            return {}
        preset = presets.get(effort)
        if not isinstance(preset, dict):
            return {}
        return {
            k: v for k, v in preset.items()
            if k in {"strategy", "reason_key", "step_budget", "artifact_inline_limit"}
        }

    def _extract_react_affordances(
        self,
        plan: SkillExecutionPlan,
    ) -> tuple[list[str], list[str], bool]:
        """Extract allowed_tools, allowed_actions, and allow_scripts from selected skill contracts."""
        allowed_tools: list[str] = []
        allowed_actions: list[str] = []
        allow_scripts = False

        for selection in _ensure_list(plan.get("selected_skills")):
            skill_id = str(_ensure_dict(selection).get("skill_id", ""))
            if not skill_id:
                continue
            try:
                contract = self.registry.inspect_skills(skill_id)
            except Exception:
                continue
            metadata = _ensure_dict(contract.get("metadata"))
            fm = _ensure_dict(metadata.get("frontmatter"))
            tools = fm.get("allowed-tools") or fm.get("allowed_tools") or []
            if isinstance(tools, list):
                for t in tools:
                    if isinstance(t, str) and t not in allowed_tools:
                        allowed_tools.append(t)
            actions = fm.get("allowed-actions") or fm.get("allowed_actions") or []
            if isinstance(actions, list):
                for a in actions:
                    if isinstance(a, str) and a not in allowed_actions:
                        allowed_actions.append(a)
            if fm.get("allow-scripts") or fm.get("allow_scripts"):
                allow_scripts = True

        return allowed_tools, allowed_actions, allow_scripts

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
        effort: str | None = None,
        execution_mode: str | None = None,
    ) -> SkillExecution:
        strategy = execution_mode or str(plan.get("execution_strategy", "single_shot"))
        close_snapshot = {
            "status": status,
            "execution_mode": strategy,
            "skill_count": len(_ensure_list(plan.get("selected_skills"))),
            "plan_id": str(plan.get("plan_id", "")),
            "effort": effort,
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
            "effort": effort,
        })
        return SkillExecution(data)
