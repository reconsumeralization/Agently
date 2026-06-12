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

# ── TaskDAG helper functions ─────────────────────────────────────────────────
# Loose helpers organized in three groups:
#   1. DAG shape queries   — _is_approval_task, _approval_required, _approval_type,
#                             _approval_payload, _fallback_action, _graph_signature
#   2. Event naming        — _chunk_name, _start_task_event, _done_task_event,
#                             _failed_task_event, _done_graph_event
#   3. Output collection   — _collect_semantic_outputs, _semantic_output_task_refs,
#                             _produce_role, _extract_artifact_refs
# If any group grows past ~10 functions, extract it into its own module.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from agently.types.data import TaskDAG, TaskDAGNode


def _is_approval_task(task: TaskDAGNode) -> bool:
    return task.kind == "approval"


def _approval_required(task: TaskDAGNode) -> bool:
    if _is_approval_task(task):
        return True
    approval = task.approval
    if approval is True:
        return True
    if isinstance(approval, Mapping):
        return bool(approval.get("required") or approval.get("mode") in {"required", "pause"})
    return False


def _approval_type(task: TaskDAGNode) -> str:
    if isinstance(task.approval, Mapping) and task.approval.get("type"):
        return str(task.approval["type"])
    return "dynamic_task_approval"


def _approval_payload(task: TaskDAGNode, task_input: Mapping[str, Any]):
    if isinstance(task.approval, Mapping) and "payload" in task.approval:
        return task.approval["payload"]
    return {
        "task_id": task.id,
        "kind": task.kind,
        "title": task.title,
        "purpose": task.purpose,
        "input": dict(task_input),
    }


def _fallback_action(task: TaskDAGNode) -> str | None:
    if isinstance(task.fallback, str):
        return task.fallback
    if isinstance(task.fallback, Mapping):
        value = task.fallback.get("on_error") or task.fallback.get("action")
        return str(value) if value is not None else None
    return None


def _validate_semantic_outputs(
    graph: TaskDAG,
    task_by_id: Mapping[str, TaskDAGNode],
) -> None:
    for role, task_id in _semantic_output_task_refs(graph.semantic_outputs).items():
        if task_id not in task_by_id:
            raise ValueError(
                f"Task DAG semantic output '{ role }' references missing task '{ task_id }'."
            )


def _validate_side_effects(graph: TaskDAG) -> None:
    approval_policy = str(graph.policies.get("approval", graph.policies.get("side_effect_approval", "allow")))
    if approval_policy not in {"require", "required", "fail_closed"}:
        return
    for task in graph.tasks:
        side_effects = task.side_effect_policy
        if not side_effects:
            continue
        has_external_write = bool(
            side_effects.get("external_write")
            or side_effects.get("credential_usage")
            or side_effects.get("local_write")
            or side_effects.get("network")
        )
        if has_external_write and not _approval_required(task):
            raise ValueError(
                f"Dynamic task '{ task.id }' declares side effects but has no approval policy."
            )


def _collect_semantic_outputs(
    graph: TaskDAG,
    task_results: Mapping[str, Any],
    artifact_refs: Mapping[str, Any],
) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    refs = _semantic_output_task_refs(graph.semantic_outputs)
    for role, task_id in refs.items():
        if task_id in artifact_refs:
            outputs[role] = {"task_id": task_id, "artifact_refs": artifact_refs[task_id]}
        elif task_id in task_results:
            outputs[role] = {"task_id": task_id, "result": task_results[task_id]}
    for task in graph.tasks:
        for item in task.produces:
            role = _produce_role(item)
            if role and role not in outputs and task.id in task_results:
                if task.id in artifact_refs:
                    outputs[role] = {"task_id": task.id, "artifact_refs": artifact_refs[task.id]}
                else:
                    outputs[role] = {"task_id": task.id, "result": task_results[task.id]}
    return outputs


def _semantic_output_task_refs(semantic_outputs: Any) -> dict[str, str]:
    refs: dict[str, str] = {}
    if isinstance(semantic_outputs, Mapping):
        for role, spec in semantic_outputs.items():
            if isinstance(spec, str):
                refs[str(role)] = spec
            elif isinstance(spec, Mapping):
                task_id = spec.get("task_id") or spec.get("from_task")
                if task_id is not None:
                    refs[str(role)] = str(task_id)
    elif isinstance(semantic_outputs, list | tuple):
        for item in semantic_outputs:
            if isinstance(item, Mapping):
                role = item.get("role") or item.get("name")
                task_id = item.get("task_id") or item.get("from_task")
                if role is not None and task_id is not None:
                    refs[str(role)] = str(task_id)
    return refs


def _produce_role(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        role = item.get("role") or item.get("name")
        return str(role) if role is not None else None
    return None


def _extract_artifact_refs(output: Any):
    if not isinstance(output, Mapping):
        return None
    refs = output.get("artifact_refs")
    if refs is None:
        refs = output.get("artifacts")
    return refs


def _graph_signature(graph: TaskDAG) -> tuple[Any, ...]:
    return tuple(
        (
            task.id,
            task.kind,
            task.depends_on,
            task.binding if isinstance(task.binding, str) else None,
            task.approval,
            task.fallback,
        )
        for task in graph.tasks
    )


def _graph_fingerprint(graph: TaskDAG) -> str:
    return hashlib.sha256(
        json.dumps(graph.to_dict(), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _chunk_name(graph_id: str, phase: str, task_id: str):
    return f"dynamic:{ graph_id }:{ phase }:{ task_id }"


def _start_task_event(task_id: str):
    return f"start:{ task_id }"


def _done_task_event(task_id: str):
    return f"done:{ task_id }"


def _failed_task_event(task_id: str):
    return f"failed:{ task_id }"


def _done_graph_event(graph_id: str):
    return f"done:graph:{ graph_id }"
