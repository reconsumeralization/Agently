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
    }


def refresh_diagnostics(owner: "AgentExecution"):
    context_diagnostics = owner.execution_context.diagnostics()
    budget = context_diagnostics.get("budget", {})
    limit_events = context_diagnostics.get("limit_events", [])
    owner.diagnostics["budget"] = budget
    owner.diagnostics["limit_events"] = limit_events
    for key in ("stages", "last_progress"):
        value = context_diagnostics.get(key)
        owner.diagnostics[key] = value or {}


def record_error_diagnostic(owner: "AgentExecution", error: BaseException):
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
    }
