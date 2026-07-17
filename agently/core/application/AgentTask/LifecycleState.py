# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence


_CARRIER_KINDS = frozenset({"task_workspace_artifact", "inline_final_result"})
_CARRIER_STATUSES = frozenset({"proposed", "materialized", "accepted", "rejected"})
_REQUESTED_STRATEGIES = frozenset({"auto", "flat", "taskboard"})
_EFFECTIVE_STRATEGIES = frozenset({"flat", "taskboard"})
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _required_text(value: Any, *, field_name: str, max_chars: int = 192) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string.")
    if len(text) > max_chars:
        raise ValueError(f"{field_name} exceeds the private lifecycle identifier limit.")
    return text


def _positive_int(value: Any, *, field_name: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an integer.") from error
    minimum = 0 if allow_zero else 1
    if result < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field_name} must be {qualifier}.")
    return result


def _content_digest(value: Any) -> str:
    digest = str(value or "").strip()
    if len(digest) != 64 or any(character not in _HEX_DIGITS for character in digest):
        raise ValueError("content_digest must be a 64-character hexadecimal SHA-256 digest.")
    return digest.lower()


@dataclass(frozen=True, slots=True)
class TerminalCarrier:
    carrier_id: str
    kind: str
    required: bool
    content_version_id: str
    path: str
    content_digest: str
    source_work_result_id: str
    state_version: int
    status: str = "proposed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "carrier_id", _required_text(self.carrier_id, field_name="carrier_id"))
        kind = str(self.kind or "").strip()
        if kind not in _CARRIER_KINDS:
            raise ValueError(f"Unsupported terminal carrier kind: {kind or '<empty>'}.")
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.required, bool):
            raise ValueError("required must be a boolean.")
        content_version_id = _required_text(
            self.content_version_id,
            field_name="content_version_id",
        )
        object.__setattr__(self, "content_version_id", content_version_id)
        path = str(self.path or "").strip()
        if kind == "task_workspace_artifact" and not path:
            raise ValueError("TaskWorkspace terminal carriers require a path.")
        if kind == "inline_final_result" and path:
            raise ValueError("Inline terminal carriers cannot own a TaskWorkspace path.")
        if kind == "inline_final_result" and not content_version_id.startswith("inline:"):
            raise ValueError("Inline terminal carriers require an inline: content_version_id.")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "content_digest", _content_digest(self.content_digest))
        object.__setattr__(
            self,
            "source_work_result_id",
            _required_text(self.source_work_result_id, field_name="source_work_result_id"),
        )
        object.__setattr__(
            self,
            "state_version",
            _positive_int(self.state_version, field_name="state_version"),
        )
        status = str(self.status or "").strip()
        if status not in _CARRIER_STATUSES:
            raise ValueError(f"Unsupported terminal carrier status: {status or '<empty>'}.")
        object.__setattr__(self, "status", status)

    @classmethod
    def from_value(cls, value: Any, *, state_version: int) -> "TerminalCarrier":
        effective_state_version = _positive_int(state_version, field_name="state_version")
        if isinstance(value, cls):
            return replace(value, state_version=effective_state_version)
        if not isinstance(value, Mapping):
            raise ValueError("Terminal carriers must be mappings or TerminalCarrier records.")
        supplied_state_version = value.get("state_version")
        if supplied_state_version is not None and int(supplied_state_version) != effective_state_version:
            raise ValueError("Terminal carrier belongs to a stale AgentTask lifecycle version.")
        required = value.get("required")
        if not isinstance(required, bool):
            raise ValueError("required must be a boolean.")
        return cls(
            carrier_id=str(value.get("carrier_id") or ""),
            kind=str(value.get("kind") or ""),
            required=required,
            content_version_id=str(value.get("content_version_id") or ""),
            path=str(value.get("path") or ""),
            content_digest=str(value.get("content_digest") or ""),
            source_work_result_id=str(value.get("source_work_result_id") or ""),
            state_version=effective_state_version,
            status=str(value.get("status") or "proposed"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "carrier_id": self.carrier_id,
            "kind": self.kind,
            "required": self.required,
            "content_version_id": self.content_version_id,
            "path": self.path,
            "content_digest": self.content_digest,
            "source_work_result_id": self.source_work_result_id,
            "state_version": self.state_version,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class TerminalCarrierInventory:
    inventory_version: int
    state_version: int
    carriers: tuple[TerminalCarrier, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "inventory_version",
            _positive_int(
                self.inventory_version,
                field_name="inventory_version",
                allow_zero=True,
            ),
        )
        object.__setattr__(
            self,
            "state_version",
            _positive_int(self.state_version, field_name="state_version"),
        )
        carrier_ids: set[str] = set()
        for carrier in self.carriers:
            if carrier.state_version != self.state_version:
                raise ValueError("Terminal carrier inventory contains a stale carrier state_version.")
            if carrier.carrier_id in carrier_ids:
                raise ValueError(f"duplicate terminal carrier_id: {carrier.carrier_id}")
            carrier_ids.add(carrier.carrier_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TerminalCarrierInventory":
        state_version = _positive_int(value.get("state_version"), field_name="state_version")
        raw_carriers = value.get("carriers")
        carriers = (
            tuple(TerminalCarrier.from_value(item, state_version=state_version) for item in raw_carriers)
            if isinstance(raw_carriers, Sequence)
            and not isinstance(raw_carriers, str | bytes | bytearray)
            else ()
        )
        return cls(
            inventory_version=_positive_int(
                value.get("inventory_version", 0),
                field_name="inventory_version",
                allow_zero=True,
            ),
            state_version=state_version,
            carriers=carriers,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory_version": self.inventory_version,
            "state_version": self.state_version,
            "carriers": [carrier.to_dict() for carrier in self.carriers],
        }


@dataclass(slots=True)
class AgentTaskLifecycleState:
    task_id: str
    requested_strategy: str
    effective_strategy: str | None = None
    phase: str = "created"
    iteration: int = 0
    state_version: int = 1
    current_frame_id: str = ""
    current_plan_id: str = ""
    work_result_id: str = ""
    evidence_ref: str = ""
    evidence_version: int = 0
    skill_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    carrier_inventory: TerminalCarrierInventory | None = None
    active_issue: dict[str, Any] = field(default_factory=dict)
    repair_contract: dict[str, Any] = field(default_factory=dict)
    replan_signal: dict[str, Any] = field(default_factory=dict)
    terminal_decision: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.task_id = _required_text(self.task_id, field_name="task_id")
        self.requested_strategy = str(self.requested_strategy or "").strip()
        if self.requested_strategy not in _REQUESTED_STRATEGIES:
            raise ValueError(f"Unsupported requested AgentTask strategy: {self.requested_strategy}.")
        if self.effective_strategy is not None:
            self.effective_strategy = str(self.effective_strategy or "").strip()
            if self.effective_strategy not in _EFFECTIVE_STRATEGIES:
                raise ValueError(f"Unsupported effective AgentTask strategy: {self.effective_strategy}.")
        self.phase = _required_text(self.phase, field_name="phase")
        self.iteration = _positive_int(self.iteration, field_name="iteration", allow_zero=True)
        self.state_version = _positive_int(self.state_version, field_name="state_version")
        self.evidence_version = _positive_int(
            self.evidence_version,
            field_name="evidence_version",
            allow_zero=True,
        )
        normalized_skill_bindings: dict[str, dict[str, Any]] = {}
        for raw_binding_id, raw_binding in dict(self.skill_bindings).items():
            if not isinstance(raw_binding, Mapping):
                raise ValueError("AgentTask Skill bindings must be mappings.")
            binding_id = _required_text(
                raw_binding.get("binding_id") or raw_binding_id,
                field_name="skill_binding.binding_id",
            )
            if binding_id in normalized_skill_bindings:
                raise ValueError(f"duplicate AgentTask Skill binding id: {binding_id}")
            canonical_skill_id = _required_text(
                raw_binding.get("canonical_skill_id"),
                field_name="skill_binding.canonical_skill_id",
            )
            mode = str(raw_binding.get("mode") or "required").strip()
            if mode not in {"required", "model_decision"}:
                raise ValueError(f"Unsupported AgentTask Skill binding mode: {mode}.")
            contexts = raw_binding.get("contexts")
            normalized_contexts = (
                [deepcopy(dict(item)) for item in contexts if isinstance(item, Mapping)]
                if isinstance(contexts, Sequence) and not isinstance(contexts, str | bytes | bytearray)
                else []
            )
            normalized_skill_bindings[binding_id] = {
                "binding_id": binding_id,
                "canonical_skill_id": canonical_skill_id,
                "mode": mode,
                "resolved_source": str(raw_binding.get("resolved_source") or ""),
                "resolved_revision": str(raw_binding.get("resolved_revision") or ""),
                "source_subpath": str(raw_binding.get("source_subpath") or ""),
                "install_status": str(raw_binding.get("install_status") or "completed"),
                "activation_status": str(raw_binding.get("activation_status") or "bound"),
                "contexts": normalized_contexts,
            }
        self.skill_bindings = normalized_skill_bindings
        if self.carrier_inventory is None:
            self.carrier_inventory = TerminalCarrierInventory(
                inventory_version=0,
                state_version=self.state_version,
            )
        elif self.carrier_inventory.state_version > self.state_version:
            raise ValueError("Terminal carrier inventory is newer than AgentTask lifecycle state.")
        self.active_issue = deepcopy(dict(self.active_issue))
        self.repair_contract = deepcopy(dict(self.repair_contract))
        self.replan_signal = deepcopy(dict(self.replan_signal))
        self.terminal_decision = deepcopy(dict(self.terminal_decision))

    def skill_binding_id(self, canonical_skill_id: str) -> str:
        skill_id = str(canonical_skill_id or "").strip()
        for binding_id, binding in self.skill_bindings.items():
            if str(binding.get("canonical_skill_id") or "") == skill_id:
                return binding_id
        return ""

    def bind_skill(
        self,
        *,
        binding_id: str,
        canonical_skill_id: str,
        mode: str = "required",
        resolved_source: str = "",
        resolved_revision: str = "",
        source_subpath: str = "",
    ) -> str:
        normalized_binding_id = _required_text(
            binding_id,
            field_name="skill_binding.binding_id",
        )
        normalized_skill_id = _required_text(
            canonical_skill_id,
            field_name="skill_binding.canonical_skill_id",
        )
        normalized_mode = str(mode or "required").strip()
        if normalized_mode not in {"required", "model_decision"}:
            raise ValueError(f"Unsupported AgentTask Skill binding mode: {normalized_mode}.")
        existing_id = self.skill_binding_id(normalized_skill_id)
        if existing_id:
            return existing_id
        if normalized_binding_id in self.skill_bindings:
            raise ValueError(f"duplicate AgentTask Skill binding id: {normalized_binding_id}")
        self.skill_bindings[normalized_binding_id] = {
            "binding_id": normalized_binding_id,
            "canonical_skill_id": normalized_skill_id,
            "mode": normalized_mode,
            "resolved_source": str(resolved_source or ""),
            "resolved_revision": str(resolved_revision or ""),
            "source_subpath": str(source_subpath or ""),
            "install_status": "completed",
            "activation_status": "bound",
            "contexts": [],
        }
        return normalized_binding_id

    def record_skill_context_binding(
        self,
        *,
        request_id: str,
        phase: str,
        bindings: Sequence[Mapping[str, Any]],
    ) -> None:
        normalized_request_id = _required_text(
            request_id,
            field_name="skill_context.request_id",
        )
        normalized_phase = _required_text(
            phase,
            field_name="skill_context.phase",
        )
        for item in bindings:
            if not isinstance(item, Mapping):
                raise ValueError("Skill context bindings must be mappings.")
            binding_id = _required_text(
                item.get("binding_id"),
                field_name="skill_context.binding_id",
            )
            binding = self.skill_bindings.get(binding_id)
            if binding is None:
                raise ValueError(f"Skill context referenced unknown AgentTask binding id: {binding_id}")
            canonical_skill_id = _required_text(
                item.get("canonical_skill_id"),
                field_name="skill_context.canonical_skill_id",
            )
            if canonical_skill_id != binding["canonical_skill_id"]:
                raise ValueError("Skill context canonical id does not match its AgentTask binding.")
            context_record = {
                "request_id": normalized_request_id,
                "phase": normalized_phase,
                "guidance_chars": _positive_int(
                    item.get("guidance_chars", 0),
                    field_name="skill_context.guidance_chars",
                    allow_zero=True,
                ),
                "resource_chars": _positive_int(
                    item.get("resource_chars", 0),
                    field_name="skill_context.resource_chars",
                    allow_zero=True,
                ),
                "selected_resource_keys": (
                    [str(value) for value in item.get("selected_resource_keys", []) if str(value).strip()]
                    if isinstance(item.get("selected_resource_keys"), Sequence)
                    and not isinstance(
                        item.get("selected_resource_keys"),
                        str | bytes | bytearray,
                    )
                    else []
                ),
                "truncated": bool(item.get("truncated", False)),
            }
            contexts = binding.setdefault("contexts", [])
            existing_index = next(
                (index for index, current in enumerate(contexts) if current.get("request_id") == normalized_request_id),
                None,
            )
            if existing_index is None:
                contexts.append(context_record)
            else:
                contexts[existing_index] = context_record

    def require_version(self, expected_version: int) -> "AgentTaskLifecycleState":
        expected = _positive_int(expected_version, field_name="expected_version")
        if expected != self.state_version:
            raise ValueError(
                f"stale AgentTask lifecycle version: expected {expected}, current {self.state_version}."
            )
        return self

    def advance(
        self,
        phase: str,
        *,
        expected_version: int,
        iteration: int | None = None,
        effective_strategy: str | None = None,
        current_plan_id: str | None = None,
        work_result_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> int:
        self.require_version(expected_version)
        next_phase = _required_text(phase, field_name="phase")
        next_iteration = (
            self.iteration
            if iteration is None
            else _positive_int(iteration, field_name="iteration", allow_zero=True)
        )
        next_effective_strategy = self.effective_strategy
        if effective_strategy is not None:
            candidate_strategy = str(effective_strategy or "").strip()
            if candidate_strategy not in _EFFECTIVE_STRATEGIES:
                raise ValueError(f"Unsupported effective AgentTask strategy: {candidate_strategy}.")
            next_effective_strategy = candidate_strategy
        self.phase = next_phase
        self.iteration = next_iteration
        self.effective_strategy = next_effective_strategy
        if current_plan_id is not None:
            self.current_plan_id = _required_text(
                current_plan_id,
                field_name="current_plan_id",
            )
        if work_result_id is not None:
            self.work_result_id = _required_text(
                work_result_id,
                field_name="work_result_id",
            )
        if evidence_ref is not None:
            self.evidence_ref = _required_text(
                evidence_ref,
                field_name="evidence_ref",
            )
            self.evidence_version += 1
        self.state_version += 1
        return self.state_version

    def open_frame(
        self,
        frame_id: str,
        *,
        expected_version: int,
        iteration: int,
    ) -> int:
        self.require_version(expected_version)
        self.current_frame_id = _required_text(frame_id, field_name="frame_id")
        self.iteration = _positive_int(
            iteration,
            field_name="iteration",
            allow_zero=True,
        )
        self.phase = "lifecycle.start"
        self.state_version += 1
        return self.state_version

    def replace_carriers(
        self,
        carriers: Sequence[Mapping[str, Any] | TerminalCarrier],
        *,
        expected_version: int,
    ) -> TerminalCarrierInventory:
        self.require_version(expected_version)
        if isinstance(carriers, str | bytes | bytearray):
            raise ValueError("Terminal carriers must be a structured sequence.")
        next_state_version = self.state_version + 1
        next_carriers = tuple(
            TerminalCarrier.from_value(carrier, state_version=next_state_version)
            for carrier in carriers
        )
        carrier_ids: set[str] = set()
        for carrier in next_carriers:
            if carrier.carrier_id in carrier_ids:
                raise ValueError(f"duplicate terminal carrier_id: {carrier.carrier_id}")
            carrier_ids.add(carrier.carrier_id)
        current_inventory = self.carrier_inventory
        next_inventory = TerminalCarrierInventory(
            inventory_version=(current_inventory.inventory_version if current_inventory else 0) + 1,
            state_version=next_state_version,
            carriers=next_carriers,
        )
        self.state_version = next_state_version
        self.phase = "outputs.materialized"
        self.carrier_inventory = next_inventory
        return next_inventory

    def record_terminal_transition(
        self,
        transition: str,
        *,
        expected_version: int,
        accepted_carrier_ids: Sequence[str] = (),
        rejected_carrier_ids: Sequence[str] = (),
        issue: Mapping[str, Any] | None = None,
        repair_contract: Mapping[str, Any] | None = None,
    ) -> TerminalCarrierInventory:
        self.require_version(expected_version)
        transition_name = str(transition or "").strip()
        if transition_name not in {
            "accepted",
            "repair",
            "blocked",
            "continue",
            "verification_retry",
        }:
            raise ValueError(f"Unsupported AgentTask terminal transition: {transition_name}.")
        current_inventory = self.carrier_inventory
        if current_inventory is None:
            raise ValueError("AgentTask lifecycle state has no terminal carrier inventory.")
        offered_ids = {
            carrier.carrier_id for carrier in current_inventory.carriers
        }
        accepted_ids = {
            str(carrier_id or "").strip()
            for carrier_id in accepted_carrier_ids
            if str(carrier_id or "").strip()
        }
        rejected_ids = {
            str(carrier_id or "").strip()
            for carrier_id in rejected_carrier_ids
            if str(carrier_id or "").strip()
        }
        unknown_ids = (accepted_ids | rejected_ids) - offered_ids
        if unknown_ids:
            raise ValueError(
                "Terminal transition referenced unknown carrier ids: "
                + ", ".join(sorted(unknown_ids))
            )
        if accepted_ids.intersection(rejected_ids):
            raise ValueError("A terminal carrier cannot be both accepted and rejected.")
        if transition_name == "accepted" and accepted_ids != offered_ids:
            raise ValueError(
                "An accepted terminal transition must accept every current carrier."
            )
        if transition_name != "accepted" and accepted_ids:
            raise ValueError(
                "Only an accepted terminal transition may contain accepted carrier ids."
            )

        next_state_version = self.state_version + 1
        next_carriers = tuple(
            replace(
                carrier,
                state_version=next_state_version,
                status=(
                    "accepted"
                    if carrier.carrier_id in accepted_ids
                    else "rejected"
                    if carrier.carrier_id in rejected_ids
                    else "materialized"
                ),
            )
            for carrier in current_inventory.carriers
        )
        next_inventory = TerminalCarrierInventory(
            inventory_version=current_inventory.inventory_version + 1,
            state_version=next_state_version,
            carriers=next_carriers,
        )
        self.state_version = next_state_version
        self.phase = "transition.decide"
        self.carrier_inventory = next_inventory
        self.active_issue = deepcopy(dict(issue or {}))
        self.repair_contract = deepcopy(dict(repair_contract or {}))
        self.terminal_decision = {
            "transition": transition_name,
            "accepted_carrier_ids": sorted(accepted_ids),
            "rejected_carrier_ids": sorted(rejected_ids),
            "state_version": next_state_version,
        }
        return next_inventory

    def to_dict(self) -> dict[str, Any]:
        inventory = self.carrier_inventory
        return {
            "task_id": self.task_id,
            "requested_strategy": self.requested_strategy,
            "effective_strategy": self.effective_strategy,
            "phase": self.phase,
            "iteration": self.iteration,
            "state_version": self.state_version,
            "current_frame_id": self.current_frame_id,
            "current_plan_id": self.current_plan_id,
            "work_result_id": self.work_result_id,
            "evidence_ref": self.evidence_ref,
            "evidence_version": self.evidence_version,
            "skill_bindings": deepcopy(self.skill_bindings),
            "carrier_inventory": inventory.to_dict() if inventory is not None else None,
            "active_issue": deepcopy(self.active_issue),
            "repair_contract": deepcopy(self.repair_contract),
            "replan_signal": deepcopy(self.replan_signal),
            "terminal_decision": deepcopy(self.terminal_decision),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentTaskLifecycleState":
        if not isinstance(value, Mapping):
            raise ValueError("AgentTask lifecycle state must be a mapping.")
        raw_inventory = value.get("carrier_inventory")
        inventory = (
            TerminalCarrierInventory.from_dict(raw_inventory)
            if isinstance(raw_inventory, Mapping)
            else None
        )
        return cls(
            task_id=str(value.get("task_id") or ""),
            requested_strategy=str(value.get("requested_strategy") or ""),
            effective_strategy=(
                str(value.get("effective_strategy")) if value.get("effective_strategy") is not None else None
            ),
            phase=str(value.get("phase") or "created"),
            iteration=value.get("iteration", 0),
            state_version=value.get("state_version", 1),
            current_frame_id=str(value.get("current_frame_id") or ""),
            current_plan_id=str(value.get("current_plan_id") or ""),
            work_result_id=str(value.get("work_result_id") or ""),
            evidence_ref=str(value.get("evidence_ref") or ""),
            evidence_version=value.get("evidence_version", 0),
            skill_bindings=dict(value.get("skill_bindings") or {}),
            carrier_inventory=inventory,
            active_issue=dict(value.get("active_issue") or {}),
            repair_contract=dict(value.get("repair_contract") or {}),
            replan_signal=dict(value.get("replan_signal") or {}),
            terminal_decision=dict(value.get("terminal_decision") or {}),
        )


__all__: list[str] = []
