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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from agently.types.data import TASK_DAG_SCHEMA_VERSION, TaskDAG, TaskDAGNode
from agently.types.trigger_flow import TriggerFlowRuntimeData

if TYPE_CHECKING:
    from agently.core.TriggerFlow.Execution import TriggerFlowExecution


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_GRAPH_SCHEMA_VERSION = TASK_DAG_SCHEMA_VERSION
_RESERVED_RESOLVER_KEYS = frozenset(
    {
        "model",
        "action",
        "skill",
        "validate",
        "approval",
        "artifact",
        "emit",
    }
)


@dataclass(frozen=True)
class DynamicTaskContext:
    graph: TaskDAG
    task: TaskDAGNode
    task_input: Mapping[str, Any]
    graph_input: Any
    dependency_results: Mapping[str, Any]
    dependency_payload: Any
    resources: Mapping[str, Any]
    runtime_data: TriggerFlowRuntimeData

    @property
    def execution(self) -> "TriggerFlowExecution":
        return self.runtime_data.execution


DynamicTaskHandler = Callable[[DynamicTaskContext], Any]


class DynamicTaskResolver:
    def __init__(self, entries: Mapping[str, Any] | None = None):
        self._entries: dict[str, Any] = {}
        self.register("validate", _default_validate_task)
        self.register("emit", _default_emit_task)
        if entries:
            for key, value in entries.items():
                self.register(str(key), value)

    def register(self, key: str, value: Any):
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("DynamicTaskResolver.register() requires a non-empty key.")
        if not _TASK_ID_PATTERN.fullmatch(normalized_key):
            raise ValueError(
                f"Dynamic task resolver key '{ normalized_key }' is invalid. "
                "Use letters, digits, underscore, dot, or dash."
            )
        self._entries[normalized_key] = value
        return self

    def has(self, key: str):
        return str(key) in self._entries

    def keys(self):
        return tuple(self._entries.keys())

    def to_mapping(self):
        return dict(self._entries)

    def resolve(self, task: TaskDAGNode):
        if task.binding is not None and callable(task.binding):
            return task.binding
        if isinstance(task.binding, str):
            if task.binding in self._entries:
                return self._resolve_registered(self._entries[task.binding], task)
            if task.kind in {"model", "action", "skill"} and task.kind in self._entries:
                return self._resolve_registered(self._entries[task.kind], task)
            return None
        if task.id in self._entries:
            return self._resolve_registered(self._entries[task.id], task)
        if task.kind in self._entries:
            return self._resolve_registered(self._entries[task.kind], task)
        return None

    @staticmethod
    def _resolve_registered(value: Any, task: TaskDAGNode):
        return value(task) if _is_task_resolver_factory(value) else value

def _coerce_resolver(value: DynamicTaskResolver | Mapping[str, Any] | None) -> DynamicTaskResolver:
    if isinstance(value, DynamicTaskResolver):
        return value
    return DynamicTaskResolver(value)

def _is_task_resolver_factory(binding: Any) -> bool:
    marker = getattr(binding, "dynamic_task_resolver_factory", False)
    return bool(marker)


def dynamic_task_resolver_factory(func: Callable[[TaskDAGNode], Any]):
    setattr(func, "dynamic_task_resolver_factory", True)
    return func


async def _default_validate_task(context: DynamicTaskContext):
    return {
        "ok": True,
        "task_id": context.task.id,
        "inputs": context.task.inputs,
        "dependency_results": dict(context.dependency_results),
    }


async def _default_emit_task(context: DynamicTaskContext):
    payload = context.task.inputs
    await context.runtime_data.execution.async_put_into_stream(
        {
            "type": "dynamic_task.emit",
            "graph_id": context.graph.graph_id,
            "task_id": context.task.id,
            "payload": payload,
        },
        _skip_contract_validation=True,
    )
    return payload
