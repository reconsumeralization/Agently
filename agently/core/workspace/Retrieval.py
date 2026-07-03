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

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.types.data.workspace import (
    WorkspaceFileSearchResult,
    WorkspaceRecordRef,
    WorkspaceRetrievalItem,
    WorkspaceRetrievalMethod,
    WorkspaceRetrievalPackage,
    WorkspaceRetrievalSelection,
)

if TYPE_CHECKING:
    from .Workspace import Workspace


RetrievalSourceName = Literal["records", "record", "files", "file"]
RerankHandler = Callable[..., Any]


async def retrieve_workspace(
    workspace: "Workspace",
    query: str | None = None,
    *,
    tags: Sequence[str] | None = None,
    filters: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    sources: Sequence[RetrievalSourceName | str] | None = None,
    budget: Mapping[str, Any] | None = None,
    selection: WorkspaceRetrievalSelection = "length",
    top_n: int | None = None,
    method: WorkspaceRetrievalMethod = "keyword",
    rerank: bool | None = None,
    rerank_handler: RerankHandler | None = None,
    max_rerank_retries: int = 1,
    file_options: Mapping[str, Any] | None = None,
    max_candidates: int | None = None,
    profile: str = "auto",
    plugin_manager: Any = None,
    settings: Any = None,
) -> WorkspaceRetrievalPackage:
    normalized_budget = dict(budget or {})
    selected_sources = _normalize_sources(sources)
    safe_max_candidates = _max_candidates(max_candidates, normalized_budget)
    scoped_filters = _scoped_retrieval_filters(filters, scope)
    normalized_tags = _normalize_tags(tags)
    diagnostics: dict[str, Any] = {
        "profile": profile,
        "method": method,
        "selection": selection,
        "sources": sorted(selected_sources),
        "filters": scoped_filters,
        "tags": normalized_tags,
        "max_candidates": safe_max_candidates,
    }

    candidates: list[dict[str, Any]] = []
    if "records" in selected_sources:
        record_candidates = await _record_candidates(
            workspace,
            query=query,
            tags=normalized_tags,
            filters=scoped_filters,
            method=method,
            max_candidates=safe_max_candidates,
            diagnostics=diagnostics,
        )
        candidates.extend(record_candidates)
    if "files" in selected_sources:
        file_candidates = await _file_candidates(
            workspace,
            query=query,
            file_options=file_options,
            max_candidates=safe_max_candidates,
            diagnostics=diagnostics,
        )
        candidates.extend(file_candidates)

    candidates = _dedupe_candidates(candidates)[:safe_max_candidates]
    diagnostics["candidate_count"] = len(candidates)

    rerank_gate = _default_rerank_gate(
        candidates,
        sources=selected_sources,
        filters=scoped_filters,
        tags=normalized_tags,
        selection=selection,
        top_n=top_n,
        budget=normalized_budget,
        file_options=file_options,
    )
    diagnostics["rerank_gate"] = rerank_gate
    should_rerank = bool(rerank) if rerank is not None else bool(rerank_gate["enabled"])
    ranked_candidates = candidates
    dropped_by_rerank = 0
    if should_rerank and candidates:
        ranked_candidates, rerank_diagnostics, dropped_by_rerank = await _rerank_with_degrade(
            query=query,
            candidates=candidates,
            budget=normalized_budget,
            rerank_handler=rerank_handler,
            max_retries=max(0, int(max_rerank_retries)),
            plugin_manager=plugin_manager,
            settings=settings,
        )
        diagnostics["rerank"] = rerank_diagnostics
    else:
        diagnostics["rerank"] = {
            "enabled": False,
            "reason": (
                "disabled"
                if rerank is False
                else "no_candidates"
                if not candidates
                else str(rerank_gate.get("reason") or "gate_skipped")
            ),
        }

    package, budget_omitted = await _package_candidates(
        workspace,
        ranked_candidates,
        query=query,
        profile=profile,
        selection=selection,
        top_n=top_n,
        budget=normalized_budget,
        diagnostics=diagnostics,
    )
    omitted = list(package["omitted"])
    if dropped_by_rerank:
        omitted.insert(0, {"reason": "rerank_drop", "count": dropped_by_rerank})
    if budget_omitted:
        omitted.append({"reason": "budget", "count": budget_omitted})
    package["omitted"] = omitted
    package["diagnostics"] = diagnostics
    return package


def _normalize_sources(sources: Sequence[RetrievalSourceName | str] | None) -> set[str]:
    if sources is None:
        return {"records"}
    normalized: set[str] = set()
    for source in sources:
        value = str(source).strip().lower()
        if value in {"record", "records"}:
            normalized.add("records")
        elif value in {"file", "files"}:
            normalized.add("files")
    return normalized or {"records"}


def _max_candidates(max_candidates: int | None, budget: Mapping[str, Any]) -> int:
    configured = max_candidates if max_candidates is not None else budget.get("max_candidates")
    if isinstance(configured, int):
        return max(1, min(configured, 500))
    return 50


def _scoped_retrieval_filters(
    filters: Mapping[str, Any] | None,
    scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scoped = dict(filters or {})
    for key, value in dict(scope or {}).items():
        filter_key = str(key) if str(key).startswith("scope.") else f"scope.{ key }"
        scoped.setdefault(filter_key, value)
    return scoped


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        normalized = str(tag).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


async def _record_candidates(
    workspace: "Workspace",
    *,
    query: str | None,
    tags: list[str],
    filters: dict[str, Any],
    method: WorkspaceRetrievalMethod,
    max_candidates: int,
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    deterministic = await _deterministic_record_candidates(
        workspace,
        query=query,
        tags=tags,
        filters=filters,
        max_candidates=max_candidates,
    )
    diagnostics["deterministic_record_candidates"] = len(deterministic)
    if method not in {"vector", "hybrid"}:
        return deterministic

    vector_records = await _vector_record_candidates(
        workspace,
        query=query,
        tags=tags,
        filters=filters,
        limit=max_candidates,
        diagnostics=diagnostics,
    )
    if method == "vector" and vector_records:
        return vector_records + deterministic
    return vector_records + deterministic


async def _deterministic_record_candidates(
    workspace: "Workspace",
    *,
    query: str | None,
    tags: list[str],
    filters: dict[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    by_id: dict[str, WorkspaceRecordRef] = {}
    ordered: list[WorkspaceRecordRef] = []

    async def add_records(records: list[WorkspaceRecordRef], *, require_tags: bool) -> None:
        for record in records:
            if require_tags and not _record_matches_tags(record, tags):
                continue
            record_id = record["id"]
            if record_id in by_id:
                continue
            by_id[record_id] = record
            ordered.append(record)

    if query:
        await add_records(await workspace.grep(query, filters=filters), require_tags=bool(tags))
    if tags:
        await add_records(await workspace.grep(None, filters=filters), require_tags=True)
    if not ordered:
        await add_records(await workspace.grep(None, filters=filters), require_tags=False)

    return [_record_candidate(record) for record in ordered[:max_candidates]]


async def _vector_record_candidates(
    workspace: "Workspace",
    *,
    query: str | None,
    tags: list[str],
    filters: dict[str, Any],
    limit: int,
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    vector_diagnostics: dict[str, Any] = {"requested": True, "used": False}
    diagnostics["vector"] = vector_diagnostics
    vector_index = getattr(workspace.backend, "vector_index", None)
    if query is None or str(query).strip() == "":
        vector_diagnostics["reason"] = "empty_query"
        return []
    if vector_index is None or getattr(vector_index, "name", None) == "noop":
        vector_diagnostics["reason"] = "vector_index_unavailable"
        return []
    try:
        records = await vector_index.search(str(query), filters=filters, limit=limit)
    except Exception as exc:
        vector_diagnostics["reason"] = "vector_search_failed"
        vector_diagnostics["error"] = str(exc)
        return []
    vector_diagnostics["used"] = True
    vector_diagnostics["candidate_count"] = len(records)
    return [_record_candidate(record) for record in records if _record_matches_tags(record, tags)]


async def _file_candidates(
    workspace: "Workspace",
    *,
    query: str | None,
    file_options: Mapping[str, Any] | None,
    max_candidates: int,
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    if query is None or str(query).strip() == "":
        diagnostics["file_candidates"] = {"count": 0, "reason": "empty_query"}
        return []
    options = dict(file_options or {})
    results = await workspace.grep_files(
        str(query),
        path=options.get("path", "."),
        pattern=options.get("pattern", "*"),
        max_results=min(max_candidates, int(options.get("max_results", max_candidates))),
        include_hidden=bool(options.get("include_hidden", False)),
        max_file_bytes=int(options.get("max_file_bytes", 200000)),
        context_lines=int(options.get("context_lines", 0)),
        max_snippet_bytes=int(options.get("max_snippet_bytes", 1200)),
    )
    if isinstance(results, dict):
        matches = results.get("matches", [])
    else:
        matches = results
    files = [cast(WorkspaceFileSearchResult, result) for result in matches if isinstance(result, dict)]
    diagnostics["file_candidates"] = {"count": len(files)}
    return [_file_candidate(result) for result in files[:max_candidates]]


def _record_candidate(record: WorkspaceRecordRef) -> dict[str, Any]:
    return {
        "source": "record",
        "candidate_id": f"record:{ record['id'] }",
        "ref": record,
        "tags": _record_tags(record),
        "score": None,
        "reason": None,
    }


def _file_candidate(result: WorkspaceFileSearchResult) -> dict[str, Any]:
    return {
        "source": "file",
        "candidate_id": f"file:{ result.get('path') }:{ result.get('line') }",
        "file": result,
        "tags": [],
        "score": None,
        "reason": None,
    }


def _record_tags(record: WorkspaceRecordRef) -> list[str]:
    tags: list[str] = []
    for holder in (record.get("meta"), record.get("scope")):
        if not isinstance(holder, dict):
            continue
        raw = holder.get("tags")
        if isinstance(raw, str):
            tags.append(raw)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            tags.extend(str(tag) for tag in raw if str(tag).strip())
    return _normalize_tags(tags)


def _record_matches_tags(record: WorkspaceRecordRef, tags: list[str]) -> bool:
    if not tags:
        return True
    record_tags = set(_record_tags(record))
    return all(tag in record_tags for tag in tags)


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        result.append(candidate)
    return result


_PROJECTION_OMIT_KEYS = {
    "audit",
    "auth",
    "collection",
    "collection_hint",
    "export_batch",
    "format",
    "labels",
    "object_type",
    "priority_hint",
    "queue",
    "routing",
    "schema",
    "source_system",
    "source_tags",
    "tags",
}

_PLAIN_TEXT_KEYS = {
    "body",
    "content",
    "conversation",
    "description",
    "fact",
    "facts",
    "message",
    "note",
    "notes",
    "raw_lines",
    "snippet",
    "text",
}


def _default_rerank_gate(
    candidates: list[dict[str, Any]],
    *,
    sources: set[str],
    filters: Mapping[str, Any],
    tags: list[str],
    selection: WorkspaceRetrievalSelection,
    top_n: int | None,
    budget: Mapping[str, Any],
    file_options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    candidate_count = len(candidates)
    target_count = _rerank_gate_target_count(selection=selection, top_n=top_n, budget=budget)
    profile = _candidate_pool_profile(candidates)
    strong_filters = _has_strong_retrieval_filters(filters=filters, tags=tags, file_options=file_options)
    reasons: list[str] = []
    if candidate_count == 0:
        return {
            "enabled": False,
            "reason": "no_candidates",
            "candidate_count": 0,
            "target_count": target_count,
            "strong_filters": strong_filters,
            **profile,
        }
    if candidate_count <= target_count:
        return {
            "enabled": False,
            "reason": "candidate_count_within_selection",
            "candidate_count": candidate_count,
            "target_count": target_count,
            "strong_filters": strong_filters,
            **profile,
        }
    if candidate_count >= max(8, target_count * 2):
        reasons.append("large_candidate_pool")
    if profile["source_count"] > 1:
        reasons.append("mixed_sources")
    if profile["collection_count"] > 1:
        reasons.append("mixed_collections")
    if profile["kind_count"] > 3:
        reasons.append("many_record_kinds")
    if profile["path_bucket_count"] > 3:
        reasons.append("many_file_paths")
    if not strong_filters:
        reasons.append("weak_structural_filters")
    enabled = bool(reasons)
    return {
        "enabled": enabled,
        "reason": "rerank_gate_matched" if enabled else "candidate_pool_structurally_focused",
        "reasons": reasons,
        "candidate_count": candidate_count,
        "target_count": target_count,
        "strong_filters": strong_filters,
        "selected_sources": sorted(sources),
        **profile,
    }


def _rerank_gate_target_count(
    *,
    selection: WorkspaceRetrievalSelection,
    top_n: int | None,
    budget: Mapping[str, Any],
) -> int:
    for value in (top_n, budget.get("top_n")):
        if isinstance(value, int):
            return max(1, min(value, 50))
    if selection == "top_n":
        return 8
    return 5


def _candidate_pool_profile(candidates: list[dict[str, Any]]) -> dict[str, int]:
    sources: set[str] = set()
    collections: set[str] = set()
    kinds: set[str] = set()
    paths: set[str] = set()
    for candidate in candidates:
        source = str(candidate.get("source") or "")
        if source:
            sources.add(source)
        ref = candidate.get("ref")
        if isinstance(ref, Mapping):
            collection = str(ref.get("collection") or "")
            kind = str(ref.get("kind") or "")
            if collection:
                collections.add(collection)
            if kind:
                kinds.add(kind)
        file_info = candidate.get("file")
        if isinstance(file_info, Mapping):
            path = str(file_info.get("path") or "")
            if path:
                paths.add(path)
    return {
        "source_count": len(sources),
        "collection_count": len(collections),
        "kind_count": len(kinds),
        "path_bucket_count": len(paths),
    }


def _has_strong_retrieval_filters(
    *,
    filters: Mapping[str, Any],
    tags: list[str],
    file_options: Mapping[str, Any] | None,
) -> bool:
    if tags:
        return True
    strong_filter_keys = {"id", "collection", "kind", "path"}
    for key, value in filters.items():
        if value in (None, "", [], {}):
            continue
        key_text = str(key)
        if key_text in strong_filter_keys or key_text.startswith("scope.") or key_text.startswith("meta."):
            return True
    options = dict(file_options or {})
    file_path = str(options.get("path") or "").strip()
    pattern = str(options.get("pattern") or "").strip()
    return bool(file_path and file_path != ".") or bool(pattern and pattern not in {"", "*", "**"})


async def _rerank_with_degrade(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    budget: Mapping[str, Any],
    rerank_handler: RerankHandler | None,
    max_retries: int,
    plugin_manager: Any,
    settings: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    rerank_limit = _rerank_limit(candidates, budget)
    rerank_candidates = candidates[:rerank_limit]
    remaining_candidates = candidates[rerank_limit:]
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "candidate_count": len(rerank_candidates),
        "attempts": 0,
    }
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        diagnostics["attempts"] = attempt + 1
        try:
            decisions = await _run_rerank_handler(
                query=query,
                candidates=rerank_candidates,
                rerank_handler=rerank_handler,
                plugin_manager=plugin_manager,
                settings=settings,
            )
            ranked, dropped = _apply_rerank_decisions(rerank_candidates, decisions)
            diagnostics.update(
                {
                    "degraded": False,
                    "dropped": dropped,
                    "kept": len(ranked),
                }
            )
            return ranked + remaining_candidates, diagnostics, dropped
        except Exception as exc:
            last_error = str(exc)
    diagnostics.update(
        {
            "degraded": True,
            "reason": "rerank_failed",
            "error": last_error,
            "dropped": 0,
        }
    )
    return candidates, diagnostics, 0


def _rerank_limit(candidates: list[dict[str, Any]], budget: Mapping[str, Any]) -> int:
    configured = budget.get("rerank_candidates")
    if isinstance(configured, int):
        return max(1, min(len(candidates), configured))
    top_n = budget.get("top_n")
    if isinstance(top_n, int):
        return max(1, min(len(candidates), 30, max(top_n * 3, top_n + 10, 20)))
    return max(1, min(len(candidates), 20))


async def _run_rerank_handler(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    rerank_handler: RerankHandler | None,
    plugin_manager: Any,
    settings: Any,
) -> Any:
    previews = [_candidate_preview(candidate) for candidate in candidates]
    if rerank_handler is not None:
        result = rerank_handler(query=query, candidates=previews)
        if inspect.isawaitable(result):
            return await result
        return result
    unavailable = _default_model_unavailable_reason(settings)
    if unavailable is not None:
        raise RuntimeError(unavailable)
    return await _default_model_rerank(query=query, candidates=previews, plugin_manager=plugin_manager, settings=settings)


def _candidate_preview(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate.get("source") == "record":
        ref = cast(WorkspaceRecordRef, candidate["ref"])
        return {
            "id": candidate["candidate_id"],
            "source": "record",
            "collection": ref.get("collection"),
            "kind": ref.get("kind"),
            "summary": ref.get("summary") or "",
            "tags": candidate.get("tags") or [],
            "scope": ref.get("scope") or {},
        }
    result = cast(WorkspaceFileSearchResult, candidate["file"])
    return {
        "id": candidate["candidate_id"],
        "source": "file",
        "path": result.get("path"),
        "line": result.get("line"),
        "snippet": result.get("snippet") or result.get("text") or "",
    }


def _default_model_unavailable_reason(settings: Any) -> str | None:
    try:
        active = str(settings.get("plugins.ModelRequester.activate", "")) if settings is not None else ""
    except Exception:
        active = ""
    if not active:
        try:
            from agently.base import settings as global_settings

            settings = global_settings
            active = str(settings.get("plugins.ModelRequester.activate", ""))
        except Exception:
            return "No active ModelRequester is available for Workspace retrieval rerank."
    if active in {"OpenAICompatible", "OpenAIResponsesCompatible"} and settings is not None:
        prefix = f"plugins.ModelRequester.{ active }"
        base_url = str(settings.get(f"{ prefix }.base_url", ""))
        full_url = settings.get(f"{ prefix }.full_url", None)
        api_key = settings.get(f"{ prefix }.api_key", None)
        auth = settings.get(f"{ prefix }.auth", None)
        if "api.openai.com" in base_url and full_url is None and not api_key and not auth:
            return "Workspace retrieval rerank skipped because the active OpenAI-compatible model has no auth."
    return None


async def _default_model_rerank(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    plugin_manager: Any,
    settings: Any,
) -> Any:
    if plugin_manager is None or settings is None:
        from agently.base import plugin_manager as global_plugin_manager
        from agently.base import settings as global_settings

        plugin_manager = plugin_manager or global_plugin_manager
        settings = settings or global_settings
    from agently.core.model import ModelRequest

    request = ModelRequest(
        plugin_manager,
        agent_name="WorkspaceRetrieval",
        parent_settings=settings,
    )
    request.input(
        {
            "query": query or "",
            "candidates": candidates,
        }
    )
    request.instruct(
        "Judge which Workspace retrieval candidates are useful for the query. "
        "Drop only candidates that are not useful. Return structured decisions."
    )
    request.output(
        {
            "decisions": [
                {
                    "id": (str, "Candidate id exactly as provided."),
                    "useful": (bool, "True if the candidate should be kept."),
                    "score": (float, "Relevance score from 0.0 to 1.0."),
                    "reason": (str, "Short reason for the decision."),
                }
            ]
        },
        format="json",
    )
    return await request.async_get_data(max_retries=0, raise_ensure_failure=True)


def _apply_rerank_decisions(
    candidates: list[dict[str, Any]],
    decisions_payload: Any,
) -> tuple[list[dict[str, Any]], int]:
    decisions = _coerce_decisions(decisions_payload)
    if not decisions:
        raise ValueError("Rerank response did not contain decisions.")
    kept: list[dict[str, Any]] = []
    dropped = 0
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        decision = _decision_for_candidate(candidate_id, decisions)
        if decision is None:
            kept.append(candidate)
            continue
        if not bool(decision["useful"]):
            dropped += 1
            continue
        ranked = dict(candidate)
        ranked["score"] = decision.get("score")
        ranked["reason"] = decision.get("reason")
        kept.append(ranked)
    kept.sort(key=lambda item: (item.get("score") is not None, float(item.get("score") or 0.0)), reverse=True)
    return kept, dropped


def _decision_for_candidate(candidate_id: str, decisions: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    decision = decisions.get(candidate_id)
    if decision is not None:
        return decision
    if ":" in candidate_id:
        _, bare_id = candidate_id.split(":", 1)
        decision = decisions.get(bare_id)
        if decision is not None:
            return decision
    for prefix in ("record:", "file:"):
        decision = decisions.get(f"{prefix}{candidate_id}")
        if decision is not None:
            return decision
    return None


def _coerce_decisions(payload: Any) -> dict[str, dict[str, Any]]:
    raw_decisions: Any
    if isinstance(payload, Mapping):
        raw_decisions = payload.get("decisions", payload)
    else:
        raw_decisions = payload
    decisions: dict[str, dict[str, Any]] = {}
    if isinstance(raw_decisions, Mapping):
        for key, value in raw_decisions.items():
            decisions[str(key)] = _coerce_decision_value(value)
        return decisions
    if isinstance(raw_decisions, Sequence) and not isinstance(raw_decisions, (str, bytes, bytearray)):
        for item in raw_decisions:
            if not isinstance(item, Mapping):
                continue
            candidate_id = item.get("id") or item.get("candidate_id")
            if candidate_id is None:
                continue
            decisions[str(candidate_id)] = _coerce_decision_value(item)
    return decisions


def _coerce_decision_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"useful": value, "score": 1.0 if value else 0.0, "reason": None}
    if isinstance(value, Mapping):
        raw_useful = value.get("useful", value.get("keep", value.get("decision", True)))
        useful = _coerce_useful(raw_useful)
        return {
            "useful": useful,
            "score": _coerce_score(value.get("score", 1.0 if useful else 0.0)),
            "reason": value.get("reason"),
        }
    return {"useful": True, "score": None, "reason": None}


def _coerce_useful(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"drop", "dropped", "discard", "false", "no", "irrelevant", "not_useful"}:
        return False
    return True


def _coerce_score(value: Any) -> float | None:
    try:
        score = float(value)
    except Exception:
        return None
    return max(0.0, min(1.0, score))


async def _package_candidates(
    workspace: "Workspace",
    candidates: list[dict[str, Any]],
    *,
    query: str | None,
    profile: str,
    selection: WorkspaceRetrievalSelection,
    top_n: int | None,
    budget: Mapping[str, Any],
    diagnostics: dict[str, Any],
) -> tuple[WorkspaceRetrievalPackage, int]:
    char_budget = _char_budget(budget)
    item_char_budget = _item_char_budget(budget, char_budget)
    record_projection = _record_projection_enabled(budget)
    selected: list[WorkspaceRetrievalItem] = []
    omitted_budget = 0
    used_chars = 0
    top_n_limit = top_n if top_n is not None else budget.get("top_n")
    if isinstance(top_n_limit, int):
        top_n_limit = max(1, top_n_limit)
    else:
        top_n_limit = None

    for candidate in candidates:
        if selection == "top_n" and top_n_limit is not None and len(selected) >= top_n_limit:
            omitted_budget += 1
            continue
        item = await _candidate_item(workspace, candidate, item_char_budget, record_projection=record_projection)
        item_chars = int(item.get("chars") or 0)
        if selection == "length" and selected and used_chars + item_chars > char_budget:
            omitted_budget += 1
            continue
        if selection == "length" and not selected and item_chars > char_budget:
            mutable_item = cast(WorkspaceRetrievalItem, dict(item))
            content = item.get("content")
            if isinstance(content, str):
                summary_text = str(mutable_item.get("summary") or "")
                remaining = max(0, char_budget - len(summary_text))
                mutable_item["content"] = _excerpt(content, remaining)
                content_text = str(mutable_item.get("content") or "")
                mutable_item["chars"] = len(summary_text) + len(content_text)
                item_chars = int(mutable_item["chars"])
            item = mutable_item
        selected.append(item)
        used_chars += item_chars

    diagnostics["used_chars"] = used_chars
    diagnostics["char_budget"] = char_budget
    diagnostics["selected_count"] = len(selected)
    return (
        {
            "query": query,
            "profile": profile,
            "selection": selection,
            "items": selected,
            "omitted": [],
            "diagnostics": diagnostics,
        },
        omitted_budget,
    )


async def _candidate_item(
    workspace: "Workspace",
    candidate: dict[str, Any],
    item_char_budget: int,
    *,
    record_projection: bool,
) -> WorkspaceRetrievalItem:
    if candidate.get("source") == "record":
        return await _record_item(
            workspace,
            candidate,
            item_char_budget,
            record_projection=record_projection,
        )
    return _file_item(candidate, item_char_budget)


async def _record_item(
    workspace: "Workspace",
    candidate: dict[str, Any],
    item_char_budget: int,
    *,
    record_projection: bool,
) -> WorkspaceRetrievalItem:
    ref = cast(WorkspaceRecordRef, candidate["ref"])
    value = await _safe_record_value(workspace, ref)
    projection = _project_record_value(value, item_char_budget, enabled=record_projection)
    summary = ref.get("summary") or ""
    content = projection["content"]
    content_text = content or ""
    return {
        "source": "record",
        "candidate_id": str(candidate.get("candidate_id")),
        "ref": ref,
        "kind": ref.get("kind"),
        "summary": summary,
        "content": content,
        "tags": list(candidate.get("tags") or []),
        "score": candidate.get("score"),
        "reason": candidate.get("reason"),
        "use": "memory" if ref.get("collection") == "memory" else "context",
        "chars": len(summary) + len(content_text),
        "body_state": "truncated" if projection["truncated"] else "bounded",
        "content_state": projection["content_state"],
        "original_ref": _original_record_ref(ref),
        "projection": projection["metadata"],
        "raw_chars": int(projection["metadata"].get("raw_chars") or 0),
        "projected_chars": int(projection["metadata"].get("projected_chars") or len(content_text)),
        "truncated": bool(projection["truncated"]),
    }


def _file_item(candidate: dict[str, Any], item_char_budget: int) -> WorkspaceRetrievalItem:
    result = cast(WorkspaceFileSearchResult, candidate["file"])
    content = _excerpt(str(result.get("snippet") or result.get("text") or ""), item_char_budget)
    summary = f"{ result.get('path') }:{ result.get('line') }"
    truncated = bool(content and content.endswith("\n[truncated]"))
    return {
        "source": "file",
        "candidate_id": str(candidate.get("candidate_id")),
        "file": result,
        "kind": "file",
        "summary": summary,
        "content": content,
        "tags": [],
        "score": candidate.get("score"),
        "reason": candidate.get("reason"),
        "use": "context",
        "chars": len(summary) + len(content or ""),
        "body_state": "truncated" if truncated else "bounded",
        "content_state": "bounded_readback_available",
        "truncated": truncated,
    }


async def _safe_record_value(workspace: "Workspace", record: WorkspaceRecordRef) -> Any:
    try:
        return await workspace.get_data(record)
    except Exception:
        return None


def _record_projection_enabled(budget: Mapping[str, Any]) -> bool:
    raw = budget.get("record_projection", budget.get("projection", True))
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "raw", "disabled"}


def _project_record_value(value: Any, max_chars: int, *, enabled: bool) -> dict[str, Any]:
    raw_text = _record_raw_text(value)
    if not enabled or not isinstance(value, (Mapping, Sequence)) or isinstance(value, (str, bytes, bytearray)):
        excerpt = _excerpt(raw_text, max_chars)
        return _projection_result(
            content=excerpt,
            raw_chars=len(raw_text or ""),
            strategy="raw_excerpt",
            content_state="bounded_readback_available",
            truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
            omitted_keys=[],
        )
    projected_lines, omitted_keys = _record_projection_lines(value)
    projected_text = "\n".join(line for line in projected_lines if line.strip()).strip()
    if not projected_text:
        excerpt = _excerpt(raw_text, max_chars)
        return _projection_result(
            content=excerpt,
            raw_chars=len(raw_text or ""),
            strategy="raw_excerpt_empty_projection",
            content_state="bounded_readback_available",
            truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
            omitted_keys=omitted_keys,
        )
    if raw_text is not None and len(projected_text) >= len(raw_text):
        excerpt = _excerpt(raw_text, max_chars)
        return _projection_result(
            content=excerpt,
            raw_chars=len(raw_text),
            strategy="raw_excerpt_not_shorter",
            content_state="bounded_readback_available",
            truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
            omitted_keys=omitted_keys,
        )
    excerpt = _excerpt(projected_text, max_chars)
    return _projection_result(
        content=excerpt,
        raw_chars=len(raw_text or ""),
        strategy="deterministic_structured_projection",
        content_state="projected_from_raw_record",
        truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
        omitted_keys=omitted_keys,
    )


def _projection_result(
    *,
    content: str | None,
    raw_chars: int,
    strategy: str,
    content_state: str,
    truncated: bool,
    omitted_keys: list[str],
) -> dict[str, Any]:
    content_text = content or ""
    return {
        "content": content,
        "content_state": content_state,
        "truncated": truncated,
        "metadata": {
            "strategy": strategy,
            "raw_chars": raw_chars,
            "projected_chars": len(content_text),
            "truncated": truncated,
            "omitted_keys": sorted(set(omitted_keys))[:24],
            "raw_content_state": "raw_readback_available",
        },
    }


def _record_raw_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _original_record_ref(ref: WorkspaceRecordRef) -> dict[str, Any]:
    return {
        "record_id": ref.get("id"),
        "collection": ref.get("collection"),
        "kind": ref.get("kind"),
        "path": ref.get("path"),
        "sha256": ref.get("sha256"),
        "size": ref.get("size"),
        "content_state": "raw_readback_available",
    }


def _record_projection_lines(value: Any) -> tuple[list[str], list[str]]:
    omitted: list[str] = []
    lines = _projection_lines(value, omitted=omitted, path=(), depth=0)
    compact: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = " ".join(str(line).split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        compact.append(normalized)
        if len(compact) >= 80:
            omitted.append("projection.line_limit")
            break
    return compact, omitted


def _projection_lines(value: Any, *, omitted: list[str], path: tuple[str, ...], depth: int) -> list[str]:
    if depth > 6:
        omitted.append(".".join(path) or "depth")
        return []
    if isinstance(value, Mapping):
        return _mapping_projection_lines(value, omitted=omitted, path=path, depth=depth)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _sequence_projection_lines(value, omitted=omitted, path=path, depth=depth)
    line = _scalar_projection_line(path[-1] if path else "", value)
    return [line] if line else []


def _mapping_projection_lines(
    value: Mapping[str, Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    ordered_keys = sorted(value.keys(), key=_projection_key_order)
    for raw_key in ordered_keys:
        key = str(raw_key)
        normalized_key = key.strip().lower()
        child_path = (*path, key)
        child = value[raw_key]
        if normalized_key in _PROJECTION_OMIT_KEYS:
            omitted.append(".".join(child_path))
            continue
        if normalized_key == "rows" and isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
            lines.extend(_row_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
            continue
        if normalized_key == "conversation" and isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
            lines.extend(_conversation_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
            continue
        if normalized_key in {"raw_lines", "parts"}:
            lines.extend(_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
            continue
        if normalized_key in _PLAIN_TEXT_KEYS and isinstance(child, str):
            lines.extend(_plain_text_lines(child))
            continue
        lines.extend(_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
    return lines


def _sequence_projection_lines(
    value: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    if _looks_like_field_value_rows(value):
        return _field_value_projection_lines(value, omitted=omitted, path=path, depth=depth + 1)
    if _looks_like_pair_rows(value):
        return _row_projection_lines(value, omitted=omitted, path=path, depth=depth + 1)
    lines: list[str] = []
    for index, item in enumerate(value):
        if index >= 80:
            omitted.append(".".join((*path, "items_after_80")))
            break
        item_path = (*path, str(index))
        if isinstance(item, Mapping) and "text" in item:
            speaker = item.get("speaker")
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"{ speaker }: { text }" if speaker else text)
            continue
        lines.extend(_projection_lines(item, omitted=omitted, path=item_path, depth=depth + 1))
    return lines


def _field_value_projection_lines(
    rows: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("field") or item.get("key") or item.get("name") or "").strip()
        value = item.get("value")
        if key.lower() in _PROJECTION_OMIT_KEYS:
            omitted.append(".".join((*path, str(index), key)))
            continue
        if key.lower() in _PLAIN_TEXT_KEYS and isinstance(value, str):
            lines.extend(_plain_text_lines(value))
            continue
        line = _scalar_projection_line(key, value)
        if line:
            lines.append(line)
    return lines


def _row_projection_lines(
    rows: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows):
        if index >= 80:
            omitted.append(".".join((*path, "rows_after_80")))
            break
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)) and len(row) >= 2:
            key = str(row[0]).strip()
            value = row[1]
            if key.lower() in _PROJECTION_OMIT_KEYS:
                omitted.append(".".join((*path, str(index), key)))
                continue
            if key.lower() in _PLAIN_TEXT_KEYS and isinstance(value, str):
                lines.extend(_plain_text_lines(value))
                continue
            line = _scalar_projection_line(key, value)
            if line:
                lines.append(line)
            continue
        lines.extend(_projection_lines(row, omitted=omitted, path=(*path, str(index)), depth=depth + 1))
    return lines


def _conversation_projection_lines(
    rows: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows):
        if index >= 80:
            omitted.append(".".join((*path, "conversation_after_80")))
            break
        if isinstance(row, Mapping):
            speaker = row.get("speaker") or row.get("role")
            text = row.get("text") or row.get("content") or row.get("message")
            if text not in (None, "", [], {}):
                line = str(text).strip()
                lines.append(f"{ speaker }: { line }" if speaker else line)
            continue
        lines.extend(_projection_lines(row, omitted=omitted, path=(*path, str(index)), depth=depth + 1))
    return lines


def _looks_like_field_value_rows(value: Sequence[Any]) -> bool:
    return bool(value) and all(
        isinstance(item, Mapping) and ("field" in item or "key" in item or "name" in item) and "value" in item
        for item in value[:8]
    )


def _looks_like_pair_rows(value: Sequence[Any]) -> bool:
    return bool(value) and all(
        isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and len(item) >= 2
        for item in value[:8]
    )


def _projection_key_order(key: Any) -> tuple[int, str]:
    text = str(key).strip().lower()
    preferred = {
        "subject": 0,
        "title": 0,
        "headline": 0,
        "summary": 0,
        "body": 1,
        "content": 1,
        "text": 1,
        "message": 1,
        "raw_lines": 1,
        "conversation": 1,
        "parts": 1,
        "rows": 1,
    }
    return (preferred.get(text, 5), text)


def _scalar_projection_line(key: str, value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = " ".join(text.split())
    if not text:
        return None
    key = str(key or "").strip()
    if not key or key.lower() in _PLAIN_TEXT_KEYS:
        return text
    return f"{ key }: { text }"


def _plain_text_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in str(text).splitlines() if line.strip()]


def _char_budget(budget: Mapping[str, Any]) -> int:
    chars = budget.get("chars")
    if isinstance(chars, int):
        return max(1, chars)
    tokens = budget.get("tokens")
    if isinstance(tokens, int):
        return max(1, tokens * 4)
    return 12000


def _item_char_budget(budget: Mapping[str, Any], char_budget: int) -> int:
    item_chars = budget.get("item_chars")
    if isinstance(item_chars, int):
        return max(1, min(char_budget, item_chars))
    return max(1, min(char_budget, 2400))


def _excerpt(content: str | None, max_chars: int) -> str | None:
    if content is None:
        return None
    if max_chars <= 0:
        return ""
    if len(content) <= max_chars:
        return content
    return content[: max(0, max_chars - 15)].rstrip() + "\n[truncated]"
