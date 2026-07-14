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

from agently.types.data import AgentExecutionWorkspacePurpose, AgentExecutionWorkspaceRecord
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


async def record_workspace(
    owner: "AgentExecution",
    *,
    purpose: AgentExecutionWorkspacePurpose = "process",
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
) -> AgentExecutionWorkspaceRecord:
    if purpose not in {"process", "deliverable", "recovery", "audit"}:
        raise ValueError(f"Unsupported AgentExecution Workspace purpose: { purpose }.")
    if purpose == "deliverable" and checkpoint:
        raise ValueError("AgentExecution Workspace deliverable records cannot also be recovery checkpoints.")
    if owner.workspace is None:
        raise RuntimeError(
            "AgentExecution has no Workspace binding. "
            "Standard Agents include a Workspace; call agent.use_workspace(...) "
            "only when you need an explicit root, mode, or provider."
        )
    if purpose in {"process", "recovery"}:
        async with owner._workspace_record_lock:
            if owner._completed or owner.status != "running":
                phase = "post-terminal" if owner._completed else "non-active"
                raise RuntimeError(
                    f"AgentExecution {phase} Workspace writes reject purpose={purpose!r}; "
                    "process and recovery writes require an active running execution."
                )
            return await _record_workspace_locked(
                owner,
                purpose=purpose,
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
                post_terminal=False,
            )
    if not owner._completed:
        await owner.async_get_data()
    async with owner._workspace_record_lock:
        return await _record_workspace_locked(
            owner,
            purpose=purpose,
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
            post_terminal=owner._completed,
        )


async def _record_workspace_locked(
    owner: "AgentExecution",
    *,
    purpose: AgentExecutionWorkspacePurpose,
    collection: str,
    kind: str | None,
    content: Any,
    summary: str | None,
    scope: dict[str, Any] | None,
    source: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    checkpoint: bool,
    checkpoint_state: dict[str, Any] | None,
    checkpoint_step_id: str | None,
    profile: str,
    post_terminal: bool,
) -> AgentExecutionWorkspaceRecord:
    owner._refresh_diagnostics()

    record_scope = workspace_scope(owner, scope)
    record_source = workspace_source(owner, source)
    record_meta = {
        "execution_id": owner.id,
        "lineage": DataFormatter.sanitize(owner.lineage),
    }
    record_meta.update(dict(meta or {}))
    record_meta["workspace_purpose"] = purpose
    record_content = content if content is not None else default_workspace_content(owner)
    record_summary = summary or default_workspace_summary(owner, collection)

    record_ref = await owner.workspace.put(
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
        checkpoint_ref = await owner.workspace.put_checkpoint(
            owner.id,
            checkpoint_state or default_checkpoint_state(owner, record_ref),
            step_id=checkpoint_step_id or owner.lineage.get("step_id"),
        )
        append_workspace_ref(owner, "checkpoints", checkpoint_ref)
        evidence_link = await owner.workspace.link_evidence(
            record_ref,
            checkpoint_ref,
            relation="checkpointed_by",
            execution_id=owner.id,
            checkpoint_id=checkpoint_ref.get("id"),
            meta={
                "owner": "AgentExecution",
                "lineage": DataFormatter.sanitize(owner.lineage),
            },
        )
        append_workspace_ref(owner, "verification_evidence", evidence_link)

    response: AgentExecutionWorkspaceRecord = {
        "record": record_ref,
        "checkpoint": checkpoint_ref,
        "workspace_refs": DataFormatter.sanitize(owner.workspace_refs),
    }
    if post_terminal:
        from .terminal_retention import apply_agent_execution_terminal_retention

        await apply_agent_execution_terminal_retention(
            owner,
            status=(
                owner._terminal_status
                or ("completed" if owner.status in {"success", "completed"} else "cancelled" if owner.status == "cancelled" else "failed")
            ),
        )
    return response


def workspace_scope(owner: "AgentExecution", scope: dict[str, Any] | None = None) -> dict[str, Any]:
    lineage_scope = owner.lineage.get("scope")
    merged = dict(lineage_scope) if isinstance(lineage_scope, dict) else {}
    merged.setdefault("execution_id", owner.id)
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
            "status": owner.status,
            "lineage": owner.lineage,
            "result": owner.result,
            "route_plan": owner.route_plan,
            "diagnostics": owner.diagnostics,
        }
    )


def default_workspace_summary(owner: "AgentExecution", collection: str) -> str:
    task_id = owner.lineage.get("task_id") or owner.id
    step_id = owner.lineage.get("step_id") or "execution"
    return f"{ task_id } { step_id } AgentExecution { collection }"


def default_checkpoint_state(owner: "AgentExecution", record_ref: dict[str, Any]) -> dict[str, Any]:
    return DataFormatter.sanitize(
        {
            "execution_id": owner.id,
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
