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

import time
from typing import Any, cast

from agently.types.data import (
    AgentExecutionLineage,
    AgentExecutionLimits,
)
from agently.utils import DataFormatter


class AgentExecutionLimitExceeded(RuntimeError):
    """Raised when a bounded AgentExecution exceeds its declared limits."""

    def __init__(self, message: str, *, limit_name: str, limit_value: Any, used: int):
        super().__init__(message)
        self.limit_name = limit_name
        self.limit_value = limit_value
        self.used = used

    def to_diagnostic(self) -> dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "message": str(self),
            "limit_name": self.limit_name,
            "limit_value": self.limit_value,
            "used": self.used,
        }


class RuntimeStageStallError(TimeoutError):
    """Raised when a runtime stage stalls or exceeds a hard deadline."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        status: str,
        response_id: str | None = None,
        run_id: str | None = None,
        agent_name: str | None = None,
        elapsed_seconds: float | None = None,
        idle_seconds: float | None = None,
        timeout_seconds: float | None = None,
        last_progress_event: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        planning_protocol: str | None = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.response_id = response_id
        self.run_id = run_id
        self.agent_name = agent_name
        self.elapsed_seconds = elapsed_seconds
        self.idle_seconds = idle_seconds
        self.timeout_seconds = timeout_seconds
        self.last_progress_event = last_progress_event
        self.provider = provider
        self.model = model
        self.planning_protocol = planning_protocol

    def to_diagnostic(self) -> dict[str, Any]:
        return {
            "error_type": self.__class__.__name__,
            "stage": self.stage,
            "status": self.status,
            "message": str(self),
            "response_id": self.response_id,
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "elapsed_seconds": self.elapsed_seconds,
            "idle_seconds": self.idle_seconds,
            "timeout_seconds": self.timeout_seconds,
            "last_progress_event": self.last_progress_event,
            "provider": self.provider,
            "model": self.model,
            "planning_protocol": self.planning_protocol,
        }


def normalize_execution_lineage(value: AgentExecutionLineage | dict[str, Any] | None = None) -> AgentExecutionLineage:
    source = dict(value or {})
    scope = source.get("scope")
    return {
        "task_id": _optional_str(source.get("task_id")),
        "iteration_id": _optional_str(source.get("iteration_id")),
        "step_id": _optional_str(source.get("step_id")),
        "parent_execution_id": _optional_str(source.get("parent_execution_id")),
        "scope": dict(scope) if isinstance(scope, dict) else {},
    }


def normalize_execution_limits(
    value: AgentExecutionLimits | dict[str, Any] | None = None,
) -> AgentExecutionLimits:
    source = dict(value or {})
    return {
        "allow_create_task": _bool(source.get("allow_create_task"), default=True),
        "max_model_requests": _normalize_limit_value(
            source.get("max_model_requests"),
            key="max_model_requests",
        ),
        "max_nested_agent_steps": _normalize_limit_value(
            source.get("max_nested_agent_steps"),
            key="max_nested_agent_steps",
        ),
        "max_seconds": _normalize_seconds_limit(source.get("max_seconds"), key="max_seconds"),
        "max_no_progress_seconds": _normalize_seconds_limit(
            source.get("max_no_progress_seconds"),
            key="max_no_progress_seconds",
        ),
    }


def merge_stream_meta(
    meta: dict[str, Any] | None,
    *,
    execution_id: str,
    lineage: AgentExecutionLineage,
) -> dict[str, Any]:
    merged = dict(meta or {})
    merged.setdefault("execution_id", execution_id)
    merged.setdefault("lineage", dict(lineage))
    return DataFormatter.sanitize(merged)


class AgentExecutionContext:
    """Execution-local budget and diagnostics state shared through contextvars."""

    def __init__(
        self,
        *,
        execution_id: str,
        lineage: AgentExecutionLineage | dict[str, Any],
        limits: AgentExecutionLimits | dict[str, Any],
        nesting_depth: int = 0,
        nesting_budget: int | None = None,
    ):
        self.execution_id = execution_id
        self.lineage = cast(AgentExecutionLineage, dict(lineage))
        self.limits = cast(AgentExecutionLimits, dict(limits))
        self.model_request_count = 0
        self.limit_events: list[dict[str, Any]] = []
        self.started_at = time.monotonic()
        self.last_progress_at = self.started_at
        self.last_progress_event: dict[str, Any] | None = None
        self.stage_events: list[dict[str, Any]] = []
        self.action_scope: dict[str, Any] = {}
        # Depth of this AgentExecution in a nested agent-step chain (root = 0).
        self.nesting_depth = int(nesting_depth)
        # Effective max nesting depth inherited from the constraining ancestor
        # (or this execution's own limit). None means unbounded.
        self.nesting_budget = nesting_budget

    def raise_if_nesting_exceeded(self):
        if self.nesting_budget is None:
            return
        if self.nesting_depth > self.nesting_budget:
            event = {
                "type": "limit_exceeded",
                "limit_name": "max_nested_agent_steps",
                "limit_value": self.nesting_budget,
                "used": self.nesting_depth,
            }
            self.limit_events.append(event)
            raise AgentExecutionLimitExceeded(
                (
                    "AgentExecution nested agent-step budget exceeded: "
                    f"max_nested_agent_steps={ self.nesting_budget }, depth={ self.nesting_depth }."
                ),
                limit_name="max_nested_agent_steps",
                limit_value=self.nesting_budget,
                used=self.nesting_depth,
            )

    def consume_model_request(self, *, response_id: str | None = None, run_id: str | None = None):
        limit = self.limits.get("max_model_requests")
        if limit is not None and self.model_request_count >= int(limit):
            event = {
                "type": "limit_exceeded",
                "limit_name": "max_model_requests",
                "limit_value": limit,
                "used": self.model_request_count,
                "response_id": response_id,
                "run_id": run_id,
            }
            self.limit_events.append(event)
            raise AgentExecutionLimitExceeded(
                (
                    "AgentExecution model request budget exceeded: "
                    f"max_model_requests={ limit }, used={ self.model_request_count }."
                ),
                limit_name="max_model_requests",
                limit_value=limit,
                used=self.model_request_count,
            )
        self.model_request_count += 1

    def diagnostics(self) -> dict[str, Any]:
        last_progress = dict(self.last_progress_event or {})
        if self.last_progress_event is not None:
            last_progress["age_seconds"] = time.monotonic() - self.last_progress_at
        return {
            "budget": {
                "model_requests_used": self.model_request_count,
                "max_model_requests": self.limits.get("max_model_requests"),
            },
            "limit_events": [dict(item) for item in self.limit_events],
            "action_scope": DataFormatter.sanitize(dict(self.action_scope)),
            "stages": {
                "events": [dict(item) for item in self.stage_events[-50:]],
            },
            "last_progress": last_progress,
        }

    def set_action_scope(
        self,
        allowed_action_ids: list[str] | tuple[str, ...] | set[str] | None,
        *,
        source: str,
    ) -> None:
        normalized: list[str] = []
        for item in allowed_action_ids or []:
            text = str(item or "").strip()
            if text and text not in normalized:
                normalized.append(text)
        if not normalized:
            self.action_scope = {}
            return
        self.action_scope = {
            "allowed_action_ids": normalized,
            "source": source,
        }

    def scoped_action_ids(self) -> set[str] | None:
        ids = self.action_scope.get("allowed_action_ids")
        if not isinstance(ids, list):
            return None
        normalized = {str(item).strip() for item in ids if str(item).strip()}
        return normalized or None

    def record_progress(
        self,
        *,
        stage: str,
        status: str = "progress",
        event_type: str | None = None,
        run_id: str | None = None,
        response_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ):
        now = time.monotonic()
        event = {
            "stage": stage,
            "status": status,
            "event_type": event_type,
            "run_id": run_id,
            "response_id": response_id,
            "monotonic_time": now,
            "meta": DataFormatter.sanitize(meta or {}),
        }
        self.last_progress_at = now
        self.last_progress_event = event
        self.stage_events.append(event)

    def raise_if_limit_exceeded(self):
        if not self.limit_events:
            return
        event = self.limit_events[-1]
        raw_used = event.get("used", 0)
        used = raw_used if isinstance(raw_used, int) else int(str(raw_used or 0))
        raise AgentExecutionLimitExceeded(
            (
                "AgentExecution model request budget exceeded: "
                f"max_model_requests={ event.get('limit_value') }, used={ event.get('used') }."
            ),
            limit_name=str(event.get("limit_name") or "max_model_requests"),
            limit_value=event.get("limit_value"),
            used=used,
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _normalize_limit_value(value: Any, *, key: str) -> int | None:
    if value is None:
        return None
    if value == -1 or value == "-1":
        return None
    if isinstance(value, bool):
        raise TypeError(f"AgentExecution limit '{ key }' must be an integer, None, or -1.")
    try:
        integer = int(value)
    except (TypeError, ValueError) as error:
        raise TypeError(f"AgentExecution limit '{ key }' must be an integer, None, or -1.") from error
    if integer < 0:
        raise ValueError(f"AgentExecution limit '{ key }' can not be negative except -1 for unlimited.")
    return integer


def _normalize_seconds_limit(value: Any, *, key: str) -> float | None:
    if value is None:
        return None
    if value == -1 or value == "-1":
        return None
    if isinstance(value, bool):
        raise TypeError(f"AgentExecution limit '{ key }' must be a number, None, or -1.")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise TypeError(f"AgentExecution limit '{ key }' must be a number, None, or -1.") from error
    if seconds < 0:
        raise ValueError(f"AgentExecution limit '{ key }' can not be negative except -1 for unlimited.")
    return seconds
