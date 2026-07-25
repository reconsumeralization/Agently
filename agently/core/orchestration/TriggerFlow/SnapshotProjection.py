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
from typing import Any


TRIGGER_FLOW_VALUE_PROJECTION_KEY = "$triggerflow_projection"
TRIGGER_FLOW_VALUE_DIGEST_KIND = "value_digest"
TRIGGER_FLOW_VALUE_DIGEST_ALGORITHM = "sha256"
TRIGGER_FLOW_SNAPSHOT_PROJECTION_VERSION = 1

_TERMINAL_INTERRUPT_STATUSES = {"resumed", "cancelled"}
_TERMINAL_SIGNAL_ATTEMPT_STATUSES = {"completed", "failed", "interrupted"}


def canonical_snapshot_value_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def is_value_digest_projection(value: Any) -> bool:
    return bool(
        isinstance(value, dict) and value.get(TRIGGER_FLOW_VALUE_PROJECTION_KEY) == TRIGGER_FLOW_VALUE_DIGEST_KIND
    )


def validate_value_digest_projection(value: Any) -> dict[str, Any]:
    if not is_value_digest_projection(value):
        raise ValueError("TriggerFlow value is not a digest projection.")
    projection = dict(value)
    algorithm = projection.get("algorithm")
    digest = projection.get("sha256")
    serialized_bytes = projection.get("serialized_bytes")
    if algorithm != TRIGGER_FLOW_VALUE_DIGEST_ALGORITHM:
        raise ValueError("TriggerFlow value digest projection uses an unsupported algorithm.")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("TriggerFlow value digest projection has an invalid sha256.")
    if (
        not isinstance(serialized_bytes, int)
        or isinstance(serialized_bytes, bool)
        or serialized_bytes < 0
    ):
        raise ValueError("TriggerFlow value digest projection has an invalid serialized_bytes.")
    return projection


def value_matches_digest_projection(value: Any, projection: Any) -> bool:
    validated = validate_value_digest_projection(projection)
    encoded = canonical_snapshot_value_bytes(value)
    return bool(
        len(encoded) == validated["serialized_bytes"] and hashlib.sha256(encoded).hexdigest() == validated["sha256"]
    )


class TriggerFlowSnapshotProjector:
    """Project terminal execution history without mutating canonical live state."""

    def __init__(self, policy: dict[str, Any]):
        self._policy = dict(policy)
        self._projected_value_count = 0
        self._original_value_bytes = 0
        self._projected_value_bytes = 0

    def project(
        self,
        *,
        interrupts: dict[str, Any],
        signal_net: dict[str, Any],
        execution_idle: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        state = self._projection_state(applied=False)
        if not self._policy.get("enabled", False):
            state["deferred_reason"] = "disabled"
            return interrupts, signal_net, state
        if not execution_idle:
            state["deferred_reason"] = "execution_not_idle"
            return interrupts, signal_net, state

        terminal_interrupt_ids = [
            str(interrupt_id)
            for interrupt_id, interrupt in interrupts.items()
            if isinstance(interrupt, dict) and interrupt.get("status") in _TERMINAL_INTERRUPT_STATUSES
        ]
        for interrupt_id in terminal_interrupt_ids:
            interrupt = interrupts.get(interrupt_id)
            if isinstance(interrupt, dict):
                self._project_terminal_interrupt(interrupt)
        if self._policy.get("project_terminal_signal_attempts", True):
            self._project_terminal_signal_attempts(
                signal_net,
                terminal_interrupt_ids=set(terminal_interrupt_ids),
            )

        state = self._projection_state(applied=True)
        state["projected_terminal_interrupt_ids"] = terminal_interrupt_ids
        return interrupts, signal_net, state

    def _projection_state(self, *, applied: bool) -> dict[str, Any]:
        return {
            "version": TRIGGER_FLOW_SNAPSHOT_PROJECTION_VERSION,
            "policy": dict(self._policy),
            "applied": applied,
            "deferred_reason": None,
            "projected_terminal_interrupt_ids": [],
            "projected_value_count": self._projected_value_count,
            "original_value_bytes": self._original_value_bytes,
            "projected_value_bytes": self._projected_value_bytes,
        }

    def _project_terminal_interrupt(self, interrupt: dict[str, Any]) -> None:
        for key in ("payload", "response", "resume_value"):
            if key in interrupt and interrupt[key] is not None:
                interrupt[key] = self._project_value(interrupt[key])

        request = interrupt.get("external_wait_request")
        if isinstance(request, dict):
            audit_metadata = request.get("audit_metadata")
            if (
                isinstance(audit_metadata, dict)
                and "payload" in audit_metadata
                and audit_metadata["payload"] is not None
            ):
                audit_metadata["payload"] = self._project_value(audit_metadata["payload"])

        resume_requests = interrupt.get("resume_requests")
        if not isinstance(resume_requests, dict):
            return
        for request_record in resume_requests.values():
            if (
                isinstance(request_record, dict)
                and request_record.get("status") == "completed"
                and "value" in request_record
                and request_record["value"] is not None
            ):
                request_record["value"] = self._project_value(request_record["value"])

    def _project_terminal_signal_attempts(
        self,
        signal_net: dict[str, Any],
        *,
        terminal_interrupt_ids: set[str],
    ) -> None:
        attempts = signal_net.get("signal_attempts")
        if not isinstance(attempts, list):
            return
        for attempt in attempts:
            if not isinstance(attempt, dict) or attempt.get("status") not in _TERMINAL_SIGNAL_ATTEMPT_STATUSES:
                continue
            meta = attempt.get("meta")
            if not isinstance(meta, dict):
                continue
            resume = meta.get("resume")
            if not isinstance(resume, dict):
                continue
            interrupt_id = str(resume.get("interrupt_id") or meta.get("interrupt_id") or "")
            if interrupt_id not in terminal_interrupt_ids:
                continue
            interrupt = resume.get("interrupt")
            interrupt_summary: dict[str, Any] = {}
            if isinstance(interrupt, dict):
                for key in (
                    "id",
                    "status",
                    "resume_request_id",
                    "resumed_at",
                    "cancelled_at",
                    "source_execution_id",
                    "source_operator_id",
                ):
                    if interrupt.get(key) is not None:
                        interrupt_summary[key] = interrupt[key]
            meta["resume"] = {
                "interrupt_id": interrupt_id,
                "resume_request_id": meta.get("resume_request_id"),
                "actor_id": meta.get("actor_id"),
                "value": self._project_value(resume.get("value")),
                "interrupt": interrupt_summary,
            }

    def _project_value(self, value: Any) -> Any:
        if is_value_digest_projection(value):
            projection = validate_value_digest_projection(value)
            self._projected_value_count += 1
            self._original_value_bytes += projection["serialized_bytes"]
            self._projected_value_bytes += len(canonical_snapshot_value_bytes(projection))
            return value
        if self._policy.get("terminal_value_mode") != "digest":
            return value
        encoded = canonical_snapshot_value_bytes(value)
        minimum_bytes = int(self._policy.get("min_value_bytes", 0))
        if len(encoded) < minimum_bytes:
            return value
        projection = {
            TRIGGER_FLOW_VALUE_PROJECTION_KEY: TRIGGER_FLOW_VALUE_DIGEST_KIND,
            "algorithm": TRIGGER_FLOW_VALUE_DIGEST_ALGORITHM,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "serialized_bytes": len(encoded),
        }
        projected_bytes = len(canonical_snapshot_value_bytes(projection))
        self._projected_value_count += 1
        self._original_value_bytes += len(encoded)
        self._projected_value_bytes += projected_bytes
        return projection
