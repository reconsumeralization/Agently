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
from dataclasses import dataclass, field
from typing import Any

from agently.types.data import TaskBoardCard, TaskBoardGraph, TaskBoardRevision

from .TaskBoardValidation import TaskBoardValidator


_FORBIDDEN_PLANNING_KEYS = {
    "allowed_action",
    "allowed_actions",
    "allowed_action_ids",
    "action_allowlist",
    "action_options",
    "fixed_action_options",
    "mandatory_actions",
    "max_cards",
    "max_card_count",
    "max_model_calls",
    "max_model_requests",
    "max_nodes",
    "max_steps",
    "model_call_limit",
    "node_count",
    "required_action",
    "required_actions",
    "required_action_ids",
    "step_count",
}


@dataclass(frozen=True)
class TaskBoardEffortProfile:
    name: str
    complexity_definition: str
    orchestration_guidance: tuple[str, ...]
    reflection_density: str
    evidence_depth: str
    repair_tendency: str
    forbidden_constraints: tuple[str, ...] = field(default_factory=tuple)

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "complexity_definition": self.complexity_definition,
            "orchestration_guidance": list(self.orchestration_guidance),
            "reflection_density": self.reflection_density,
            "evidence_depth": self.evidence_depth,
            "repair_tendency": self.repair_tendency,
            "forbidden_constraints": list(self.forbidden_constraints),
        }


@dataclass(frozen=True)
class TaskBoardPlanningPolicy:
    effort_profile: TaskBoardEffortProfile
    action_block_meaning: str
    task_complexity_basis: tuple[str, ...]
    owner_boundaries: tuple[str, ...]
    control_card_guidance: tuple[str, ...]
    evidence_reuse_guidance: tuple[str, ...]
    repair_orchestration_guidance: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "effort_profile": self.effort_profile.to_prompt_payload(),
            "action_block_meaning": self.action_block_meaning,
            "task_complexity_basis": list(self.task_complexity_basis),
            "owner_boundaries": list(self.owner_boundaries),
            "control_card_guidance": list(self.control_card_guidance),
            "evidence_reuse_guidance": list(self.evidence_reuse_guidance),
            "repair_orchestration_guidance": list(self.repair_orchestration_guidance),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardPlanningResult:
    revision: TaskBoardRevision
    planning_policy: TaskBoardPlanningPolicy
    raw_result: Mapping[str, Any]
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision.to_dict(),
            "planning_policy": self.planning_policy.to_prompt_payload(),
            "raw_result": dict(self.raw_result),
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


def resolve_task_board_effort_profile(effort: Any = "medium") -> TaskBoardEffortProfile:
    name, overrides = _normalize_effort(effort)
    profile = _profile_presets().get(name) or _profile_presets()["medium"]
    if not overrides:
        return profile
    return TaskBoardEffortProfile(
        name=str(overrides.get("name") or profile.name),
        complexity_definition=str(overrides.get("complexity_definition") or profile.complexity_definition),
        orchestration_guidance=_str_tuple(overrides.get("orchestration_guidance")) or profile.orchestration_guidance,
        reflection_density=str(overrides.get("reflection_density") or profile.reflection_density),
        evidence_depth=str(overrides.get("evidence_depth") or profile.evidence_depth),
        repair_tendency=str(overrides.get("repair_tendency") or profile.repair_tendency),
        forbidden_constraints=profile.forbidden_constraints,
    )


def resolve_task_board_planning_policy(
    effort: Any = "medium",
    *,
    task_complexity_basis: Sequence[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TaskBoardPlanningPolicy:
    profile = resolve_task_board_effort_profile(effort)
    return TaskBoardPlanningPolicy(
        effort_profile=profile,
        action_block_meaning=(
            "An action block is a TaskBoard card-level unit of intended work. "
            "It may involve reading evidence, comparing facts, drafting, reviewing, repairing, "
            "deciding, waiting for input, finalizing, or other work that fits the task. "
            "These examples are vocabulary guidance only: they are not required actions, "
            "not an allowlist, not a target count, and not a scoring rule."
        ),
        task_complexity_basis=tuple(task_complexity_basis or _default_task_complexity_basis()),
        owner_boundaries=(
            "AgentExecution or an explicit caller decides whether the task enters TaskBoard.",
            "TaskBoard owns board-internal planning shape after submission.",
            "TaskBoard policy may shape orchestration complexity, reflection density, evidence depth, and repair tendency.",
            "TaskBoard policy must not grant permissions, hide capabilities, or define hard budgets.",
            "TriggerFlow owns framework-visible lifecycle; TaskBoard does not own ModelRequest or ActionRuntime.",
        ),
        control_card_guidance=(
            "Use allowed_execution_shape='control' for synthesis, verification, finalization, or board-continuation decisions that do not need tools.",
            "Use allowed_execution_shape='readback' when the card's only work is bounded cold artifact readback from existing dependency refs.",
            "A control card returns the deliverable or card-local synthesis plus sufficient, gaps, "
            "next_board_action, diagnostics, and optional patch_proposal in one structured payload. "
            "patch_proposal may be a TaskBoardPatch for board-state changes or a Workspace text patch "
            "for precise artifact repair.",
            "After evidence fan-in, prefer one terminal control card that combines synthesis, verification, and next-step decision instead of a serial chain of synthesis -> risk -> finalization -> review control cards.",
            "Create multiple dependent control cards only when each one produces a distinct user-visible artifact, waits for different upstream evidence, or represents a materially separate decision that cannot be verified in the same request.",
            "Do not use a downstream control card only to review, summarize, or repackage the immediately previous control card; put that review or finalization into the earlier control card fields.",
        ),
        evidence_reuse_guidance=(
            "Use existing TaskBoard card results, artifact refs, file refs, and scoped readback before planning or executing another evidence-gathering action.",
            "When dependency evidence already contains the needed facts or cold refs, prefer synthesis, comparison, or local repair over re-gathering the same external evidence.",
            "Re-gather evidence only when the current card objective requires fresh evidence, the existing evidence is missing, stale, contradictory, or readback diagnostics show it cannot be used.",
            "Make card completion conditions outcome-based. Do not make a provider, endpoint, file format, or auxiliary guidance source a hard dependency unless the user goal explicitly requires that exact source or artifact.",
            "Mark auxiliary evidence, style guidance, optional cross-checks, or replaceable source attempts as optional or degradable so downstream synthesis can continue with diagnostics when core evidence is sufficient.",
        ),
        repair_orchestration_guidance=(
            "When review localizes a defect, prefer the smallest repair that can fix the affected card output or artifact while preserving valid evidence.",
            "Do not turn a localized defect into broad repair work that repeats completed evidence-gathering cards.",
            "Use board results, diagnostics, and refs to carry localized repair context forward.",
            "When a card fails but enough alternative evidence exists, prefer a bounded partial result with explicit diagnostics over blocking the whole board.",
        ),
        metadata=dict(metadata or {}),
    )


def task_board_planning_output_schema() -> dict[str, Any]:
    return {
        "board_goal": (str, "Goal represented by the TaskBoard.", True),
        "cards": [
            {
                "id": (str, "Stable TaskBoard card id.", True),
                "action_block": (str, "Card-level work block in the model's own words.", True),
                "objective": (str, "Objective for this card.", True),
                "depends_on": ([str], "Upstream card ids this card depends on.", True),
                "evidence_to_use": ([str], "Evidence sources or upstream refs this card expects to use.", False),
                "done_when": (str, "Completion condition for this card.", True),
                "allowed_execution_shape": (
                    str,
                    "Optional handler-defined execution shape. Use control for synthesis/finalization/verification "
                    "cards that should run as one structured model request; use readback for scoped cold artifact "
                    "readback; use auto or actions for tool, Workspace, side-effect, or mixed action/readback cards. "
                    "Avoid serial control-only chains when one control card can synthesize, verify, and decide continuation.",
                    False,
                ),
                "failure_policy": (
                    str,
                    "required, optional, or degradable. Required failure blocks dependents. Optional/degradable failure may unblock dependents with diagnostics when enough core evidence exists.",
                    False,
                ),
                "scoped_retrieval": (
                    dict,
                    "Optional bounded retrieval plan: {query_groups: [{query, expected_role, search_surface?, path?, pattern?, filters?, max_results?, snippet_limit?}]}. query is content text or an exact phrase to search, not a list/read/search command. For workspace_files, path is the directory/file scope and pattern is one file glob such as *.md, * or **. Use filters.content_contains only for explicit content keyword lists. The executor returns factual locator_ref/evidence_snippet records only.",
                    False,
                ),
            }
        ],
        "reflection_points": ([str], "Review, correction, or decision points judged necessary by the model.", False),
        "completion_gate": (str, "How TaskBoard knows the submitted task is complete.", True),
        "why_this_effort_shape": (str, "Why this board shape matches the effort complexity definition.", True),
        "risk_notes": ([str], "Risks or limitations in this plan.", False),
    }


def coerce_task_board_planning_result(
    value: Mapping[str, Any],
    *,
    board_id: str,
    graph_id: str | None = None,
    effort: Any = "medium",
    planning_policy: TaskBoardPlanningPolicy | None = None,
    metadata: Mapping[str, Any] | None = None,
    validator: TaskBoardValidator | None = None,
) -> TaskBoardPlanningResult:
    if not isinstance(value, Mapping):
        raise TypeError(f"TaskBoard planning result must be a mapping, got: { type(value) }.")
    _reject_forbidden_planning_keys(value)
    policy = planning_policy or resolve_task_board_planning_policy(effort)
    raw_cards = value.get("cards")
    if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, str | bytes | bytearray):
        raise TypeError("TaskBoard planning result requires 'cards' as a sequence.")
    cards = tuple(_card_from_planning_item(item) for item in raw_cards)
    board_metadata = {
        "board_goal": value.get("board_goal"),
        "reflection_points": _str_list(value.get("reflection_points")),
        "completion_gate": value.get("completion_gate"),
        "why_this_effort_shape": value.get("why_this_effort_shape"),
        "risk_notes": _str_list(value.get("risk_notes")),
        "planning_policy": policy.to_prompt_payload(),
        **dict(metadata or {}),
    }
    revision = TaskBoardRevision.create(
        board_id=board_id,
        graph=TaskBoardGraph(
            graph_id=graph_id or f"{ board_id }.graph",
            cards=cards,
            metadata={"planning_policy": policy.to_prompt_payload()},
        ),
        metadata=board_metadata,
    )
    effective_validator = validator or TaskBoardValidator()
    effective_validator.validate(revision)
    return TaskBoardPlanningResult(
        revision=revision,
        planning_policy=policy,
        raw_result=dict(value),
        diagnostics=_mapping_tuple(value.get("diagnostics")),
    )


def _profile_presets() -> dict[str, TaskBoardEffortProfile]:
    forbidden = (
        "Do not use hard budgets as effort semantics.",
        "Do not use fixed card counts, node counts, model-call counts, or tool-call counts.",
        "Do not use mandatory action option sets or action allowlists as effort semantics.",
        "Do not grant permissions, hide configured capabilities, or change data visibility through effort.",
    )
    return {
        "low": TaskBoardEffortProfile(
            name="low",
            complexity_definition=(
                "Minimum viable orchestration: choose the simplest board shape that can complete the "
                "submitted task honestly. Necessary evidence, repair, and completion checks remain allowed."
            ),
            orchestration_guidance=(
                "Prefer compact card structure when the task risk allows it.",
                "Keep reflection focused on necessary completion and evidence checks.",
                "Escalate decomposition only when the task cannot be completed honestly without it.",
            ),
            reflection_density="minimum necessary",
            evidence_depth="enough to support the final claim without optional exploration",
            repair_tendency="local repair when a card fails or evidence contradicts the result",
            forbidden_constraints=forbidden,
        ),
        "medium": TaskBoardEffortProfile(
            name="medium",
            complexity_definition=(
                "Balanced orchestration: use enough decomposition and review density to reduce likely "
                "mistakes while avoiding exhaustive process."
            ),
            orchestration_guidance=(
                "Separate evidence gathering, synthesis, and final decision when that improves reliability.",
                "Use review points where dependency or evidence mistakes are likely.",
                "Keep repair opportunities available for failed or contradicted cards.",
            ),
            reflection_density="balanced",
            evidence_depth="cover primary sources and material dependencies",
            repair_tendency="repair or revise when evidence, dependency, or readback quality is insufficient",
            forbidden_constraints=forbidden,
        ),
        "high": TaskBoardEffortProfile(
            name="high",
            complexity_definition=(
                "Thorough orchestration: use deeper decomposition, evidence exploration, and reflection "
                "where risk, side effects, or artifact quality justify it, while avoiding symbolic process."
            ),
            orchestration_guidance=(
                "Make important evidence chains explicit.",
                "Add review or consistency cards where final quality depends on multiple sources or artifacts.",
                "Use repair and replan opportunities when direction, evidence, or produced artifacts are wrong.",
            ),
            reflection_density="thorough when it materially reduces risk",
            evidence_depth="deeper source, artifact, and readback evidence for risky or multi-capability work",
            repair_tendency="prefer explicit repair or board patch when progress is blocked, wrong, or under-evidenced",
            forbidden_constraints=forbidden,
        ),
    }


def _default_task_complexity_basis() -> tuple[str, ...]:
    return (
        "number and diversity of required capabilities",
        "dependency depth and fanout/fanin shape",
        "external evidence, file, or artifact requirements",
        "side-effect or approval risk",
        "need for readback, repair, replan, or final quality judgment",
    )


def _normalize_effort(effort: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(effort, Mapping):
        source = dict(effort)
        name = source.pop("name", source.pop("preset", source.pop("level", "medium")))
        return _effort_alias(str(name or "medium")), source
    return _effort_alias(str(effort or "medium")), {}


def _effort_alias(name: str) -> str:
    normalized = name.strip().lower() or "medium"
    if normalized in {"minimal", "fast"}:
        return "low"
    if normalized == "normal":
        return "medium"
    if normalized == "max":
        return "high"
    return normalized


def _card_from_planning_item(value: Any) -> TaskBoardCard:
    if not isinstance(value, Mapping):
        raise TypeError(f"TaskBoard planning card must be a mapping, got: { type(value) }.")
    card_id = str(value.get("id") or value.get("card_id") or "").strip()
    if not card_id:
        raise ValueError("TaskBoard planning card requires non-empty 'id'.")
    objective = str(value.get("objective") or value.get("goal") or "").strip()
    if not objective:
        raise ValueError(f"TaskBoard planning card '{ card_id }' requires non-empty objective.")
    evidence_to_use = _str_list(value.get("evidence_to_use"))
    action_block = str(value.get("action_block") or "").strip()
    done_when = str(value.get("done_when") or "").strip()
    failure_policy = _failure_policy(value.get("failure_policy"))
    scoped_retrieval = value.get("scoped_retrieval")
    metadata: dict[str, Any] = {
        "action_block": action_block,
        "done_when": done_when,
        "failure_policy": failure_policy,
    }
    evidence_contract: dict[str, Any] = {
        "action_block": action_block,
        "evidence_to_use": evidence_to_use,
        "done_when": done_when,
        "failure_policy": failure_policy,
    }
    if isinstance(scoped_retrieval, Mapping):
        evidence_contract["scoped_retrieval"] = dict(scoped_retrieval)
        metadata["scoped_retrieval"] = dict(scoped_retrieval)
    return TaskBoardCard(
        id=card_id,
        objective=objective,
        depends_on=tuple(_str_list(value.get("depends_on"))),
        input_refs=tuple(evidence_to_use),
        required_outputs=(done_when,) if done_when else (),
        allowed_execution_shape=str(value.get("allowed_execution_shape") or "auto"),
        evidence_contract=evidence_contract,
        failure_policy=failure_policy,
        metadata=metadata,
    )


def _reject_forbidden_planning_keys(value: Any, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower()
            if normalized in _FORBIDDEN_PLANNING_KEYS:
                location = f"{ path }.{ key_text }" if path else key_text
                raise ValueError(f"TaskBoard planning payload contains forbidden effort-control key: { location }.")
            next_path = f"{ path }.{ key_text }" if path else key_text
            _reject_forbidden_planning_keys(item, path=next_path)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            _reject_forbidden_planning_keys(item, path=f"{ path }[{ index }]")


def _str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(_str_list(value))


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                result.append(text)
                seen.add(text)
        return result
    text = str(value).strip()
    return [text] if text else []


def _failure_policy(value: Any) -> str:
    text = str(value or "required").strip().lower().replace("-", "_")
    aliases = {
        "must": "required",
        "mandatory": "required",
        "critical": "required",
        "nice_to_have": "optional",
        "best_effort": "optional",
        "non_blocking": "optional",
        "nonblocking": "optional",
        "soft": "degradable",
        "fallback": "degradable",
        "degrade": "degradable",
    }
    normalized = aliases.get(text, text)
    if normalized not in {"required", "optional", "degradable"}:
        return "required"
    return normalized


def _mapping_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (dict(value),)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(dict(item) if isinstance(item, Mapping) else {"value": item} for item in value)
    return ({"value": value},)
