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

from .TaskShared import *

_GUIDANCE_PREVIEW_CHARS = 800


class AgentTaskGuidanceMixin(AgentTaskMixinBase):
    async def async_add_guidance(
        self,
        content: Any,
        *,
        guidance_id: str | None = None,
        author: str | None = None,
        target: Any = "task",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(content, str) and not content.strip():
            raise ValueError("AgentTask guidance content must not be empty.")
        lock = self._ensure_guidance_lock()
        async with lock:
            guidance_ref = self._new_guidance_ref(
                content,
                guidance_id=guidance_id,
                author=author,
                target=target,
                meta=meta,
            )
            terminal = bool(getattr(self, "_completed", False))
            guidance_ref["status"] = "received_after_terminal" if terminal else "received"
            record_ref = await self.workspace.ingest(
                content={
                    "schema_version": "agent_task_guidance/v1",
                    "task_id": self.id,
                    "guidance_id": guidance_ref["id"],
                    "status": guidance_ref["status"],
                    "content": guidance_ref["content"],
                    "content_preview": guidance_ref["content_preview"],
                    "target": guidance_ref["target"],
                    "author": guidance_ref.get("author"),
                    "received_at": guidance_ref["received_at"],
                    "meta": guidance_ref.get("meta", {}),
                },
                collection="guidance",
                kind="agent_task_guidance",
                summary=f"{self.id} runtime guidance {guidance_ref['id']}",
                scope={
                    "task_id": self.id,
                    "guidance_id": guidance_ref["id"],
                    "target": DataFormatter.sanitize(guidance_ref["target"]),
                },
                source={"type": "agent_task", "phase": "guidance", "author": author},
                meta={
                    "task_id": self.id,
                    "guidance_id": guidance_ref["id"],
                    "schema_version": "agent_task_guidance/v1",
                },
            )
            guidance_ref["workspace_ref"] = DataFormatter.sanitize(record_ref)
            self._append_workspace_ref("guidance", record_ref)
            checkpoint_ref = await self.workspace.put_checkpoint(
                self.id,
                {
                    "schema_version": "agent_task_guidance_checkpoint/v1",
                    "task_id": self.id,
                    "guidance_id": guidance_ref["id"],
                    "status": guidance_ref["status"],
                    "guidance_ref": record_ref.get("id"),
                    "guidance_items": self._guidance_context_projection(extra=[guidance_ref]),
                },
                step_id=f"guidance-{guidance_ref['id']}",
            )
            guidance_ref["checkpoint_ref"] = DataFormatter.sanitize(checkpoint_ref)
            self._append_workspace_ref("checkpoints", checkpoint_ref)
            self.guidance_items.append(DataFormatter.sanitize(guidance_ref))
            self._record_guidance_diagnostic(guidance_ref["status"])
            event_name = (
                "agent_task.guidance.ignored"
                if guidance_ref["status"] == "received_after_terminal"
                else "agent_task.guidance.received"
            )
            await self._emit(
                event_name,
                self._guidance_event_payload(guidance_ref),
                meta={
                    "task_id": self.id,
                    "status": self.status,
                    "stream_kind": "guidance",
                    "guidance_status": guidance_ref["status"],
                    "guidance_id": guidance_ref["id"],
                },
            )
            return DataFormatter.sanitize(guidance_ref)

    def add_guidance(self, *args: Any, **kwargs: Any) -> Any:
        return FunctionShifter.syncify(self.async_add_guidance)(*args, **kwargs)

    async def _apply_guidance_boundary(
        self,
        *,
        iteration_index: int | None = None,
        boundary: str,
        target: Any = None,
    ) -> list[dict[str, Any]]:
        applicable_statuses = {"received", "queued", "forwarded"}
        applied: list[dict[str, Any]] = []
        now = time.time()
        for item in getattr(self, "guidance_items", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") not in applicable_statuses:
                continue
            if target not in (None, "", "task"):
                guidance_target = item.get("target")
                if guidance_target not in (target, "task"):
                    continue
            item["status"] = "applied"
            item["applied_at"] = now
            item["applied_iteration"] = iteration_index
            item["applied_boundary"] = boundary
            applied.append(DataFormatter.sanitize(item))
        if not applied:
            return []
        self._record_guidance_diagnostic("applied", count=len(applied))
        await self._emit(
            "agent_task.guidance.applied",
            {
                "task_id": self.id,
                "status": "applied",
                "guidance_ids": [item["id"] for item in applied],
                "iteration": iteration_index,
                "boundary": boundary,
                "guidance": self._guidance_context_projection(items=applied),
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration_index,
                "stream_kind": "guidance",
                "guidance_status": "applied",
                "boundary": boundary,
            },
        )
        return applied

    def _context_pack_with_guidance(self, context_pack: Any) -> "WorkspaceContextPackage":
        if not isinstance(context_pack, Mapping):
            return cast("WorkspaceContextPackage", context_pack)
        context = dict(context_pack)
        projection = self._guidance_context_projection()
        if projection:
            context["guidance"] = projection
            diagnostics = context.get("diagnostics")
            diagnostics = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
            diagnostics["guidance_count"] = len(projection)
            diagnostics["guidance_ids"] = [item["id"] for item in projection]
            context["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return cast("WorkspaceContextPackage", DataFormatter.sanitize(context))

    def _guidance_context_projection(
        self,
        *,
        items: Sequence[Mapping[str, Any]] | None = None,
        extra: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        source_items: list[Mapping[str, Any]] = []
        if items is None:
            for item in getattr(self, "guidance_items", []) or []:
                if isinstance(item, Mapping):
                    source_items.append(item)
        else:
            source_items.extend(item for item in items if isinstance(item, Mapping))
        if extra is not None:
            source_items.extend(item for item in extra if isinstance(item, Mapping))
        projection: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in source_items:
            guidance_id = str(item.get("id") or "").strip()
            if not guidance_id or guidance_id in seen:
                continue
            status = str(item.get("status") or "")
            if status in {"ignored", "received_after_terminal"}:
                continue
            workspace_ref = item.get("workspace_ref")
            workspace_ref_id = workspace_ref.get("id") if isinstance(workspace_ref, Mapping) else None
            projection.append(
                DataFormatter.sanitize(
                    {
                        "id": guidance_id,
                        "kind": "guidance",
                        "status": status,
                        "target": item.get("target", "task"),
                        "content_preview": item.get("content_preview"),
                        "workspace_ref": workspace_ref_id,
                        "applied_iteration": item.get("applied_iteration"),
                        "applied_boundary": item.get("applied_boundary"),
                    }
                )
            )
            seen.add(guidance_id)
        return projection

    def _new_guidance_ref(
        self,
        content: Any,
        *,
        guidance_id: str | None = None,
        author: str | None = None,
        target: Any = "task",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._guidance_sequence = int(getattr(self, "_guidance_sequence", 0)) + 1
        resolved_id = str(guidance_id or "").strip() or f"guidance-{uuid.uuid4().hex}"
        return {
            "id": resolved_id,
            "task_id": self.id,
            "kind": "guidance",
            "sequence": self._guidance_sequence,
            "content": DataFormatter.sanitize(content),
            "content_preview": self._guidance_preview(content),
            "author": str(author or "").strip() or None,
            "target": DataFormatter.sanitize(target or "task"),
            "status": "received",
            "received_at": time.time(),
            "meta": DataFormatter.sanitize(meta or {}),
        }

    def _ensure_guidance_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_guidance_lock", None)
        if lock is None or not hasattr(lock, "acquire"):
            lock = asyncio.Lock()
            self._guidance_lock = lock
        return lock

    @staticmethod
    def _guidance_preview(content: Any) -> str:
        text = str(content if content is not None else "").strip()
        if len(text) <= _GUIDANCE_PREVIEW_CHARS:
            return text
        return text[: max(0, _GUIDANCE_PREVIEW_CHARS - 16)].rstrip() + " [truncated]"

    def _record_guidance_diagnostic(self, status: str, *, count: int = 1) -> None:
        diagnostics = self.diagnostics.setdefault("guidance", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {}
            self.diagnostics["guidance"] = diagnostics
        key = str(status or "received")
        diagnostics[key] = int(diagnostics.get(key) or 0) + count
        diagnostics["total"] = len([item for item in getattr(self, "guidance_items", []) if isinstance(item, dict)])

    @staticmethod
    def _guidance_event_payload(guidance_ref: Mapping[str, Any]) -> dict[str, Any]:
        return DataFormatter.sanitize(
            {
                "task_id": guidance_ref.get("task_id"),
                "guidance_id": guidance_ref.get("id"),
                "kind": "guidance",
                "status": guidance_ref.get("status"),
                "target": guidance_ref.get("target"),
                "content_preview": guidance_ref.get("content_preview"),
                "workspace_ref": guidance_ref.get("workspace_ref"),
                "checkpoint_ref": guidance_ref.get("checkpoint_ref"),
            }
        )


__all__ = ["AgentTaskGuidanceMixin"]
