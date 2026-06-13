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

import asyncio
import uuid
from pathlib import Path
from collections.abc import AsyncGenerator, Generator
from typing import Any, Literal, TYPE_CHECKING, cast

import json5
import yaml

from agently.core.application.AgentExecution import (
    AgentExecutionContext,
    AgentExecutionPromptDraft,
    AgentExecutionResult,
    AgentExecutionStream,
    RuntimeStageStallError,
    merge_stream_meta,
    normalize_execution_limits,
    normalize_execution_lineage,
)
from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter, FunctionShifter

from .bridges import (
    bridge_model_stream_item as bridge_model_stream_item_entry,
    bridge_task_dag_stream_item as bridge_task_dag_stream_item_entry,
    record_action_log as record_action_log_entry,
    record_model_response_id as record_model_response_id_entry,
)
from .diagnostics import (
    build_execution_meta,
    initial_diagnostics,
    initial_workspace_refs,
    record_error_diagnostic,
    refresh_diagnostics,
)
from .limits import (
    await_route_with_limits,
    build_execution_stall_error,
    cancel_limited_task,
)
from .result_views import (
    async_get_data as async_get_data_entry,
    async_get_meta as async_get_meta_entry,
    async_get_text as async_get_text_entry,
    get_async_generator as get_async_generator_entry,
    sync_generator as sync_generator_entry,
)
from .route_execution import async_execute_route, start_execution
from .routing import HybridRoutePlanner
from .state import (
    ExecutionOptionsState,
    apply_effort_strategy_limits,
    build_effective_options,
    configure_effort,
    configure_execution_options,
    is_task_strategy as state_is_task_strategy,
    load_strategy_state_from_options,
    normalize_options_state,
    record_consumed_option as state_record_consumed_option,
    route_options as state_route_options,
    set_execution_goals,
    set_success_criteria,
    task_goal as state_task_goal,
    task_success_criteria as state_task_success_criteria,
    task_target as state_task_target,
)
from .workspace_records import (
    append_workspace_ref,
    default_checkpoint_state,
    default_workspace_content,
    default_workspace_summary,
    record_workspace as record_workspace_entry,
    workspace_scope,
    workspace_source,
)

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        OutputValidateHandler,
        RunContext,
    )


_DYNAMIC_TASK_GRAPH_INPUT_UNSET = object()


class AgentExecution:
    """Unified execution draft, run owner, and result source for one Agent run."""

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: Any = None,
        parent_run_context: "RunContext | None" = None,
        request: Any = None,
    ):
        self.agent = getattr(agent, "_agent", agent)
        self.request = self._resolve_request(agent, request)
        self.request_prompt = self.request.prompt
        self.prompt = self.request_prompt
        self._draft = AgentExecutionPromptDraft(self.agent, self.request)
        self.id = uuid.uuid4().hex
        self.lineage: "AgentExecutionLineage" = normalize_execution_lineage(lineage)
        self.limits: "AgentExecutionLimits" = normalize_execution_limits(limits)
        self._effort_applied_limits: set[str] = set()
        self.options: ExecutionOptionsState = normalize_options_state(self, options)
        self.task_refs: dict[str, Any] = {}
        self.task_record: Any = None
        self.goal_items: list[str] = []
        self.success_criteria_items: list[str] = []
        self.generated_success_criteria: list[str] = []
        self.local_action_ids: list[str] = []
        self.local_required_action_ids: list[str] = []
        self.local_skill_selectors: list[dict[str, Any]] = []
        self.local_skills_pack_selectors: list[dict[str, Any]] = []
        self.local_dynamic_task_candidates: list[dict[str, Any]] = []
        self._agent_task_step_overrides: dict[str, Any] = {}
        self.task_options: dict[str, Any] = {}
        self.strategy_name: str | None = None
        self.effective_options: dict[str, Any] = {}
        self.consumed_options: dict[str, Any] = {}
        self.workspace = getattr(self.agent, "workspace", None)
        self._nesting_depth, self._nesting_budget = self._resolve_nesting_state()
        self.execution_context = AgentExecutionContext(
            execution_id=self.id,
            lineage=self.lineage,
            limits=self.limits,
            nesting_depth=self._nesting_depth,
            nesting_budget=self._nesting_budget,
        )
        self.parent_run_context = parent_run_context
        self.route_info: dict[str, Any] = {}
        self.route_plan: dict[str, Any] = {}
        self.close_snapshot: dict[str, Any] = {}
        self.logs: dict[str, Any] = {
            "model_response_ids": [],
            "action_logs": [],
            "artifact_refs": [],
            "route_logs": {},
        }
        self.diagnostics: dict[str, Any] = initial_diagnostics()
        self.workspace_refs: dict[str, Any] = initial_workspace_refs()
        self._load_strategy_state_from_options()
        self.effective_options = self._build_effective_options()
        apply_effort_strategy_limits(self)
        self.effective_options = self._build_effective_options()
        self.result: Any = None
        self.status = "created"
        self.prompt_snapshot: dict[str, Any] = self._snapshot_prompt()

        self._started = False
        self._completed = False
        self._start_lock = asyncio.Lock()
        self.route_planner = HybridRoutePlanner(self.agent, prompt_snapshot=self.prompt_snapshot, execution=self)
        self.stream = AgentExecutionStream(
            execution_id=self.id,
            lineage=self.lineage,
        ).bind_execution(self)
        self._error: BaseException | None = None
        self._selected_route: tuple[str, dict[str, Any]] | None = None
        self._seen_action_log_keys: set[str] = set()

        self.start = FunctionShifter.syncify(self.async_start)
        self.get_data = FunctionShifter.syncify(self.async_get_data)
        self.get_text = FunctionShifter.syncify(self.async_get_text)
        self.get_meta = FunctionShifter.syncify(self.async_get_meta)
        self.record_workspace = FunctionShifter.syncify(self.async_record_workspace)
        self.get_generator = self._get_generator
        self.run = self._compat_run
        self.async_run = self.async_start
        self.meta = self._compat_meta

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.agent, name)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any):
            result = attr(*args, **kwargs)
            return self if result is self.agent else result

        return wrapper

    def _resolve_request(self, agent: Any, request: Any):
        if request is not None:
            return request
        if getattr(agent, "_agent", None) is not None:
            return agent.request
        isolated_request = self.agent.create_request()
        pending_prompt = self.agent._snapshot_request_prompt()
        if pending_prompt:
            isolated_request.prompt.update(pending_prompt)
            self.agent.request.prompt.clear()
        return isolated_request

    def _snapshot_prompt(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get()
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, dict) else {}

    def _refresh_prompt_snapshot(self):
        self.prompt_snapshot = self._snapshot_prompt()
        self.route_planner.prompt_snapshot = dict(self.prompt_snapshot)
        self._selected_route = None
        return self

    def _load_strategy_state_from_options(self):
        load_strategy_state_from_options(self)

    def _resolve_nesting_state(self) -> tuple[int, int | None]:
        """Compute this execution's nesting depth and the effective nesting budget.

        Depth is one deeper than the currently bound parent AgentExecutionContext
        (root = 0). The budget is the most restrictive `max_nested_agent_steps`
        among the constraining ancestor and this execution's own limits.
        """
        from agently.core.runtime.RuntimeContext import get_current_agent_execution_context

        parent_context = get_current_agent_execution_context()
        parent_depth = getattr(parent_context, "nesting_depth", None)
        depth = parent_depth + 1 if isinstance(parent_depth, int) else 0
        own_budget = self.limits.get("max_nested_agent_steps")
        parent_budget = getattr(parent_context, "nesting_budget", None)
        budgets = [value for value in (parent_budget, own_budget) if isinstance(value, int)]
        budget = min(budgets) if budgets else None
        return depth, budget

    def _replace_runtime_context(self):
        self._nesting_depth, self._nesting_budget = self._resolve_nesting_state()
        self.execution_context = AgentExecutionContext(
            execution_id=self.id,
            lineage=self.lineage,
            limits=self.limits,
            nesting_depth=self._nesting_depth,
            nesting_budget=self._nesting_budget,
        )
        self.stream = AgentExecutionStream(
            execution_id=self.id,
            lineage=self.lineage,
        ).bind_execution(self)
        self._selected_route = None
        self.route_info = {}
        self.route_plan = {}
        self.effective_options = self._build_effective_options()

    def _build_effective_options(self) -> dict[str, Any]:
        return build_effective_options(self)

    def configure_options(self, options: Any):
        return configure_execution_options(self, options)

    def create_execution(
        self,
        *,
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: Any = None,
        parent_run_context: "RunContext | None" = None,
    ):
        if self._started:
            if any(value is not None for value in (lineage, limits, options, parent_run_context)):
                raise RuntimeError("Cannot reconfigure an AgentExecution after it has started.")
            return self
        if lineage is not None:
            self.lineage = normalize_execution_lineage(lineage)
        self.limits = normalize_execution_limits(limits)
        if options is not None:
            self.configure_options(options)
        if parent_run_context is not None:
            self.parent_run_context = parent_run_context
        self._replace_runtime_context()
        return self

    def get_result(self) -> AgentExecutionResult:
        return AgentExecutionResult(self)

    def get_response(self) -> AgentExecutionResult:
        return self.get_result()

    def _compat_run(self, *args: Any, **kwargs: Any):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.start(*args, **kwargs)
        return self.async_start(*args, **kwargs)

    def _compat_meta(self, *args: Any, **kwargs: Any):
        if self.task_record is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return self.task_record._meta()
            return self.task_record.async_meta()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.get_meta(*args, **kwargs)
        return self.async_get_meta(*args, **kwargs)

    async def async_meta(self):
        if self.task_record is not None:
            return await self.task_record.async_meta()
        await self.async_start()
        if self.task_record is not None:
            return await self.task_record.async_meta()
        return await self.async_get_meta()

    def set_execution_prompt(self, key: Any, value: Any, *, mappings: dict[str, Any] | None = None):
        self._draft.set_execution_prompt(key, value, mappings=mappings)
        return self._refresh_prompt_snapshot()

    def remove_execution_prompt(self, key: Any):
        self._draft.remove_execution_prompt(key)
        return self._refresh_prompt_snapshot()

    def validate(self, handler: "OutputValidateHandler"):
        self._draft.validate(handler)
        return self

    def system(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False):
        self._draft.system(prompt, mappings=mappings, always=always)
        return self._refresh_prompt_snapshot()

    def rule(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False):
        self._draft.rule(prompt, mappings=mappings, always=always)
        return self._refresh_prompt_snapshot()

    def role(self, *args: Any, **kwargs: Any):
        self._draft.role(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def user_info(self, *args: Any, **kwargs: Any):
        self._draft.user_info(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def input(self, *args: Any, **kwargs: Any):
        self._draft.input(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def info(self, *args: Any, **kwargs: Any):
        self._draft.info(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def instruct(self, *args: Any, **kwargs: Any):
        self._draft.instruct(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def examples(self, *args: Any, **kwargs: Any):
        self._draft.examples(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def output(self, *args: Any, **kwargs: Any):
        self._draft.output(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def attachment(self, *args: Any, **kwargs: Any):
        self._draft.attachment(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def image(self, *args: Any, **kwargs: Any):
        self._draft.image(*args, **kwargs)
        return self._refresh_prompt_snapshot()

    def set_prompt_options(self, options: dict[str, Any], *, always: bool = False):
        self._draft.set_prompt_options(options, always=always)
        return self._refresh_prompt_snapshot()

    def use_dynamic_task(self, *args: Any, **kwargs: Any):
        if args:
            raise TypeError("AgentExecution.use_dynamic_task(...) accepts keyword arguments only.")
        self._add_dynamic_task_candidate(kwargs)
        self._selected_route = None
        self.effective_options = self._build_effective_options()
        return self

    def _add_dynamic_task_candidate(self, candidate: dict[str, Any]):
        self.local_dynamic_task_candidates.append(self._normalize_dynamic_task_candidate(candidate))
        self._selected_route = None
        self.effective_options = self._build_effective_options()
        return self

    @staticmethod
    def _normalize_dynamic_task_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        mode = str(candidate.get("mode") or "auto")
        if mode not in {"auto", "submitted"}:
            raise ValueError("Dynamic Task mode must be one of: 'auto', 'submitted'.")
        plan = candidate.get("plan")
        if mode == "submitted" and plan is None:
            raise ValueError("use_dynamic_task(mode='submitted') requires plan=.")
        graph_input_provided = candidate.get("graph_input_provided", _DYNAMIC_TASK_GRAPH_INPUT_UNSET)
        if graph_input_provided is _DYNAMIC_TASK_GRAPH_INPUT_UNSET:
            graph_input_provided = "graph_input" in candidate
        return {
            "mode": mode,
            "plan": plan,
            "planner": candidate.get("planner"),
            "model": candidate.get("model"),
            "actions": candidate.get("actions"),
            "skills": candidate.get("skills"),
            "handlers": candidate.get("handlers"),
            "name": candidate.get("name"),
            "max_tasks": candidate.get("max_tasks"),
            "output_schema": candidate.get("output_schema"),
            "ensure_keys": candidate.get("ensure_keys"),
            "output_format": candidate.get("output_format"),
            "graph_input": candidate.get("graph_input"),
            "graph_input_provided": bool(graph_input_provided),
            "timeout": candidate.get("timeout"),
            "max_retries": candidate.get("max_retries", 3),
        }

    def resolve_skills_plan(self, *args: Any, **kwargs: Any):
        kwargs = self._with_local_skill_kwargs(kwargs)
        return self._draft.resolve_skills_plan(*args, **kwargs)

    async def async_resolve_skills_plan(self, *args: Any, **kwargs: Any):
        kwargs = self._with_local_skill_kwargs(kwargs)
        return await self._draft.async_resolve_skills_plan(*args, **kwargs)

    def run_skills_task(self, *args: Any, **kwargs: Any):
        kwargs = self._with_local_skill_kwargs(kwargs)
        return self._draft.run_skills_task(*args, **kwargs)

    async def async_run_skills_task(self, *args: Any, **kwargs: Any):
        kwargs = self._with_local_skill_kwargs(kwargs)
        return await self._draft.async_run_skills_task(*args, **kwargs)

    def _with_local_skill_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        updated = dict(kwargs)
        mode = str(updated.get("mode") or "model_decision")
        if "skills" not in updated:
            selectors = [
                item.get("selector")
                for item in self.local_skill_selectors
                if item.get("mode") == mode
            ]
            if selectors:
                updated["skills"] = selectors
        if "skills_packs" not in updated:
            pack_selectors = [
                item.get("selector")
                for item in self.local_skills_pack_selectors
                if item.get("mode") == mode
            ]
            if pack_selectors:
                updated["skills_packs"] = pack_selectors
        return updated

    def create_dynamic_task(self, *args: Any, **kwargs: Any):
        return self._draft.create_dynamic_task(*args, **kwargs)

    def get_prompt_text(self):
        return self._draft.get_prompt_text()

    def get_json_prompt(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ):
        prompt_data = {
            ".agent": self.agent.agent_prompt.to_serializable_prompt_data(),
            ".execution": self.request_prompt.to_serializable_prompt_data(),
        }
        content = json5.dumps(
            prompt_data,
            indent=2,
            ensure_ascii=False,
        )
        if save_to is not None:
            target = Path(save_to)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
        return content

    def get_yaml_prompt(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ):
        prompt_data = {
            ".agent": self.agent.agent_prompt.to_serializable_prompt_data(),
            ".execution": self.request_prompt.to_serializable_prompt_data(),
        }
        content = yaml.safe_dump(
            prompt_data,
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )
        if save_to is not None:
            target = Path(save_to)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
        return content

    def goal(self, goal: Any, success_criteria: Any = None):
        if isinstance(goal, (list, tuple, set)):
            set_execution_goals(self, tuple(goal))
        else:
            text = str(goal or "").strip()
            if text:
                set_execution_goals(self, (text,))
        if success_criteria is not None:
            set_success_criteria(self, success_criteria)
        return self

    goals = goal

    def effort(self, value: Any = "medium", **strategy: Any):
        return configure_effort(self, value, **strategy)

    def use_actions(self, *args: Any, **kwargs: Any):
        register = getattr(self.agent, "_register_action_items", None)
        if callable(register):
            raw_names = register(args[0] if args else None)
        else:
            agent_any = cast(Any, self.agent)
            agent_any.use_actions(*args, always=True, **kwargs)
            raw_names = getattr(self.agent, "_normalize_registered_action_ids", lambda value: [])(args[0] if args else None)
        names = raw_names if isinstance(raw_names, (list, tuple, set)) else []
        for name in names:
            text = str(name or "").strip()
            if text and text not in self.local_action_ids:
                self.local_action_ids.append(text)
        self._selected_route = None
        self.effective_options = self._build_effective_options()
        return self

    def require_actions(self, *args: Any, **kwargs: Any):
        register = getattr(self.agent, "_register_action_items", None)
        if callable(register):
            raw_names = register(args[0] if args else None)
        else:
            agent_any = cast(Any, self.agent)
            agent_any.require_actions(*args, always=True, **kwargs)
            raw_names = getattr(self.agent, "_normalize_registered_action_ids", lambda value: [])(args[0] if args else None)
        names = raw_names if isinstance(raw_names, (list, tuple, set)) else []
        for name in names:
            text = str(name or "").strip()
            if text and text not in self.local_action_ids:
                self.local_action_ids.append(text)
            if text and text not in self.local_required_action_ids:
                self.local_required_action_ids.append(text)
        self._selected_route = None
        self.effective_options = self._build_effective_options()
        return self

    def use_skills(self, skills: Any, **kwargs: Any):
        normalize = getattr(self.agent, "_normalize_skill_selector_entries", None)
        if callable(normalize):
            raw_entries = normalize(skills, **kwargs)
        else:
            raw_entries = [{"selector": skills, "mode": kwargs.get("mode", "model_decision")}]
        entries = raw_entries if isinstance(raw_entries, list) else []
        self.local_skill_selectors.extend(entries)
        self._selected_route = None
        self.effective_options = self._build_effective_options()
        return self

    def require_skills(self, skills: Any, **kwargs: Any):
        kwargs["mode"] = "required"
        return self.use_skills(skills, **kwargs)

    def use_skills_packs(self, skills_packs: Any, *, mode: Any = "model_decision"):
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill pack mode must be one of: 'model_decision', 'required'.")
        items = skills_packs if isinstance(skills_packs, (list, tuple, set)) else [skills_packs]
        self.local_skills_pack_selectors.extend(
            {"selector": item, "mode": mode}
            for item in items
        )
        self._selected_route = None
        self.effective_options = self._build_effective_options()
        return self

    def route_policy(self, value: Any):
        self.options["route_policy"] = DataFormatter.sanitize(value)
        self.effective_options = self._build_effective_options()
        self._selected_route = None
        return self

    def access_control_policy(self, value: Any):
        self.options["access_control_policy"] = DataFormatter.sanitize(value)
        self.effective_options = self._build_effective_options()
        return self

    def strategy(self, value: str | None = None, **options: Any):
        if value is not None:
            self.strategy_name = str(value)
            self.options["strategy"] = self.strategy_name
        if options:
            self.task_options.update(options)
        self.effective_options = self._build_effective_options()
        self._selected_route = None
        return self

    def route_options(self, route_name: str) -> dict[str, Any]:
        return state_route_options(self, route_name)

    def record_consumed_option(self, path: str, value: Any, *, owner: str):
        state_record_consumed_option(self, path, value, owner_name=owner)

    def task_target(self) -> str:
        return state_task_target(self)

    def task_goal(self) -> str:
        return state_task_goal(self)

    def task_success_criteria(self) -> list[str]:
        return state_task_success_criteria(self)

    def required_action_ids(self) -> list[str]:
        collect = getattr(self.agent, "_collect_required_action_ids", None)
        required = [*self.local_required_action_ids]
        if callable(collect):
            collected = collect()
            if isinstance(collected, (list, tuple, set)):
                required.extend(collected)
        constraints = self.options.get("capability_constraints")
        if isinstance(constraints, dict):
            actions = constraints.get("actions")
            if isinstance(actions, dict):
                configured = actions.get("required", [])
            else:
                configured = constraints.get("required_actions", [])
            if isinstance(configured, str):
                required = [*required, configured]
            elif isinstance(configured, (list, tuple, set)):
                required = [*required, *configured]
        result: list[str] = []
        for item in required:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def required_skill_ids(self) -> list[str]:
        required: list[Any] = []
        for item in self.local_skill_selectors:
            if item.get("mode") == "required":
                selector = item.get("selector")
                if isinstance(selector, dict):
                    required.append(selector.get("id") or selector.get("skill_id") or selector.get("name") or selector.get("source"))
                else:
                    required.append(selector)
        collect = getattr(self.agent, "_collect_skill_selectors", None)
        try:
            raw_required_selectors = collect(skills=None, mode="required") if callable(collect) else []
        except Exception:
            raw_required_selectors = []
        required_selectors = raw_required_selectors if isinstance(raw_required_selectors, (list, tuple, set)) else []
        for item in required_selectors:
            selector = item.get("selector") if isinstance(item, dict) else item
            if isinstance(selector, dict):
                required.append(selector.get("id") or selector.get("skill_id") or selector.get("name") or selector.get("source"))
            else:
                required.append(selector)
        constraints = self.options.get("capability_constraints")
        if isinstance(constraints, dict):
            skills = constraints.get("skills")
            if isinstance(skills, dict):
                configured = skills.get("required", [])
            else:
                configured = constraints.get("required_skills", [])
            if isinstance(configured, str):
                required.append(configured)
            elif isinstance(configured, (list, tuple, set)):
                required.extend(configured)
        result: list[str] = []
        for item in required:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def is_task_strategy(self) -> bool:
        return state_is_task_strategy(self)

    def task_strategy_options(self) -> dict[str, Any]:
        return dict(self.task_options)

    async def emit_stream(
        self,
        path: str,
        value: Any,
        *,
        route: str | None = None,
        source: str | None = "agent_execution",
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        is_complete: bool | None = None,
        event_type: Literal["delta", "done"] = "done",
        delta: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        if path != "error":
            self.execution_context.record_progress(
                stage=path,
                status="completed" if completed else "progress",
                event_type=path,
                meta=meta,
            )
        stream_meta = merge_stream_meta(
            meta,
            execution_id=self.id,
            lineage=self.lineage,
        )
        return await self.stream.emit(
            path,
            value,
            delta=delta,
            route=route,
            source=source,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            is_complete=completed,
            event_type=event_type,
            meta=stream_meta,
        )

    async def close_streams(self):
        await self.stream.close()

    def dynamic_task_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.dynamic_task_candidates()

    def action_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.action_candidates()

    def skill_candidate_summary(self) -> dict[str, Any]:
        return self.route_planner.skill_candidate_summary()

    async def select_route(self) -> tuple[str, dict[str, Any]]:
        if self._selected_route is not None:
            return self._selected_route
        self._refresh_prompt_snapshot()
        if self.is_task_strategy():
            strategy = self.strategy_name or "task"
            route, route_meta = "agent_task", {
                "strategy": strategy,
                "selected_by": "execution_strategy",
                "goals": list(self.goal_items),
                "success_criteria": list(self.success_criteria_items),
                "generated_success_criteria": list(self.generated_success_criteria),
            }
        elif self.required_action_ids() and self.route_planner.route_allowed("model_request"):
            route, route_meta = "model_request", {
                "with_actions": True,
                "required_actions": self.required_action_ids(),
                "selected_by": "required_capability",
            }
        else:
            route, route_meta = await self.route_planner.select_route()
        self._selected_route = (route, route_meta)
        self.route_info = {
            "selected_route": route,
            "selected_by": route_meta.get("selected_by"),
            "options": DataFormatter.sanitize(route_meta),
            "reusable": True,
        }
        return self._selected_route

    async def _async_execute_route(
        self,
        *,
        type: Literal["original", "parsed", "all"],
        ensure_keys: list[str] | None,
        ensure_all_keys: bool | None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
        key_style: Literal["dot", "slash"],
        max_retries: int,
        raise_ensure_failure: bool,
    ) -> tuple[str, Any]:
        return await async_execute_route(
            self,
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    def record_model_response_id(self, response_id: str | None):
        record_model_response_id_entry(self, response_id)

    async def record_action_log(
        self,
        log: Any,
        *,
        route: str,
        source: str = "action",
        emit: bool = True,
    ) -> dict[str, Any] | None:
        return await record_action_log_entry(self, log, route=route, source=source, emit=emit)

    async def bridge_task_dag_stream_item(self, item: Any, *, route: str):
        await bridge_task_dag_stream_item_entry(self, item, route=route)

    async def bridge_model_stream_item(
        self,
        item: Any,
        *,
        route: str,
        source: str = "model_request",
        path_prefix: str | None = None,
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ):
        await bridge_model_stream_item_entry(
            self,
            item,
            route=route,
            source=source,
            path_prefix=path_prefix,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=meta,
        )

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await start_execution(
            self,
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def _await_route_with_limits(self, run_coro: Any):
        return await await_route_with_limits(self, run_coro)

    async def _cancel_limited_task(self, task: "asyncio.Task[Any]"):
        await cancel_limited_task(task)

    def _build_execution_stall_error(
        self,
        *,
        status: Literal["stalled", "timed_out"],
        message: str,
        elapsed_seconds: float | None,
        idle_seconds: float | None,
        timeout_seconds: float | None,
    ) -> RuntimeStageStallError:
        return build_execution_stall_error(
            self,
            status=status,
            message=message,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=idle_seconds,
            timeout_seconds=timeout_seconds,
        )

    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await async_get_data_entry(
            self,
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_get_text(
        self,
        *,
        parent_run_context: "RunContext | None" = None,
        **kwargs: Any,
    ) -> str:
        return await async_get_text_entry(self, parent_run_context=parent_run_context, **kwargs)

    async def async_get_meta(self) -> dict[str, Any]:
        return await async_get_meta_entry(self)

    async def async_record_workspace(
        self,
        *,
        collection: str = "observations",
        kind: str | None = "agent_execution_observation",
        content: Any = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        checkpoint: bool = False,
        checkpoint_state: dict[str, Any] | None = None,
        checkpoint_step_id: str | None = None,
        profile: str = "fast",
    ) -> dict[str, Any]:
        return await record_workspace_entry(
            self,
            collection=collection,
            kind=kind,
            content=content,
            summary=summary,
            scope=scope,
            source=source,
            meta=meta,
            checkpoint=checkpoint,
            checkpoint_state=checkpoint_state,
            checkpoint_step_id=checkpoint_step_id,
            profile=profile,
        )

    async def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "all"] | str | None = "instant",
        content: Any = None,
        **_: Any,
    ) -> AsyncGenerator[Any, None]:
        async for item in get_async_generator_entry(self, type=type, content=content, **_):
            yield item

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return sync_generator_entry(self, *args, **kwargs)

    def _refresh_diagnostics(self):
        refresh_diagnostics(self)

    def _record_error_diagnostic(self, error: BaseException):
        record_error_diagnostic(self, error)

    def raise_if_limit_exceeded(self):
        self.execution_context.raise_if_limit_exceeded()

    def _workspace_scope(self, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        return workspace_scope(self, scope)

    def _workspace_source(self, source: dict[str, Any] | None = None) -> dict[str, Any]:
        return workspace_source(self, source)

    def _default_workspace_content(self) -> dict[str, Any]:
        return default_workspace_content(self)

    def _default_workspace_summary(self, collection: str) -> str:
        return default_workspace_summary(self, collection)

    def _default_checkpoint_state(self, record_ref: dict[str, Any]) -> dict[str, Any]:
        return default_checkpoint_state(self, record_ref)

    def _append_workspace_ref(self, key: str, ref: dict[str, Any]):
        append_workspace_ref(self, key, ref)
