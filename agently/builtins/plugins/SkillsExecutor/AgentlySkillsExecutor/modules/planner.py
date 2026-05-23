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
from typing import Any

from agently.types.data import (
    SkillContract,
    SkillExecutionPlan,
    SkillMode,
    SkillPlanRejection,
    SkillPlanSelection,
    SkillsPackRecord,
)
from agently.types.plugins import SkillsPlanningContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list, _ensure_string_list

from .registry import SkillRegistry


def _matches_selector(contract: SkillContract, selector: Any) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        card = _ensure_dict(contract.get("card"))
        return selector in {
            str(contract.get("skill_id") or ""),
            str(card.get("display_name") or ""),
            str(card.get("name") or ""),
        }
    if not isinstance(selector, dict):
        return False
    skill_id = selector.get("skill_id") or selector.get("id") or selector.get("name")
    if skill_id and str(skill_id) != contract.get("skill_id") and str(skill_id) != contract.get("card", {}).get("display_name"):
        return False
    return True


def _normalize_skills_pack_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("skills_pack_id") or value.get("name") or value.get("id")
    return str(value or "").strip()


def _matches_skills_pack_selector(contract: SkillContract, selector: Any) -> bool:
    skills_pack_selector = _normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    source = _ensure_dict(contract.get("source"))
    install = _ensure_dict(contract.get("install_metadata"))
    candidates = {
        str(source.get("skills_pack_id") or ""),
        str(source.get("skills_pack_name") or ""),
        str(install.get("skills_pack_id") or ""),
        str(install.get("skills_pack_name") or ""),
    }
    return skills_pack_selector in candidates


def _matches_record_selector(record: dict[str, Any], selector: Any) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        return selector in {
            str(record.get("skill_id") or ""),
            str(record.get("display_name") or ""),
            str(record.get("name") or ""),
        }
    if not isinstance(selector, dict):
        return False
    skill_id = selector.get("skill_id") or selector.get("id") or selector.get("name")
    if skill_id:
        return str(skill_id) in {
            str(record.get("skill_id") or ""),
            str(record.get("display_name") or ""),
            str(record.get("name") or ""),
        }
    return True


def _matches_record_pack_selector(record: dict[str, Any], selector: Any) -> bool:
    skills_pack_selector = _normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    return skills_pack_selector in {
        str(record.get("skills_pack_id") or ""),
        str(record.get("skills_pack_name") or ""),
    }


class SkillPlanner:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def resolve(
        self,
        *,
        context: SkillsPlanningContext,
        task: str | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
    ) -> SkillExecutionPlan:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        task_text = str(task or "")
        selectors = _ensure_list(skills)
        pack_selectors = _ensure_list(skills_packs)
        records = self._records_for_scope(
            self.registry.list_skills(),
            selectors=selectors,
            pack_selectors=pack_selectors,
        )
        installed, diagnostics = self._inspect_records(records)
        rejected: list[SkillPlanRejection] = []
        rejected_packs: list[dict[str, Any]] = []
        selected_pack_records: dict[str, SkillsPackRecord] = {}

        if mode == "required":
            selected, rejected, rejected_packs = self._select_required(installed, selectors=selectors, pack_selectors=pack_selectors)
        else:
            candidates = self._model_decision_candidates(installed, task_text=task_text, selectors=selectors, pack_selectors=pack_selectors)
            selected = await self._select_model_ordered(context=context, task_text=task_text, candidates=candidates)

        for contract in selected:
            pack_record = self._skills_pack_record_for_contract(contract)
            if pack_record:
                selected_pack_records[str(pack_record.get("skills_pack_id", ""))] = pack_record

        selections = [
            self._to_selection(contract, required=mode == "required", selected_by="required" if mode == "required" else "model_decision")
            for contract in selected
        ]
        prompt_bindings = [self._prompt_binding_for_selection(selection) for selection in selections]
        prompt_bindings = [item for item in prompt_bindings if item]
        status = "resolved" if selections else "no_match"
        if mode == "required" and (rejected or rejected_packs):
            status = "blocked"

        return SkillExecutionPlan({
            "plan_id": uuid.uuid4().hex,
            "mode": mode,
            "status": status,
            "task_summary": task_text,
            "selected_skills": selections,
            "selected_skills_packs": list(selected_pack_records.values()),
            "rejected_skills": rejected,
            "rejected_skills_packs": rejected_packs,
            "decision_cards": [_copy_public(selection.get("decision_card", {})) for selection in selections],
            "prompt_bindings": prompt_bindings,
            "resource_bindings": [_copy_public(selection.get("resource_index", {})) for selection in selections],
            "expected_result_shape": _ensure_dict(semantic_outputs),
            "diagnostics": diagnostics,
        })

    def _records_for_scope(
        self,
        records: list[dict[str, Any]],
        *,
        selectors: list[Any],
        pack_selectors: list[Any],
    ) -> list[dict[str, Any]]:
        if not selectors and not pack_selectors:
            return records
        return [
            record
            for record in records
            if any(_matches_record_selector(record, selector) for selector in selectors)
            or any(_matches_record_pack_selector(record, selector) for selector in pack_selectors)
        ]

    def _inspect_records(self, records: list[dict[str, Any]]) -> tuple[list[SkillContract], list[dict[str, Any]]]:
        installed: list[SkillContract] = []
        diagnostics: list[dict[str, Any]] = []
        for record in records:
            skill_id = str(record.get("skill_id") or "")
            if not skill_id:
                diagnostics.append({
                    "level": "warning",
                    "code": "skill_unreadable",
                    "skill_id": "",
                    "message": "Installed skill index entry is missing skill_id.",
                    "record": _copy_public(record),
                })
                continue
            try:
                installed.append(self.registry.inspect_skills(skill_id))
            except Exception as error:
                diagnostics.append({
                    "level": "warning",
                    "code": "skill_unreadable",
                    "skill_id": skill_id,
                    "message": str(error),
                    "record": _copy_public(record),
                })
        return installed, diagnostics

    def _select_required(
        self,
        installed: list[SkillContract],
        *,
        selectors: list[Any],
        pack_selectors: list[Any],
    ) -> tuple[list[SkillContract], list[SkillPlanRejection], list[dict[str, Any]]]:
        selected: list[SkillContract] = []
        rejected: list[SkillPlanRejection] = []
        rejected_packs: list[dict[str, Any]] = []
        for selector in selectors:
            matches = [contract for contract in installed if _matches_selector(contract, selector)]
            if not matches:
                rejected.append({
                    "skill_id": str(selector),
                    "reason_code": "required_not_found",
                    "reason": f"Required skill '{ selector }' is not installed.",
                })
                continue
            for contract in matches:
                if contract not in selected:
                    selected.append(contract)
        for selector in pack_selectors:
            matches = [contract for contract in installed if _matches_skills_pack_selector(contract, selector)]
            if not matches:
                pack_id = _normalize_skills_pack_identifier(selector)
                rejected_packs.append({
                    "skills_pack_id": pack_id,
                    "reason_code": "required_pack_not_found",
                    "reason": f"Required skills pack '{ pack_id }' had no installed standard Skills.",
                })
                continue
            for contract in matches:
                if contract not in selected:
                    selected.append(contract)
        return selected, rejected, rejected_packs

    def _model_decision_candidates(
        self,
        installed: list[SkillContract],
        *,
        task_text: str,
        selectors: list[Any],
        pack_selectors: list[Any],
    ) -> list[SkillContract]:
        if selectors or pack_selectors:
            candidates = [
                contract
                for contract in installed
                if any(_matches_selector(contract, selector) for selector in selectors)
                or any(_matches_skills_pack_selector(contract, selector) for selector in pack_selectors)
            ]
            return candidates
        task_lower = task_text.lower()
        candidates = []
        for contract in installed:
            card = _ensure_dict(contract.get("card"))
            decision_card = _ensure_dict(contract.get("decision_card"))
            names = [str(contract.get("skill_id", "")), str(card.get("display_name", "")), str(decision_card.get("name", ""))]
            keywords = _ensure_string_list(_ensure_dict(card.get("activation_hints")).get("keywords"))
            keywords.extend(_ensure_string_list(decision_card.get("keywords")))
            haystack = " ".join([*names, *keywords, str(card.get("description", "")), str(decision_card.get("description", ""))]).lower()
            if any(term and term.lower() in task_lower for term in [*names, *keywords]) or any(word in task_lower for word in haystack.split()):
                candidates.append(contract)
        return candidates

    async def _select_model_ordered(
        self,
        *,
        context: SkillsPlanningContext,
        task_text: str,
        candidates: list[SkillContract],
    ) -> list[SkillContract]:
        if len(candidates) <= 1:
            return candidates
        candidate_by_id = {str(contract.get("skill_id")): contract for contract in candidates}
        try:
            result = await context.async_request_model(
                prompt={
                    "task": task_text,
                    "candidate_skill_cards": [_copy_public(contract.get("decision_card", {})) for contract in candidates],
                    "routing_policy": [
                        "Select and order the Skills that should be used for this task.",
                        "Only choose from candidate_skill_cards.",
                        "Do not exclude a selected Skill because of Agently metadata; base relevance on SKILL.md name, description, and summary.",
                    ],
                },
                output_schema={
                    "selected_skill_ids": [(str, "Selected skill ids in execution order.", True)],
                    "reason": (str, "Concise route choice reason."),
                },
                ensure_keys=["selected_skill_ids"],
                max_retries=2,
            )
        except Exception:
            return candidates
        ordered = []
        for skill_id in _ensure_string_list(_ensure_dict(result).get("selected_skill_ids")):
            if skill_id in candidate_by_id and candidate_by_id[skill_id] not in ordered:
                ordered.append(candidate_by_id[skill_id])
        return ordered or candidates

    def _to_selection(self, contract: SkillContract, *, required: bool, selected_by: str) -> SkillPlanSelection:
        skill_id = str(contract.get("skill_id", ""))
        source = _ensure_dict(contract.get("source"))
        card = _ensure_dict(contract.get("card"))
        return SkillPlanSelection({
            "skill_id": skill_id,
            "skills_pack_id": str(source.get("skills_pack_id") or ""),
            "skills_pack_name": str(source.get("skills_pack_name") or ""),
            "version": str(contract.get("version", "")),
            "display_name": str(card.get("display_name") or skill_id),
            "reason": "required skill" if required else "selected by model decision",
            "selected_by": selected_by,
            "required": required,
            "card": _copy_public(card),
            "decision_card": _copy_public(contract.get("decision_card", {})),
            "guidance": _copy_public(contract.get("guidance", {})),
            "resource_index": _copy_public(contract.get("resource_index", {})),
        })

    def _prompt_binding_for_selection(self, selection: SkillPlanSelection) -> dict[str, Any]:
        guidance = _ensure_dict(selection.get("guidance"))
        content = str(guidance.get("content") or "").strip()
        if not content:
            return {}
        return {
            "skill_id": str(selection.get("skill_id", "")),
            "display_name": str(selection.get("display_name") or selection.get("skill_id", "")),
            "path": str(guidance.get("path") or "SKILL.md"),
            "content": content,
            "format": "markdown",
        }

    def _skills_pack_record_for_contract(self, contract: SkillContract) -> SkillsPackRecord | None:
        source = _ensure_dict(contract.get("source"))
        pack_id = str(source.get("skills_pack_id") or "")
        if not pack_id:
            return None
        try:
            return self.registry.inspect_skills_pack(pack_id)
        except Exception:
            return SkillsPackRecord({
                "skills_pack_id": pack_id,
                "name": str(source.get("skills_pack_name") or pack_id),
                "source": str(source.get("source") or ""),
                "source_type": str(source.get("source_type") or ""),
                "installed_skills": [str(contract.get("skill_id", ""))],
                "failed_skills": [],
                "status": "unknown",
            })
