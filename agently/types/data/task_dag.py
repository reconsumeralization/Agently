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
from collections.abc import Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import json5
import yaml

from agently.utils import DataLocator


TASK_DAG_SCHEMA_VERSION = "task_dag/v1"


@dataclass(frozen=True)
class TaskDAGNode:
    id: str
    kind: str = "task"
    title: str | None = None
    purpose: str | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    inputs: Any = field(default_factory=dict)
    binding: Any = None
    produces: tuple[Any, ...] = field(default_factory=tuple)
    side_effect_policy: Mapping[str, Any] = field(default_factory=dict)
    fallback: Any = None
    approval: Any = None

    @classmethod
    def from_value(cls, value: "TaskDAGNode | Mapping[str, Any]") -> "TaskDAGNode":
        if isinstance(value, TaskDAGNode):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"Dynamic task must be a mapping or TaskDAGNode, got: { type(value) }.")
        task_id = value.get("id")
        if task_id is None:
            raise ValueError("Dynamic task requires non-empty 'id'.")
        depends_on = value.get("depends_on", ())
        if depends_on is None:
            depends_on = ()
        if isinstance(depends_on, str):
            depends_on = (depends_on,)
        produces = value.get("produces", ())
        if produces is None:
            produces = ()
        if isinstance(produces, (str, Mapping)):
            produces = (produces,)
        return cls(
            id=str(task_id).strip(),
            kind=str(value.get("kind", "task")).strip() or "task",
            title=str(value["title"]) if value.get("title") is not None else None,
            purpose=str(value["purpose"]) if value.get("purpose") is not None else None,
            depends_on=tuple(str(item).strip() for item in depends_on),
            inputs=value.get("inputs", {}),
            binding=value.get("binding"),
            produces=tuple(produces),
            side_effect_policy=(
                dict(value.get("side_effect_policy") or {})
                if isinstance(value.get("side_effect_policy") or {}, Mapping)
                else {"value": value.get("side_effect_policy")}
            ),
            fallback=value.get("fallback"),
            approval=value.get("approval"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "purpose": self.purpose,
            "depends_on": list(self.depends_on),
            "inputs": self.inputs,
            "binding": self.binding,
            "produces": list(self.produces),
            "side_effect_policy": dict(self.side_effect_policy),
            "fallback": self.fallback,
            "approval": self.approval,
        }


@dataclass(frozen=True)
class TaskDAG:
    graph_id: str
    tasks: tuple[TaskDAGNode, ...]
    task_schema_version: str = TASK_DAG_SCHEMA_VERSION
    semantic_outputs: Any = field(default_factory=dict)
    policies: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_value(cls, value: "TaskDAG | Mapping[str, Any]") -> "TaskDAG":
        if isinstance(value, TaskDAG):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"Task DAG must be a mapping or TaskDAG, got: { type(value) }.")
        raw_tasks = value.get("tasks")
        if raw_tasks is None:
            raise ValueError("Task DAG requires 'tasks'.")
        if not isinstance(raw_tasks, list | tuple):
            raise TypeError(f"Task DAG 'tasks' must be a list/tuple, got: { type(raw_tasks) }.")
        graph_id = str(value.get("graph_id") or f"graph-{ uuid.uuid4().hex[:12] }").strip()
        if not graph_id:
            raise ValueError("Task DAG requires non-empty 'graph_id'.")
        diagnostics = value.get("diagnostics", ())
        if isinstance(diagnostics, Mapping):
            diagnostics = (diagnostics,)
        return cls(
            graph_id=graph_id,
            task_schema_version=str(value.get("task_schema_version") or TASK_DAG_SCHEMA_VERSION),
            tasks=tuple(TaskDAGNode.from_value(task) for task in raw_tasks),
            semantic_outputs=value.get("semantic_outputs", {}),
            policies=dict(value.get("policies") or {}),
            diagnostics=tuple(
                dict(item) if isinstance(item, Mapping) else {"message": str(item)}
                for item in (diagnostics or ())
            ),
        )

    @classmethod
    def from_json(
        cls,
        path_or_content: str | Path,
        *,
        task_dag_key_path: str | None = None,
        encoding: str | None = "utf-8",
    ) -> "TaskDAG":
        graph_data = _load_json_task_dag_data(path_or_content, encoding=encoding)
        if task_dag_key_path is not None:
            graph_data = DataLocator.locate_path_in_dict(graph_data, task_dag_key_path)
        if not isinstance(graph_data, Mapping):
            raise TypeError(
                "Cannot load JSON TaskDAG config, expect dictionary data"
                f"{ ' from [' + task_dag_key_path + ']' if task_dag_key_path is not None else '' } "
                f"but got: { graph_data }"
            )
        return cls.from_value(graph_data)

    @classmethod
    def from_yaml(
        cls,
        path_or_content: str | Path,
        *,
        task_dag_key_path: str | None = None,
        encoding: str | None = "utf-8",
    ) -> "TaskDAG":
        graph_data = _load_yaml_task_dag_data(path_or_content, encoding=encoding)
        if task_dag_key_path is not None:
            graph_data = DataLocator.locate_path_in_dict(graph_data, task_dag_key_path)
        if not isinstance(graph_data, Mapping):
            raise TypeError(
                "Cannot load YAML TaskDAG config, expect dictionary data"
                f"{ ' from [' + task_dag_key_path + ']' if task_dag_key_path is not None else '' } "
                f"but got: { graph_data }"
            )
        return cls.from_value(graph_data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "task_schema_version": self.task_schema_version,
            "tasks": [task.to_dict() for task in self.tasks],
            "semantic_outputs": self.semantic_outputs,
            "policies": dict(self.policies),
            "diagnostics": [dict(item) for item in self.diagnostics],
        }

    def get_json(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ) -> str:
        content = json5.dumps(
            self.to_dict(),
            indent=2,
            ensure_ascii=False,
        )
        if save_to is not None:
            target = Path(save_to)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
        return content

    def get_yaml(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ) -> str:
        content = yaml.safe_dump(
            self.to_dict(),
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )
        if save_to is not None:
            target = Path(save_to)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
        return content


def _is_existing_file_path(path_or_content: str | Path) -> bool:
    path = Path(path_or_content)
    try:
        return path.exists() and path.is_file()
    except (OSError, ValueError):
        return False


def _load_json_task_dag_data(
    path_or_content: str | Path,
    *,
    encoding: str | None,
) -> Any:
    path = Path(path_or_content)
    if _is_existing_file_path(path_or_content):
        try:
            return json5.loads(path.read_text(encoding=encoding))
        except (JSONDecodeError, ValueError) as error:
            raise ValueError(f"Cannot load TaskDAG JSON file '{ path_or_content }'.\nError: { error }")
    try:
        return json5.loads(str(path_or_content))
    except (JSONDecodeError, ValueError) as error:
        raise ValueError(f"Cannot load TaskDAG JSON content or file path not existed.\nError: { error }")


def _load_yaml_task_dag_data(
    path_or_content: str | Path,
    *,
    encoding: str | None,
) -> Any:
    path = Path(path_or_content)
    if _is_existing_file_path(path_or_content):
        try:
            return yaml.safe_load(path.read_text(encoding=encoding))
        except yaml.YAMLError as error:
            raise ValueError(f"Cannot load TaskDAG YAML file '{ path_or_content }'.\nError: { error }")
    try:
        return yaml.safe_load(str(path_or_content))
    except yaml.YAMLError as error:
        raise ValueError(f"Cannot load TaskDAG YAML content or file path not existed.\nError: { error }")
