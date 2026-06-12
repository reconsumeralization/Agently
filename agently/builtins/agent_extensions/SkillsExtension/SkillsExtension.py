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

from typing import TYPE_CHECKING, Any, Literal, cast

from agently.core import BaseAgent
from agently.types.data import SkillContextPack, SkillContextPackIncludeMode, SkillContract, SkillExecutionPlan, SkillMode, SkillRuntimeStreamHandler
from agently.types.plugins import SkillsExecutor
from agently.utils import DeprecationWarnings, FunctionShifter
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list
from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.planner import (
    _matches_selector,
    _matches_skills_pack_selector,
)

from ._SkillsContext import create_agent_skills_runtime_context

if TYPE_CHECKING:
    from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.executor import SkillExecution
    from agently.core import Prompt
    from agently.utils import Settings


class SkillsExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from agently.base import skills_executor

        self.skills_executor = cast(SkillsExecutor, skills_executor)

        self.__session_skill_selectors: list[Any] = []
        self.__session_skills_pack_selectors: list[Any] = []
        self.__skill_execution_logs: list[Any] = []

        request_prefixes = self.extension_handlers.get("request_prefixes", [])
        if not isinstance(request_prefixes, list):
            request_prefixes = []
        self.extension_handlers.set("request_prefixes", [self.__request_prefix, *request_prefixes])
        self.extension_handlers.append("finally", self.__finally)

    # ── User-facing API ─────────────────────────────────────────────────────

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
        always: bool = False,
    ):
        if not always:
            return self.create_execution().use_skills(skills, mode=mode, auto_allow=auto_allow)
        self._add_skill_selectors(skills, mode=mode, auto_allow=auto_allow)
        return self

    def _normalize_skill_selector_entries(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> list[dict[str, Any]]:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        entries: list[dict[str, Any]] = []
        for item in _ensure_list(skills):
            selector = _copy_public(item)
            if isinstance(selector, dict):
                selector.setdefault("auto_allow", bool(auto_allow))
            elif auto_allow and isinstance(selector, str):
                raw_selector = selector.strip()
                if "://" in raw_selector or raw_selector.startswith("git@") or "/" in raw_selector:
                    selector = {"source": raw_selector, "auto_allow": True}
                else:
                    selector = {"id": raw_selector, "auto_allow": True}
            entries.append({"selector": selector, "mode": mode})
        return entries

    def _add_skill_selectors(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> list[dict[str, Any]]:
        entries = self._normalize_skill_selector_entries(skills, mode=mode, auto_allow=auto_allow)
        self.__session_skill_selectors.extend(entries)
        return entries

    def require_skills(
        self,
        skills: Any,
        *,
        auto_allow: bool = False,
        always: bool = False,
    ):
        return self.use_skills(skills, mode="required", auto_allow=auto_allow, always=always)

    def use_skills_packs(
        self,
        skills_packs: Any,
        *,
        mode: SkillMode = "model_decision",
        always: bool = False,
    ):
        if not always:
            return self.create_execution().use_skills_packs(skills_packs, mode=mode)
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        for item in _ensure_list(skills_packs):
            self.__session_skills_pack_selectors.append({"selector": _copy_public(item), "mode": mode})
        return self

    def configure_skill_capabilities(
        self,
        *,
        auto_load: dict[str, str] | None = None,
        workspace_root: str | None = None,
        mcp_config: Any = None,
        python: dict[str, Any] | None = None,
        search: dict[str, Any] | None = None,
    ):
        policy = _ensure_dict(self.settings.get("skills.capability_policy", {}))
        if auto_load is not None:
            policy["auto_load"] = dict(auto_load)
        if workspace_root is not None:
            workspace = _ensure_dict(policy.get("workspace"))
            workspace["root"] = workspace_root
            policy["workspace"] = workspace
        if mcp_config is not None:
            mcp = _ensure_dict(policy.get("mcp"))
            mcp["config"] = _copy_public(mcp_config)
            policy["mcp"] = mcp
        if python is not None:
            policy["python"] = dict(python)
        if search is not None:
            policy["web_search"] = dict(search)
        self.settings.set("skills.capability_policy", policy)
        return self

    def _skills_prompt_defaults(
        self,
        task: str | None,
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> tuple[str, Any, Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"]]:
        if output is not None and semantic_outputs is not None:
            raise ValueError("Use either output= or semantic_outputs= for Skills execution, not both.")
        if semantic_outputs is not None:
            DeprecationWarnings.warn_deprecated_once(
                "skills_executor.semantic_outputs.execution",
                "semantic_outputs= is deprecated for Skills execution; use output= instead.",
                stacklevel=3,
            )
        prompt_defaults = self._dynamic_task_prompt_defaults(task)
        resolved_task = task if task is not None and prompt_defaults["target"] is None else prompt_defaults["target"]
        if not resolved_task:
            raise ValueError("Skills execution requires task=... or a configured agent.input(...).")
        explicit_output = output if output is not None else semantic_outputs
        resolved_outputs = explicit_output if explicit_output is not None else prompt_defaults["output_schema"]
        resolved_format = output_format or cast(Any, prompt_defaults["output_format"])
        return str(resolved_task), resolved_outputs, cast(Any, resolved_format)

    async def async_resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> SkillExecutionPlan:
        task, output, output_format = self._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        selectors = self._collect_skill_selectors(skills=skills, mode=mode)
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=skills_packs, mode=mode)
        context = create_agent_skills_runtime_context(self)
        return await self.skills_executor.async_resolve_plan(
            context=context,
            task=task,
            skills=selectors,
            skills_packs=skills_pack_selectors,
            mode=mode,
            output=output,
            output_format=output_format,
        )

    def resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> SkillExecutionPlan:
        return FunctionShifter.syncify(self.async_resolve_skills_plan)(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )

    async def async_build_skills_context_pack(
        self,
        task: str | None = None,
        *,
        intent: str | None = None,
        skill_ids: list[str] | tuple[str, ...] | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        include_guidance: bool = True,
        include_examples: SkillContextPackIncludeMode = "auto",
        include_references: SkillContextPackIncludeMode = "auto",
        include_assets: SkillContextPackIncludeMode = False,
        include_public_lookup: bool = False,
        actionize_scripts: bool = False,
        budget_chars: int = 12000,
        max_resource_chars: int = 6000,
    ) -> SkillContextPack:
        prompt_defaults = self._dynamic_task_prompt_defaults(task)
        resolved_task = task if task is not None and prompt_defaults["target"] is None else prompt_defaults["target"]
        selectors = self._collect_skill_selectors(skills=skills, mode="model_decision")
        required_selectors = self._collect_skill_selectors(skills=None, mode="required")
        selectors.extend(required_selectors)
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=skills_packs, mode="model_decision")
        skills_pack_selectors.extend(self._collect_skills_pack_selectors(skills_packs=None, mode="required"))
        context = create_agent_skills_runtime_context(
            self,
            resource_reader=lambda sid, path, mb: self.skills_executor.read_resource(
                sid, path, max_bytes=mb
            ),
        )
        return await self.skills_executor.async_build_context_pack(
            context=context,
            task=str(resolved_task or ""),
            intent=intent,
            skill_ids=skill_ids,
            skills=selectors,
            skills_packs=skills_pack_selectors,
            include_guidance=include_guidance,
            include_examples=include_examples,
            include_references=include_references,
            include_assets=include_assets,
            include_public_lookup=include_public_lookup,
            actionize_scripts=actionize_scripts,
            budget_chars=budget_chars,
            max_resource_chars=max_resource_chars,
        )

    def build_skills_context_pack(
        self,
        task: str | None = None,
        *,
        intent: str | None = None,
        skill_ids: list[str] | tuple[str, ...] | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        include_guidance: bool = True,
        include_examples: SkillContextPackIncludeMode = "auto",
        include_references: SkillContextPackIncludeMode = "auto",
        include_assets: SkillContextPackIncludeMode = False,
        include_public_lookup: bool = False,
        actionize_scripts: bool = False,
        budget_chars: int = 12000,
        max_resource_chars: int = 6000,
    ) -> SkillContextPack:
        return FunctionShifter.syncify(self.async_build_skills_context_pack)(
            task,
            intent=intent,
            skill_ids=skill_ids,
            skills=skills,
            skills_packs=skills_packs,
            include_guidance=include_guidance,
            include_examples=include_examples,
            include_references=include_references,
            include_assets=include_assets,
            include_public_lookup=include_public_lookup,
            actionize_scripts=actionize_scripts,
            budget_chars=budget_chars,
            max_resource_chars=max_resource_chars,
        )

    async def async_run_skills_task(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        stream_handler: SkillRuntimeStreamHandler | None = None,
        effort: str | None = None,
    ) -> "SkillExecution":
        task, output, output_format = self._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        self.request.prompt.clear()
        plan = await self.async_resolve_skills_plan(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            output_format=output_format,
        )
        execution = await self.async_execute_skills_plan(
            task,
            plan=plan,
            output_format=output_format,
            stream_handler=stream_handler,
            effort=effort,
        )
        self.__skill_execution_logs.append(execution.to_dict())
        return execution

    def run_skills_task(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        stream_handler: SkillRuntimeStreamHandler | None = None,
        effort: str | None = None,
    ) -> "SkillExecution":
        return FunctionShifter.syncify(self.async_run_skills_task)(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
            stream_handler=stream_handler,
            effort=effort,
        )

    async def async_execute_skills_plan(
        self,
        task: str,
        *,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        stream_handler: SkillRuntimeStreamHandler | None = None,
        effort: str | None = None,
    ) -> "SkillExecution":
        context = create_agent_skills_runtime_context(
            self,
            runtime_stream_handler=stream_handler,
            resource_reader=lambda sid, path, mb: self.skills_executor.read_resource(
                sid, path, max_bytes=mb
            ),
        )
        return await self.skills_executor.async_execute_plan(
            context=context,
            task=task,
            plan=plan,
            output_format=output_format,
            effort=effort,
        )

    async def async_execute_skills_plans(
        self,
        task: str,
        *,
        plans: list[SkillExecutionPlan],
        mode: Literal["concurrent", "sequential"] = "concurrent",
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        stream_handler: SkillRuntimeStreamHandler | None = None,
        effort: str | None = None,
    ) -> list[Any]:
        """Execute multiple skill plans concurrently or sequentially.

        In *concurrent* mode, plans execute through TriggerFlow batch fan-out and
        results are returned in the same order as *plans*.

        In *sequential* mode, TriggerFlow executes each plan in order and folds
        the previous result into the next task context.
        """
        from agently.core.orchestration.TriggerFlow import TriggerFlow

        if mode == "sequential":
            flow = TriggerFlow(name="skills-plans-sequential")

            async def init(data: Any):
                await data.async_set_state("results", [])
                await data.async_set_state("task", task)
                return task

            chain = flow.to(init)

            for i, plan in enumerate(plans):
                async def run_plan(data: Any, *, index: int = i, current_plan: SkillExecutionPlan = plan):
                    results = list(data.get_state("results", []) or [])
                    accumulated = data.get_state("task", task)
                    if index > 0 and results:
                        prev = results[-1]
                        prev_output = getattr(prev, "output", prev) if hasattr(prev, "output") else prev
                        accumulated = f"{task}\n\n[Prior result from skill {index}]: {str(prev_output)[:2000]}"
                        await data.async_set_state("task", accumulated)
                    ctx = create_agent_skills_runtime_context(
                        self,
                        runtime_stream_handler=stream_handler,
                        resource_reader=lambda sid, path, mb: self.skills_executor.read_resource(
                            sid, path, max_bytes=mb
                        ),
                    )
                    exec_result = await self.skills_executor.async_execute_plan(
                        context=ctx,
                        task=accumulated,
                        plan=current_plan,
                        output_format=output_format,
                        effort=effort,
                    )
                    results.append(exec_result)
                    self.__skill_execution_logs.append(exec_result.to_dict())
                    await data.async_set_state("results", results)
                    return exec_result

                chain = chain.to(run_plan)

            execution = flow.create_execution(auto_close=False)
            await execution.async_start(task)
            state = await execution.async_close()
            return list(state.get("results", []) or [])

        # concurrent mode — TriggerFlow owns fan-out / collect
        chunks = []
        for i, plan in enumerate(plans):
            async def run_one(data: Any, *, index: int = i, current_plan: SkillExecutionPlan = plan):
                ctx = create_agent_skills_runtime_context(
                    self,
                    runtime_stream_handler=stream_handler,
                    resource_reader=lambda sid, path, mb: self.skills_executor.read_resource(
                        sid, path, max_bytes=mb
                    ),
                )
                exec_result = await self.skills_executor.async_execute_plan(
                    context=ctx,
                    task=task,
                    plan=current_plan,
                    output_format=output_format,
                    effort=effort,
                )
                self.__skill_execution_logs.append(exec_result.to_dict())
                return {"index": index, "execution": exec_result}

            chunks.append((f"plan_{i}", run_one))

        flow = TriggerFlow(name="skills-plans-concurrent")

        async def collect(data: Any):
            keyed_results = data.value if isinstance(data.value, dict) else {}
            ordered: list[Any] = [None] * len(plans)
            for item in keyed_results.values():
                if isinstance(item, dict):
                    index = item.get("index")
                    if isinstance(index, int) and 0 <= index < len(ordered):
                        ordered[index] = item.get("execution")
            await data.async_set_state("results", ordered)
            return ordered

        flow.batch(*chunks).to(collect)
        execution = flow.create_execution(auto_close=False)
        await execution.async_start(task)
        state = await execution.async_close()
        return list(state.get("results", []) or [])

    def get_skills_execution_logs(self) -> list[dict[str, Any]]:
        return _copy_public(self.__skill_execution_logs)

    # ── Selector collection ─────────────────────────────────────────────────

    def _collect_skill_selectors(self, *, skills: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills is not None:
            selectors.extend(_ensure_list(skills))
        for item in self.__session_skill_selectors:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    def _collect_skills_pack_selectors(self, *, skills_packs: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills_packs is not None:
            selectors.extend(_ensure_list(skills_packs))
        for item in self.__session_skills_pack_selectors:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    # ── Prompt injection ────────────────────────────────────────────────────

    async def _apply_skill_cards_to_prompt(self, prompt: "Prompt"):
        selectors = self._collect_skill_selectors(skills=None, mode="model_decision")
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=None, mode="model_decision")
        if not selectors and not skills_pack_selectors:
            return
        cards = []
        guidance = []
        settings = getattr(self, "settings")
        include_guidance = bool(settings.get("skills.prompt.include_primary_guidance", True))
        max_guidance_chars = int(settings.get("skills.prompt.max_guidance_chars_per_skill", 6000) or 6000)
        for record in self.skills_executor.list_skills():
            contract = self.skills_executor.inspect_skills(str(record["skill_id"]))
            if any(_matches_selector(contract, selector) for selector in selectors) or any(
                _matches_skills_pack_selector(contract, selector) for selector in skills_pack_selectors
            ):
                cards.append(contract.get("card", {}))
                if include_guidance:
                    guidance.extend(self._collect_prompt_guidance(contract, max_chars=max_guidance_chars))
        if not cards:
            return
        prompt_mode = str(settings.get("skills.prompt.mode", settings.get("agent.auto_orchestration.skills_prompt_mode", "route_owned")))
        if prompt_mode == "route_owned":
            prompt.append(
                "info",
                {
                    "skill_candidates": cards,
                    "skill_instruction": (
                        "These skills are route candidates for Agent auto-orchestration. "
                        "Do not claim that a Skill was executed unless the selected route provides skill execution logs."
                    ),
                },
            )
            return
        payload = {
            "skill_cards": cards,
            "skill_instruction": (
                "These skills are optional behavior-loop candidates. "
                "Use them only when they fit the task; otherwise answer normally."
            ),
        }
        if guidance:
            payload["skill_guidance"] = guidance
        prompt.append("info", payload)

    def _clear_request_skill_selectors(self):
        return

    def _collect_prompt_guidance(self, contract: SkillContract, *, max_chars: int) -> list[dict[str, Any]]:
        guidance = _ensure_dict(contract.get("guidance"))
        content = str(guidance.get("content") or "")
        if not content.strip():
            return []
        trimmed = content[:max_chars]
        return [{
            "skill_id": str(contract.get("skill_id", "")),
            "path": str(guidance.get("path") or "SKILL.md"),
            "title": str(contract.get("card", {}).get("display_name", "")),
            "content": trimmed,
            "truncated": len(content) > len(trimmed),
        }]

    # ── Extension handlers ──────────────────────────────────────────────────

    async def __request_prefix(self, prompt: "Prompt", _settings: "Settings"):
        await self._apply_skill_cards_to_prompt(prompt)

    async def __finally(self, *_):
        self._clear_request_skill_selectors()
