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

from typing import Any, TYPE_CHECKING

from agently.core.application.AgentExecution import AgentExecutionLimitExceeded, RuntimeStageStallError
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


def initial_diagnostics() -> dict[str, Any]:
    return {
        "budget": {},
        "limit_events": [],
        "errors": [],
        "stalls": [],
        "timeouts": [],
        "stages": {},
        "last_progress": {},
    }


def initial_workspace_refs() -> dict[str, Any]:
    return {
        "observations": [],
        "artifacts": [],
        "decisions": [],
        "checkpoints": [],
        "verification_evidence": [],
        "guidance": [],
    }


def refresh_diagnostics(owner: "AgentExecution"):
    context_diagnostics = owner.execution_context.diagnostics()
    budget = context_diagnostics.get("budget", {})
    limit_events = context_diagnostics.get("limit_events", [])
    _merge_context_action_records(owner)
    owner.diagnostics["budget"] = budget
    owner.diagnostics["limit_events"] = limit_events
    for key in ("stages", "last_progress", "action_records"):
        value = context_diagnostics.get(key)
        owner.diagnostics[key] = value or {}


def record_error_diagnostic(owner: "AgentExecution", error: BaseException):
    _merge_context_action_records(owner)
    errors = owner.diagnostics.setdefault("errors", [])
    if isinstance(errors, list):
        item = (
            error.to_diagnostic()
            if isinstance(error, (AgentExecutionLimitExceeded, RuntimeStageStallError))
            else {"type": error.__class__.__name__, "message": str(error)}
        )
        errors.append(item)
        if isinstance(error, RuntimeStageStallError):
            target_key = "timeouts" if error.status == "timed_out" else "stalls"
            target = owner.diagnostics.setdefault(target_key, [])
            if isinstance(target, list):
                target.append(item)


def _merge_context_action_records(owner: "AgentExecution") -> None:
    records = getattr(owner.execution_context, "action_records", [])
    if not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, dict):
            continue
        normalized = _normalize_context_action_record(record)
        key = _action_log_key(normalized)
        if key in owner._seen_action_log_keys:
            continue
        owner._seen_action_log_keys.add(key)
        action_logs = owner.logs.setdefault("action_logs", [])
        if isinstance(action_logs, list):
            action_logs.append(normalized)
        artifact_refs = normalized.get("artifact_refs", [])
        if not isinstance(artifact_refs, list):
            continue
        aggregated_artifact_refs = owner.logs.setdefault("artifact_refs", [])
        if isinstance(aggregated_artifact_refs, list):
            for ref in artifact_refs:
                if ref not in aggregated_artifact_refs:
                    aggregated_artifact_refs.append(DataFormatter.sanitize(ref))


def _normalize_context_action_record(record: dict[str, Any]) -> dict[str, Any]:
    raw_model_digest = record.get("model_digest")
    model_digest: dict[str, Any] = raw_model_digest if isinstance(raw_model_digest, dict) else {}
    action_id = str(record.get("action_id") or record.get("tool_name") or model_digest.get("action_id") or "action")
    action_call_id = record.get("action_call_id") or model_digest.get("action_call_id")
    status = str(record.get("status") or model_digest.get("status") or "")
    artifact_refs = record.get("artifact_refs") or model_digest.get("artifact_refs") or []
    if not isinstance(artifact_refs, list):
        artifact_refs = []
    data = record.get("data")
    if data is None:
        data = record.get("result")
    return DataFormatter.sanitize(
        {
            "action_call_id": action_call_id,
            "action_id": action_id,
            "status": status,
            "success": record.get("success") if "success" in record else model_digest.get("success"),
            "source": record.get("source", "ActionFlow"),
            "route": record.get("route", "model_request"),
            "data": data if isinstance(data, dict) else {},
            "model_digest": model_digest,
            "artifact_refs": artifact_refs,
            "raw": record,
        }
    )


def _action_log_key(log: dict[str, Any]) -> str:
    action_call_id = log.get("action_call_id")
    if action_call_id:
        return str(action_call_id)
    action_id = str(log.get("action_id") or "action")
    status = str(log.get("status") or "")
    digest = str(DataFormatter.sanitize(log.get("data") if log.get("data") is not None else log.get("result")))
    return f"{ action_id }:{ status }:{ hash(digest) }"


def build_execution_meta(owner: "AgentExecution") -> dict[str, Any]:
    return {
        "execution_id": owner.id,
        "status": owner.status,
        "strategy": owner.strategy_name,
        "goals": DataFormatter.sanitize(owner.goal_items),
        "success_criteria": DataFormatter.sanitize(owner.success_criteria_items),
        "generated_success_criteria": DataFormatter.sanitize(owner.generated_success_criteria),
        "task_refs": DataFormatter.sanitize(owner.task_refs),
        "lineage": DataFormatter.sanitize(owner.lineage),
        "limits": DataFormatter.sanitize(owner.limits),
        "options": DataFormatter.sanitize(owner.options),
        "effective_options": DataFormatter.sanitize(owner.effective_options),
        "consumed_options": DataFormatter.sanitize(owner.consumed_options),
        "route_plan": DataFormatter.sanitize(owner.route_plan),
        "route": DataFormatter.sanitize(owner.route_info),
        "close_snapshot": DataFormatter.sanitize(owner.close_snapshot),
        "logs": DataFormatter.sanitize(owner.logs),
        "diagnostics": DataFormatter.sanitize(owner.diagnostics),
        "workspace_refs": DataFormatter.sanitize(owner.workspace_refs),
        "guidance_items": DataFormatter.sanitize(getattr(owner, "guidance_items", [])),
    }
