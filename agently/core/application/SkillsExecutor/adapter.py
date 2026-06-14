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

"""Skills capability adapter roles (spec section 9).

Replaces the monolithic Skill executor with smaller roles that map onto the
official progressive-disclosure standard:

    SkillDiscovery          metadata-only listing for planners (spec 7.2)
    SkillActivationLoader   load SKILL.md + selected resources under budget (7.4)
    SkillContextPackager    cited context packs and resource refs (9.1)
    SkillCapabilityResolver infer capability needs / action candidates (7.5)
    SkillPlanBlockAdvisor   suggest relevant PlanBlocks without granting access
    SkillEvidenceRecorder   label loaded guidance vs downstream evidence (9.3)

The adapter reads a `SkillSource` (in production the installed-Skill registry,
in tests an in-memory contract map). Discovery is metadata-only; it must not
read bundled resources or mount executables. Activation loads selected content
under a budget and produces a `SkillActivation`, but never executes a script,
calls MCP, writes files, grants capability, or declares success — a discovered
capability need is evidence of need, not permission.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from agently.types.data import SkillActivation


@runtime_checkable
class SkillSource(Protocol):
    """Read access to installed Skills, backed by the registry or a test map."""

    def list_skills(self) -> Sequence[Mapping[str, Any]]:
        """Return Skill contracts/cards as inert mappings (metadata)."""
        ...

    def get_skill(self, skill_id: str) -> Mapping[str, Any] | None:
        """Return one full Skill contract mapping, or None if unknown."""
        ...


class DictSkillSource:
    """In-memory `SkillSource` over a mapping of skill_id -> contract."""

    def __init__(self, contracts: Mapping[str, Mapping[str, Any]]):
        self._contracts = {str(skill_id): dict(contract) for skill_id, contract in contracts.items()}

    def list_skills(self) -> Sequence[Mapping[str, Any]]:
        return list(self._contracts.values())

    def get_skill(self, skill_id: str) -> Mapping[str, Any] | None:
        return self._contracts.get(str(skill_id))


class RegistrySkillSource:
    """`SkillSource` adapter over the installed Skills registry.

    Discovery stays metadata-only through `list_skills()`. Activation is the
    first point that inspects a selected Skill contract.
    """

    def __init__(self, registry: Any):
        self._registry = registry

    def list_skills(self) -> Sequence[Mapping[str, Any]]:
        records = self._registry.list_skills()
        return [dict(record) for record in records if isinstance(record, Mapping)]

    def get_skill(self, skill_id: str) -> Mapping[str, Any] | None:
        try:
            contract = self._registry.inspect_skills(str(skill_id))
        except Exception:
            return None
        return dict(contract) if isinstance(contract, Mapping) else None


def _card_of(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    card = contract.get("card")
    return card if isinstance(card, Mapping) else {}


def _skill_id_of(contract: Mapping[str, Any]) -> str:
    return str(contract.get("skill_id") or _card_of(contract).get("skill_id") or "").strip()


class SkillDiscovery:
    """Expose name/description/path/source metadata to planners (spec 7.2).

    Metadata-only by contract: this never opens bundled resources. The returned
    cards are exactly what the planner sees during progressive disclosure.
    """

    def __init__(self, source: SkillSource):
        self._source = source

    def discover(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for contract in self._source.list_skills():
            card = _card_of(contract)
            source_meta = contract.get("source")
            cards.append(
                {
                    "skill_id": _skill_id_of(contract),
                    "name": card.get("name") or contract.get("name"),
                    "description": card.get("description"),
                    "path": (source_meta or {}).get("path") if isinstance(source_meta, Mapping) else None,
                    "source": dict(source_meta) if isinstance(source_meta, Mapping) else {},
                    "trust_level": contract.get("trust_level"),
                }
            )
            if limit is not None and len(cards) >= limit:
                break
        return cards


# Keyword -> (capability need name, risk level). Inference only: a matched need
# is evidence the task may require that capability, never a grant.
_NEED_KEYWORDS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("browser", "screenshot", "navigate", "render", "webpage", "click"), "web_browse", "network"),
    (("web search", "search the web", "google", "search engine"), "web_search", "network"),
    (("write file", "save to", "output file", "workspace write", "create file"), "workspace_write", "filesystem_write"),
    (("read file", "load file", "workspace read"), "workspace_read", "read_only"),
    (("run script", "scripts/", "execute script", "run.py", "python script"), "script_run", "local_exec"),
    (("mcp", "model context protocol"), "mcp", "external_side_effect"),
    (("http request", "api call", "curl", "fetch url", "rest api"), "http_request", "network"),
    (("shell", "bash", "command line", "terminal"), "shell", "local_exec"),
    ((" python ", "python code", "exec("), "python", "local_exec"),
)


class SkillCapabilityResolver:
    """Infer capability needs and action candidate specs from a Skill (spec 7.5).

    Inference reads loaded guidance text and the resource index; it never reads
    the host environment or grants anything. Script-typed resources always
    surface a `script_run` need so a side effect cannot hide behind Skill text.
    """

    def infer(self, contract: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        skill_id = _skill_id_of(contract)
        guidance = contract.get("guidance")
        text = ""
        if isinstance(guidance, Mapping):
            text = str(guidance.get("body") or guidance.get("text") or "")
        text_lower = f" { text.lower() } "

        needs: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_need(need: str, risk: str, source: str, evidence: str) -> None:
            if need in seen:
                return
            seen.add(need)
            needs.append(
                {
                    "skill_id": skill_id,
                    "need": need,
                    "risk": risk,
                    "source": source,
                    "evidence": evidence,
                }
            )

        for keywords, need, risk in _NEED_KEYWORDS:
            hit = next((kw for kw in keywords if kw in text_lower), None)
            if hit is not None:
                add_need(need, risk, "body", f"guidance mentions '{ hit.strip() }'")

        resource_index = contract.get("resource_index")
        if isinstance(resource_index, Mapping):
            for path, meta in resource_index.items():
                kind = str(meta.get("kind", "")).lower() if isinstance(meta, Mapping) else ""
                if kind in ("script", "command", "executable") or str(path).startswith("scripts/"):
                    add_need("script_run", "local_exec", "resource_index", f"script resource '{ path }'")

        action_candidates = [
            {
                "capability": need["need"],
                "risk": need["risk"],
                "required_capability": need["risk"],
                "skill_id": skill_id,
                "grants": False,
            }
            for need in needs
            if need["risk"] != "read_only"
        ]
        return needs, action_candidates


class SkillContextPackager:
    """Select cited resource refs under a character budget (spec 8.1).

    Selection scores resource summaries against the task and stops at the
    budget. Selection records refs and citations only; content materialization
    behind a budget is the loader's responsibility and stays inert.
    """

    def pack(
        self,
        contract: Mapping[str, Any],
        *,
        task: str | None = None,
        budget_chars: int = 4000,
    ) -> tuple[list[str], list[str], int]:
        skill_id = _skill_id_of(contract)
        resource_index = contract.get("resource_index")
        if not isinstance(resource_index, Mapping):
            return [], [], 0

        task_terms = {term for term in (task or "").lower().split() if len(term) > 2}

        def score(path: str, meta: Mapping[str, Any]) -> int:
            summary = str(meta.get("summary", "")).lower()
            haystack = f"{ path.lower() } { summary }"
            return sum(1 for term in task_terms if term in haystack)

        ranked = sorted(
            ((str(path), meta) for path, meta in resource_index.items() if isinstance(meta, Mapping)),
            key=lambda item: score(item[0], item[1]),
            reverse=True,
        )

        selected: list[str] = []
        citations: list[str] = []
        used = 0
        for path, meta in ranked:
            size = int(meta.get("size", len(str(meta.get("summary", ""))) or 200))
            if used + size > budget_chars and selected:
                break
            used += size
            selected.append(path)
            citations.append(f"{ skill_id }:{ path }")
        return selected, citations, used


class SkillPlanBlockAdvisor:
    """Suggest relevant PlanBlocks from loaded Skill guidance (spec 9.1).

    Recommendations are planning hints only. They reference existing PlanBlock
    candidates or baseline PlanBlock kinds; they do not create a duplicate
    capability catalog, grant access, execute scripts, or prove side effects.
    """

    _NEED_TO_PLAN_BLOCKS: Mapping[str, tuple[str, ...]] = {
        "web_browse": ("action_call", "validation", "observation"),
        "web_search": ("action_call", "model_request", "validation"),
        "workspace_write": ("workspace_operation", "action_call", "validation"),
        "workspace_read": ("workspace_operation", "observation"),
        "script_run": ("script_action", "validation"),
        "mcp": ("mcp_tool_call", "validation"),
        "http_request": ("action_call", "validation"),
        "shell": ("action_call", "validation"),
        "python": ("script_action", "validation"),
    }

    def advise(
        self,
        activation: SkillActivation,
        *,
        available_plan_block_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        available = set(available_plan_block_ids or ())
        recommendations: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for need in activation.capability_needs:
            need_name = str(need.get("need") or "").strip()
            for plan_block_kind in self._NEED_TO_PLAN_BLOCKS.get(need_name, ()):
                if available and plan_block_kind not in available:
                    matching = [candidate for candidate in available if candidate.endswith(f".{ plan_block_kind }")]
                    if not matching:
                        continue
                    candidate_ids = matching
                else:
                    candidate_ids = [plan_block_kind]
                for candidate_id in candidate_ids:
                    key = (need_name, candidate_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    recommendations.append(
                        {
                            "skill_id": activation.skill_id,
                            "need": need_name,
                            "plan_block_id": candidate_id,
                            "grants": False,
                        }
                    )
        return recommendations


class SkillEvidenceRecorder:
    """Label Skill context evidence and keep it distinct from execution (spec 8.3)."""

    def record_activation(self, activation: SkillActivation) -> dict[str, Any]:
        return {
            "skill_id": activation.skill_id,
            "loaded_guidance_refs": list(activation.loaded_guidance_refs),
            "selected_resource_refs": list(activation.selected_resource_refs),
            "capability_needs": [dict(need) for need in activation.capability_needs],
            "citations": list(activation.citations),
            # The phrase "Skill executed" is imprecise: loading is not execution.
            "proves_side_effect": False,
            "evidence_kind": "skill_context",
        }


class SkillActivationLoader:
    """Load a Skill's guidance + selected resources into a SkillActivation (spec 7.4).

    Activation reads the already-parsed `SKILL.md` guidance on the contract,
    selects task-relevant resource refs under a budget, indexes script/command
    resources as metadata, and infers capability needs. It performs no side
    effects: no script run, no MCP, no shell/Python, no file write, no grant.
    """

    def __init__(
        self,
        source: SkillSource,
        *,
        packager: SkillContextPackager | None = None,
        capability_resolver: SkillCapabilityResolver | None = None,
        plan_block_advisor: SkillPlanBlockAdvisor | None = None,
    ):
        self._source = source
        self._packager = packager or SkillContextPackager()
        self._capability_resolver = capability_resolver or SkillCapabilityResolver()
        self._plan_block_advisor = plan_block_advisor or SkillPlanBlockAdvisor()

    def activate(
        self,
        skill_id: str,
        *,
        task: str | None = None,
        budget_chars: int = 4000,
    ) -> SkillActivation:
        contract = self._source.get_skill(skill_id)
        if contract is None:
            return SkillActivation.from_value(
                {
                    "skill_id": skill_id,
                    "diagnostics": [{"code": "skill_not_found", "skill_id": skill_id}],
                }
            )

        guidance = contract.get("guidance")
        guidance_refs: list[str] = []
        if isinstance(guidance, Mapping) and (guidance.get("body") or guidance.get("text")):
            guidance_refs.append(f"{ skill_id }:SKILL.md")

        selected_refs, citations, used_chars = self._packager.pack(
            contract, task=task, budget_chars=budget_chars
        )
        needs, action_candidates = self._capability_resolver.infer(contract)

        activation = SkillActivation.from_value(
            {
                "skill_id": skill_id,
                "source": contract.get("source") if isinstance(contract.get("source"), Mapping) else {},
                "loaded_guidance_refs": guidance_refs,
                "selected_resource_refs": selected_refs,
                "capability_needs": needs,
                "action_candidate_specs": action_candidates,
                "citations": citations,
                "diagnostics": [{"used_chars": used_chars, "budget_chars": budget_chars}],
            }
        )
        return SkillActivation.from_value(
            {
                **activation.to_dict(),
                "plan_block_recommendations": self._plan_block_advisor.advise(activation),
            }
        )


class SkillCapabilityAdapter:
    """Composition facade over the Skills capability adapter roles (spec 8.1)."""

    def __init__(self, source: SkillSource):
        self._source = source
        self.discovery = SkillDiscovery(source)
        self.capability_resolver = SkillCapabilityResolver()
        self.packager = SkillContextPackager()
        self.plan_block_advisor = SkillPlanBlockAdvisor()
        self.loader = SkillActivationLoader(
            source,
            packager=self.packager,
            capability_resolver=self.capability_resolver,
            plan_block_advisor=self.plan_block_advisor,
        )
        self.evidence_recorder = SkillEvidenceRecorder()

    def discover(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.discovery.discover(limit=limit)

    def activate(self, skill_id: str, *, task: str | None = None, budget_chars: int = 4000) -> SkillActivation:
        return self.loader.activate(skill_id, task=task, budget_chars=budget_chars)

    def activate_many(
        self,
        skill_ids: Sequence[str],
        *,
        task: str | None = None,
        budget_chars: int = 4000,
    ) -> list[SkillActivation]:
        return [self.activate(skill_id, task=task, budget_chars=budget_chars) for skill_id in skill_ids]
