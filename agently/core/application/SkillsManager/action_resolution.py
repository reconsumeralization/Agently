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
from collections.abc import Mapping, Sequence
from inspect import isawaitable
from typing import Any, cast

from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list


_ACTION_ID_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


class LocalSkillActionResolver:
    """Resolve Skill capability needs to already-mounted local Action ids.

    This resolver never executes or registers Actions. It produces candidate
    records that the caller may pass to policy and then to ActionRuntime by
    exact ``action_id``.
    """

    _PREFERRED_IDS: Mapping[str, tuple[str, ...]] = {
        "web_search": ("search", "search_news", "search_wikipedia", "search_arxiv"),
        "web_browse": ("browse",),
        "workspace_read": ("read_file", "list_files", "search_files", "grep_files", "glob_files"),
        "workspace_write": ("write_file", "edit_file", "apply_patch", "export_file"),
        "python": ("run_python", "python_sandbox"),
        "shell": ("run_bash", "bash_sandbox"),
        "script_run": ("run_bash", "bash_sandbox"),
        "http_request": ("http_request",),
        "mcp": ("mcp",),
    }

    _ID_TOKENS: Mapping[str, tuple[str, ...]] = {
        "web_search": ("search",),
        "web_browse": ("browse", "browser"),
        "workspace_read": ("read", "list", "grep", "glob", "search", "file", "files"),
        "workspace_write": ("write", "edit", "patch", "export", "file", "files"),
        "python": ("python", "py"),
        "shell": ("bash", "shell", "sh", "cmd"),
        "script_run": ("script", "bash", "shell", "runner", "run"),
        "http_request": ("http", "request", "fetch"),
        "mcp": ("mcp",),
    }

    def __init__(self, *, min_confidence: float = 0.75):
        self.min_confidence = max(0.0, min(float(min_confidence), 1.0))

    async def async_resolve(
        self,
        *,
        agent: Any,
        context: Any,
        need: Mapping[str, Any],
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        need_name = str(need.get("need") or "").strip()
        candidates = self._collect_action_candidates(agent)
        structural = self._structural_matches(need_name, candidates)
        selected = self._select_structural(need_name, structural)
        if selected.get("status") == "selected":
            return self._resolution(need, selected_action=selected["candidate"], alternatives=structural, matched_by=selected["matched_by"], confidence=selected["confidence"])
        if selected.get("status") == "ambiguous":
            return self._resolution(need, status="ambiguous", alternatives=structural, diagnostics=[{"code": "local_action_resolution.ambiguous_structural"}])

        if bool(_ensure_dict(policy).get("model_assisted", False)):
            model_resolution = await self._model_resolve(
                context=context,
                need=need,
                candidates=candidates,
                policy=_ensure_dict(policy),
            )
            if model_resolution.get("status") in {"selected", "ambiguous", "low_confidence"}:
                return model_resolution

        return self._resolution(need, status="no_match", alternatives=structural)

    def _collect_action_candidates(self, agent: Any) -> list[dict[str, Any]]:
        action = getattr(agent, "action", None)
        get_action_list = getattr(action, "get_action_list", None)
        if not callable(get_action_list):
            return []
        agent_name = str(getattr(agent, "name", "agent"))
        try:
            action_items = cast(list[dict[str, Any]], get_action_list(tags=[f"agent-{ agent_name }"]))
        except Exception:
            return []
        candidates: list[dict[str, Any]] = []
        for item in action_items:
            if not isinstance(item, Mapping):
                continue
            action_id = str(item.get("action_id") or item.get("name") or "").strip()
            if not action_id:
                continue
            candidates.append(
                {
                    "action_id": action_id,
                    "name": str(item.get("name") or action_id),
                    "desc": str(item.get("desc") or item.get("description") or ""),
                    "kwargs": _copy_public(_ensure_dict(item.get("kwargs"))),
                    "meta": _copy_public(_ensure_dict(item.get("meta"))),
                    "tags": [str(tag) for tag in _ensure_list(item.get("tags"))],
                    "execution_resources": _copy_public(_ensure_list(item.get("execution_resources"))),
                }
            )
        return candidates

    def _structural_matches(self, need_name: str, candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        preferred = self._PREFERRED_IDS.get(need_name, ())
        need_tokens = set(self._ID_TOKENS.get(need_name, ()))
        for candidate in candidates:
            action_id = str(candidate.get("action_id") or "")
            meta = _ensure_dict(candidate.get("meta"))
            declared_caps = {
                str(value).strip()
                for key in ("capability", "capabilities", "capability_tags", "skill_capabilities")
                for value in _ensure_list(meta.get(key))
                if str(value).strip()
            }
            tags = {str(item).strip() for item in _ensure_list(candidate.get("tags")) if str(item).strip()}
            action_tokens = self._action_id_tokens(action_id)
            if action_id in preferred:
                matches.append(self._candidate_match(candidate, confidence=1.0, matched_by="exact_action_id"))
            elif need_name in declared_caps or need_name in tags:
                matches.append(self._candidate_match(candidate, confidence=0.95, matched_by="declared_capability"))
            elif self._id_tokens_match(need_name, need_tokens, action_tokens):
                matches.append(self._candidate_match(candidate, confidence=0.78, matched_by="action_id_tokens"))
        deduped: dict[str, dict[str, Any]] = {}
        for item in matches:
            action_id = str(item.get("action_id") or "")
            previous = deduped.get(action_id)
            if previous is None or float(item.get("confidence") or 0) > float(previous.get("confidence") or 0):
                deduped[action_id] = item
        return sorted(deduped.values(), key=lambda item: float(item.get("confidence") or 0), reverse=True)

    def _select_structural(self, need_name: str, matches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not matches:
            return {"status": "no_match"}
        preferred = self._PREFERRED_IDS.get(need_name, ())
        for preferred_id in preferred:
            preferred_matches = [item for item in matches if item.get("action_id") == preferred_id]
            if preferred_matches:
                item = preferred_matches[0]
                return {
                    "status": "selected",
                    "candidate": dict(item),
                    "matched_by": str(item.get("matched_by") or "exact_action_id"),
                    "confidence": float(item.get("confidence") or 1.0),
                }
        best = dict(matches[0])
        best_confidence = float(best.get("confidence") or 0.0)
        if best_confidence < self.min_confidence:
            return {"status": "low_confidence", "candidate": best, "confidence": best_confidence}
        tied = [
            dict(item)
            for item in matches
            if abs(float(item.get("confidence") or 0.0) - best_confidence) < 0.02
        ]
        if len(tied) > 1:
            return {"status": "ambiguous", "candidates": tied}
        return {
            "status": "selected",
            "candidate": best,
            "matched_by": str(best.get("matched_by") or "action_id_tokens"),
            "confidence": best_confidence,
        }

    async def _model_resolve(
        self,
        *,
        context: Any,
        need: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        request_model = getattr(context, "async_request_model", None)
        if not callable(request_model) or not candidates:
            return self._resolution(need, status="no_match", diagnostics=[{"code": "local_action_resolution.model_context_unavailable"}])
        max_candidates = int(policy.get("max_model_candidates", 24) or 24)
        visible_candidates = [
            {
                "action_id": item.get("action_id"),
                "name": item.get("name"),
                "desc": item.get("desc"),
                "kwargs": item.get("kwargs"),
                "meta": item.get("meta"),
                "tags": item.get("tags"),
            }
            for item in candidates[:max(1, max_candidates)]
        ]
        try:
            request_kwargs: dict[str, Any] = {
                "prompt": {
                    "task": "Resolve a selected Skill capability need to one already-mounted local Action.",
                    "need": _copy_public(dict(need)),
                    "action_candidates": visible_candidates,
                    "policy": [
                        "Return one exact action_id only when it clearly satisfies the capability need.",
                        "Return ambiguous when multiple candidates are equally suitable.",
                        "Return no_match when no candidate is suitable.",
                        "Do not invent action ids. Do not execute actions.",
                    ],
                },
                "output_schema": {
                    "status": (str, "selected, ambiguous, no_match, or low_confidence.", True),
                    "selected_action_id": (str, "Exact selected action_id when status is selected.", False),
                    "confidence": (float, "0 to 1 confidence.", False),
                    "matched_by": (str, "Reason category for the match.", False),
                    "alternative_action_ids": ([str], "Alternative exact action_ids considered.", False),
                    "reason": (str, "Short diagnostic reason.", False),
                },
                "output_format": "json",
                "ensure_keys": ["status"],
                "max_retries": 2,
            }
            model_key = str(policy.get("model_key") or "").strip()
            if model_key:
                request_kwargs["model_key"] = model_key
            raw_result = request_model(**request_kwargs)
            if isawaitable(raw_result):
                result = await raw_result
            else:
                result = raw_result
        except Exception as error:
            return self._resolution(need, status="no_match", diagnostics=[{"code": "local_action_resolution.model_failed", "message": str(error)}])
        data = _ensure_dict(result)
        status = str(data.get("status") or "no_match").strip()
        selected_action_id = str(data.get("selected_action_id") or "").strip()
        by_id = {str(item.get("action_id") or ""): dict(item) for item in candidates}
        alternatives = [
            self._candidate_match(by_id[action_id], confidence=0.5, matched_by="model_alternative")
            for action_id in _ensure_list(data.get("alternative_action_ids"))
            if str(action_id) in by_id
        ]
        if status == "selected" and selected_action_id in by_id:
            confidence = max(0.0, min(float(data.get("confidence") or 0.5), 1.0))
            if confidence < self.min_confidence:
                return self._resolution(need, status="low_confidence", alternatives=alternatives, diagnostics=[{"code": "local_action_resolution.model_low_confidence", "confidence": confidence}])
            return self._resolution(
                need,
                selected_action=self._candidate_match(
                    by_id[selected_action_id],
                    confidence=confidence,
                    matched_by=str(data.get("matched_by") or "model_semantic"),
                ),
                alternatives=alternatives,
                matched_by=str(data.get("matched_by") or "model_semantic"),
                confidence=confidence,
                diagnostics=[{"code": "local_action_resolution.model_selected", "reason": str(data.get("reason") or "")}],
            )
        if status == "ambiguous":
            return self._resolution(need, status="ambiguous", alternatives=alternatives, diagnostics=[{"code": "local_action_resolution.model_ambiguous", "reason": str(data.get("reason") or "")}])
        return self._resolution(need, status="no_match", alternatives=alternatives, diagnostics=[{"code": "local_action_resolution.model_no_match", "reason": str(data.get("reason") or "")}])

    @staticmethod
    def _action_id_tokens(action_id: str) -> set[str]:
        return {part.lower() for part in _ACTION_ID_SPLIT_RE.split(action_id) if part}

    @staticmethod
    def _id_tokens_match(need_name: str, need_tokens: set[str], action_tokens: set[str]) -> bool:
        if not need_tokens or not action_tokens:
            return False
        if need_name == "workspace_read":
            return bool(action_tokens.intersection({"read", "list", "grep", "glob", "search"}))
        if need_name == "workspace_write":
            return bool(action_tokens.intersection({"write", "edit", "patch", "export"}))
        if need_name == "script_run":
            return bool(action_tokens.intersection({"script"})) or (
                "run" in action_tokens and bool(action_tokens.intersection({"bash", "shell", "runner"}))
            )
        return bool(need_tokens.intersection(action_tokens))

    @staticmethod
    def _candidate_match(candidate: Mapping[str, Any], *, confidence: float, matched_by: str) -> dict[str, Any]:
        return {
            "action_id": str(candidate.get("action_id") or ""),
            "name": str(candidate.get("name") or candidate.get("action_id") or ""),
            "matched_by": matched_by,
            "confidence": float(confidence),
            "execution_resource_requirements": _copy_public(_ensure_list(candidate.get("execution_resources"))),
        }

    @staticmethod
    def _resolution(
        need: Mapping[str, Any],
        *,
        status: str = "selected",
        selected_action: Mapping[str, Any] | None = None,
        alternatives: Sequence[Mapping[str, Any]] | None = None,
        matched_by: str = "",
        confidence: float = 0.0,
        diagnostics: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        selected_action_id = str((selected_action or {}).get("action_id") or "")
        return {
            "skill_id": str(need.get("skill_id") or ""),
            "need": str(need.get("need") or ""),
            "status": status,
            "selected_action_id": selected_action_id,
            "action_ids": [selected_action_id] if selected_action_id else [],
            "matched_by": matched_by or str((selected_action or {}).get("matched_by") or ""),
            "confidence": float(confidence or (selected_action or {}).get("confidence") or 0.0),
            "alternatives": [dict(item) for item in alternatives or ()],
            "execution_resource_requirements": _copy_public(
                _ensure_list((selected_action or {}).get("execution_resource_requirements"))
            ),
            "diagnostics": [dict(item) for item in diagnostics or ()],
        }
