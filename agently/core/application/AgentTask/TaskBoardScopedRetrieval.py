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

import hashlib
import json

from .TaskShared import *


class AgentTaskTaskBoardScopedRetrievalMixin(AgentTaskMixinBase):
    _SCOPED_EVIDENCE_IDENTITY_FIELDS = (
        "execution_block_id",
        "block_id",
        "source_id",
        "source_revision",
        "source_ref",
        "binding_id",
    )
    _SCOPED_MODEL_PRIVATE_IDENTITY_FIELDS = (
        "execution_block_id",
        "block_id",
        "source_id",
        "binding_id",
    )

    @classmethod
    def _taskboard_scoped_retrieval_plan_digest(
        cls,
        scoped_retrieval: Mapping[str, Any] | None,
    ) -> str:
        """Return one stable host-owned identity for a normalized retrieval plan."""

        normalized = cls._normalize_scoped_retrieval_plan(scoped_retrieval)
        if not normalized:
            return ""
        encoded = json.dumps(
            DataFormatter.sanitize(normalized),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _scoped_evidence_identity_value(record: Mapping[str, Any], field: str) -> Any:
        value = record.get(field)
        if value not in (None, "", [], {}):
            return value
        provenance = record.get("provenance")
        if isinstance(provenance, Mapping):
            return provenance.get(field)
        return None

    @classmethod
    def _scoped_evidence_identity_key(
        cls,
        record: Mapping[str, Any],
        *,
        kind: str,
        query: str,
    ) -> tuple[str, ...] | None:
        identity = tuple(
            str(cls._scoped_evidence_identity_value(record, field) or "").strip()
            for field in cls._SCOPED_EVIDENCE_IDENTITY_FIELDS
        )
        if not query or any(not value for value in identity):
            return None
        range_start = cls._scoped_evidence_identity_value(record, "range_start")
        return (
            str(kind or "").strip(),
            query,
            *identity,
            str(range_start) if range_start is not None else "",
        )

    @classmethod
    def _scoped_evidence_scope_key(
        cls,
        record: Mapping[str, Any],
        *,
        query: str,
    ) -> tuple[str, ...] | None:
        key = cls._scoped_evidence_identity_key(
            record,
            kind="scoped_source",
            query=query,
        )
        return key[1:] if key is not None else None

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
        scoped_results = self._scoped_retrieval_results_from_block_context(
            block_context,
            include_host_identity=True,
        )
        current_ledger = self._evidence_ledger_from_block_context(
            block_context,
            include_host_identity=True,
        )
        current_items = current_ledger.get("items") if isinstance(current_ledger, Mapping) else None
        scoped_reference_ids: set[str] = set()
        content_covered_scopes: set[tuple[str, ...]] = set()
        current_item_list = (
            list(current_items)
            if isinstance(current_items, Sequence)
            and not isinstance(current_items, str | bytes | bytearray)
            else []
        )
        if scoped_results:
            reference_ids_by_identity: dict[tuple[str, ...], set[str]] = {}
            for item in current_item_list:
                if not isinstance(item, Mapping):
                    continue
                kind = str(item.get("kind") or "")
                if kind not in {"locator_ref", "evidence_snippet"}:
                    continue
                query = str(item.get("query") or "").strip()
                reference_id = str(item.get("reference_id") or "").strip()
                identity_key = self._scoped_evidence_identity_key(
                    item,
                    kind=kind,
                    query=query,
                )
                if identity_key is not None and reference_id:
                    reference_ids_by_identity.setdefault(identity_key, set()).add(
                        reference_id
                    )
            for result in scoped_results:
                query = str(result.get("query") or "").strip()
                raw_diagnostics = result.get("diagnostics")
                diagnostics = (
                    [dict(item) for item in raw_diagnostics if isinstance(item, Mapping)]
                    if isinstance(raw_diagnostics, Sequence)
                    and not isinstance(raw_diagnostics, str | bytes | bytearray)
                    else []
                )
                identity_join_failures = 0
                for collection_name, kind in (
                    ("locator_refs", "locator_ref"),
                    ("evidence_snippets", "evidence_snippet"),
                ):
                    records = result.get(collection_name)
                    if not isinstance(records, Sequence) or isinstance(
                        records,
                        str | bytes | bytearray,
                    ):
                        continue
                    joined_records: list[dict[str, Any]] = []
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        identity_key = self._scoped_evidence_identity_key(
                            record,
                            kind=kind,
                            query=query,
                        )
                        reference_ids = (
                            reference_ids_by_identity.get(identity_key, set())
                            if identity_key is not None
                            else set()
                        )
                        if len(reference_ids) != 1:
                            identity_join_failures += 1
                            diagnostics.append(
                                {
                                    "code": "agent_task.scoped_retrieval.evidence_identity_unresolved",
                                    "message": (
                                        "Excluded one scoped retrieval record because its canonical "
                                        "evidence identity did not join one-to-one."
                                    ),
                                    "evidence_role": kind,
                                    "reason": (
                                        "missing_source_identity"
                                        if identity_key is None
                                        else "missing_ledger_join"
                                        if not reference_ids
                                        else "ambiguous_ledger_join"
                                    ),
                                    "source_ref": str(record.get("source_ref") or ""),
                                }
                            )
                            continue
                        reference_id = next(iter(reference_ids))
                        record["reference_id"] = reference_id
                        scoped_reference_ids.add(reference_id)
                        joined_records.append(record)
                    result[collection_name] = joined_records
                content_scopes = {
                    scope_key
                    for record in result.get("evidence_snippets", [])
                    if isinstance(record, Mapping)
                    and record.get("content") not in (None, "", [], {})
                    and (
                        scope_key := self._scoped_evidence_scope_key(
                            record,
                            query=query,
                        )
                    )
                    is not None
                }
                content_covered_scopes.update(content_scopes)
                locators = result.get("locator_refs")
                if isinstance(locators, Sequence) and not isinstance(
                    locators,
                    str | bytes | bytearray,
                ):
                    result["locator_refs"] = [
                        locator
                        for locator in locators
                        if not isinstance(locator, Mapping)
                        or self._scoped_evidence_scope_key(
                            locator,
                            query=query,
                        )
                        not in content_scopes
                    ]
                for collection_name in ("locator_refs", "evidence_snippets"):
                    records = result.get(collection_name)
                    if not isinstance(records, Sequence) or isinstance(
                        records,
                        str | bytes | bytearray,
                    ):
                        continue
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        for field in self._SCOPED_MODEL_PRIVATE_IDENTITY_FIELDS:
                            record.pop(field, None)
                result["diagnostics"] = diagnostics
                bounded = result.get("bounded")
                if isinstance(bounded, dict):
                    bounded["model_visible_results"] = len(
                        result.get("locator_refs", [])
                    ) + len(result.get("evidence_snippets", []))
                    bounded["identity_join_failures"] = identity_join_failures
            payload["scoped_retrieval_results"] = DataFormatter.sanitize(
                scoped_results
            )
        if current_item_list:
            historical_ledger = payload.get("evidence_ledger")
            historical_items = (
                historical_ledger.get("items")
                if isinstance(historical_ledger, Mapping)
                else None
            )
            merged_items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for items, is_current in (
                (current_item_list, True),
                (historical_items, False),
            ):
                if not isinstance(items, Sequence) or isinstance(
                    items,
                    str | bytes | bytearray,
                ):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    if is_current:
                        kind = str(item.get("kind") or "")
                        query = str(item.get("query") or "").strip()
                        reference_id = str(item.get("reference_id") or "").strip()
                        if kind == "context_read.read":
                            continue
                        if scoped_results and kind in {"locator_ref", "evidence_snippet"} and (
                            reference_id not in scoped_reference_ids
                        ):
                            continue
                        item_scope = self._scoped_evidence_scope_key(
                            item,
                            query=query,
                        )
                        if kind == "locator_ref" and item_scope in content_covered_scopes:
                            continue
                    reference_id = str(item.get("reference_id") or "").strip()
                    if not reference_id or reference_id in seen:
                        continue
                    seen.add(reference_id)
                    projected_item = dict(DataFormatter.sanitize(item))
                    for field in self._SCOPED_MODEL_PRIVATE_IDENTITY_FIELDS:
                        projected_item.pop(field, None)
                    if reference_id in scoped_reference_ids:
                        projected_item.pop("body_preview", None)
                        projected_item["body_location"] = "scoped_retrieval_results"
                    merged_items.append(projected_item)
            merged_ledger = (
                dict(DataFormatter.sanitize(historical_ledger))
                if isinstance(historical_ledger, Mapping)
                else {}
            )
            merged_ledger.update(
                {
                    "items": merged_items,
                    "item_count": len(merged_items),
                    "omitted_count": max(
                        int(current_ledger.get("omitted_count") or 0)
                        + int(merged_ledger.get("omitted_count") or 0),
                        0,
                    ),
                    "selection_policy": (
                        "Use only an exact offered items[].reference_id in evidence_use.evidence_ids. "
                        "The host joins that task-scoped key to canonical evidence and provenance."
                    ),
                }
            )
            payload["evidence_ledger"] = merged_ledger
        return payload


__all__ = ["AgentTaskTaskBoardScopedRetrievalMixin"]
