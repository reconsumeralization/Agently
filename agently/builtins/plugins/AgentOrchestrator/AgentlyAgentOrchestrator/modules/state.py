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
            owner.success_criteria(criteria)


def build_effective_options(owner: "AgentExecution") -> dict[str, Any]:
    effective = dict(owner.options)
    execution_options = effective.get("execution")
    execution_options = dict(execution_options) if isinstance(execution_options, dict) else {}
    execution_options.update(
        {
            "mode": owner.mode,
            "lineage": owner.lineage,
            "limits": owner.limits,
        }
    )
    if owner.strategy_name is not None:
        execution_options.setdefault("strategy", owner.strategy_name)
    effective["execution"] = execution_options
    if owner.strategy_name is not None:
        effective.setdefault("strategy", owner.strategy_name)
    if owner.goal_items or owner.success_criteria_items or owner.task_options:
        effective["task"] = {
            **dict(owner.task_options),
            "goals": list(owner.goal_items),
            "success_criteria": list(owner.success_criteria_items),
            "generated_success_criteria": list(owner.generated_success_criteria),
        }
    return effective


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
