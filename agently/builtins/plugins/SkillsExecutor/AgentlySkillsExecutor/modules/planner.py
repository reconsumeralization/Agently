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

import asyncio
import uuid
from typing import Any, Callable

from agently.types.data import (
    SkillContract,
    SkillExecutionPlan,
    SkillMode,
    SkillPlanRejection,
    SkillPlanSelection,
    SkillScope,
    SkillsPackRecord,
)
from agently.types.plugins import SkillsPlanningContext
from agently.utils.DataGuardian import (
    _copy_public,
    _ensure_dict,
    _ensure_dict_list,
    _ensure_list,
    _ensure_string_list,
    _sanitize_id,
)

from .errors import SkillExecutionError, SkillInstallError
from .helpers import _SEMANTIC_TYPE_ALIASES, _semantic_role_and_type
from .registry import SkillRegistry


# ── Selector matching ───────────────────────────────────────────────────────

def _matches_selector(contract: SkillContract, selector: Any) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        return selector == contract.get("skill_id")
    if not isinstance(selector, dict):
        return False
    skill_id = selector.get("skill_id") or selector.get("id")
    if skill_id and str(skill_id) != contract.get("skill_id"):
        return False
    tags = selector.get("tags")
    if tags:
        contract_tags = set(str(tag) for tag in _ensure_list(contract.get("metadata", {}).get("tags")))
        if not set(str(tag) for tag in _ensure_list(tags)).issubset(contract_tags):
            return False
    return True


def _matches_skills_pack_selector(contract: SkillContract, selector: Any) -> bool:
    skills_pack_selector = _normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    source = _ensure_dict(contract.get("source"))
    metadata = _ensure_dict(contract.get("metadata"))
    candidates = {
        str(source.get("skills_pack_id") or ""),
        str(source.get("skills_pack_name") or ""),
        str(metadata.get("skills_pack_id") or ""),
        str(metadata.get("skills_pack_name") or ""),
    }
    return skills_pack_selector in candidates


def _normalize_skills_pack_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("skills_pack_id") or value.get("name") or value.get("id")
    return str(value or "").strip()


# ── Semantic output contract helpers ────────────────────────────────────────

def _flatten_public_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_public_text(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return " ".join(_flatten_public_text(item) for item in value)
    return str(value)


def _normalize_deliverable(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        role, output_type = _semantic_role_and_type(value)
        return {
            "role": role,
            "type": output_type,
            "required": True,
            "aliases": [value, role, role.replace("_", " ")],
        }
    data = _ensure_dict(value)
    role = str(data.get("role") or data.get("name") or data.get("id") or "")
    output_type = str(data.get("type") or data.get("artifact_type") or "")
    if not role and data.get("path"):
        role, inferred_type = _semantic_role_and_type(str(data.get("path")))
        output_type = output_type or inferred_type
    if not role:
        return {}
    role = _sanitize_id(role)
    output_type = output_type or "artifact"
    aliases = _ensure_string_list(data.get("aliases")) or [role, role.replace("_", " ")]
    return {
        "role": role,
        "type": output_type,
        "required": bool(data.get("required", True)),
        "validation": _copy_public(data.get("validation", {})),
        "aliases": aliases,
    }


def _normalize_semantic_output_contract(value: Any) -> dict[str, Any]:
    if not value:
        return {"deliverables": []}
    if isinstance(value, dict) and isinstance(value.get("deliverables"), list):
        deliverables = [_normalize_deliverable(item) for item in value.get("deliverables", [])]
        return {"deliverables": [item for item in deliverables if item.get("role")]}
    if isinstance(value, dict):
        deliverables = []
        for role, spec in value.items():
            spec_data = _ensure_dict(spec)
            deliverables.append(
                _normalize_deliverable({
                    **spec_data,
                    "role": spec_data.get("role") or role,
                    "type": spec_data.get("type") or spec_data.get("artifact_type") or "artifact",
                    "aliases": spec_data.get("aliases") or [role],
                })
            )
        return {"deliverables": [item for item in deliverables if item.get("role")]}
    deliverables = [_normalize_deliverable(item) for item in _ensure_list(value)]
    return {"deliverables": [item for item in deliverables if item.get("role")]}


def _expected_output_names(contract: dict[str, Any]) -> list[str]:
    names = []
    for item in _ensure_list(contract.get("deliverables")):
        data = _ensure_dict(item)
        role = str(data.get("role") or "")
        output_type = str(data.get("type") or "artifact")
        if not role:
            continue
        if output_type == "directory":
            names.append(f"{ role }/")
        elif output_type == "artifact":
            names.append(role)
        else:
            names.append(f"{ role }.{ output_type }")
    return names


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
        scope: SkillScope = "session",
        decision_handler: Callable[..., Any] | None = None,
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecutionPlan:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        task_text = str(task or "")
        selectors = _ensure_list(skills)
        skills_pack_selectors = _ensure_list(skills_packs)
        installed = [self.registry.inspect_skills(str(item["skill_id"])) for item in self.registry.list_skills()]
        selected: list[SkillPlanSelection] = []
        selected_skills_packs: dict[str, SkillsPackRecord] = {}
        rejected: list[SkillPlanRejection] = []
        rejected_skills_packs: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        requirements: list[Any] = []
        stage_graph: list[dict[str, Any]] = []
        planned_semantic_outputs: dict[str, Any] = {}

        for contract in installed:
            matched_skill_selector = any(_matches_selector(contract, selector) for selector in selectors) if selectors else False
            matched_skills_pack_selector = any(_matches_skills_pack_selector(contract, selector) for selector in skills_pack_selectors) if skills_pack_selectors else False
            matched_selector = matched_skill_selector or matched_skills_pack_selector
            is_required = mode == "required" and matched_selector
            eligible, reason_code, reason = self._is_eligible(context, contract)
            if not eligible:
                if matched_skill_selector:
                    rejected.append({"skill_id": str(contract.get("skill_id", "")), "reason_code": reason_code, "reason": reason})
                elif matched_skills_pack_selector:
                    diagnostics.append({
                        "level": "warning",
                        "code": reason_code,
                        "skill_id": str(contract.get("skill_id", "")),
                        "skills_pack_id": str(contract.get("source", {}).get("skills_pack_id", "")),
                        "message": reason,
                    })
                continue
            if not self._should_select(contract, task_text=task_text, matched_selector=matched_selector, mode=mode):
                continue

            selection = self._to_selection(contract, scope=scope, required=is_required, selected_by="required" if is_required else "model_planner")
            selected.append(selection)
            skills_pack_record = self._skills_pack_record_for_contract(contract)
            if skills_pack_record:
                selected_skills_packs[str(skills_pack_record.get("skills_pack_id", ""))] = skills_pack_record
            for requirement in _ensure_list(contract.get("execution_environment_requirements")):
                requirements.append(_copy_public(requirement))
            for stage in _ensure_list(selection.get("stages")):
                stage_key = str(stage.get("stage_id") or stage.get("id") or "")
                stage_graph.append(
                    {
                        "skill_id": str(selection.get("skill_id", "")),
                        "stage_id": stage_key,
                        "kind": stage.get("kind", "model"),
                    }
                )
                if stage_key:
                    planned_semantic_outputs[stage_key] = {
                        "role": stage_key,
                        "type": str(stage.get("kind", "model")),
                    }

        if mode == "required":
            required_ids = {str(selector) for selector in selectors if isinstance(selector, str)}
            selected_ids = {str(item.get("skill_id", "")) for item in selected}
            for missing in sorted(required_ids - selected_ids):
                if not any(item.get("skill_id") == missing for item in rejected):
                    rejected.append(
                        {
                            "skill_id": missing,
                            "reason_code": "required_not_selected",
                            "reason": f"Required skill '{ missing }' was not selected.",
                        }
                    )
            for selector in skills_pack_selectors:
                skills_pack_id = _normalize_skills_pack_identifier(selector)
                if not skills_pack_id:
                    continue
                selected_from_pack = any(
                    _matches_skills_pack_selector(self.registry.inspect_skills(str(item.get("skill_id"))), selector)
                    for item in selected
                    if item.get("skill_id")
                )
                if not selected_from_pack:
                    rejected_skills_packs.append({
                        "skills_pack_id": skills_pack_id,
                        "reason_code": "required_pack_not_selected",
                        "reason": f"Required skills pack '{ skills_pack_id }' had no eligible selected skills.",
                    })

        status = "resolved" if selected else "no_match"
        if mode == "required" and (rejected or rejected_skills_packs):
            status = "blocked"
        semantic_output_contract = _normalize_semantic_output_contract(semantic_outputs)
        if semantic_output_contract.get("deliverables"):
            planned_semantic_outputs.update({
                str(item.get("role")): _copy_public(item)
                for item in _ensure_list(semantic_output_contract.get("deliverables"))
                if item.get("role")
            })

        plan = SkillExecutionPlan({
            "plan_id": uuid.uuid4().hex,
            "mode": mode,
            "status": status,
            "task_summary": task_text,
            "selected_skills": selected,
            "selected_skills_packs": list(selected_skills_packs.values()),
            "rejected_skills": rejected,
            "rejected_skills_packs": rejected_skills_packs,
            "composed_stage_graph": stage_graph,
            "dynamic_task_graph": {},
            "prompt_bindings": [],
            "action_bindings": [
                {"skill_id": str(item.get("skill_id", "")), "actions": item.get("card", {}).get("available_action_summary", [])}
                for item in selected
            ],
            "resource_bindings": [],
            "execution_environment_requirements": requirements,
            "approval_requests": [],
            "state_keys": [str(stage.get("stage_id")) for item in selected for stage in _ensure_list(item.get("stages"))],
            "semantic_outputs": planned_semantic_outputs,
            "artifact_bindings": [],
            "expected_result_shape": semantic_output_contract,
            "stream_policy": {},
            "fallback_policy": {"normal_agent_response_allowed": mode == "model_decision"},
            "cleanup_policy": {"scope": scope},
            "diagnostics": diagnostics,
        })
        plan = await self._compose_plan_with_model(
            context=context,
            plan=plan,
            semantic_output_contract=semantic_output_contract,
            planner_mode=planner_mode,
            max_revisions=planner_max_revisions,
        )
        if decision_handler is not None:
            handler_context = {"context": context, "task": task_text}
            agent_ref = getattr(context, "agent", None)
            if agent_ref is not None:
                handler_context["agent"] = agent_ref
            plan = await self._apply_decision_handler(decision_handler, plan=plan, context=handler_context)
        return plan

    async def _compose_plan_with_model(
        self,
        *,
        context: SkillsPlanningContext,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
        planner_mode: str,
        max_revisions: int,
    ) -> SkillExecutionPlan:
        selected = _ensure_list(plan.get("selected_skills"))
        if not selected:
            return plan
        deliverables = _ensure_list(semantic_output_contract.get("deliverables"))
        should_compose = planner_mode in {"model", "auto", "model_decision"} and (len(selected) > 1 or bool(deliverables))
        if planner_mode == "deterministic":
            should_compose = False
        if not should_compose and not deliverables:
            return plan

        model_result: dict[str, Any] = {}
        if should_compose:
            try:
                model_result = await self._request_model_plan(
                    context=context,
                    plan=plan,
                    semantic_output_contract=semantic_output_contract,
                    max_revisions=max_revisions,
                )
            except Exception as error:
                plan.setdefault("diagnostics", []).append({
                    "level": "warning",
                    "code": "model_planner_failed",
                    "message": str(error),
                })

        repaired = self._repair_planner_result(
            model_result,
            plan=plan,
            semantic_output_contract=semantic_output_contract,
        )
        evaluation = self._evaluate_planner_result(repaired, plan=plan, semantic_output_contract=semantic_output_contract)
        return self._apply_planner_result(plan, repaired, evaluation)

    async def _request_model_plan(
        self,
        *,
        context: SkillsPlanningContext,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
        max_revisions: int,
    ) -> dict[str, Any]:
        return await context.async_request_model_plan(
            plan=plan,
            semantic_output_contract=semantic_output_contract,
            output_schema=self._planner_output_schema(),
            max_revisions=max_revisions,
        )

    def _planner_output_schema(self) -> dict[str, Any]:
        return {
            "selected_skill_ids": [(str, "Skill ids selected from candidates.", True)],
            "entry_skill_id": (str, "Primary entry skill id, or none.", True),
            "stage_plan": [(str, "Ordered stage with skill handoff, dependency, and output notes.", True)],
            "skill_switches": [(str, "Where execution switches between skills or capability layers.", True)],
            "intermediate_artifacts": [(str, "Intermediate artifact role and producer/consumer.", True)],
            "external_side_effects": [(str, "External API/MCP/SaaS writes, local command effects, or file writes.", True)],
            "approval_gates": [(str, "Approval gates before side effects or credentialed actions.", True)],
            "fallbacks": [(str, "Retry, fallback, or degraded-mode behavior.", True)],
            "expected_outputs": [(str, "Final semantic deliverable role/type/path.", True)],
            "boundary_notes": [(str, "Skill vs Action/tool/API/artifact boundary.", True)],
            "risks": [(str, "Missing dependency, policy, environment, or data-quality risk.", True)],
        }

    def _repair_planner_result(
        self,
        result: dict[str, Any],
        *,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_ids = [str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills")) if item.get("skill_id")]
        selected_ids = [item for item in _ensure_string_list(result.get("selected_skill_ids")) if item in candidate_ids]
        output_names = _expected_output_names(semantic_output_contract)
        deliverables = _ensure_list(semantic_output_contract.get("deliverables"))
        selected_ids = self._ensure_supporting_skills(selected_ids, candidate_ids, deliverables)

        repaired = {
            "task_summary": str(plan.get("task_summary", "")),
            "selected_skill_ids": selected_ids,
            "entry_skill_id": str(result.get("entry_skill_id") or (selected_ids[0] if selected_ids else "none")),
            "stage_plan": _ensure_string_list(result.get("stage_plan")),
            "skill_switches": _ensure_string_list(result.get("skill_switches")),
            "intermediate_artifacts": _ensure_string_list(result.get("intermediate_artifacts")),
            "external_side_effects": _ensure_string_list(result.get("external_side_effects")),
            "approval_gates": _ensure_string_list(result.get("approval_gates")),
            "fallbacks": _ensure_string_list(result.get("fallbacks")),
            "expected_outputs": _ensure_string_list(result.get("expected_outputs")),
            "boundary_notes": _ensure_string_list(result.get("boundary_notes")),
            "risks": _ensure_string_list(result.get("risks")),
        }

        for output_name in output_names:
            if not self._text_covers_output(output_name, repaired):
                repaired["expected_outputs"].append(output_name)
                role, output_type = _semantic_role_and_type(output_name)
                repaired["intermediate_artifacts"].append(
                    f"{ role } ({ output_type }) is a required semantic deliverable and must be produced or marked partial."
                )

        flags = self._infer_plan_flags(plan=plan, selected_ids=selected_ids, deliverables=deliverables)
        if flags["external"] and not repaired["external_side_effects"]:
            repaired["external_side_effects"].append(
                "External API/MCP/browser/local process or local file writes may be required; keep them separate from Skill guidance."
            )
        if flags["approval"] and not repaired["approval_gates"]:
            repaired["approval_gates"].append(
                "Require human approval before credentialed external writes, SaaS mutation, browser/server actions, or final local file writes."
            )
        if flags["fallback"] and not repaired["fallbacks"]:
            repaired["fallbacks"].append(
                "If a dependency, API, tool, or artifact writer fails, retry once, then produce a degraded local package and mark partial outputs."
            )
        if not repaired["boundary_notes"]:
            repaired["boundary_notes"].append(
                "Agent Skills packages provide behavior-loop guidance; Actions/tools/APIs execute atomic work; artifacts are explicit outputs."
            )
        if not repaired["skill_switches"] and selected_ids:
            for left, right in zip(selected_ids, selected_ids[1:]):
                repaired["skill_switches"].append(f"{ left } -> { right } after the previous semantic artifact is ready.")
        if not repaired["risks"]:
            repaired["risks"].append("Missing tool, credential, environment, or artifact writer can produce partial outputs.")

        repaired["stage_plan"] = self._ensure_stage_plan(
            stage_plan=repaired["stage_plan"],
            selected_ids=selected_ids,
            deliverables=deliverables,
            flags=flags,
        )
        return repaired

    def _ensure_supporting_skills(self, selected_ids: list[str], candidate_ids: list[str], deliverables: list[Any]) -> list[str]:
        result = [item for item in selected_ids if item in candidate_ids]
        if not result and candidate_ids:
            result.append(candidate_ids[0])
        return result

    def _infer_plan_flags(self, *, plan: SkillExecutionPlan, selected_ids: list[str], deliverables: list[Any]) -> dict[str, bool]:
        selected_cards = [_ensure_dict(item).get("card", {}) for item in _ensure_list(plan.get("selected_skills"))]
        selected_text = _flatten_public_text(selected_cards).lower()
        deliverable_types = {str(_ensure_dict(item).get("type") or "").lower() for item in deliverables}
        external_sources = _ensure_list(plan.get("execution_environment_requirements"))
        side_effect_terms = [
            str(effect.get("kind") or effect.get("policy") or "")
            for card in selected_cards
            for effect in _ensure_dict_list(_ensure_dict(card).get("side_effects"))
        ]
        capability_terms = [
            str(item)
            for card in selected_cards
            for item in _ensure_list(_ensure_dict(card).get("required_capabilities"))
        ]
        policy_text = " ".join([selected_text, *side_effect_terms, *capability_terms]).lower()
        has_external = bool(external_sources) or any(
            term in policy_text
            for term in [
                "api",
                "mcp",
                "browser",
                "server",
                "process",
                "shell",
                "external",
                "credential",
                "file_write",
                "local_file_write",
            ]
        )
        has_artifact = any(item for item in deliverable_types if item and item != "text")
        return {
            "external": has_external,
            "approval": has_external or has_artifact or "approval" in policy_text,
            "fallback": True,
        }

    def _ensure_stage_plan(
        self,
        *,
        stage_plan: list[str],
        selected_ids: list[str],
        deliverables: list[Any],
        flags: dict[str, bool],
    ) -> list[str]:
        stages = list(stage_plan)
        for index, skill_id in enumerate(selected_ids, start=1):
            if not any(skill_id in item for item in stages):
                stages.append(f"Stage { index }: use { skill_id } for its declared role and pass structured results to the next stage.")
        for item in deliverables:
            data = _ensure_dict(item)
            role = str(data.get("role") or "")
            output_type = str(data.get("type") or "artifact")
            if role and not any(role in stage for stage in stages):
                stages.append(f"Produce semantic deliverable { role } as { output_type } and attach validation/artifact refs.")
        if selected_ids and len(stages) < len(selected_ids):
            for skill_id in selected_ids:
                if len(stages) >= len(selected_ids):
                    break
                stages.append(
                    f"Dynamic Task node: run { skill_id } as an explicit skill stage and expose its produced artifacts to dependent stages."
                )
        if flags["approval"] and not any("approval" in stage.lower() or "confirm" in stage.lower() for stage in stages):
            stages.append("Approval gate: pause before external writes, credentialed tools, browser/server actions, or final artifact writes.")
        if flags["fallback"] and not any("fallback" in stage.lower() or "degraded" in stage.lower() for stage in stages):
            stages.append("Fallback stage: retry failed tools once, then produce degraded local outputs with partial status and diagnostics.")
        stages.append("QA/trace stage: validate semantic output coverage, source/compliance notes, boundary notes, and skill_trace/execution_log.")
        return stages

    def _evaluate_planner_result(
        self,
        result: dict[str, Any],
        *,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_ids = {str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills")) if item.get("skill_id")}
        selected_ids = set(_ensure_string_list(result.get("selected_skill_ids")))
        output_coverage = {
            output_name: self._text_covers_output(output_name, result)
            for output_name in _expected_output_names(semantic_output_contract)
        }
        checks = {
            "selected_skills_are_candidates": selected_ids.issubset(candidate_ids),
            "has_stage_plan": bool(result.get("stage_plan")),
            "has_skill_switches": bool(result.get("skill_switches")) or len(selected_ids) <= 1,
            "has_intermediate_artifacts": bool(result.get("intermediate_artifacts")),
            "has_boundaries": self._contains_any(result, ["skill", "action", "tool", "api", "mcp", "artifact"]),
            "covers_semantic_outputs": all(output_coverage.values()),
        }
        return {
            "status": "pass" if all(checks.values()) else "needs_revision",
            "checks": checks,
            "output_coverage": output_coverage,
        }

    def _apply_planner_result(
        self,
        plan: SkillExecutionPlan,
        result: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> SkillExecutionPlan:
        selected_by_id = {
            str(item.get("skill_id")): item
            for item in _ensure_list(plan.get("selected_skills"))
            if item.get("skill_id")
        }
        ordered_selected = [
            selected_by_id[skill_id]
            for skill_id in _ensure_string_list(result.get("selected_skill_ids"))
            if skill_id in selected_by_id
        ]
        if ordered_selected:
            plan["selected_skills"] = _copy_public(ordered_selected)
        stages = []
        selected_ids = _ensure_string_list(result.get("selected_skill_ids"))
        for index, text in enumerate(_ensure_string_list(result.get("stage_plan")), start=1):
            skill_id = selected_ids[(index - 1) % len(selected_ids)] if selected_ids else ""
            stage_id = f"stage_{ index }"
            stages.append({
                "task_id": stage_id,
                "stage_id": stage_id,
                "skill_id": skill_id,
                "kind": "model_plan",
                "title": text[:120],
                "purpose": text,
                "depends_on": [f"stage_{ index - 1 }"] if index > 1 else [],
                "produces": self._stage_produces(index=index, text=text, plan=plan),
            })
        if stages:
            plan["composed_stage_graph"] = stages
        expected_outputs = _ensure_string_list(result.get("expected_outputs"))
        plan["planner_result"] = _copy_public(result)
        plan["planner_evaluation"] = _copy_public(evaluation)
        plan["stage_plan"] = _ensure_string_list(result.get("stage_plan"))
        plan["skill_switches"] = _ensure_string_list(result.get("skill_switches"))
        plan["intermediate_artifacts"] = _ensure_string_list(result.get("intermediate_artifacts"))
        plan["external_side_effects"] = _ensure_string_list(result.get("external_side_effects"))
        plan["approval_gates"] = _ensure_string_list(result.get("approval_gates"))
        plan["fallbacks"] = _ensure_string_list(result.get("fallbacks"))
        plan["expected_outputs"] = expected_outputs
        plan["boundary_notes"] = _ensure_string_list(result.get("boundary_notes"))
        plan["risks"] = _ensure_string_list(result.get("risks"))
        plan["approval_requests"] = [
            {"reason": item, "status": "required"}
            for item in _ensure_string_list(result.get("approval_gates"))
        ]
        plan["fallback_policy"] = {
            **_ensure_dict(plan.get("fallback_policy")),
            "planned_fallbacks": _ensure_string_list(result.get("fallbacks")),
            "semantic_evaluator": True,
        }
        plan["artifact_bindings"] = [
            {"role": role, "target": output_name}
            for output_name in expected_outputs
            for role, _ in [_semantic_role_and_type(output_name)]
        ]
        plan.setdefault("diagnostics", []).append({
            "level": "info",
            "code": "model_composed_plan",
            "message": f"Planner evaluation { evaluation.get('status') }.",
        })
        return plan

    def _stage_produces(self, *, index: int, text: str, plan: SkillExecutionPlan) -> list[dict[str, Any]]:
        produced: list[dict[str, Any]] = []
        lower = text.lower()
        for item in _ensure_list(_ensure_dict(plan.get("expected_result_shape")).get("deliverables")):
            data = _ensure_dict(item)
            role = str(data.get("role") or "")
            if role and (role.lower() in lower or role.replace("_", " ").lower() in lower):
                produced.append({"role": role, "type": str(data.get("type") or "artifact")})
        return produced or [{"role": f"stage_{ index }", "type": "plan"}]

    def _text_covers_output(self, output_name: str, result: dict[str, Any]) -> bool:
        role, output_type = _semantic_role_and_type(output_name)
        searchable = _flatten_public_text(result).lower()
        if output_name.lower().strip("/") in searchable:
            return True
        role_terms = [role, role.replace("_", " "), role.replace("_", "-")]
        type_aliases = _SEMANTIC_TYPE_ALIASES.get(output_type, [output_type])
        return any(term.lower() in searchable for term in role_terms if term) and any(
            alias.lower() in searchable for alias in type_aliases
        )

    def _contains_any(self, value: Any, terms: list[str]) -> bool:
        text = _flatten_public_text(value).lower()
        return any(term.lower() in text for term in terms)

    def _is_eligible(self, context: SkillsPlanningContext, contract: SkillContract) -> tuple[bool, str, str]:
        allowed_trust = {str(item) for item in _ensure_list(context.get_setting("skills.allowed_trust_levels", []))}
        trust_level = str(contract.get("trust_level", "local"))
        if allowed_trust and trust_level not in allowed_trust:
            return False, "trust_denied", f"Trust level '{ trust_level }' is not allowed."
        for action_id in _ensure_list(contract.get("action_requirements")):
            resolved, reason = self._ensure_action_available_or_resolvable(context, str(action_id))
            if not resolved:
                return False, "missing_action", reason
        return True, "", ""

    def _ensure_action_available_or_resolvable(self, context: SkillsPlanningContext, action_id: str) -> tuple[bool, str]:
        if context.action_available(action_id):
            return True, "available"
        if context.can_auto_bind_bash_action(action_id):
            try:
                context.auto_bind_bash_action(action_id)
            except Exception as error:
                return False, f"Required action '{ action_id }' could not be auto-bound to Bash sandbox: { error }"
            if context.action_available(action_id):
                return True, "auto_bound_bash"
        return False, (
            f"Required action '{ action_id }' is not available, and Skills Executor could not find a "
            "controlled built-in substitute. Bind an Action, enable an execution environment, or approve a "
            "trusted replacement before running this Skill."
        )

    def _skills_pack_record_for_contract(self, contract: SkillContract) -> SkillsPackRecord | None:
        source = _ensure_dict(contract.get("source"))
        skills_pack_id = str(source.get("skills_pack_id") or "")
        if not skills_pack_id:
            return None
        try:
            return self.registry.inspect_skills_pack(skills_pack_id)
        except SkillInstallError:
            return SkillsPackRecord({
                "skills_pack_id": skills_pack_id,
                "name": str(source.get("skills_pack_name") or skills_pack_id),
                "source": str(source.get("source") or ""),
                "source_type": str(source.get("source_type") or ""),
                "installed_skills": [str(contract.get("skill_id", ""))],
                "failed_skills": [],
                "status": "unknown",
            })

    def _should_select(
        self,
        contract: SkillContract,
        *,
        task_text: str,
        matched_selector: bool,
        mode: SkillMode,
    ) -> bool:
        if matched_selector:
            return True
        if mode == "required":
            return False
        task_lower = task_text.lower()
        hints = _ensure_dict(contract.get("card", {}).get("activation_hints"))
        keywords = [str(item).lower() for item in _ensure_list(hints.get("keywords"))]
        names = [str(item).lower() for item in _ensure_list(hints.get("invocation_names"))]
        return any(keyword and keyword in task_lower for keyword in keywords) or any(
            name and (name in task_lower or f"${ name }" in task_lower) for name in names
        )

    def _to_selection(
        self,
        contract: SkillContract,
        *,
        scope: SkillScope,
        required: bool,
        selected_by: str,
    ) -> SkillPlanSelection:
        skill_id = str(contract.get("skill_id", ""))
        return SkillPlanSelection({
            "skill_id": skill_id,
            "skills_pack_id": str(contract.get("source", {}).get("skills_pack_id", "")),
            "skills_pack_name": str(contract.get("source", {}).get("skills_pack_name", "")),
            "version": str(contract.get("version", "")),
            "display_name": str(contract.get("card", {}).get("display_name", skill_id)),
            "scope": scope,
            "reason": "matched selector" if required else "matched skill card",
            "selected_by": selected_by,
            "required": required,
            "card": _copy_public(contract.get("card", {})),
            "stages": _copy_public(contract.get("declarative_stages", [])),
        })

    async def _apply_decision_handler(
        self,
        decision_handler: Callable[..., Any],
        *,
        plan: SkillExecutionPlan,
        context: dict[str, Any],
    ) -> SkillExecutionPlan:
        if asyncio.iscoroutinefunction(decision_handler):
            result = await decision_handler(_copy_public(plan), context)
        else:
            result = decision_handler(_copy_public(plan), context)
            if asyncio.iscoroutine(result):
                result = await result
        if result is False:
            plan["status"] = "rejected"
            plan["selected_skills"] = []
            return plan
        if isinstance(result, dict):
            merged = _copy_public(plan)
            merged.update(result)
            return SkillExecutionPlan(merged)
        return plan
