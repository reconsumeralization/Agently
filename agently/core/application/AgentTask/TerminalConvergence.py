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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agently.utils import DataFormatter


TERMINAL_CONVERGENCE_SCHEMA_VERSION = "agent_task_terminal_convergence/v1"
_RELEVANT_STATE_FIELDS = (
    "candidate_content_version_ids",
    "source_reference_targets",
    "capability_facts",
    "criterion_subjects",
    "output_subjects",
    "repair_contract",
)


@dataclass(frozen=True, slots=True)
class TerminalIssue:
    gate_kind: str
    issue_code: str
    contract_subject: str

    def __post_init__(self) -> None:
        for value in self.key:
            if not value.strip():
                raise ValueError("Terminal convergence issue keys cannot contain empty fields.")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.gate_kind, self.issue_code, self.contract_subject)


def relevant_state_digest(value: Mapping[str, Any]) -> str:
    relevant = {field: _canonical_state_value(value.get(field)) for field in _RELEVANT_STATE_FIELDS if field in value}
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TerminalConvergenceState:
    def __init__(self, task_id: str):
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("Terminal convergence state requires a task_id.")
        self.task_id = normalized_task_id
        self._records: dict[str, dict[str, Any]] = {}

    def record_detection(
        self,
        issue: TerminalIssue,
        state_digest: str,
        *,
        repair_contract: Mapping[str, Any],
        verifier_called: bool = True,
        unrecoverable: bool = False,
    ) -> dict[str, Any]:
        normalized_digest = str(state_digest or "").strip()
        if len(normalized_digest) != 64 or any(character not in "0123456789abcdef" for character in normalized_digest):
            raise ValueError("Terminal convergence requires a SHA-256 relevant-state digest.")
        record_key = self._record_key(issue)
        previous = self._records.get(record_key)
        if previous is not None and previous.get("terminal") is True:
            raise RuntimeError("Terminal convergence issue is already terminal; no further repair may be scheduled.")
        previous_digest = str(previous.get("last_state_digest") or "") if previous else ""
        occurrence = int(previous.get("occurrence") or 0) + 1 if previous else 1
        repair_count = int(previous.get("repair_count") or 0) if previous else 0
        terminal = bool(unrecoverable or occurrence >= 3)
        should_repair = not terminal and repair_count < 2
        if should_repair:
            repair_count += 1
        state_changed = previous_digest != normalized_digest
        skip_verifier = (not verifier_called) and (not state_changed)
        record = {
            "issue": {
                "gate_kind": issue.gate_kind,
                "issue_code": issue.issue_code,
                "contract_subject": issue.contract_subject,
            },
            "occurrence": occurrence,
            "repair_count": repair_count,
            "last_state_digest": normalized_digest,
            "repair_contract": DataFormatter.sanitize(dict(repair_contract)),
            "active": not terminal,
            "resolved": False,
            "terminal": terminal,
            "unrecoverable": bool(unrecoverable),
            "last_verifier_called": bool(verifier_called),
        }
        self._records[record_key] = record
        return {
            "occurrence": occurrence,
            "state_changed": state_changed,
            "verifier_called": bool(verifier_called),
            "should_repair": should_repair,
            "terminal": terminal,
            "skip_verifier": skip_verifier,
            "repair_count": repair_count,
        }

    def mark_resolved(self, issue: TerminalIssue) -> None:
        record = self._records.get(self._record_key(issue))
        if record is None:
            return
        record["active"] = False
        record["resolved"] = True

    def mark_all_resolved(self) -> None:
        for record in self._records.values():
            if record.get("terminal") is True:
                continue
            record["active"] = False
            record["resolved"] = True

    def active_records(self) -> list[dict[str, Any]]:
        return [
            dict(DataFormatter.sanitize(record))
            for record in self._records.values()
            if record.get("active") is True and record.get("terminal") is not True
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": TERMINAL_CONVERGENCE_SCHEMA_VERSION,
            "task_id": self.task_id,
            "records": DataFormatter.sanitize(self._records),
        }

    @classmethod
    def from_snapshot(cls, task_id: str, value: Mapping[str, Any]) -> "TerminalConvergenceState":
        if value.get("schema_version") != TERMINAL_CONVERGENCE_SCHEMA_VERSION:
            raise ValueError("Terminal convergence snapshot schema is unsupported.")
        if str(value.get("task_id") or "") != str(task_id):
            raise ValueError("Terminal convergence snapshot belongs to a different task.")
        records = value.get("records")
        if not isinstance(records, Mapping):
            raise ValueError("Terminal convergence snapshot records are invalid.")
        state = cls(task_id)
        for record_key, raw_record in records.items():
            if not isinstance(raw_record, Mapping):
                raise ValueError("Terminal convergence snapshot contains an invalid record.")
            record = dict(DataFormatter.sanitize(raw_record))
            issue_value = record.get("issue")
            if not isinstance(issue_value, Mapping):
                raise ValueError("Terminal convergence snapshot issue is invalid.")
            issue = TerminalIssue(
                str(issue_value.get("gate_kind") or ""),
                str(issue_value.get("issue_code") or ""),
                str(issue_value.get("contract_subject") or ""),
            )
            if str(record_key) != state._record_key(issue):
                raise ValueError("Terminal convergence snapshot issue key is invalid.")
            occurrence = record.get("occurrence")
            repair_count = record.get("repair_count")
            digest = str(record.get("last_state_digest") or "")
            if (
                isinstance(occurrence, bool)
                or not isinstance(occurrence, int)
                or occurrence < 1
                or occurrence > 3
                or isinstance(repair_count, bool)
                or not isinstance(repair_count, int)
                or repair_count < 0
                or repair_count > 2
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("Terminal convergence snapshot counters or digest are invalid.")
            state._records[str(record_key)] = record
        return state

    @staticmethod
    def _record_key(issue: TerminalIssue) -> str:
        return json.dumps(issue.key, ensure_ascii=False, separators=(",", ":"))

def _canonical_state_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_state_value(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        normalized = [_canonical_state_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str),
        )
    return DataFormatter.sanitize(value)


__all__ = [
    "TERMINAL_CONVERGENCE_SCHEMA_VERSION",
    "TerminalConvergenceState",
    "TerminalIssue",
    "relevant_state_digest",
]
