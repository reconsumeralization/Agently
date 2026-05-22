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

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from agently.types.data import TaskDAG, TaskDAGNode

from .TaskDAGHelpers import (
    _approval_required,
    _is_approval_task,
    _semantic_output_task_refs,
    _validate_semantic_outputs,
    _validate_side_effects,
)
from .TaskDAGResolver import (
    _GRAPH_SCHEMA_VERSION,
    _TASK_ID_PATTERN,
    DynamicTaskResolver,
    _coerce_resolver,
)


@dataclass(frozen=True)
class TaskDAGValidation:
    graph: TaskDAG
    task_ids: tuple[str, ...]
    root_task_ids: tuple[str, ...]
    topological_task_ids: tuple[str, ...]
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

class TaskDAGValidator:
    def __init__(
        self,
        resolver: DynamicTaskResolver | Mapping[str, Any] | None = None,
        *,
        schema_version: str = _GRAPH_SCHEMA_VERSION,
    ):
        self.resolver = _coerce_resolver(resolver)
        self.schema_version = schema_version

    def validate(
        self,
        graph: TaskDAG | Mapping[str, Any],
        *,
        resolver: DynamicTaskResolver | Mapping[str, Any] | None = None,
        strict_schema_version: bool = False,
    ) -> TaskDAGValidation:
        validation = validate_task_dag(
            graph,
            resolver=self._merge_resolver(resolver),
        )
        if strict_schema_version and validation.graph.task_schema_version != self.schema_version:
            raise ValueError(
                f"Task DAG schema version must be '{ self.schema_version }', "
                f"got '{ validation.graph.task_schema_version }'."
            )
        return validation

    def validate_planner_output(
        self,
        result: Mapping[str, Any],
        context: Any = None,
        *,
        resolver: DynamicTaskResolver | Mapping[str, Any] | None = None,
    ):
        return validate_task_dag_planner_output(
            result,
            resolver=self._merge_resolver(resolver),
            schema_version=self.schema_version,
        )

    def _merge_resolver(self, resolver: DynamicTaskResolver | Mapping[str, Any] | None):
        if resolver is None:
            return self.resolver
        merged = DynamicTaskResolver(self.resolver.to_mapping())
        for key, value in _coerce_resolver(resolver).to_mapping().items():
            merged.register(key, value)
        return merged

def validate_task_dag(
    graph: TaskDAG | Mapping[str, Any],
    *,
    resolver: DynamicTaskResolver | Mapping[str, Any] | None = None,
) -> TaskDAGValidation:
    normalized = TaskDAG.from_value(graph)
    resolved_resolver = _coerce_resolver(resolver)
    tasks = list(normalized.tasks)
    if not tasks:
        raise ValueError("Task DAG must contain at least one task.")

    task_by_id: dict[str, TaskDAGNode] = {}
    duplicates: list[str] = []
    for task in tasks:
        if not task.id:
            raise ValueError("Dynamic task id must be non-empty.")
        if not _TASK_ID_PATTERN.fullmatch(task.id):
            raise ValueError(
                f"Dynamic task id '{ task.id }' is invalid. Use letters, digits, underscore, dot, or dash."
            )
        if task.id in task_by_id:
            duplicates.append(task.id)
        task_by_id[task.id] = task
    if duplicates:
        raise ValueError(f"Duplicate dynamic task id(s): { ', '.join(sorted(set(duplicates))) }.")

    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in task_by_id:
                raise ValueError(
                    f"Dynamic task '{ task.id }' depends on missing task '{ dependency }'."
                )

    adjacency: dict[str, list[str]] = {task.id: [] for task in tasks}
    indegree: dict[str, int] = {task.id: 0 for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            adjacency[dependency].append(task.id)
            indegree[task.id] += 1

    roots = tuple(task.id for task in tasks if not task.depends_on)
    if not roots:
        raise ValueError("Task DAG must contain at least one root task.")

    queue = deque(task_id for task_id in roots)
    ordered: list[str] = []
    while queue:
        task_id = queue.popleft()
        ordered.append(task_id)
        for child_id in adjacency[task_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                queue.append(child_id)
    if len(ordered) != len(tasks):
        cycle_ids = sorted(task_id for task_id, count in indegree.items() if count > 0)
        raise ValueError(f"Task DAG contains a dependency cycle: { ', '.join(cycle_ids) }.")

    normalized, tasks, task_by_id, roots, ordered_ids = _prune_unresolved_optional_tasks(
        normalized,
        tasks,
        task_by_id,
        roots,
        tuple(ordered),
        resolved_resolver,
    )

    for task in tasks:
        if _is_approval_task(task):
            continue
        if resolved_resolver.resolve(task) is None:
            raise ValueError(
                f"Dynamic task '{ task.id }' kind '{ task.kind }' has no executor resolver entry."
            )

    _validate_semantic_outputs(normalized, task_by_id)
    _validate_side_effects(normalized)

    return TaskDAGValidation(
        graph=normalized,
        task_ids=tuple(task.id for task in tasks),
        root_task_ids=roots,
        topological_task_ids=ordered_ids,
        diagnostics=normalized.diagnostics,
    )


def validate_task_dag_planner_output(
    result: Mapping[str, Any],
    *,
    resolver: DynamicTaskResolver | Mapping[str, Any] | None = None,
    schema_version: str = _GRAPH_SCHEMA_VERSION,
):
    try:
        validation = validate_task_dag(result, resolver=resolver)
    except Exception as error:
        return {
            "ok": False,
            "reason": str(error),
            "validator_name": "task_dag",
        }
    if validation.graph.task_schema_version != schema_version:
        return {
            "ok": False,
            "reason": (
                f"Task DAG schema version must be '{ schema_version }', "
                f"got '{ validation.graph.task_schema_version }'."
            ),
            "validator_name": "task_dag.schema_version",
        }
    return True

def _prune_unresolved_optional_tasks(
    graph: TaskDAG,
    tasks: list[TaskDAGNode],
    task_by_id: dict[str, TaskDAGNode],
    roots: tuple[str, ...],
    ordered: tuple[str, ...],
    resolver: DynamicTaskResolver,
) -> tuple[TaskDAG, list[TaskDAGNode], dict[str, TaskDAGNode], tuple[str, ...], tuple[str, ...]]:
    required_ids = _semantic_required_task_ids(graph, task_by_id)
    pruned: set[str] = set()
    diagnostics: list[Mapping[str, Any]] = [dict(item) for item in graph.diagnostics]

    def fail_or_prune(task: TaskDAGNode, reason: str):
        if (
            task.id in required_ids
            or _approval_required(task)
            or bool(task.side_effect_policy)
        ):
            raise ValueError(
                f"Dynamic task '{ task.id }' kind '{ task.kind }' has no executor resolver entry."
            )
        pruned.add(task.id)
        diagnostics.append(
            {
                "level": "warning",
                "code": reason,
                "task_id": task.id,
                "kind": task.kind,
                "binding": task.binding,
            }
        )

    for task in tasks:
        if _is_approval_task(task):
            continue
        if resolver.resolve(task) is None:
            fail_or_prune(task, "dynamic_task.unknown_optional_binding_skipped")

    changed = True
    while changed:
        changed = False
        for task in tasks:
            if task.id in pruned:
                continue
            missing_pruned_dependencies = [dependency for dependency in task.depends_on if dependency in pruned]
            if missing_pruned_dependencies:
                if task.id in required_ids:
                    raise ValueError(
                        f"Dynamic task '{ task.id }' depends on skipped optional task(s): "
                        f"{ ', '.join(missing_pruned_dependencies) }."
                    )
                pruned.add(task.id)
                diagnostics.append(
                    {
                        "level": "warning",
                        "code": "dynamic_task.optional_dependent_skipped",
                        "task_id": task.id,
                        "dependencies": missing_pruned_dependencies,
                    }
                )
                changed = True

    if not pruned:
        return graph, tasks, task_by_id, roots, ordered

    kept_tasks = [task for task in tasks if task.id not in pruned]
    if not kept_tasks:
        raise ValueError("Task DAG has no executable tasks after pruning unresolved optional tasks.")
    kept_ids = {task.id for task in kept_tasks}
    kept_by_id = {task.id: task for task in kept_tasks}
    kept_roots = tuple(task.id for task in kept_tasks if not task.depends_on)
    if not kept_roots:
        raise ValueError("Task DAG has no root task after pruning unresolved optional tasks.")
    kept_ordered = tuple(task_id for task_id in ordered if task_id in kept_ids)
    return (
        replace(graph, tasks=tuple(kept_tasks), diagnostics=tuple(diagnostics)),
        kept_tasks,
        kept_by_id,
        kept_roots,
        kept_ordered,
    )


def _semantic_required_task_ids(
    graph: TaskDAG,
    task_by_id: Mapping[str, TaskDAGNode],
) -> set[str]:
    required = set(_semantic_output_task_refs(graph.semantic_outputs).values())
    queue = deque(required)
    while queue:
        task_id = queue.popleft()
        task = task_by_id.get(task_id)
        if task is None:
            continue
        for dependency in task.depends_on:
            if dependency not in required:
                required.add(dependency)
                queue.append(dependency)
    return required
