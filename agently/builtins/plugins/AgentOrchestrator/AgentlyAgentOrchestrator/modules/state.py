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

from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

from agently.types.options import normalize_execution_options
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


class ExecutionOptionsState(dict):
    """Callable dict preserving AgentExecution.options(...) compatibility."""

    def __init__(self, owner: "AgentExecution", initial: dict[str, Any]):
        super().__init__(initial)
        self._owner = owner

    def __call__(self, options: dict[str, Any], *, always: bool = False):
        if always:
            self._owner.agent.options(options, always=True)
            return self._owner
        self._owner.configure_options(options)
        return self._owner


def normalize_options_state(owner: "AgentExecution", options: Any) -> ExecutionOptionsState:
    return ExecutionOptionsState(owner, normalize_execution_options(options))


def configure_execution_options(owner: "AgentExecution", options: Any):
    normalized = normalize_execution_options(options)
    deep_merge(owner.options, normalized)
    load_strategy_state_from_options(owner)
    owner.effective_options = build_effective_options(owner)
    apply_effort_strategy_limits(owner)
    owner.effective_options = build_effective_options(owner)
    return owner


def load_strategy_state_from_options(owner: "AgentExecution"):
    strategy = owner.options.get("strategy")
    if strategy is None:
        execution_options = owner.options.get("execution")
        if isinstance(execution_options, dict):
            strategy = execution_options.get("strategy")
    if strategy is not None:
        owner.strategy_name = str(strategy)

    task_options = owner.options.get("task")
    if isinstance(task_options, dict):
        owner.task_options.update(task_options)
        goal = task_options.get("goal")
        if goal is not None:
            owner.goal(goal)
        criteria = task_options.get("success_criteria")
        if criteria is not None:
            set_success_criteria(owner, criteria)


def build_effective_options(owner: "AgentExecution") -> dict[str, Any]:
    effective = dict(owner.options)
    execution_options = effective.get("execution")
    execution_options = dict(execution_options) if isinstance(execution_options, dict) else {}
    execution_options.update(
        {
            "lineage": owner.lineage,
            "limits": owner.limits,
        }
    )
    if owner.strategy_name is not None:
        execution_options.setdefault("strategy", owner.strategy_name)
    effective["execution"] = execution_options
    if owner.strategy_name is not None:
        effective.setdefault("strategy", owner.strategy_name)
    effort = effective.get("effort")
    if effort is not None:
        effort_name, effort_detail = normalize_effort_configuration(
            effort,
            effective.get("effort_strategy"),
        )
        effective["effort"] = effort_name
        effective["effort_strategy"] = resolve_effort_strategy(effort_name, effort_detail)
    required_actions = owner.required_action_ids()
    required_skills = owner.required_skill_ids()
    if required_actions or required_skills:
        constraints = dict(effective.get("capability_constraints") or {})
        if required_actions:
            actions = dict(constraints.get("actions") or {})
            actions["required"] = required_actions
            constraints["actions"] = actions
        if required_skills:
            skills = dict(constraints.get("skills") or {})
            skills["required"] = required_skills
            constraints["skills"] = skills
        effective["capability_constraints"] = constraints
    if owner.goal_items or owner.success_criteria_items or owner.task_options:
        effective["task"] = {
            **dict(owner.task_options),
            "goals": list(owner.goal_items),
            "success_criteria": list(owner.success_criteria_items),
            "generated_success_criteria": list(owner.generated_success_criteria),
        }
    return effective


def configure_effort(
    owner: "AgentExecution",
    value: Any = "medium",
    **strategy: Any,
):
    name, detail = normalize_effort_configuration(value, strategy)
    owner.options["effort"] = name
    if detail:
        owner.options["effort_strategy"] = detail
    else:
        owner.options.pop("effort_strategy", None)
    owner.effective_options = build_effective_options(owner)
    apply_effort_strategy_limits(owner)
    owner.effective_options = build_effective_options(owner)
    owner._selected_route = None
    return owner


def normalize_effort_configuration(
    effort: Any = "medium",
    detail: Any = None,
) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {}
    if isinstance(effort, Mapping):
        source = dict(effort)
        name = source.pop("name", None)
        if name is None:
            name = source.pop("preset", None)
        if name is None:
            name = source.pop("level", None)
        if name is None:
            name = "medium"
        details = _copy_effort_mapping(source)
    else:
        name = effort if effort is not None else "medium"

    if isinstance(detail, Mapping):
        deep_merge(details, _copy_effort_mapping(detail))
    elif detail is not None:
        details["detail"] = detail

    effort_name = str(name or "medium").strip().lower() or "medium"
    return effort_name, details


def resolve_effort_strategy(effort: Any, detail: Any = None) -> dict[str, Any]:
    name, detail_map = normalize_effort_configuration(effort, detail)
    presets: dict[str, dict[str, Any]] = {
        "minimal": {"planning_depth": "shallow", "max_iterations": 1, "verifier_strength": "standard"},
        "low": {"planning_depth": "shallow", "max_iterations": 1, "verifier_strength": "standard"},
        "fast": {"planning_depth": "shallow", "max_iterations": 1, "verifier_strength": "standard"},
        "medium": {"planning_depth": "standard", "max_iterations": 3, "verifier_strength": "strong"},
        "normal": {"planning_depth": "standard", "max_iterations": 3, "verifier_strength": "strong"},
        "high": {"planning_depth": "deep", "max_iterations": 5, "verifier_strength": "strong"},
        "max": {"planning_depth": "deep", "max_iterations": 5, "verifier_strength": "strong"},
    }
    resolved = dict(presets.get(name) or presets["medium"])
    resolved["name"] = name
    if detail_map:
        deep_merge(resolved, detail_map)
    _apply_effort_aliases(resolved)
    return resolved


def apply_effort_strategy_limits(owner: "AgentExecution"):
    strategy = owner.effective_options.get("effort_strategy")
    if not isinstance(strategy, dict):
        return owner
    effort_applied_limits = getattr(owner, "_effort_applied_limits", set())
    applied: dict[str, tuple[str, Any]] = {
        "max_model_requests": ("effort.budget.model_call_limit", strategy.get("max_model_requests")),
        "max_seconds": ("effort.budget.wall_time_seconds", strategy.get("max_seconds")),
        "max_no_progress_seconds": (
            "effort.budget.no_progress_seconds",
            strategy.get("max_no_progress_seconds"),
        ),
    }
    for limit_name, (option_path, value) in applied.items():
        if value is None:
            if limit_name in effort_applied_limits:
                owner.limits[limit_name] = None
                effort_applied_limits.discard(limit_name)
            continue
        if owner.limits.get(limit_name) is not None and limit_name not in effort_applied_limits:
            continue
        owner.limits[limit_name] = value
        effort_applied_limits.add(limit_name)
        record_consumed_option(owner, option_path, value, owner_name="AgentExecution")
    owner._effort_applied_limits = effort_applied_limits
    execution_context = getattr(owner, "execution_context", None)
    if execution_context is not None:
        execution_context.limits = owner.limits
    return owner


def _copy_effort_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            copied[str(key)] = _copy_effort_mapping(item)
        elif isinstance(item, (list, tuple)):
            copied[str(key)] = list(item)
        else:
            copied[str(key)] = item
    return copied


def _apply_effort_aliases(strategy: dict[str, Any]):
    budget = strategy.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    planning = strategy.get("planning")
    planning = planning if isinstance(planning, dict) else {}
    verification = strategy.get("verification")
    verification = verification if isinstance(verification, dict) else {}

    iteration_limit = _first_present(
        budget,
        "iteration_limit",
        "max_iterations",
        fallback=strategy.get("iteration_limit", strategy.get("max_iterations")),
    )
    if iteration_limit is not None:
        value = _positive_int(iteration_limit, "effort budget iteration_limit")
        strategy["max_iterations"] = value
        if isinstance(budget, dict):
            budget["iteration_limit"] = value

    model_call_limit = _first_present(
        budget,
        "model_call_limit",
        "max_model_requests",
        fallback=strategy.get("model_call_limit", strategy.get("max_model_requests")),
    )
    if model_call_limit is not None:
        value = _positive_int(model_call_limit, "effort budget model_call_limit")
        strategy["max_model_requests"] = value
        if isinstance(budget, dict):
            budget["model_call_limit"] = value

    wall_time_seconds = _first_present(
        budget,
        "wall_time_seconds",
        "max_seconds",
        fallback=strategy.get("wall_time_seconds", strategy.get("max_seconds")),
    )
    if wall_time_seconds is not None:
        value = _positive_float(wall_time_seconds, "effort budget wall_time_seconds")
        strategy["max_seconds"] = value
        if isinstance(budget, dict):
            budget["wall_time_seconds"] = value

    no_progress_seconds = _first_present(
        budget,
        "no_progress_seconds",
        "max_no_progress_seconds",
        fallback=strategy.get("no_progress_seconds", strategy.get("max_no_progress_seconds")),
    )
    if no_progress_seconds is not None:
        value = _positive_float(no_progress_seconds, "effort budget no_progress_seconds")
        strategy["max_no_progress_seconds"] = value
        if isinstance(budget, dict):
            budget["no_progress_seconds"] = value

    planning_depth = planning.get("depth") if isinstance(planning, dict) else None
    if planning_depth is not None:
        strategy["planning_depth"] = str(planning_depth).strip() or strategy.get("planning_depth")

    verification_strictness = verification.get("strictness") if isinstance(verification, dict) else None
    if verification_strictness is not None:
        strategy["verifier_strength"] = str(verification_strictness).strip() or strategy.get("verifier_strength")


def _first_present(source: dict[str, Any], *keys: str, fallback: Any = None) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return fallback


def _positive_int(value: Any, label: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{ label } must be a positive integer.") from error
    if normalized < 1:
        raise ValueError(f"{ label } must be a positive integer.")
    return normalized


def _positive_float(value: Any, label: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{ label } must be a positive number.") from error
    if normalized <= 0:
        raise ValueError(f"{ label } must be a positive number.")
    return normalized


def set_execution_goals(owner: "AgentExecution", goals: tuple[Any, ...]):
    if len(goals) == 1 and isinstance(goals[0], (list, tuple, set)):
        goals = tuple(goals[0])
    normalized = [str(item).strip() for item in goals if str(item or "").strip()]
    if normalized:
        owner.goal_items = normalized
    owner.effective_options = build_effective_options(owner)
    owner._selected_route = None
    return owner


def set_success_criteria(owner: "AgentExecution", criteria: Any = None, *more: Any):
    if more:
        items = [criteria, *more]
    elif isinstance(criteria, (list, tuple, set)):
        items = list(criteria)
    elif criteria is None:
        items = []
    else:
        items = [criteria]
    normalized = [str(item).strip() for item in items if str(item or "").strip()]
    if normalized:
        owner.success_criteria_items = normalized
    owner.effective_options = build_effective_options(owner)
    owner._selected_route = None
    return owner


def task_target(owner: "AgentExecution") -> str:
    if owner.goal_items:
        return owner.goal_items[0]
    return owner.route_planner.task_target()


def task_goal(owner: "AgentExecution") -> str:
    if owner.goal_items:
        return "\n".join(owner.goal_items)
    return task_target(owner)


def task_success_criteria(owner: "AgentExecution") -> list[str]:
    if owner.success_criteria_items:
        return list(owner.success_criteria_items)
    goal = task_goal(owner)
    generated = [f"Complete the requested goal with concrete evidence: { goal }"]
    owner.generated_success_criteria = generated
    owner.success_criteria_items = generated
    owner.diagnostics.setdefault("success_criteria", {})["generated"] = generated
    owner.effective_options = build_effective_options(owner)
    return list(generated)


def is_task_strategy(owner: "AgentExecution") -> bool:
    if owner.strategy_name in {"task", "task_loop", "long_task"}:
        return True
    if owner.goal_items or owner.success_criteria_items:
        return True
    task_options = owner.options.get("task")
    return isinstance(task_options, dict) and bool(task_options)


def route_options(owner: "AgentExecution", route_name: str) -> dict[str, Any]:
    routes = owner.options.get("routes", {})
    if not isinstance(routes, dict):
        return {}
    options = routes.get(route_name, {})
    return dict(options) if isinstance(options, dict) else {}


def record_consumed_option(owner: "AgentExecution", path: str, value: Any, *, owner_name: str):
    owner.consumed_options[path] = {
        "value": DataFormatter.sanitize(value),
        "owner": owner_name,
    }


def deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            deep_merge(existing, value)
        else:
            target[key] = value
    return target
