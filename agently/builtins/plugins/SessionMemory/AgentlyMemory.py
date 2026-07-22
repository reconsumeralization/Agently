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
from collections.abc import Mapping, Sequence
from typing import Any, cast

from agently.core.context import ModelRequestContextSelector, TaskContext
from agently.core.storage.Retrieval import _default_model_unavailable_reason
from agently.types.data import ContextBudget, ContextReadIntent
from agently.types.plugins import SessionMemory
from agently.utils import DataFormatter, Settings, SettingsNamespace

from .ContextSource import AgentlyMemoryContextSource


GLOBAL_MEMORY = "GLOBAL_MEMORY"
SESSION_MEMORY = "SESSION_MEMORY"


class AgentlyMemory(SessionMemory):
    name = "AgentlyMemory"
    DEFAULT_SETTINGS = {
        "$global": {
            "session.memory.AgentlyMemory": {
                "recall": {
                    "enabled": True,
                    "budget": {
                        "max_chars": 4000,
                        "max_blocks": 16,
                        "max_block_chars": 1200,
                    },
                    "prompt_query_chars": 2000,
                },
                "extract": {
                    "enabled": True,
                    "max_memories": 6,
                    "max_context_chars": 12000,
                },
                "body_schema": {
                    "summary": "string",
                    "details": "object or string",
                    "confidence": "number from 0.0 to 1.0",
                },
                "vector_index": {
                    "enabled": False,
                },
            }
        }
    }

    def __init__(
        self,
        *,
        session: Any,
        memory_store: Any = None,
        plugin_manager: Any,
        settings: Settings,
    ) -> None:
        self.session = session
        self.memory_store = memory_store
        self.plugin_manager = plugin_manager
        self.settings = settings
        self.plugin_settings = SettingsNamespace(settings, "session.memory.AgentlyMemory")
        self.diagnostics: list[dict[str, Any]] = []

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def bind_memory_store(self, memory_store: Any) -> None:
        self.memory_store = memory_store

    def create_context_source(
        self,
        *,
        session: Any,
        settings: Any,
    ) -> AgentlyMemoryContextSource:
        del settings
        return AgentlyMemoryContextSource(
            self._require_memory_store(),
            session_id=str(session.id),
        )

    async def prepare_request(
        self,
        *,
        prompt: Any,
        session: Any,
        settings: Any,
    ) -> dict[str, Any]:
        del settings
        if not self.plugin_settings.get("recall.enabled", True):
            return {"enabled": False, "reason": "recall_disabled"}
        context = TaskContext(
            task_id=f"session-request:{session.id}:{uuid.uuid4().hex}"
        )
        source = self.create_context_source(session=session, settings=self.settings)
        context.attach(
            source,
            binding_id=f"session-memory-binding:{session.id}",
            scope="session",
            metadata={"session_id": str(session.id)},
        )
        reader = context.reader(
            consumer=f"session-request:{session.id}",
            phase="direct",
            budget=self._context_budget(),
            semantic_selector=ModelRequestContextSelector(
                lambda: self._create_model_request("selection")
            ),
        )
        package = await reader.async_read(
            ContextReadIntent(query=self._prompt_text(prompt))
        )
        prompt.set(
            "info.session_memory_context",
            {
                "blocks": [
                    {
                        "content": DataFormatter.sanitize(block.content),
                        "role": block.role,
                        "source_ref": block.source_ref,
                        "completeness": block.completeness,
                    }
                    for block in package.blocks
                ]
            },
        )
        diagnostics: dict[str, Any] = {
            "enabled": True,
            "path": "task_context",
            "task_context_id": context.context_id,
            "package_id": package.package_id,
            "source_catalog": context.source_catalog(),
            "diagnostics": [item.to_dict() for item in package.diagnostics],
        }
        self.diagnostics.append({"phase": "prepare_request", **diagnostics})
        return diagnostics

    async def after_turn(
        self,
        *,
        session: Any,
        user_content: str | None,
        assistant_content: str | None,
        result: Any,
        settings: Any,
    ) -> dict[str, Any]:
        _ = (result, settings)
        if not self.plugin_settings.get("extract.enabled", True):
            return {"enabled": False, "reason": "extract_disabled"}
        if not user_content and not assistant_content:
            return {"enabled": True, "stored": 0, "reason": "empty_turn"}
        memory_store = self._require_memory_store()
        try:
            memories = await self._extract_memories(
                session=session,
                user_content=user_content,
                assistant_content=assistant_content,
            )
        except Exception as exc:
            diagnostics = {
                "enabled": True,
                "stored": 0,
                "degraded": True,
                "reason": "extract_failed",
                "error": str(exc),
            }
            self.diagnostics.append({"phase": "after_turn", **diagnostics})
            return diagnostics

        stored_refs = []
        for memory in memories:
            normalized = self._normalize_memory(memory, session=session)
            if normalized is None:
                continue
            stored_refs.append(await self._store_memory(memory_store, normalized, session=session))
        diagnostics = {
            "enabled": True,
            "stored": len(stored_refs),
            "refs": stored_refs,
        }
        self.diagnostics.append({"phase": "after_turn", **diagnostics})
        return diagnostics

    def _require_memory_store(self) -> Any:
        if self.memory_store is None:
            raise RuntimeError(
                "Session memory mode 'AgentlyMemory' requires a RecordStore. "
                "Pass memory_store=... to Session.use_memory(...) or activate the Session from an Agent with memory_store support."
            )
        return self.memory_store

    async def _extract_memories(
        self,
        *,
        session: Any,
        user_content: str | None,
        assistant_content: str | None,
    ) -> list[dict[str, Any]]:
        max_context_chars = self._int_setting("extract.max_context_chars", 12000)
        context = {
            "session_id": str(session.id),
            "user_content": self._limit_text(str(user_content or ""), max_context_chars // 2),
            "assistant_content": self._limit_text(str(assistant_content or ""), max_context_chars // 2),
            "body_schema": self.plugin_settings.get("body_schema", {}),
            "max_memories": self.plugin_settings.get("extract.max_memories", 6),
        }
        payload = await self._model_request(
            phase="extract",
            default_input=context,
            default_instruct=(
                "Extract durable memories from this completed turn. "
                "Keep only facts that will likely help future requests. "
                "Compress each memory into the configured body schema."
            ),
            default_output={
                "memories": [
                    {
                        "scope": (str, "GLOBAL_MEMORY or SESSION_MEMORY."),
                        "summary": (str, "Short stable memory summary."),
                        "body": (dict, "Memory body matching the configured body schema."),
                        "tags": ([str], "Retrieval tags."),
                        "importance": (float, "Importance from 0.0 to 1.0."),
                    }
                ]
            },
        )
        raw_memories = payload.get("memories", []) if isinstance(payload, Mapping) else []
        if not isinstance(raw_memories, Sequence) or isinstance(raw_memories, (str, bytes, bytearray)):
            return []
        return [dict(memory) for memory in raw_memories if isinstance(memory, Mapping)]

    async def _model_request(
        self,
        *,
        phase: str,
        default_input: Any,
        default_instruct: str,
        default_output: Any,
    ) -> Any:
        request = self._create_model_request(phase)
        request.input(default_input)
        request.instruct(default_instruct)
        request.output(default_output, format="json")
        return await request.async_get_data(max_retries=0, raise_ensure_failure=True)

    def _create_model_request(self, phase: str) -> Any:
        unavailable = _default_model_unavailable_reason(self.settings)
        if unavailable is not None:
            raise RuntimeError(unavailable)
        from agently.core.model import ModelRequest

        request = ModelRequest(
            self.plugin_manager,
            agent_name=f"SessionMemory:{ self.name }:{ phase }",
            parent_settings=self.settings,
        )
        self._apply_execution_config(request, phase)
        return request

    def _apply_execution_config(self, request: Any, phase: str) -> None:
        execution = self.plugin_settings.get(f"{ phase }.execution", None)
        if not isinstance(execution, Mapping):
            return
        for key in ("system", "developer", "input", "info", "instruct", "examples"):
            if key in execution:
                request.set_prompt(key, execution[key])
        if "output" in execution:
            output_format = execution.get("output_format", execution.get("$format", "json"))
            request.output(execution["output"], format=cast(Any, output_format))
        elif "output_format" in execution or "$format" in execution:
            request.set_prompt("output_format", execution.get("output_format", execution.get("$format")))

    def _normalize_memory(self, memory: Mapping[str, Any], *, session: Any) -> dict[str, Any] | None:
        summary = str(memory.get("summary") or "").strip()
        body = memory.get("body", memory.get("memory", memory.get("content")))
        if not summary and body in (None, "", {}, []):
            return None
        scope = str(memory.get("scope") or SESSION_MEMORY).strip().upper()
        if scope not in {GLOBAL_MEMORY, SESSION_MEMORY}:
            scope = SESSION_MEMORY
        tags = self._merge_tags(memory.get("tags", []))
        provenance = {
            "plugin": self.name,
            "session_id": str(session.id),
            "turn_index": len(getattr(session, "full_context", []) or []),
        }
        return {
            "memory_scope": scope,
            "kind": "global_memory" if scope == GLOBAL_MEMORY else "session_memory",
            "summary": summary,
            "body": body if body is not None else {"summary": summary},
            "tags": tags,
            "importance": memory.get("importance"),
            "provenance": provenance,
        }

    async def _store_memory(self, memory_store: Any, memory: dict[str, Any], *, session: Any) -> Any:
        memory_scope = str(memory["memory_scope"])
        scope = {"memory_scope": memory_scope}
        if memory_scope == SESSION_MEMORY:
            scope["session_id"] = str(session.id)
        vector_enabled = bool(self.plugin_settings.get("vector_index.enabled", False))
        vector_meta = self._vector_index_meta(memory_store)
        provenance = dict(memory["provenance"])
        record_body = {
            "memory_scope": memory_scope,
            "summary": memory["summary"],
            "body": memory["body"],
            "tags": memory["tags"],
            "importance": memory.get("importance"),
            "provenance": provenance,
            "vector_index": vector_meta,
        }
        return await memory_store.put(
            record_body,
            collection="memory",
            kind=str(memory["kind"]),
            summary=str(memory["summary"]),
            scope=scope,
            source=provenance,
            vector=vector_enabled,
            meta={
                "tags": memory["tags"],
                "memory_scope": memory_scope,
                "session_id": str(session.id) if memory_scope == SESSION_MEMORY else None,
                "vector_index": vector_meta,
            },
        )

    def _vector_index_meta(self, memory_store: Any) -> dict[str, Any]:
        capabilities = memory_store.capabilities()
        materialized = {
            str(component)
            for component in capabilities.get("materialized_components", [])
        }
        return {
            "requested": bool(self.plugin_settings.get("vector_index.enabled", False)),
            "backend": None,
            "available": {"embedding", "vector"}.issubset(materialized),
        }

    def _prompt_text(self, prompt: Any) -> str:
        try:
            text = prompt.to_text()
        except Exception:
            try:
                text = prompt.to_serializable_prompt_data(inherit=True)
            except Exception:
                text = prompt
        resolved = self._limit_text(
            str(text or ""),
            self._int_setting("recall.prompt_query_chars", 2000),
        )
        return resolved.strip() or "Recall task-relevant Session memory."

    def _context_budget(self) -> ContextBudget:
        budget = self._dict_setting(
            "recall.budget",
            {
                "max_chars": 4000,
                "max_blocks": 16,
                "max_block_chars": 1200,
            },
        )
        defaults = {
            "max_chars": 4000,
            "max_blocks": 16,
            "max_block_chars": 1200,
        }
        resolved: dict[str, int] = {}
        for key, default in defaults.items():
            value = budget.get(key, default)
            resolved[key] = (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
                else default
            )
        return ContextBudget(**resolved)

    def _dict_setting(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        value = self.plugin_settings.get(key, default)
        return dict(value) if isinstance(value, Mapping) else dict(default)

    def _int_setting(self, key: str, default: int) -> int:
        value = self.plugin_settings.get(key, default)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, str)):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return default

    def _merge_tags(self, *tag_groups: Any) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for group in tag_groups:
            if isinstance(group, str):
                candidates = [group]
            elif isinstance(group, Sequence) and not isinstance(group, (bytes, bytearray)):
                candidates = [str(item) for item in group]
            else:
                candidates = []
            for item in candidates:
                normalized = item.strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    tags.append(normalized)
        return tags

    @staticmethod
    def _limit_text(text: str, max_chars: int) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 15)].rstrip() + "\n[truncated]"
