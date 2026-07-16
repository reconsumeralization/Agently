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

from .TaskShared import *


class AgentTaskTaskBoardScopedRetrievalMixin(AgentTaskMixinBase):
    def _taskboard_card_scoped_retrieval(self, card: Any) -> dict[str, Any]:
        for container in (
            getattr(card, "metadata", None),
            getattr(card, "evidence_contract", None),
        ):
            if not isinstance(container, Mapping):
                continue
            normalized = self._normalize_scoped_retrieval_plan(container.get("scoped_retrieval"))
            if normalized:
                return normalized
        return {}

    def _taskboard_card_carrier_plan(self, card: Any) -> dict[str, Any]:
        action_requirements = self._taskboard_card_action_requirements(card)
        required_action_ids = self._taskboard_card_required_action_ids(card)
        execution_shape = "actions" if required_action_ids else "taskboard_card"
        expected_evidence: list[Any] = [
            *list(getattr(card, "required_outputs", ()) or ()),
            *action_requirements,
        ]
        plan = {
            "execution_shape": execution_shape,
            "effective_execution_shape": execution_shape,
            "step_instruction": str(getattr(card, "objective", "") or ""),
            "expected_evidence": expected_evidence,
            "rationale": "Execute one TaskBoard card through the shared Block carrier.",
            "step_scope": {
                "allowed_capability_ids": required_action_ids,
            },
        }
        if required_action_ids:
            plan["required_action_ids"] = required_action_ids
        scoped_retrieval = self._taskboard_card_scoped_retrieval(card)
        if scoped_retrieval:
            plan["scoped_retrieval"] = scoped_retrieval
        return plan

    def _taskboard_card_required_action_ids(self, card: Any) -> list[str]:
        candidates = self._normalize_string_list(
            [
                requirement.get("capability_id")
                for requirement in self._taskboard_card_action_requirements(card)
            ]
        )
        for container in (
            getattr(card, "metadata", None),
            getattr(card, "evidence_contract", None),
        ):
            if isinstance(container, Mapping):
                candidates.extend(self._normalize_string_list(container.get("requires_capability_ids")))
        # Preserve the declared identity even when the capability is missing.
        # The execution owner must see that missing requirement and fail closed
        # instead of silently degrading the card to a generic carrier.
        return self._merge_string_lists(candidates)

    @classmethod
    def _taskboard_card_action_requirements(cls, card: Any) -> list[dict[str, Any]]:
        evidence_contract = getattr(card, "evidence_contract", None)
        requirements = cls._capability_evidence_requirements_from_mapping(
            evidence_contract if isinstance(evidence_contract, Mapping) else None
        )
        return cls._merge_capability_evidence_requirements(
            [
                requirement
                for requirement in requirements
                if requirement.get("required", True) is not False
                and str(requirement.get("kind") or "capability_used")
                == "action_succeeded"
                and str(requirement.get("capability_kind") or "action") == "action"
            ]
        )

    def _taskboard_card_payload_with_scoped_retrieval_results(
        self,
        card_input_payload: Mapping[str, Any],
        block_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(card_input_payload)
        scoped_results = self._scoped_retrieval_results_from_block_context(block_context)
        if scoped_results:
            payload["scoped_retrieval_results"] = DataFormatter.sanitize(scoped_results)
        return payload


__all__ = ["AgentTaskTaskBoardScopedRetrievalMixin"]
