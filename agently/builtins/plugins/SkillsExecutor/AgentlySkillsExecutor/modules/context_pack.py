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
from pathlib import Path
from typing import Any, Literal, cast

from agently.types.data import (
    SkillContextPack,
    SkillContextPackIncludeMode,
    SkillContextPackResource,
    SkillContextPackSkill,
    SkillContract,
)
from agently.types.plugins import SkillsPlanningContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list, _ensure_string_list, _sanitize_id

from .planner import _matches_selector, _matches_skills_pack_selector
from .registry import SkillRegistry


# ASCII identifier runs OR single CJK/Kana/Hangul ideographs. Scripts without
# word spacing are tokenized per ideograph so non-Latin task text still produces
# overlap terms instead of degrading to an empty set.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.#/-]+|[一-鿿぀-ヿ가-힯]")
_CODE_HINTS = {
    "api",
    "code",
    "coding",
    "config",
    "configuration",
    "example",
    "function",
    "generate",
    "implementation",
    "import",
    "migration",
    "provider",
    "python",
    "script",
    "setup",
    "test",
}


class SkillContextPackBuilder:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def async_build(
        self,
        *,
        context: SkillsPlanningContext | None = None,
        task: str | None = None,
        intent: str | None = None,
        skill_ids: list[str] | tuple[str, ...] | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        include_guidance: bool = True,
        include_examples: SkillContextPackIncludeMode = "auto",
        include_references: SkillContextPackIncludeMode = "auto",
        include_assets: SkillContextPackIncludeMode = False,
        include_public_lookup: bool = False,
        actionize_scripts: bool = False,
        budget_chars: int = 12000,
        max_resource_chars: int = 6000,
    ) -> SkillContextPack:
        task_text = str(task or "")
        resolved_intent = str(intent or "auto")
        budget = max(0, int(budget_chars or 0))
        max_resource_budget = max(1, int(max_resource_chars or 1))
        diagnostics: list[dict[str, Any]] = []
        citations: list[str] = []
        selected_skills: list[SkillContextPackSkill] = []
        used_chars = 0

        contracts = self._resolve_contracts(
            skill_ids=skill_ids,
            skills=skills,
            skills_packs=skills_packs,
            diagnostics=diagnostics,
        )
        code_intent = self._is_code_intent(task_text, resolved_intent)
        task_terms = self._task_terms(task_text, resolved_intent)

        for contract in contracts:
            remaining = max(0, budget - used_chars)
            skill_item, consumed, skill_citations = self._build_skill_item(
                contract,
                task_terms=task_terms,
                task_text=task_text,
                intent=resolved_intent,
                code_intent=code_intent,
                include_guidance=include_guidance,
                include_examples=include_examples,
                include_references=include_references,
                include_assets=include_assets,
                remaining_budget=remaining,
                max_resource_chars=max_resource_budget,
            )
            used_chars += consumed
            citations.extend(skill_citations)
            if actionize_scripts:
                candidates, action_diagnostics = await self._actionize_scripts(
                    context=context,
                    contract=contract,
                )
                skill_item["action_candidates"] = candidates
                diagnostics.extend(action_diagnostics)
            selected_skills.append(skill_item)

        public_sources: list[dict[str, Any]] = []
        if include_public_lookup:
            public_sources, public_diagnostics = await self._public_lookup(
                context=context,
                task=task_text,
            )
            diagnostics.extend(public_diagnostics)
            for source in public_sources:
                url = str(source.get("url") or "")
                if url:
                    citations.append(url)

        return SkillContextPack({
            "schema_version": "agently.skills.context_pack.v1",
            "task": task_text,
            "intent": resolved_intent,
            "budget_chars": budget,
            "used_chars": used_chars,
            "truncated": used_chars >= budget if budget > 0 else bool(selected_skills),
            "skills": selected_skills,
            "public_sources": public_sources,
            "citations": self._dedupe_strings(citations),
            "diagnostics": diagnostics,
        })

    def task_dag_resolver(
        self,
        *,
        context: SkillsPlanningContext | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async def build_context_pack(task_context: Any):
            task = getattr(task_context, "task", None)
            inputs = _ensure_dict(getattr(task, "inputs", {}))
            graph_input = getattr(task_context, "graph_input", None)
            task_value = inputs.get("task", inputs.get("target", graph_input))
            merged = {**_ensure_dict(defaults), **inputs}
            return await self.async_build(
                context=context,
                task=str(task_value or ""),
                intent=merged.get("intent"),
                skill_ids=_ensure_string_list(merged.get("skill_ids")),
                skills=merged.get("skills"),
                skills_packs=merged.get("skills_packs"),
                include_guidance=bool(merged.get("include_guidance", True)),
                include_examples=cast(Any, merged.get("include_examples", "auto")),
                include_references=cast(Any, merged.get("include_references", "auto")),
                include_assets=cast(Any, merged.get("include_assets", False)),
                include_public_lookup=bool(merged.get("include_public_lookup", False)),
                actionize_scripts=bool(merged.get("actionize_scripts", False)),
                budget_chars=int(merged.get("budget_chars", 12000) or 12000),
                max_resource_chars=int(merged.get("max_resource_chars", 6000) or 6000),
            )

        return {"skill": build_context_pack}

    def _resolve_contracts(
        self,
        *,
        skill_ids: list[str] | tuple[str, ...] | None,
        skills: Any,
        skills_packs: Any,
        diagnostics: list[dict[str, Any]],
    ) -> list[SkillContract]:
        requested_ids = [str(item).strip() for item in (skill_ids or ()) if str(item).strip()]
        contracts: list[SkillContract] = []
        seen: set[str] = set()

        def add_contract(contract: SkillContract):
            skill_id = str(contract.get("skill_id") or "")
            if not skill_id or skill_id in seen:
                return
            seen.add(skill_id)
            contracts.append(contract)

        for skill_id in requested_ids:
            try:
                add_contract(self.registry.inspect_skills(skill_id))
            except Exception as error:
                diagnostics.append({
                    "level": "warning",
                    "code": "skill_context_pack.skill_not_found",
                    "skill_id": skill_id,
                    "message": str(error),
                })

        selectors = _ensure_list(skills)
        pack_selectors = _ensure_list(skills_packs)
        if selectors or pack_selectors:
            for record in self.registry.list_skills():
                skill_id = str(record.get("skill_id") or "")
                try:
                    contract = self.registry.inspect_skills(skill_id)
                except Exception as error:
                    diagnostics.append({
                        "level": "warning",
                        "code": "skill_context_pack.skill_unreadable",
                        "skill_id": skill_id,
                        "message": str(error),
                    })
                    continue
                if any(_matches_selector(contract, selector) for selector in selectors) or any(
                    _matches_skills_pack_selector(contract, selector) for selector in pack_selectors
                ):
                    add_contract(contract)

        if not requested_ids and not selectors and not pack_selectors:
            for record in self.registry.list_skills():
                skill_id = str(record.get("skill_id") or "")
                try:
                    add_contract(self.registry.inspect_skills(skill_id))
                except Exception as error:
                    diagnostics.append({
                        "level": "warning",
                        "code": "skill_context_pack.skill_unreadable",
                        "skill_id": skill_id,
                        "message": str(error),
                    })
        return contracts

    def _build_skill_item(
        self,
        contract: SkillContract,
        *,
        task_terms: set[str],
        task_text: str,
        intent: str,
        code_intent: bool,
        include_guidance: bool,
        include_examples: SkillContextPackIncludeMode,
        include_references: SkillContextPackIncludeMode,
        include_assets: SkillContextPackIncludeMode,
        remaining_budget: int,
        max_resource_chars: int,
    ) -> tuple[SkillContextPackSkill, int, list[str]]:
        skill_id = str(contract.get("skill_id") or "")
        citations: list[str] = []
        used_chars = 0
        skill_item = SkillContextPackSkill({
            "skill_id": skill_id,
            "display_name": str(_ensure_dict(contract.get("card")).get("display_name") or skill_id),
            "source": _copy_public(_ensure_dict(contract.get("source"))),
            "selected_resources": [],
            "resource_index": self._compact_resource_index(contract),
            "action_candidates": [],
        })

        if include_guidance and remaining_budget > 0:
            guidance = _ensure_dict(contract.get("guidance"))
            content = str(guidance.get("content") or "")
            guidance_budget = min(remaining_budget, max(800, remaining_budget // 4))
            excerpt, truncated = self._take_budget(content, guidance_budget)
            if excerpt:
                skill_item["guidance"] = {
                    "path": str(guidance.get("path") or "SKILL.md"),
                    "excerpt": excerpt,
                    "truncated": truncated,
                    "citation": f"skills/{ skill_id }/SKILL.md",
                }
                used_chars += len(excerpt)
                remaining_budget -= len(excerpt)
                citations.append(f"skills/{ skill_id }/SKILL.md")

        resources = self._select_resources(
            contract,
            task_terms=task_terms,
            task_text=task_text,
            intent=intent,
            code_intent=code_intent,
            include_examples=include_examples,
            include_references=include_references,
            include_assets=include_assets,
        )
        selected_resources: list[SkillContextPackResource] = []
        for resource in resources:
            if remaining_budget <= 0:
                break
            path = str(resource.get("path") or "")
            if not path:
                continue
            read_budget = min(remaining_budget, max_resource_chars)
            try:
                content = self.registry.read_resource(skill_id, path, max_bytes=read_budget)
            except Exception as error:
                selected_resources.append(SkillContextPackResource({
                    "skill_id": skill_id,
                    "path": path,
                    "kind": str(resource.get("kind") or ""),
                    "summary": str(resource.get("summary") or ""),
                    "reason": "resource_read_failed",
                    "score": float(resource.get("_score", 0.0)),
                    "truncated": False,
                    "citation": f"skills/{ skill_id }/{ path }",
                    "content": "",
                }))
                continue
            excerpt, truncated = self._take_budget(content, remaining_budget)
            if not excerpt:
                break
            item = SkillContextPackResource({
                "skill_id": skill_id,
                "path": path,
                "kind": str(resource.get("kind") or ""),
                "content": excerpt,
                "summary": str(resource.get("summary") or ""),
                "reason": str(resource.get("_reason") or "task_relevance"),
                "sha256": str(resource.get("sha256") or ""),
                "size": int(resource.get("size") or 0),
                "score": float(resource.get("_score", 0.0)),
                "truncated": truncated or len(excerpt) < len(content),
                "citation": f"skills/{ skill_id }/{ path }",
            })
            selected_resources.append(item)
            citations.append(str(item.get("citation") or ""))
            used_chars += len(excerpt)
            remaining_budget -= len(excerpt)
        skill_item["selected_resources"] = selected_resources
        return skill_item, used_chars, citations

    def _select_resources(
        self,
        contract: SkillContract,
        *,
        task_terms: set[str],
        task_text: str,
        intent: str,
        code_intent: bool,
        include_examples: SkillContextPackIncludeMode,
        include_references: SkillContextPackIncludeMode,
        include_assets: SkillContextPackIncludeMode,
    ) -> list[dict[str, Any]]:
        resources = [
            dict(item)
            for item in _ensure_list(_ensure_dict(contract.get("resource_index")).get("resources"))
            if isinstance(item, dict)
        ]
        selected: list[dict[str, Any]] = []
        for resource in resources:
            kind = str(resource.get("kind") or "")
            if kind == "example" and not self._include_mode(include_examples, default=code_intent):
                continue
            if kind == "reference" and not self._include_mode(include_references, default=True):
                continue
            if kind == "asset" and not self._include_mode(include_assets, default=False):
                continue
            if kind == "script":
                continue
            score = self._score_resource(resource, task_terms=task_terms, code_intent=code_intent)
            if score <= 0 and kind == "reference" and include_references == "auto":
                continue
            item = dict(resource)
            item["_score"] = score
            item["_reason"] = self._resource_reason(resource, task_text=task_text, intent=intent, code_intent=code_intent)
            selected.append(item)
        selected.sort(key=lambda item: (-float(item.get("_score", 0.0)), str(item.get("path") or "")))
        return selected

    def _score_resource(self, resource: dict[str, Any], *, task_terms: set[str], code_intent: bool) -> float:
        path = str(resource.get("path") or "").lower()
        summary = str(resource.get("summary") or "").lower()
        kind = str(resource.get("kind") or "")
        resource_terms = set(_TOKEN_RE.findall(f"{ path } { summary }".lower()))
        overlap = len(task_terms.intersection(resource_terms))
        score = float(overlap)
        if kind == "example" and code_intent:
            score += 8.0
        elif kind == "example":
            score += 2.0
        elif kind == "reference":
            score += 3.0
        elif kind == "asset":
            score += 1.0
        if any(token in path for token in ("minimal", "quickstart", "setup", "example")):
            score += 2.0
        return score

    def _resource_reason(self, resource: dict[str, Any], *, task_text: str, intent: str, code_intent: bool) -> str:
        kind = str(resource.get("kind") or "")
        if kind == "example" and code_intent:
            return "code_generation_example"
        if kind == "reference":
            return "task_relevant_reference"
        if kind == "asset":
            return "task_relevant_asset"
        return f"matched_{ intent or 'task' }"

    async def _actionize_scripts(
        self,
        *,
        context: SkillsPlanningContext | None,
        contract: SkillContract,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        skill_id = str(contract.get("skill_id") or "")
        script_resources = [
            dict(item)
            for item in _ensure_list(_ensure_dict(contract.get("resource_index")).get("resources"))
            if isinstance(item, dict) and str(item.get("kind") or "") == "script"
        ]
        if not script_resources:
            return [], []
        diagnostics: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        if context is None or not hasattr(context, "agent"):
            return [], [{
                "level": "error",
                "code": "skill_context_pack.script_actionization_context_missing",
                "skill_id": skill_id,
            }]
        policy = _ensure_dict(context.get_setting("skills.capability_policy", {}))
        mode = self._policy_mode(policy, "script_run")
        if mode == "approval":
            decision = await self._approval_decision(context, contract, "script_run")
            if decision.get("approved") is True:
                mode = "allow"
                diagnostics.append({
                    "level": "info",
                    "code": "skill_context_pack.script_actionization_approved",
                    "skill_id": skill_id,
                    "approval": _copy_public(decision),
                })
            else:
                return [], [{
                    "level": "error",
                    "code": "skill_context_pack.script_actionization_approval_required",
                    "skill_id": skill_id,
                    "approval": _copy_public(decision),
                }]
        if mode != "allow":
            return [], [{
                "level": "error",
                "code": "skill_context_pack.script_actionization_disabled",
                "skill_id": skill_id,
                "policy": mode,
            }]
        agent = getattr(context, "agent")
        action_id = f"run_{ _sanitize_id(skill_id).replace('-', '_').replace('.', '_') }_script"
        root = Path(str(_ensure_dict(contract.get("source")).get("installed_path") or ".")).expanduser().resolve()
        commands = self._script_commands(script_resources)
        agent.enable_shell(root=root, commands=commands or None, action_id=action_id)
        for resource in script_resources:
            path = str(resource.get("path") or "")
            candidates.append({
                "action_id": action_id,
                "skill_id": skill_id,
                "source_path": path,
                "kind": "script",
                "status": "available",
                "policy": "allow",
                "citation": f"skills/{ skill_id }/{ path }",
            })
        return candidates, diagnostics

    async def _public_lookup(
        self,
        *,
        context: SkillsPlanningContext | None,
        task: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if context is None or not hasattr(context, "async_call_action"):
            return [], [{
                "level": "error",
                "code": "skill_context_pack.public_lookup_context_missing",
            }]
        policy = _ensure_dict(context.get_setting("skills.capability_policy", {}))
        mode = self._policy_mode(policy, "web_search")
        if mode != "allow":
            return [], [{
                "level": "error",
                "code": "skill_context_pack.public_lookup_disabled",
                "policy": mode,
            }]
        agent = getattr(context, "agent", None)
        if agent is not None and not agent.action.action_registry.has("search"):
            from agently.builtins.actions import Search

            agent.use_actions(Search())
        try:
            raw = await getattr(context, "async_call_action")("search", query=task, max_results=3)
        except Exception as error:
            return [], [{
                "level": "warning",
                "code": "skill_context_pack.public_lookup_failed",
                "message": str(error),
            }]
        return self._normalize_public_sources(raw), []

    def _normalize_public_sources(self, raw: Any) -> list[dict[str, Any]]:
        payload = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        items = _ensure_list(payload)
        sources: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("href") or item.get("link") or "")
            title = str(item.get("title") or item.get("name") or url)
            excerpt = str(item.get("body") or item.get("snippet") or item.get("content") or item.get("summary") or "")
            if not url and not excerpt:
                continue
            sources.append({
                "url": url,
                "title": title,
                "excerpt": excerpt[:1000],
                "retrieved_by": "search",
            })
        return sources

    async def _approval_decision(
        self,
        context: SkillsPlanningContext,
        contract: SkillContract,
        capability: str,
    ) -> dict[str, Any]:
        from agently.base import policy_approval

        return _copy_public(await policy_approval.async_resolve(
            {
                "source": "skills_context_pack",
                "capability": capability,
                "subject": str(contract.get("skill_id") or capability),
                "risk": "capability_mount",
                "payload": {"skill_id": str(contract.get("skill_id") or "")},
                "policy": _copy_public(_ensure_dict(context.get_setting("skills.capability_policy", {}))),
                "lineage": {"skill_id": str(contract.get("skill_id") or "")},
            },
            handler=str(context.get_setting("policy_approval.handler", "") or "") or None,
        ))

    def _policy_mode(self, policy: dict[str, Any], need_name: str) -> str:
        auto_load = _ensure_dict(policy.get("auto_load"))
        raw = auto_load.get(need_name, policy.get(need_name, "off"))
        if isinstance(raw, dict):
            raw = raw.get("mode", raw.get("policy", "off"))
        mode = str(raw or "off").strip().lower()
        if mode in {"allow", "allowed", "true", "yes", "auto"}:
            return "allow"
        if mode in {"approval", "approve", "ask"}:
            return "approval"
        return "off"

    def _script_commands(self, resources: list[dict[str, Any]]) -> list[str]:
        commands = ["bash", "sh", "python", "python3", "node", "npx", "npm"]
        for resource in resources:
            path = str(resource.get("path") or "")
            if path:
                commands.append(path)
                commands.append(Path(path).name)
        return self._dedupe_strings(commands)

    def _compact_resource_index(self, contract: SkillContract, *, limit: int = 24) -> dict[str, Any]:
        resources = []
        for item in _ensure_list(_ensure_dict(contract.get("resource_index")).get("resources"))[:limit]:
            if not isinstance(item, dict):
                continue
            resources.append({
                "path": item.get("path"),
                "kind": item.get("kind"),
                "size": item.get("size"),
                "summary": str(item.get("summary") or "")[:240],
            })
        return {
            "schema_version": "agently.skills.resources.v1",
            "resource_count": len(_ensure_list(_ensure_dict(contract.get("resource_index")).get("resources"))),
            "resources": resources,
        }

    def _include_mode(self, mode: SkillContextPackIncludeMode, *, default: bool) -> bool:
        if mode == "auto":
            return default
        return bool(mode)

    def _is_code_intent(self, task_text: str, intent: str) -> bool:
        if intent in {"generate_code", "execute"}:
            return True
        terms = self._task_terms(task_text, intent)
        return bool(terms.intersection(_CODE_HINTS))

    def _task_terms(self, task_text: str, intent: str) -> set[str]:
        terms: set[str] = set()
        for token in _TOKEN_RE.findall(f"{ task_text } { intent }"):
            stripped = token.strip()
            # Keep ASCII terms of length >= 2 and any single non-ASCII ideograph.
            if len(stripped) >= 2 or (stripped and not stripped.isascii()):
                terms.add(stripped.lower())
        return terms

    def _take_budget(self, content: str, budget: int) -> tuple[str, bool]:
        if budget <= 0 or not content:
            return "", bool(content)
        if len(content) <= budget:
            return content, False
        if budget <= 32:
            return content[:budget], True
        marker = "\n\n... [truncated by SkillContextPack budget]"
        return content[:max(0, budget - len(marker))] + marker, True

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "")
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result
