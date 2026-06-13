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

import ipaddress
import socket
import uuid
from inspect import isawaitable
from pathlib import Path
from typing import Any, Callable, Literal, cast
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agently.types.data import SkillExecutionDict, SkillExecutionPlan, SkillExecutionStatus
from agently.types.plugins import SkillsEffortStrategyHandler, SkillsExecutionContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list

from .registry import SkillRegistry
from .effort_strategies import create_builtin_effort_strategy_handlers
from .contexts import RuntimeStreamCaptureContext


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

    # ── Snapshot inspection ──
    # A SkillExecution snapshot is a read-only record of a completed run. It is
    # for persistence and inspection, not active resume: a closed snapshot has no
    # live TriggerFlow execution to continue. Resuming an active wait must use the
    # underlying TriggerFlow execution continue_with(...) lifecycle.

    def save_snapshot(self, path: str) -> None:
        """Persist the execution snapshot to a JSON file for later inspection."""
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
        """Return intervention records recorded as pending in this snapshot.

        These are informational for inspection; resuming them requires the live
        TriggerFlow execution, not a closed snapshot.
        """
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
        raise NotImplementedError(
            f"Skill wait '{wait_id}' is not resumable from a closed SkillExecution snapshot, which is "
            "inspection-only. Resume an active wait through the underlying TriggerFlow execution "
            "continue_with(...) lifecycle while the execution is still open."
        )


class SkillExecutor:
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        effort_strategy_handlers: dict[str, SkillsEffortStrategyHandler] | None = None,
    ):
        self.registry = registry
        self.effort_strategy_handlers = dict(effort_strategy_handlers or {})

    async def execute(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
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

        capability_result = await self._prepare_capabilities(context=context, plan=plan)
        runtime_stream.extend(capability_result["runtime_stream"])
        capability_policy = self._capability_policy(context=context, plan=plan)
        capability_scope = str(capability_policy.get("capability_scope") or "agent").strip().lower()
        mounted_action_ids = list(capability_result.get("mounted_action_ids", []) or [])
        if capability_result["status"] in {"blocked", "approval_required"}:
            self._release_scoped_capabilities(context, capability_scope, mounted_action_ids)
            return self._build_execution(
                execution_id=execution_id,
                status="blocked",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output={
                    "error": "Skill capability preparation is blocked.",
                    "diagnostics": capability_result["diagnostics"],
                },
                effort=effort,
            )

        effort_config = self._resolve_effort(context, effort)
        strategy_name = self._resolve_strategy_name(plan=plan, effort=effort, effort_config=effort_config)
        strategy_handler = self._strategy_handlers().get(strategy_name)
        if strategy_handler is None:
            self._release_scoped_capabilities(context, capability_scope, mounted_action_ids)
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output={
                    "error": f"Unknown Skills effort strategy '{ strategy_name }'.",
                    "available_strategies": sorted(self._strategy_handlers()),
                },
                effort=effort,
                execution_mode=strategy_name,
            )
        try:
            return await strategy_handler(
                context=context,
                task=task,
                plan=plan,
                execution_id=execution_id,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output_format=output_format,
                effort_config=effort_config,
                effort=effort,
                strategy_name=strategy_name,
            )
        finally:
            # capability_scope="execution" reverses one-time capability mounts so
            # they do not persist on the host past this Skills execution. The
            # default "agent" scope keeps mounts on the agent (current contract).
            self._release_scoped_capabilities(context, capability_scope, mounted_action_ids)

    @staticmethod
    def _release_scoped_capabilities(
        context: SkillsExecutionContext,
        capability_scope: str,
        mounted_action_ids: list[str],
    ) -> None:
        if capability_scope != "execution" or not mounted_action_ids:
            return
        agent = getattr(context, "agent", None)
        action = getattr(agent, "action", None)
        unregister = getattr(action, "unregister_action", None)
        if callable(unregister):
            unregister(mounted_action_ids)

    def _resolve_effort(
        self,
        context: SkillsExecutionContext,
        effort: str | None,
    ) -> dict[str, Any]:
        """Resolve an effort preset name into concrete execution overrides.

        Returns a dict with optional keys: strategy, reason_key, step_budget,
        artifact_inline_limit. Empty dict means no overrides (use plan defaults).
        """
        base: dict[str, Any] = {}
        if effort == "fast":
            base = {"strategy": "single_shot", "retry_count": 0}
        elif effort == "normal":
            base = {
                "strategy": "runtime_chain",
                "chain_phases": ["preflight", "research", "plan", "execute", "verify", "reflect", "finalize"],
                "retry_count": 1,
            }
        elif effort == "max":
            base = {
                "strategy": "runtime_chain",
                "chain_phases": ["preflight", "research", "plan", "execute", "verify", "reflect", "finalize"],
                "retry_count": 2,
                "step_budget": 30,
                "allow_dynamic_task": True,
            }
        elif not effort:
            return {}
        presets = context.get_setting("effort_presets", None)
        if presets is None:
            presets = self.registry.settings.get("effort_presets") or {}
        if not isinstance(presets, dict):
            return base
        preset = presets.get(effort)
        if not isinstance(preset, dict):
            return base
        overrides = dict(preset)
        return {**base, **overrides}

    def _strategy_handlers(self) -> dict[str, Any]:
        handlers = create_builtin_effort_strategy_handlers(self)
        for name, handler in self.effort_strategy_handlers.items():
            handlers[name] = self._custom_strategy_adapter(name, handler)
        return handlers

    def _resolve_strategy_name(
        self,
        *,
        plan: SkillExecutionPlan,
        effort: str | None,
        effort_config: dict[str, Any],
    ) -> str:
        effort_name = str(effort or "").strip()
        configured = str(effort_config.get("strategy") or "").strip()
        if configured:
            return configured
        if effort_name and effort_name in self._strategy_handlers():
            return effort_name
        return str(plan.get("execution_strategy") or "single_shot")

    def _custom_strategy_adapter(self, strategy_name: str, handler: SkillsEffortStrategyHandler):
        async def run(
            *,
            context: SkillsExecutionContext,
            task: str,
            plan: SkillExecutionPlan,
            execution_id: str,
            runtime_stream: list[dict[str, Any]],
            skill_logs: list[dict[str, Any]],
            output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
            effort_config: dict[str, Any] | None = None,
            effort: str | None = None,
            strategy_name: str = strategy_name,
        ) -> SkillExecution:
            return await self._execute_custom_strategy(
                context=context,
                task=task,
                plan=plan,
                execution_id=execution_id,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output_format=output_format,
                effort_config=effort_config,
                effort=effort,
                strategy_name=strategy_name,
                handler=handler,
            )

        return run

    async def _execute_custom_strategy(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        execution_id: str,
        runtime_stream: list[dict[str, Any]],
        skill_logs: list[dict[str, Any]],
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        effort_config: dict[str, Any] | None = None,
        effort: str | None = None,
        strategy_name: str,
        handler: SkillsEffortStrategyHandler,
    ) -> SkillExecution:
        del skill_logs
        capture_context = RuntimeStreamCaptureContext(context, runtime_stream)
        await self._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.custom_strategy.start",
                "action": "start",
                "strategy": strategy_name,
                "effort": effort,
            },
        )
        try:
            result = handler(
                context=cast(SkillsExecutionContext, capture_context),
                task=task,
                plan=plan,
                output_format=output_format,
                effort=effort,
                effort_config=dict(effort_config or {}),
            )
            if isawaitable(result):
                result = await result
        except Exception as error:
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=[],
                output={"error": str(error)},
                effort=effort,
                execution_mode=f"custom:{ strategy_name }",
            )
        if isinstance(result, SkillExecution):
            return result
        await self._emit_runtime_item(
            context=context,
            runtime_stream=runtime_stream,
            item={
                "type": "skills.custom_strategy.done",
                "action": "done",
                "strategy": strategy_name,
                "effort": effort,
            },
        )
        return self._build_execution(
            execution_id=execution_id,
            status="success",
            plan=plan,
            runtime_stream=runtime_stream,
            skill_logs=[],
            output=_copy_public(result),
            effort=effort,
            execution_mode=f"custom:{ strategy_name }",
        )

    def _stage_model_key(self, plan: SkillExecutionPlan, stage: str) -> str:
        configured = _ensure_dict(plan.get("stage_model_keys"))
        value = configured.get(stage)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if stage == "finalizer":
            return "finalizer"
        return str(plan.get("model_key") or "reason")

    def _stage_key_for_phase(self, phase: str) -> str:
        return {
            "preflight": "planner",
            "research": "research",
            "plan": "planner",
            "execute": "executor",
            "verify": "verifier",
            "reflect": "reflector",
            "finalize": "finalizer",
        }.get(phase, "reason")

    def _phase_instruction(self, phase: str) -> str:
        return {
            "preflight": "Check task intent, constraints, selected Skills, and missing prerequisites before execution.",
            "research": "Gather and summarize context needed to apply the selected Skills. Read or request only necessary context.",
            "plan": "Create a concise execution plan with ordered steps and expected evidence.",
            "execute": "Execute the plan using selected Skill guidance and available mounted capabilities. Produce concrete task output.",
            "verify": "Validate the execution output against the original task, selected Skill guidance, and expected result shape.",
            "reflect": "Reflect on verification issues and define corrections. If verification passed, summarize why no retry is needed.",
            "finalize": "Produce the final user-facing result using the execution and verification outputs.",
        }.get(phase, "Continue the Skills runtime plan.")

    def _compact_resource_index(self, resource_index: Any, *, limit: int = 24) -> dict[str, Any]:
        data = _ensure_dict(resource_index)
        resources = []
        for item in _ensure_list(data.get("resources"))[:limit]:
            if not isinstance(item, dict):
                continue
            resources.append({
                "path": item.get("path"),
                "kind": item.get("kind"),
                "size": item.get("size"),
                "summary": str(item.get("summary") or "")[:240],
            })
        total = len(_ensure_list(data.get("resources")))
        return {
            "schema_version": str(data.get("schema_version") or "agently.skills.resources.v1"),
            "resource_count": total,
            "resources": resources,
            "truncated": total > len(resources),
        }

    async def _prepare_capabilities(
        self,
        *,
        context: SkillsExecutionContext,
        plan: SkillExecutionPlan,
    ) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        runtime_stream: list[dict[str, Any]] = []
        agent = getattr(context, "agent", None)
        if agent is None:
            return {"status": "success", "diagnostics": diagnostics, "runtime_stream": runtime_stream}
        needs = [
            _ensure_dict(item)
            for item in _ensure_list(plan.get("capability_needs"))
            if isinstance(item, dict)
        ]
        if not needs:
            return {"status": "success", "diagnostics": diagnostics, "runtime_stream": runtime_stream}

        selected_by_id = {
            str(_ensure_dict(item).get("skill_id") or ""): _ensure_dict(item)
            for item in _ensure_list(plan.get("selected_skills"))
            if isinstance(item, dict)
        }
        policy = self._capability_policy(context=context, plan=plan)
        min_confidence = self._min_auto_mount_confidence(policy)
        approval_required = False
        blocked = False
        mounted_keys: set[tuple[str, str]] = set()
        mounted_action_ids: list[str] = []

        for need in needs:
            need_name = str(need.get("need") or "")
            skill_id = str(need.get("skill_id") or "")
            # Low-confidence needs inferred only from SKILL.md prose are advisory
            # when the host configured a min_auto_mount_confidence floor: they are
            # reported but not auto-mounted, to avoid over-granting on false
            # positives. Structured declarations (frontmatter, resources) are not
            # affected unless they too fall below the floor.
            if (
                min_confidence is not None
                and str(need.get("source") or "") in {"body", "metadata"}
                and float(need.get("confidence") or 0.0) < min_confidence
            ):
                diagnostics.append({
                    **self._capability_diagnostic("capability_low_confidence_advisory", need, mode="advisory"),
                    "min_auto_mount_confidence": min_confidence,
                })
                runtime_stream.append(self._capability_event("skills.capability.advisory", need, mode="advisory"))
                continue
            mode = self._policy_mode(policy, need_name)
            event_policy_mode = mode
            if mode == "off":
                diagnostics.append(self._capability_diagnostic("capability_disabled", need, mode=mode))
                runtime_stream.append(self._capability_event("skills.capability.disabled", need, mode=mode))
                blocked = True
                continue
            if mode == "approval":
                decision = await self._resolve_capability_approval(
                    context=context,
                    need=need,
                    policy=policy,
                )
                if decision.get("approved") is not True:
                    diagnostics.append({
                        **self._capability_diagnostic("approval_required", need, mode=mode),
                        "approval": _copy_public(decision),
                    })
                    runtime_stream.append({
                        **self._capability_event("skills.capability.approval_required", need, mode=mode),
                        "approval": _copy_public(decision),
                    })
                    approval_required = True
                    continue
                runtime_stream.append({
                    **self._capability_event("skills.capability.approved", need, mode=mode),
                    "approval": _copy_public(decision),
                })
                mode = "allow"
            if mode != "allow":
                diagnostics.append(self._capability_diagnostic("capability_policy_missing", need, mode=mode))
                runtime_stream.append(self._capability_event("skills.capability.policy_missing", need, mode=mode))
                blocked = True
                continue

            mount_key = (need_name, str(need.get("resource_path") or ""))
            if mount_key in mounted_keys:
                continue
            mounted_keys.add(mount_key)
            try:
                action_ids = await self._mount_capability(
                    agent=agent,
                    need=need,
                    policy=policy,
                    selection=selected_by_id.get(skill_id, {}),
                )
            except Exception as error:
                diagnostics.append({
                    **self._capability_diagnostic("capability_mount_failed", need, mode=event_policy_mode),
                    "message": str(error),
                })
                runtime_stream.append({
                    **self._capability_event("skills.capability.mount_failed", need, mode=event_policy_mode),
                    "message": str(error),
                })
                blocked = True
                continue
            for action_id in action_ids:
                if action_id and action_id not in mounted_action_ids:
                    mounted_action_ids.append(action_id)
            diagnostics.append({
                **self._capability_diagnostic("capability_mounted", need, mode=event_policy_mode),
                "action_ids": action_ids,
            })
            runtime_stream.append({
                **self._capability_event("skills.capability.mounted", need, mode=event_policy_mode),
                "action_ids": action_ids,
            })

        if blocked:
            return {"status": "blocked", "diagnostics": diagnostics, "runtime_stream": runtime_stream, "mounted_action_ids": mounted_action_ids}
        if approval_required:
            return {"status": "approval_required", "diagnostics": diagnostics, "runtime_stream": runtime_stream, "mounted_action_ids": mounted_action_ids}
        return {"status": "success", "diagnostics": diagnostics, "runtime_stream": runtime_stream, "mounted_action_ids": mounted_action_ids}

    def _capability_policy(
        self,
        *,
        context: SkillsExecutionContext,
        plan: SkillExecutionPlan,
    ) -> dict[str, Any]:
        configured = context.get_setting("skills.capability_policy", None)
        if configured is None:
            configured = self.registry.settings.get("skills.capability_policy") or {}
        policy = _ensure_dict(configured)
        plan_policy = _ensure_dict(plan.get("capability_policy"))
        if plan_policy:
            merged = dict(policy)
            for key, value in plan_policy.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**_ensure_dict(merged.get(key)), **value}
                else:
                    merged[key] = value
            policy = merged
        return policy

    @staticmethod
    def _min_auto_mount_confidence(policy: dict[str, Any]) -> float | None:
        raw = policy.get("min_auto_mount_confidence")
        if raw is None:
            return None
        try:
            return max(0.0, min(float(raw), 1.0))
        except (TypeError, ValueError):
            return None

    def _policy_mode(self, policy: dict[str, Any], need_name: str) -> str:
        auto_load = _ensure_dict(policy.get("auto_load"))
        raw = auto_load.get(need_name, policy.get(need_name, "off"))
        if isinstance(raw, dict):
            raw = raw.get("mode", raw.get("policy", "off"))
        mode = str(raw or "off").strip().lower()
        if mode in {"allow", "allowed", "true", "yes", "auto"}:
            return "allow"
        if mode in {"approval", "approve", "ask"}:
            return "approval"
        return "off"

    async def _resolve_capability_approval(
        self,
        *,
        context: SkillsExecutionContext,
        need: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        from agently.base import policy_approval

        decision = await policy_approval.async_resolve(
            {
                "source": "skills_capability",
                "capability": str(need.get("need") or ""),
                "subject": str(need.get("skill_id") or need.get("need") or ""),
                "risk": "capability_mount",
                "payload": {
                    "need": _copy_public(need),
                    "task": getattr(context, "task", None),
                },
                "policy": _copy_public(policy),
                "lineage": {
                    "skill_id": str(need.get("skill_id") or ""),
                    "source": str(need.get("source") or ""),
                },
            },
            handler=str(context.get_setting("policy_approval.handler", "") or "") or None,
        )
        return _copy_public(decision)

    def _capability_diagnostic(self, code: str, need: dict[str, Any], *, mode: str) -> dict[str, Any]:
        return {
            "level": "info" if code == "capability_mounted" else "error",
            "code": code,
            "skill_id": str(need.get("skill_id") or ""),
            "need": str(need.get("need") or ""),
            "source": str(need.get("source") or ""),
            "risk": str(need.get("risk") or ""),
            "policy": mode,
            "evidence": str(need.get("evidence") or ""),
            "resource_path": str(need.get("resource_path") or ""),
        }

    def _capability_event(self, event_type: str, need: dict[str, Any], *, mode: str) -> dict[str, Any]:
        return {
            "type": event_type,
            "action": event_type.rsplit(".", 1)[-1],
            "skill_id": str(need.get("skill_id") or ""),
            "need": str(need.get("need") or ""),
            "source": str(need.get("source") or ""),
            "risk": str(need.get("risk") or ""),
            "policy": mode,
            "resource_path": str(need.get("resource_path") or ""),
        }

    async def _mount_capability(
        self,
        *,
        agent: Any,
        need: dict[str, Any],
        policy: dict[str, Any],
        selection: dict[str, Any],
    ) -> list[str]:
        need_name = str(need.get("need") or "")
        if need_name == "web_search":
            from agently.builtins.actions import Search
            options = self._web_search_options(policy)
            before = self._agent_action_ids(agent)
            agent.use_actions(Search(
                backend=cast(Any, options.get("backend", "auto")),
                search_backend=cast(Any, options.get("search_backend", None)),
                news_backend=cast(Any, options.get("news_backend", None)),
                timeout=cast(Any, options.get("timeout", None)),
                proxy=cast(Any, options.get("proxy", None)),
                region=cast(Any, options.get("region", "us-en")),
                options=cast(Any, options.get("options", None)),
            ))
            return self._new_action_ids(agent, before)
        if need_name == "web_browse":
            from agently.builtins.actions import Browse
            before = self._agent_action_ids(agent)
            agent.use_actions(Browse())
            return self._new_action_ids(agent, before)
        if need_name == "workspace_write":
            before = self._agent_action_ids(agent)
            options = _ensure_dict(policy.get("workspace"))
            root = options.get("root", policy.get("workspace_root", None))
            if root is None:
                workspace = getattr(agent, "workspace", None)
                enable_file_actions = getattr(workspace, "enable_file_actions", None)
                if callable(enable_file_actions):
                    enable_file_actions(agent, write=True)
                else:
                    agent.enable_workspace_file_actions(write=True)
            else:
                agent.enable_workspace_file_actions(root=root, write=True)
            return self._new_action_ids(agent, before)
        if need_name == "workspace_read":
            before = self._agent_action_ids(agent)
            options = _ensure_dict(policy.get("workspace"))
            root = options.get("root", policy.get("workspace_root", None))
            if root is None:
                workspace = getattr(agent, "workspace", None)
                enable_file_actions = getattr(workspace, "enable_file_actions", None)
                if callable(enable_file_actions):
                    enable_file_actions(agent, write=False)
                else:
                    agent.enable_workspace_file_actions(write=False)
            else:
                agent.enable_workspace_file_actions(root=root, write=False)
            return self._new_action_ids(agent, before)
        if need_name == "python":
            before = self._agent_action_ids(agent)
            action_id = str(_ensure_dict(policy.get("python")).get("action_id") or "run_python")
            agent.enable_python(action_id=action_id)
            return self._new_action_ids(agent, before) or [action_id]
        if need_name in {"shell", "script_run"}:
            before = self._agent_action_ids(agent)
            root = self._skill_root_for_selection(selection)
            commands = self._script_commands(selection, need)
            action_id = self._script_action_id(need, selection)
            agent.enable_shell(root=root, commands=commands or None, action_id=action_id)
            return self._new_action_ids(agent, before) or [action_id]
        if need_name == "http_request":
            return self._mount_http_request(agent, _ensure_dict(policy.get("http_request")))
        if need_name == "mcp":
            mcp_config = _ensure_dict(policy.get("mcp")).get("config")
            if not mcp_config:
                raise RuntimeError("MCP capability requires skills.capability_policy.mcp.config.")
            await agent.async_use_mcp(mcp_config)
            return ["mcp"]
        raise RuntimeError(f"No built-in capability loader for need '{ need_name }'.")

    def _web_search_options(self, policy: dict[str, Any]) -> dict[str, Any]:
        options = _ensure_dict(policy.get("web_search"))
        search_options = _ensure_dict(policy.get("search"))
        merged = {**search_options, **options}
        merged.setdefault("backend", "auto")
        return merged

    def _agent_action_ids(self, agent: Any) -> set[str]:
        action = getattr(agent, "action", None)
        get_action_list = getattr(action, "get_action_list", None)
        if not callable(get_action_list):
            return set()
        agent_name = str(getattr(agent, "name", "agent"))
        action_items = cast(list[dict[str, Any]], get_action_list(tags=[f"agent-{ agent_name }"]))
        return {
            str(item.get("action_id") or item.get("name") or "")
            for item in action_items
            if str(item.get("action_id") or item.get("name") or "")
        }

    def _new_action_ids(self, agent: Any, before: set[str]) -> list[str]:
        return sorted(self._agent_action_ids(agent) - before)

    def _skill_root_for_selection(self, selection: dict[str, Any]) -> Path:
        raw = _ensure_dict(selection.get("source")).get("installed_path")
        if raw:
            return Path(str(raw)).expanduser().resolve()
        return Path(".").resolve()

    def _script_commands(self, selection: dict[str, Any], need: dict[str, Any]) -> list[str]:
        # Local interpreters plus the Skill's declared script paths. Package
        # runners (npx/npm exec) are deliberately excluded from the default
        # allowlist: they fetch and execute arbitrary remote code, defeating the
        # allowlist. Hosts that need them must add them through capability policy.
        commands = ["bash", "sh", "python", "python3", "node"]
        resource_path = str(need.get("resource_path") or "")
        if resource_path:
            name = Path(resource_path).name
            if name:
                commands.append(name)
                commands.append(resource_path)
        for item in _ensure_list(_ensure_dict(selection.get("resource_index")).get("resources")):
            if not isinstance(item, dict) or str(item.get("kind")) != "script":
                continue
            path = str(item.get("path") or "")
            if path:
                commands.append(path)
                commands.append(Path(path).name)
        deduped: list[str] = []
        for command in commands:
            if command and command not in deduped:
                deduped.append(command)
        return deduped

    def _script_action_id(self, need: dict[str, Any], selection: dict[str, Any]) -> str:
        skill_id = str(selection.get("skill_id") or need.get("skill_id") or "skill")
        base = skill_id.replace(".", "_").replace("-", "_")
        return f"run_{ base }_script"

    @staticmethod
    def _http_host_allowed(host: str, http_policy: dict[str, Any]) -> bool:
        """Block private/loopback/link-local targets unless explicitly allowed.

        Default-deny SSRF guard for the built-in read-only HTTP capability. A host
        policy may allow internal hosts via `allow_private: true` or an explicit
        `allow_hosts` list, and may extend the denylist via `deny_hosts`.
        """
        host = (host or "").strip().lower()
        if not host:
            return False
        deny_hosts = {str(item).strip().lower() for item in _ensure_list(http_policy.get("deny_hosts"))}
        if host in deny_hosts:
            return False
        allow_hosts = {str(item).strip().lower() for item in _ensure_list(http_policy.get("allow_hosts"))}
        if host in allow_hosts:
            return True
        if bool(http_policy.get("allow_private")):
            return True

        def is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            )

        # A literal IP is checked directly; a hostname is resolved and every
        # resolved address must be public (fail closed on resolution failure).
        try:
            return not is_blocked(ipaddress.ip_address(host))
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if is_blocked(ip):
                return False
        return True

    def _mount_http_request(self, agent: Any, http_policy: dict[str, Any] | None = None) -> list[str]:
        before = self._agent_action_ids(agent)
        host_policy = _ensure_dict(http_policy)

        def http_request(url: str, method: str = "GET", headers: dict[str, str] | None = None, body: str | None = None, timeout: int = 20):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("Only http/https URLs are allowed.")
            if not self._http_host_allowed(parsed.hostname or "", host_policy):
                raise ValueError(
                    "The built-in Skills HTTP capability blocks private, loopback, and link-local hosts. "
                    "Allow an internal host through skills.capability_policy.http_request.allow_hosts."
                )
            resolved_method = method.upper()
            if resolved_method not in {"GET", "HEAD"}:
                raise ValueError("The built-in Skills HTTP capability is read-only and supports GET/HEAD only.")
            data = body.encode("utf-8") if body and resolved_method != "GET" else None
            request = Request(url, data=data, headers=headers or {}, method=resolved_method)
            with urlopen(request, timeout=timeout) as response:
                content = response.read(200000)
                return {
                    "url": url,
                    "status": response.status,
                    "headers": dict(response.headers.items()),
                    "content": content.decode("utf-8", errors="replace"),
                    "truncated": len(content) >= 200000,
                }

        agent.register_action(
            name="http_request",
            desc="Perform a read-only HTTP GET/HEAD request for Skills-guided research or API inspection.",
            kwargs={
                "url": (str, "HTTP or HTTPS URL."),
                "method": (str, "GET or HEAD. Default: GET."),
                "headers": (dict, "Optional request headers."),
                "timeout": (int, "Timeout seconds. Default: 20."),
            },
            func=http_request,
        )
        return self._new_action_ids(agent, before) or ["http_request"]

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
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None,
        default_output_format: Any = "json",
    ) -> Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"]:
        candidate = str(
            output_format
            or plan.get("expected_result_format")
            or default_output_format
        )
        if candidate not in {"json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"}:
            raise ValueError(
                "Skill execution output_format must be one of: json, flat_markdown, hybrid, "
                "xml_field, yaml_literal, auto."
            )
        return cast(Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"], candidate)

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
