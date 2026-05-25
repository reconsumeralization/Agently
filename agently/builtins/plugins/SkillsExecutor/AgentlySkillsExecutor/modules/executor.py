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
from inspect import isawaitable
from pathlib import Path
from typing import Any, Literal, cast

from agently.types.data import SkillContract, SkillExecutionDict, SkillExecutionPlan, SkillExecutionStatus
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

        capability_result = await self._mount_declared_capabilities(context=context, plan=plan)
        runtime_stream.extend(capability_result["runtime_stream"])
        if capability_result["status"] in {"blocked", "approval_required"}:
            return self._build_execution(
                execution_id=execution_id,
                status="blocked",
                plan=plan,
                runtime_stream=runtime_stream,
                skill_logs=skill_logs,
                output={
                    "error": "Skill capability mounting is blocked.",
                    "diagnostics": capability_result["diagnostics"],
                },
                effort=effort,
            )

        effort_config = self._resolve_effort(context, effort)
        strategy_name = self._resolve_strategy_name(plan=plan, effort=effort, effort_config=effort_config)
        strategy_handler = self._strategy_handlers().get(strategy_name)
        if strategy_handler is None:
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
        if effort_name and effort_name in self.effort_strategy_handlers:
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
            output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None = None,
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
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None = None,
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

    async def _mount_declared_capabilities(
        self,
        *,
        context: SkillsExecutionContext,
        plan: SkillExecutionPlan,
    ) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        runtime_stream: list[dict[str, Any]] = []
        policy = _ensure_dict(plan.get("capability_policy"))
        auto_allow = bool(policy.get("auto_allow"))
        agent = getattr(context, "agent", None)
        if agent is None:
            return {"status": "success", "diagnostics": diagnostics, "runtime_stream": runtime_stream}

        for selection in _ensure_list(plan.get("selected_skills")):
            skill_id = str(_ensure_dict(selection).get("skill_id") or "")
            if not skill_id:
                continue
            try:
                contract = self.registry.inspect_skills(skill_id)
            except Exception as error:
                diagnostics.append({
                    "level": "warning",
                    "code": "capability_contract_unreadable",
                    "skill_id": skill_id,
                    "message": str(error),
                })
                continue
            fm = _ensure_dict(_ensure_dict(contract.get("metadata")).get("frontmatter"))
            mcp_config = self._mcp_config_from_frontmatter(fm)
            if mcp_config:
                if self._mcp_requires_approval(mcp_config) and not auto_allow:
                    diagnostics.append({
                        "level": "error",
                        "code": "approval_required",
                        "skill_id": skill_id,
                        "capability": "mcp",
                        "message": "Skill-declared stdio/local command MCP requires auto_allow=True or an approval handler.",
                    })
                    return {"status": "approval_required", "diagnostics": diagnostics, "runtime_stream": runtime_stream}
                try:
                    await agent.async_use_mcp(mcp_config)
                    item = {
                        "type": "skills.capability.mounted",
                        "action": "mounted",
                        "skill_id": skill_id,
                        "capability": "mcp",
                    }
                    runtime_stream.append(item)
                    diagnostics.append({
                        "level": "info",
                        "code": "capability_mounted",
                        "skill_id": skill_id,
                        "capability": "mcp",
                    })
                except Exception as error:
                    diagnostics.append({
                        "level": "error",
                        "code": "capability_mount_failed",
                        "skill_id": skill_id,
                        "capability": "mcp",
                        "message": str(error),
                    })
                    return {"status": "blocked", "diagnostics": diagnostics, "runtime_stream": runtime_stream}

            bash_actions = self._bash_action_ids_from_contract(contract)
            if bash_actions:
                if not auto_allow:
                    diagnostics.append({
                        "level": "error",
                        "code": "approval_required",
                        "skill_id": skill_id,
                        "capability": "bash_action",
                        "message": "Skill-declared Bash/scripts require auto_allow=True or an approval handler before runtime actions are mounted.",
                    })
                    return {"status": "approval_required", "diagnostics": diagnostics, "runtime_stream": runtime_stream}
                mounted = self._mount_bash_actions_for_skill(agent=agent, contract=contract, action_ids=bash_actions)
                for action_id in mounted:
                    item = {
                        "type": "skills.capability.mounted",
                        "action": "mounted",
                        "skill_id": skill_id,
                        "capability": "bash_action",
                        "action_id": action_id,
                    }
                    runtime_stream.append(item)
                    diagnostics.append({
                        "level": "info",
                        "code": "capability_mounted",
                        "skill_id": skill_id,
                        "capability": "bash_action",
                        "action_id": action_id,
                    })

            missing_result = self._resolve_missing_declared_actions(
                agent=agent,
                contract=contract,
            )
            runtime_stream.extend(missing_result["runtime_stream"])
            diagnostics.extend(missing_result["diagnostics"])
            if missing_result["status"] != "success":
                return {
                    "status": missing_result["status"],
                    "diagnostics": diagnostics,
                    "runtime_stream": runtime_stream,
                }
        return {"status": "success", "diagnostics": diagnostics, "runtime_stream": runtime_stream}

    def _mcp_config_from_frontmatter(self, frontmatter: dict[str, Any]) -> Any:
        if isinstance(frontmatter.get("mcpServers"), dict):
            return {"mcpServers": frontmatter["mcpServers"]}
        if isinstance(frontmatter.get("mcp_servers"), dict):
            return {"mcpServers": frontmatter["mcp_servers"]}
        mcp = frontmatter.get("mcp")
        if isinstance(mcp, str) and mcp.strip():
            return mcp.strip()
        if isinstance(mcp, dict):
            if isinstance(mcp.get("mcpServers"), dict):
                return mcp
            if mcp.get("url") or mcp.get("command"):
                return {"mcpServers": {"default": mcp}}
        return None

    def _mcp_requires_approval(self, mcp_config: Any) -> bool:
        if isinstance(mcp_config, str):
            return not (mcp_config.startswith("http://") or mcp_config.startswith("https://"))
        if not isinstance(mcp_config, dict):
            return True
        servers = _ensure_dict(mcp_config.get("mcpServers"))
        for config in servers.values():
            if not isinstance(config, dict):
                continue
            if config.get("command") or config.get("args"):
                return True
        return False

    def _bash_action_ids_from_contract(self, contract: SkillContract) -> list[str]:
        metadata = _ensure_dict(contract.get("metadata"))
        fm = _ensure_dict(metadata.get("frontmatter"))
        action_ids: list[str] = []
        for tool_name in self._declared_tool_names(fm.get("allowed-tools") or fm.get("allowed_tools")):
            base = tool_name.lower()
            if base in {"bash", "shell", "run_bash"} and tool_name not in action_ids:
                action_ids.append(tool_name)
        has_scripts = self._contract_has_scripts(contract)
        if has_scripts and (fm.get("allow-scripts") or fm.get("allow_scripts")) and "run_bash" not in action_ids:
            action_ids.append("run_bash")
        return action_ids

    def _contract_has_scripts(self, contract: SkillContract) -> bool:
        return any(
            str(item.get("kind")) == "script"
            for item in _ensure_list(_ensure_dict(contract.get("resource_index")).get("resources"))
            if isinstance(item, dict)
        )

    def _declared_tool_names(self, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        names: list[str] = []
        for item in values:
            if not isinstance(item, str):
                continue
            raw = item.strip()
            if not raw:
                continue
            name = raw.split("(", 1)[0].strip()
            if name and name not in names:
                names.append(name)
        return names

    def _mount_bash_actions_for_skill(
        self,
        *,
        agent: Any,
        contract: SkillContract,
        action_ids: list[str],
    ) -> list[str]:
        skill_id = str(contract.get("skill_id") or "skill")
        skill_root = Path(str(_ensure_dict(contract.get("source")).get("installed_path") or "")).expanduser().resolve()
        commands = self._skill_script_command_prefixes(contract)
        mounted: list[str] = []
        for action_id in action_ids:
            if not isinstance(action_id, str) or not action_id.strip():
                continue
            resolved_id = action_id.strip()
            desc = (
                f"Runtime shell capability mounted for Skill '{ skill_id }'. "
                f"Commands are restricted to the installed Skill directory: { skill_root }."
            )
            if hasattr(agent, "enable_shell"):
                agent.enable_shell(
                    root=skill_root,
                    commands=commands,
                    action_id=resolved_id,
                    desc=desc,
                    expose_to_model=True,
                )
                mounted.append(resolved_id)
        return mounted

    def _skill_script_command_prefixes(self, contract: SkillContract) -> list[str]:
        commands = ["bash", "sh", "python", "python3", "node", "npx", "npm"]
        for item in _ensure_list(_ensure_dict(contract.get("resource_index")).get("resources")):
            if not isinstance(item, dict) or str(item.get("kind")) != "script":
                continue
            path = Path(str(item.get("path") or ""))
            if path.name and path.name not in commands:
                commands.append(path.name)
        return commands

    def _resolve_missing_declared_actions(
        self,
        *,
        agent: Any,
        contract: SkillContract,
    ) -> dict[str, Any]:
        skill_id = str(contract.get("skill_id") or "")
        diagnostics: list[dict[str, Any]] = []
        runtime_stream: list[dict[str, Any]] = []
        missing: list[str] = []
        unsafe_missing: list[str] = []
        synthesized: list[str] = []

        for action_id in self._declared_action_names(contract):
            if self._agent_has_action(agent, action_id):
                continue
            if self._can_synthesize_python_action(action_id):
                if self._mount_python_sandbox_action(agent=agent, contract=contract, action_id=action_id):
                    synthesized.append(action_id)
                    runtime_stream.append({
                        "type": "skills.capability.mounted",
                        "action": "mounted",
                        "skill_id": skill_id,
                        "capability": "python_sandbox_action",
                        "action_id": action_id,
                    })
                    diagnostics.append({
                        "level": "info",
                        "code": "capability_synthesized",
                        "skill_id": skill_id,
                        "capability": "python_sandbox_action",
                        "action_id": action_id,
                    })
                    continue
            if self._is_business_or_external_capability(action_id):
                unsafe_missing.append(action_id)
            else:
                missing.append(action_id)

        if unsafe_missing:
            diagnostics.append({
                "level": "error",
                "code": "capability_missing",
                "skill_id": skill_id,
                "capability": "business_action",
                "required": unsafe_missing,
                "message": (
                    "Skill declares business/external capabilities but no Action, MCP tool, "
                    "or connector is mounted. Provide a real backend; Python sandbox synthesis is not allowed."
                ),
            })
            return {"status": "blocked", "diagnostics": diagnostics, "runtime_stream": runtime_stream}
        if missing:
            diagnostics.append({
                "level": "error",
                "code": "capability_missing",
                "skill_id": skill_id,
                "capability": "action",
                "required": missing,
                "message": "Skill declares capabilities that are not mounted and cannot be safely synthesized.",
            })
            return {"status": "blocked", "diagnostics": diagnostics, "runtime_stream": runtime_stream}
        return {"status": "success", "diagnostics": diagnostics, "runtime_stream": runtime_stream, "synthesized": synthesized}

    def _declared_action_names(self, contract: SkillContract) -> list[str]:
        metadata = _ensure_dict(contract.get("metadata"))
        fm = _ensure_dict(metadata.get("frontmatter"))
        names: list[str] = []
        bash_actions = set(self._bash_action_ids_from_contract(contract))
        if not self._mcp_config_from_frontmatter(fm):
            for name in self._declared_tool_names(fm.get("allowed-tools") or fm.get("allowed_tools") or []):
                if name in bash_actions:
                    continue
                if name and name not in names:
                    names.append(name)
        actions = fm.get("allowed-actions") or fm.get("allowed_actions") or []
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, str):
                    name = self._declared_tool_names(action)
                    if name and name[0] not in names:
                        names.append(name[0])
        return names

    def _agent_has_action(self, agent: Any, action_id: str) -> bool:
        registry = getattr(getattr(agent, "action", None), "action_registry", None)
        has_action = getattr(registry, "has", None)
        return bool(callable(has_action) and has_action(action_id))

    def _mount_python_sandbox_action(
        self,
        *,
        agent: Any,
        contract: SkillContract,
        action_id: str,
    ) -> bool:
        action = getattr(agent, "action", None)
        register = getattr(action, "register_python_sandbox_action", None)
        if not callable(register):
            return False
        skill_id = str(contract.get("skill_id") or "skill")
        agent_name = str(getattr(agent, "name", "agent"))
        register(
            action_id=action_id,
            desc=(
                f"Ephemeral pure-Python sandbox action synthesized for Skill '{ skill_id }'. "
                "Use only for deterministic in-memory calculation, parsing, validation, "
                "format conversion, or data shaping. Do not access network, secrets, files, "
                "subprocesses, business systems, or external services. Assign final output to `result`."
            ),
            tags=[f"agent-{ agent_name }", f"skill-{ skill_id }", "skills-synthesized"],
            expose_to_model=True,
        )
        return True

    def _can_synthesize_python_action(self, action_id: str) -> bool:
        lowered = action_id.lower().replace("-", "_")
        if self._is_business_or_external_capability(lowered):
            return False
        safe_terms = {
            "calculate",
            "compute",
            "validate",
            "check",
            "parse",
            "format",
            "transform",
            "convert",
            "normalize",
            "extract",
            "score",
            "rank",
            "compare",
            "merge",
            "split",
            "dedupe",
            "filter",
            "aggregate",
            "summarize",
            "render",
        }
        return any(term in lowered for term in safe_terms)

    def _is_business_or_external_capability(self, action_id: str) -> bool:
        lowered = action_id.lower().replace("-", "_")
        risky_terms = {
            "api",
            "http",
            "url",
            "fetch",
            "search",
            "browse",
            "web",
            "download",
            "upload",
            "file",
            "read",
            "write",
            "save",
            "delete",
            "remove",
            "create",
            "update",
            "patch",
            "post",
            "put",
            "send",
            "email",
            "notify",
            "slack",
            "sms",
            "crm",
            "salesforce",
            "hubspot",
            "database",
            "db",
            "sql",
            "query",
            "payment",
            "charge",
            "refund",
            "invoice",
            "order",
            "booking",
            "calendar",
            "github",
            "jira",
            "notion",
            "sheet",
            "docx",
            "pdf",
            "pptx",
            "mcp",
            "bash",
            "shell",
            "python",
            "node",
        }
        return any(term in lowered for term in risky_terms)

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
            for t in self._declared_tool_names(fm.get("allowed-tools") or fm.get("allowed_tools") or []):
                if t not in allowed_tools:
                    allowed_tools.append(t)
            actions = fm.get("allowed-actions") or fm.get("allowed_actions") or []
            if isinstance(actions, list):
                for a in actions:
                    if isinstance(a, str) and a not in allowed_actions:
                        allowed_actions.append(a)
            if fm.get("allow-scripts") or fm.get("allow_scripts"):
                allow_scripts = True
                if self._contract_has_scripts(contract) and "run_bash" not in allowed_tools:
                    allowed_tools.append("run_bash")

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
