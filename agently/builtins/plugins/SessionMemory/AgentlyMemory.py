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

from collections.abc import Mapping, Sequence
from typing import Any, cast

from agently.core.workspace.Retrieval import _default_model_unavailable_reason
from agently.types.data.workspace import WorkspaceRetrievalPackage
from agently.types.plugins import SessionMemory
from agently.utils import Settings, SettingsNamespace


GLOBAL_MEMORY = "GLOBAL_MEMORY"
SESSION_MEMORY = "SESSION_MEMORY"


class AgentlyMemory(SessionMemory):
    name = "AgentlyMemory"
    DEFAULT_SETTINGS = {
        "$global": {
            "session.memory.AgentlyMemory": {
                "retrieve": {
                    "enabled": True,
                    "budget": {"chars": 4000, "item_chars": 1200},
                    "selection": "length",
                    "sources": ["records"],
                    "method": "keyword",
                    "rerank": True,
                    "rerank_min_candidates": 2,
                    "keep_candidates_on_empty_rerank": True,
                    "tags": [],
                    "max_candidates": 50,
                },
                "retrieve_plan": {
                    "enabled": True,
                    "fallback_query_chars": 2000,
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
        workspace: Any = None,
        plugin_manager: Any,
        settings: Settings,
    ) -> None:
        self.session = session
        self.workspace = workspace
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

    def bind_workspace(self, workspace: Any) -> None:
        self.workspace = workspace

    async def prepare_request(
        self,
        *,
        prompt: Any,
        session: Any,
        settings: Any,
    ) -> dict[str, Any]:
        if not self.plugin_settings.get("retrieve.enabled", True):
            return {"enabled": False, "reason": "retrieve_disabled"}
        workspace = self._require_workspace()
        plan = await self._plan_retrieval(prompt=prompt, session=session)
        diagnostics: dict[str, Any] = {"plan": plan, "packages": {}}
        budget = self._dict_setting("retrieve.budget", {"chars": 4000, "item_chars": 1200})
        selection = str(self.plugin_settings.get("retrieve.selection", "length"))
        if selection not in {"length", "top_n"}:
            selection = "length"
        tags = self._merge_tags(
            self.plugin_settings.get("retrieve.tags", []),
            plan.get("tags", []),
        )
        scopes = []
        if bool(plan.get("include_global", True)):
            scopes.append(GLOBAL_MEMORY)
        if bool(plan.get("include_session", True)):
            scopes.append(SESSION_MEMORY)
        for memory_scope in scopes:
            package = await self._retrieve_scope(
                workspace=workspace,
                session=session,
                query=str(plan.get("query") or ""),
                tags=tags,
                budget=budget,
                selection=cast(Any, selection),
                memory_scope=memory_scope,
            )
            diagnostics["packages"][memory_scope] = package.get("diagnostics", {})
            prompt.set(memory_scope, self._prompt_memory_package(package))
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
        workspace = self._require_workspace()
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
            stored_refs.append(await self._store_memory(workspace, normalized, session=session))
        diagnostics = {
            "enabled": True,
            "stored": len(stored_refs),
            "refs": stored_refs,
        }
        self.diagnostics.append({"phase": "after_turn", **diagnostics})
        return diagnostics

    def _require_workspace(self) -> Any:
        if self.workspace is None:
            raise RuntimeError(
                "Session memory mode 'AgentlyMemory' requires a Workspace. "
                "Pass workspace=... to Session.use_memory(...) or activate the Session from an Agent with workspace support."
            )
        return self.workspace

    async def _retrieve_scope(
        self,
        *,
        workspace: Any,
        session: Any,
        query: str,
        tags: list[str],
        budget: dict[str, Any],
        selection: Any,
        memory_scope: str,
    ) -> WorkspaceRetrievalPackage:
        kind = "global_memory" if memory_scope == GLOBAL_MEMORY else "session_memory"
        scope = {"memory_scope": memory_scope}
        if memory_scope == SESSION_MEMORY:
            scope["session_id"] = str(session.id)
        retrieve_kwargs = {
            "tags": tags,
            "filters": {"collection": "memory", "kind": kind},
            "scope": scope,
            "sources": self._list_setting("retrieve.sources", ["records"]),
            "budget": budget,
            "selection": selection,
            "method": cast(Any, self.plugin_settings.get("retrieve.method", "keyword")),
            "max_candidates": self._int_setting("retrieve.max_candidates", 50),
            "plugin_manager": self.plugin_manager,
            "settings": self.settings,
        }
        rerank_enabled = bool(self.plugin_settings.get("retrieve.rerank", True))
        if not rerank_enabled:
            return await workspace.retrieve(
                query,
                rerank=False,
                **retrieve_kwargs,
            )

        deterministic_package = await workspace.retrieve(
            query,
            rerank=False,
            **retrieve_kwargs,
        )
        deterministic_diagnostics = deterministic_package.get("diagnostics", {})
        candidate_count = self._diagnostic_count(deterministic_diagnostics, "candidate_count")
        rerank_min_candidates = max(1, self._int_setting("retrieve.rerank_min_candidates", 2))
        if candidate_count < rerank_min_candidates:
            updated_diagnostics = dict(deterministic_diagnostics)
            updated_diagnostics["memory_rerank_skipped"] = {
                "enabled": True,
                "reason": "candidate_count_below_min",
                "candidate_count": candidate_count,
                "rerank_min_candidates": rerank_min_candidates,
            }
            deterministic_package["diagnostics"] = updated_diagnostics
            return deterministic_package

        package = await workspace.retrieve(
            query,
            rerank=True,
            rerank_handler=self._rerank_candidates,
            **retrieve_kwargs,
        )
        if self._should_use_empty_rerank_fallback(package):
            fallback_diagnostics = dict(deterministic_package.get("diagnostics") or {})
            fallback_diagnostics["memory_rerank_empty_fallback"] = {
                "enabled": True,
                "reason": "rerank_dropped_all_memory_candidates",
                "original": package.get("diagnostics", {}),
            }
            deterministic_package["diagnostics"] = fallback_diagnostics
            return deterministic_package
        return package

    @staticmethod
    def _diagnostic_count(diagnostics: Any, key: str) -> int:
        if not isinstance(diagnostics, Mapping):
            return 0
        try:
            return int(diagnostics.get(key, 0))
        except (TypeError, ValueError):
            return 0

    def _should_use_empty_rerank_fallback(self, package: WorkspaceRetrievalPackage) -> bool:
        if not bool(self.plugin_settings.get("retrieve.keep_candidates_on_empty_rerank", True)):
            return False
        diagnostics = package.get("diagnostics", {})
        if not isinstance(diagnostics, Mapping):
            return False
        candidate_count = diagnostics.get("candidate_count", 0)
        selected_count = diagnostics.get("selected_count", len(package.get("items", [])))
        rerank = diagnostics.get("rerank", {})
        if not isinstance(rerank, Mapping):
            return False
        try:
            candidates = int(candidate_count)
            selected = int(selected_count)
            dropped = int(rerank.get("dropped", 0))
        except (TypeError, ValueError):
            return False
        return (
            candidates > 0
            and selected == 0
            and bool(rerank.get("enabled", False))
            and not bool(rerank.get("degraded", False))
            and dropped >= candidates
        )

    async def _plan_retrieval(self, *, prompt: Any, session: Any) -> dict[str, Any]:
        prompt_text = self._prompt_text(prompt)
        if not self.plugin_settings.get("retrieve_plan.enabled", True):
            return self._fallback_retrieval_plan(prompt_text)
        try:
            payload = await self._model_request(
                phase="retrieve_plan",
                default_input={
                    "session_id": str(session.id),
                    "prompt": prompt_text,
                    "configured_tags": self.plugin_settings.get("retrieve.tags", []),
                },
                default_instruct=(
                    "Generate a compact retrieval query and optional tags for Session memory. "
                    "Decide whether GLOBAL_MEMORY and SESSION_MEMORY are useful for this request."
                ),
                default_output={
                    "query": (str, "Compact retrieval query for Workspace memory."),
                    "tags": ([str], "Tags that should be used for memory retrieval."),
                    "include_global": (bool, "Whether to retrieve Workspace-global memory."),
                    "include_session": (bool, "Whether to retrieve active-session memory."),
                },
            )
        except Exception as exc:
            plan = self._fallback_retrieval_plan(prompt_text)
            plan["diagnostics"] = {"degraded": True, "reason": "retrieve_plan_failed", "error": str(exc)}
            return plan
        if not isinstance(payload, Mapping):
            return self._fallback_retrieval_plan(prompt_text)
        return {
            "query": str(payload.get("query") or self._fallback_query(prompt_text)),
            "tags": self._merge_tags(payload.get("tags", [])),
            "include_global": bool(payload.get("include_global", True)),
            "include_session": bool(payload.get("include_session", True)),
        }

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

    async def _rerank_candidates(self, *, query: str | None, candidates: list[dict[str, Any]]) -> Any:
        return await self._model_request(
            phase="rerank",
            default_input={
                "query": query or "",
                "candidates": candidates,
            },
            default_instruct=(
                "Judge which retrieved memory candidates are useful for the query. "
                "Drop irrelevant candidates and keep useful candidates with relevance scores."
            ),
            default_output={
                "decisions": [
                    {
                        "id": (str, "Candidate id exactly as provided."),
                        "useful": (bool, "True when this memory should be injected."),
                        "score": (float, "Relevance from 0.0 to 1.0."),
                        "reason": (str, "Brief decision reason."),
                    }
                ]
            },
        )

    async def _model_request(
        self,
        *,
        phase: str,
        default_input: Any,
        default_instruct: str,
        default_output: Any,
    ) -> Any:
        unavailable = _default_model_unavailable_reason(self.settings)
        if unavailable is not None:
            raise RuntimeError(unavailable)
        from agently.core.model import ModelRequest

        request = ModelRequest(
            self.plugin_manager,
            agent_name=f"SessionMemory:{ self.name }:{ phase }",
            parent_settings=self.settings,
        )
        request.input(default_input)
        request.instruct(default_instruct)
        request.output(default_output, format="json")
        self._apply_execution_config(request, phase)
        return await request.async_get_data(max_retries=0, raise_ensure_failure=True)

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

    async def _store_memory(self, workspace: Any, memory: dict[str, Any], *, session: Any) -> Any:
        memory_scope = str(memory["memory_scope"])
        scope = {"memory_scope": memory_scope}
        if memory_scope == SESSION_MEMORY:
            scope["session_id"] = str(session.id)
        vector_meta = self._vector_index_meta(workspace)
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
        return await workspace.put(
            record_body,
            collection="memory",
            kind=str(memory["kind"]),
            summary=str(memory["summary"]),
            scope=scope,
            source=provenance,
            meta={
                "tags": memory["tags"],
                "memory_scope": memory_scope,
                "session_id": str(session.id) if memory_scope == SESSION_MEMORY else None,
                "vector_index": vector_meta,
            },
        )

    def _vector_index_meta(self, workspace: Any) -> dict[str, Any]:
        vector_index = getattr(getattr(workspace, "backend", None), "vector_index", None)
        return {
            "requested": bool(self.plugin_settings.get("vector_index.enabled", False)),
            "backend": type(vector_index).__name__ if vector_index is not None else None,
            "available": vector_index is not None and getattr(vector_index, "name", None) != "noop",
        }

    def _prompt_memory_package(self, package: WorkspaceRetrievalPackage) -> list[dict[str, Any]]:
        items = []
        for item in package.get("items", []):
            record = item.get("ref", {})
            items.append(
                {
                    "summary": item.get("summary"),
                    "content": item.get("content"),
                    "tags": item.get("tags", []),
                    "score": item.get("score"),
                    "source_ref": {
                        "collection": record.get("collection") if isinstance(record, Mapping) else None,
                        "kind": record.get("kind") if isinstance(record, Mapping) else None,
                        "id": record.get("id") if isinstance(record, Mapping) else None,
                    },
                }
            )
        return items

    def _prompt_text(self, prompt: Any) -> str:
        try:
            text = prompt.to_text()
        except Exception:
            try:
                text = prompt.to_serializable_prompt_data(inherit=True)
            except Exception:
                text = prompt
        return self._limit_text(str(text or ""), self._int_setting("retrieve_plan.fallback_query_chars", 2000))

    def _fallback_retrieval_plan(self, prompt_text: str) -> dict[str, Any]:
        return {
            "query": self._fallback_query(prompt_text),
            "tags": self._merge_tags(self.plugin_settings.get("retrieve.tags", [])),
            "include_global": True,
            "include_session": True,
            "diagnostics": {"degraded": True, "reason": "deterministic_fallback"},
        }

    def _fallback_query(self, prompt_text: str) -> str:
        limit = self._int_setting("retrieve_plan.fallback_query_chars", 2000)
        return self._limit_text(prompt_text, limit)

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

    def _list_setting(self, key: str, default: list[str]) -> list[str]:
        value = self.plugin_settings.get(key, default)
        if isinstance(value, str):
            return [value]
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [str(item) for item in value]
        return list(default)

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
