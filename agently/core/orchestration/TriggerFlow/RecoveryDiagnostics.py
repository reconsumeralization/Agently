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

from typing import Any

from agently.types.trigger_flow import (
    TriggerFlowRecoveryDiagnostic,
    TriggerFlowRuntimeEventProjection,
)


def project_runtime_event_record(record: dict[str, Any]) -> TriggerFlowRuntimeEventProjection:
    return {
        "execution_id": str(record.get("execution_id", "")),
        "sequence": int(record.get("sequence", 0) or 0),
        "event_id": str(record.get("event_id", "")),
        "event_type": str(record.get("event_type", "")),
        "state_version": record.get("state_version"),
        "parent_event_id": record.get("parent_id"),
        "causation_id": record.get("causation_id"),
        "parent_signal_id": record.get("parent_signal_id"),
        "aggregation_scope": record.get("aggregation_scope"),
        "operator_id": record.get("operator_id"),
        "interrupt_id": record.get("interrupt_id"),
        "resume_request_id": record.get("resume_request_id"),
        "actor_id": record.get("actor_id"),
        "lease_owner_id": record.get("lease_owner_id"),
        "snapshot_ref": record.get("snapshot_ref"),
        "artifact_refs": list(record.get("artifact_refs", []) or []),
        "runtime_event": dict(record.get("event", {}) or {}),
    }


def diagnose_runtime_event_records(records: list[dict[str, Any]]) -> list[TriggerFlowRecoveryDiagnostic]:
    diagnostics: list[TriggerFlowRecoveryDiagnostic] = []
    normalized = sorted(records, key=lambda record: int(record.get("sequence", 0) or 0))
    diagnostics.extend(_sequence_diagnostics(normalized))
    diagnostics.extend(_parent_signal_cycle_diagnostics(normalized))
    return diagnostics


def _sequence_diagnostics(records: list[dict[str, Any]]) -> list[TriggerFlowRecoveryDiagnostic]:
    diagnostics: list[TriggerFlowRecoveryDiagnostic] = []
    seen_sequences: set[int] = set()
    expected = 1
    for record in records:
        sequence = int(record.get("sequence", 0) or 0)
        if sequence in seen_sequences:
            diagnostics.append(
                {
                    "code": "triggerflow.runtime_event.duplicate_sequence",
                    "severity": "error",
                    "message": "Durable RuntimeEvent sequence is duplicated.",
                    "execution_id": _string_or_none(record.get("execution_id")),
                    "sequence": sequence,
                    "event_id": _string_or_none(record.get("event_id")),
                    "details": {"sequence": sequence},
                }
            )
            continue
        seen_sequences.add(sequence)
        if sequence != expected:
            diagnostics.append(
                {
                    "code": "triggerflow.runtime_event.missing_sequence",
                    "severity": "error",
                    "message": "Durable RuntimeEvent sequence has a gap or out-of-order record.",
                    "execution_id": _string_or_none(record.get("execution_id")),
                    "expected_sequence": expected,
                    "actual_sequence": sequence,
                    "event_id": _string_or_none(record.get("event_id")),
                    "details": {"expected_sequence": expected, "actual_sequence": sequence},
                }
            )
            expected = sequence + 1
        else:
            expected += 1
    return diagnostics


def _parent_signal_cycle_diagnostics(records: list[dict[str, Any]]) -> list[TriggerFlowRecoveryDiagnostic]:
    by_signal_id: dict[str, dict[str, Any]] = {}
    parent_by_signal_id: dict[str, str] = {}
    for record in records:
        signal_id = _signal_id(record)
        parent_signal_id = _string_or_none(record.get("parent_signal_id"))
        if signal_id is None:
            continue
        by_signal_id[signal_id] = record
        if parent_signal_id is not None:
            parent_by_signal_id[signal_id] = parent_signal_id

    diagnostics: list[TriggerFlowRecoveryDiagnostic] = []
    emitted_cycles: set[tuple[str, ...]] = set()
    for signal_id in sorted(parent_by_signal_id):
        path: list[str] = []
        visiting: set[str] = set()
        current: str | None = signal_id
        while current is not None and current in parent_by_signal_id:
            if current in visiting:
                cycle_start = path.index(current)
                cycle = tuple(path[cycle_start:] + [current])
                canonical = tuple(sorted(set(cycle)))
                if canonical not in emitted_cycles:
                    emitted_cycles.add(canonical)
                    record = by_signal_id.get(current, {})
                    diagnostics.append(
                        {
                            "code": "triggerflow.runtime_event.parent_signal_cycle",
                            "severity": "error",
                            "message": "Durable RuntimeEvent parent signal lineage contains a cycle.",
                            "execution_id": _string_or_none(record.get("execution_id")),
                            "signal_id": current,
                            "parent_signal_id": _string_or_none(record.get("parent_signal_id")),
                            "event_id": _string_or_none(record.get("event_id")),
                            "details": {"cycle": list(cycle)},
                        }
                    )
                break
            visiting.add(current)
            path.append(current)
            parent = parent_by_signal_id.get(current)
            if parent not in by_signal_id:
                break
            current = parent
    return diagnostics


def _signal_id(record: dict[str, Any]) -> str | None:
    event = record.get("event", {})
    if not isinstance(event, dict):
        return None
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return None
    for key in ("SIGNAL_ID", "signal_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
