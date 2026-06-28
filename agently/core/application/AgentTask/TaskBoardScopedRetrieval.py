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
        plan = {
            "execution_shape": "taskboard_card",
            "effective_execution_shape": "taskboard_card",
            "step_instruction": str(getattr(card, "objective", "") or ""),
            "expected_evidence": list(getattr(card, "required_outputs", ()) or ()),
            "rationale": "Execute one TaskBoard card through the shared Block carrier.",
            "step_scope": {},
        }
        scoped_retrieval = self._taskboard_card_scoped_retrieval(card)
        if scoped_retrieval:
            plan["scoped_retrieval"] = scoped_retrieval
        return plan

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
