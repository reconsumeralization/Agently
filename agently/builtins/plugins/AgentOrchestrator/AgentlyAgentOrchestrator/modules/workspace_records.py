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

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


async def record_workspace(
    owner: "AgentExecution",
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
    if owner.workspace is None:
        raise RuntimeError(
            "AgentExecution has no Workspace binding. "
            "Call agent.use_workspace(...) before create_execution(...)."
        )
    if not owner._completed:
        await owner.async_get_data()
    owner._refresh_diagnostics()

    record_scope = workspace_scope(owner, scope)
    record_source = workspace_source(owner, source)
    record_meta = {
        "execution_id": owner.id,
        "execution_mode": owner.mode,
        "lineage": DataFormatter.sanitize(owner.lineage),
    }
    record_meta.update(dict(meta or {}))
    record_content = content if content is not None else default_workspace_content(owner)
    record_summary = summary or default_workspace_summary(owner, collection)

    record_ref = await owner.workspace.ingest(
        content=record_content,
        collection=collection,
        kind=kind,
        scope=record_scope,
        source=record_source,
        summary=record_summary,
        meta=record_meta,
        profile=profile,
    )
    append_workspace_ref(owner, collection, record_ref)

    checkpoint_ref = None
    if checkpoint:
        checkpoint_run_id = str(record_scope.get("task_id") or owner.lineage.get("task_id") or owner.id)
        checkpoint_ref = await owner.workspace.checkpoint(
            checkpoint_run_id,
            checkpoint_state or default_checkpoint_state(owner, record_ref),
            step_id=checkpoint_step_id or owner.lineage.get("step_id"),
        )
        append_workspace_ref(owner, "checkpoints", checkpoint_ref)

    return DataFormatter.sanitize(
        {
            "record": record_ref,
            "checkpoint": checkpoint_ref,
            "workspace_refs": owner.workspace_refs,
        }
    )


def workspace_scope(owner: "AgentExecution", scope: dict[str, Any] | None = None) -> dict[str, Any]:
    lineage_scope = owner.lineage.get("scope")
    merged = dict(lineage_scope) if isinstance(lineage_scope, dict) else {}
    for key in ("task_id", "iteration_id", "step_id"):
        value = owner.lineage.get(key)
        if value is not None:
            merged.setdefault(key, value)
    merged.update(dict(scope or {}))
    return DataFormatter.sanitize(merged)


def workspace_source(owner: "AgentExecution", source: dict[str, Any] | None = None) -> dict[str, Any]:
    default_source = {
        "type": "agent_execution",
        "execution_id": owner.id,
        "execution_mode": owner.mode,
        "task_id": owner.lineage.get("task_id"),
        "iteration_id": owner.lineage.get("iteration_id"),
        "step_id": owner.lineage.get("step_id"),
    }
    default_source.update(dict(source or {}))
    return DataFormatter.sanitize(default_source)


def default_workspace_content(owner: "AgentExecution") -> dict[str, Any]:
    return DataFormatter.sanitize(
        {
            "execution_id": owner.id,
            "execution_mode": owner.mode,
            "status": owner.status,
            "lineage": owner.lineage,
            "result": owner.result,
            "route_plan": owner.route_plan,
            "diagnostics": owner.diagnostics,
        }
    )


def default_workspace_summary(owner: "AgentExecution", collection: str) -> str:
    task_id = owner.lineage.get("task_id") or owner.id
    step_id = owner.lineage.get("step_id") or owner.mode
    return f"{ task_id } { step_id } AgentExecution { collection }"


def default_checkpoint_state(owner: "AgentExecution", record_ref: dict[str, Any]) -> dict[str, Any]:
    return DataFormatter.sanitize(
        {
            "execution_id": owner.id,
            "execution_mode": owner.mode,
            "status": owner.status,
            "lineage": owner.lineage,
            "record_ref": record_ref,
            "diagnostics": owner.diagnostics,
        }
    )


def append_workspace_ref(owner: "AgentExecution", key: str, ref: dict[str, Any]):
    ref_id = ref.get("id")
    if not ref_id:
        return
    refs = owner.workspace_refs.setdefault(key, [])
    if isinstance(refs, list) and ref_id not in refs:
        refs.append(ref_id)
