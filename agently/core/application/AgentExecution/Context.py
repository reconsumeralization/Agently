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

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, TYPE_CHECKING, cast

from agently.types.data import (
    AgentExecutionLineage,
    AgentExecutionLimits,
)
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from agently.types.plugins import ExecutionExchangeProvider


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
        diagnostic_context: Mapping[str, Any] | None = None,
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
        self.diagnostic_context = dict(diagnostic_context or {})

    def to_diagnostic(self) -> dict[str, Any]:
        diagnostic = {
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
        if self.diagnostic_context:
            diagnostic["diagnostic_context"] = dict(self.diagnostic_context)
        return diagnostic


class _ModelRequestBudget:
    """One execution-local counter linked to every constraining ancestor."""

    def __init__(
        self,
        *,
        execution_id: str,
        limit: int | None,
        parent: "_ModelRequestBudget | None" = None,
    ) -> None:
        self.execution_id = execution_id
        self.limit = limit
        self.parent = parent
        self.count = 0
        self.limit_events: list[dict[str, Any]] = []
        self._lock = parent._lock if parent is not None else threading.RLock()

    def _lineage(self) -> list["_ModelRequestBudget"]:
        lineage: list[_ModelRequestBudget] = []
        current: _ModelRequestBudget | None = self
        while current is not None:
            lineage.append(current)
            current = current.parent
        return lineage

    def try_consume(
        self,
        *,
        response_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any] | None:
        lineage = self._lineage()
        with self._lock:
            # Ancestor authorization is checked first so an exhausted root
            # budget remains the canonical failure even when a child has the
            # same numeric limit.
            for budget in reversed(lineage):
                if budget.limit is None or budget.count < budget.limit:
                    continue
                event = {
                    "type": "limit_exceeded",
                    "limit_name": "max_model_requests",
                    "limit_value": budget.limit,
                    "used": budget.count,
                    "response_id": response_id,
                    "run_id": run_id,
                    "budget_execution_id": budget.execution_id,
                }
                budget.limit_events.append(event)
                return event
            for budget in lineage:
                budget.count += 1
        return None


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
        task_execution_strategy: str | None = None,
        effective_task_execution_strategy: str | None = None,
        strategy_context_source: str | None = None,
        task_workspace: Any | None = None,
        execution_exchange_provider: "ExecutionExchangeProvider | None" = None,
        parent_model_request_budget: _ModelRequestBudget | None = None,
    ):
        self.execution_id = execution_id
        self.lineage = cast(AgentExecutionLineage, dict(lineage))
        self.limits = cast(AgentExecutionLimits, dict(limits))
        raw_model_limit = self.limits.get("max_model_requests")
        self._model_request_budget = _ModelRequestBudget(
            execution_id=self.execution_id,
            limit=(int(raw_model_limit) if raw_model_limit is not None else None),
            parent=parent_model_request_budget,
        )
        self.limit_events: list[dict[str, Any]] = []
        self.started_at = time.monotonic()
        self.last_progress_at = self.started_at
        self.last_progress_event: dict[str, Any] | None = None
        self.stage_events: list[dict[str, Any]] = []
        self._progress_callback: Callable[[dict[str, Any]], Any] | None = None
        self._exchange_callback: Callable[[str, list[dict[str, Any]], dict[str, Any]], Any] | None = None
        self.action_scope: dict[str, Any] = {}
        self._skill_script_exec_authorizations: dict[str, dict[str, Any]] = {}
        self.action_artifact_recall_records: list[dict[str, Any]] = []
        from agently.core.runtime.RuntimeContext import get_current_agent_execution_context
        parent_context = get_current_agent_execution_context()
        self._parent_execution_context = (
            parent_context if isinstance(parent_context, AgentExecutionContext)
            and parent_context.execution_id != execution_id else None
        )
        self._dispatched_action_ids: set[str] = set()
        self._resource_handle_ids: set[str] = set()
        self._rework_blocked_action_ids: set[str] = set()
        self.action_records: list[dict[str, Any]] = []
        self._seen_action_record_keys: set[str] = set()
        # Depth of this AgentExecution in a nested agent-step chain (root = 0).
        self.nesting_depth = int(nesting_depth)
        # Effective max nesting depth inherited from the constraining ancestor
        # (or this execution's own limit). None means unbounded.
        self.nesting_budget = nesting_budget
        self.task_execution_strategy = _optional_str(task_execution_strategy)
        self.effective_task_execution_strategy = _optional_str(effective_task_execution_strategy)
        self.strategy_context_source = _optional_str(strategy_context_source)
        self.task_workspace = task_workspace
        self.execution_exchange_provider = execution_exchange_provider

    @property
    def model_request_budget(self) -> _ModelRequestBudget:
        return self._model_request_budget

    @property
    def model_request_count(self) -> int:
        return self._model_request_budget.count

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
        event = self._model_request_budget.try_consume(
            response_id=response_id,
            run_id=run_id,
        )
        if event is not None:
            if event.get("budget_execution_id") != self.execution_id:
                self.limit_events.append(dict(event))
            raise AgentExecutionLimitExceeded(
                (
                    "AgentExecution model request budget exceeded: "
                    f"max_model_requests={ event['limit_value'] }, used={ event['used'] }."
                ),
                limit_name="max_model_requests",
                limit_value=event["limit_value"],
                used=int(event["used"]),
            )

    def diagnostics(self) -> dict[str, Any]:
        last_progress = dict(self.last_progress_event or {})
        if self.last_progress_event is not None:
            last_progress["age_seconds"] = time.monotonic() - self.last_progress_at
        return {
            "budget": {
                "model_requests_used": self.model_request_count,
                "max_model_requests": self.limits.get("max_model_requests"),
            },
            "limit_events": [
                dict(item)
                for item in [
                    *self._model_request_budget.limit_events,
                    *self.limit_events,
                ]
            ],
            "action_scope": DataFormatter.sanitize(dict(self.action_scope)),
            "skill_script_exec": {
                "authorized_actions": [
                    {
                        "action_id": action_id,
                        "language": str(value.get("language") or ""),
                        "binding_ids": [
                            str(getattr(binding, "binding_id", ""))
                            for binding in value.get("bindings", ())
                        ],
                        "expected_outputs": list(value.get("expected_outputs", ())),
                    }
                    for action_id, value in self._skill_script_exec_authorizations.items()
                ]
            },
            "action_artifact_recall": {
                "record_count": len(self.action_artifact_recall_records),
                "artifact_ref_count": sum(
                    len(item.get("artifact_refs", []))
                    for item in self.action_artifact_recall_records
                    if isinstance(item.get("artifact_refs"), list)
                ),
            },
            "action_records": {
                "record_count": len(self.action_records),
            },
            "stages": {
                "events": [dict(item) for item in self.stage_events[-50:]],
            },
            "task_execution_strategy": {
                "requested": self.task_execution_strategy,
                "effective": self.effective_task_execution_strategy,
                "source": self.strategy_context_source,
            },
            "last_progress": last_progress,
        }

    def set_task_execution_strategy(
        self,
        *,
        requested: str | None,
        effective: str | None = None,
        source: str,
    ) -> None:
        self.task_execution_strategy = _optional_str(requested)
        self.effective_task_execution_strategy = _optional_str(effective)
        self.strategy_context_source = _optional_str(source)

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

    def set_skill_script_exec_authorization(
        self,
        action_id: str,
        *,
        language: str,
        bindings: tuple[Any, ...],
        expected_outputs: tuple[str, ...],
    ) -> None:
        """Bind one stable script Action to this execution's exact Skill scope."""

        normalized_action_id = str(action_id or "").strip()
        normalized_language = str(language or "").strip()
        if not normalized_action_id or not normalized_language:
            raise ValueError("Skill script authorization requires an Action id and language.")
        if not bindings:
            raise ValueError("Skill script authorization requires exact Skill bindings.")
        for binding in bindings:
            if str(getattr(binding, "task_id", "")) != self.execution_id:
                raise PermissionError("Skill binding belongs to another AgentExecution.")
        self._skill_script_exec_authorizations[normalized_action_id] = {
            "execution_id": self.execution_id,
            "language": normalized_language,
            "bindings": tuple(bindings),
            "expected_outputs": tuple(str(item) for item in expected_outputs),
        }

    def get_skill_script_exec_authorization(
        self,
        action_id: str,
    ) -> dict[str, Any] | None:
        value = self._skill_script_exec_authorizations.get(str(action_id or ""))
        if value is None:
            return None
        return {
            **value,
            "bindings": tuple(value.get("bindings", ())),
            "expected_outputs": tuple(value.get("expected_outputs", ())),
        }

    def clear_skill_script_exec_authorizations(
        self,
        action_ids: set[str] | tuple[str, ...] | list[str] | None = None,
    ) -> tuple[str, ...]:
        selected = (
            set(self._skill_script_exec_authorizations)
            if action_ids is None
            else {str(item) for item in action_ids}
        )
        removed: list[str] = []
        for action_id in tuple(self._skill_script_exec_authorizations):
            if action_id not in selected:
                continue
            self._skill_script_exec_authorizations.pop(action_id, None)
            removed.append(action_id)
        return tuple(removed)

    def set_action_artifact_recall_records(
        self,
        records: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
        *,
        source: str,
    ) -> None:
        normalized: list[dict[str, Any]] = []
        for item in records or []:
            if not isinstance(item, dict):
                continue
            refs = item.get("artifact_refs")
            if not isinstance(refs, list) or not refs:
                continue
            normalized.append(
                {
                    "action_id": str(item.get("action_id") or "upstream_action_artifact"),
                    "status": str(item.get("status") or "success"),
                    "artifact_refs": DataFormatter.sanitize(refs),
                    "source": source,
                }
            )
        self.action_artifact_recall_records = normalized

    def _check_action_replay(self, action_id: str, *, replay_safe: bool) -> None:
        context: AgentExecutionContext | None = self
        while context is not None:
            if action_id in context._rework_blocked_action_ids and not replay_safe:
                raise PermissionError(
                    f"Action {action_id!r} was previously dispatched and has no replay_safe authorization for rework."
                )
            context = context._parent_execution_context

    def _record_action_dispatch(self, action_id: str) -> None:
        context: AgentExecutionContext | None = self
        while context is not None:
            context._dispatched_action_ids.add(action_id)
            context = context._parent_execution_context

    def _record_resource_handles(self, handle_ids: list[str]) -> None:
        context: AgentExecutionContext | None = self
        while context is not None:
            context._resource_handle_ids.update(handle_ids)
            context = context._parent_execution_context

    def record_action_records(
        self,
        records: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
        *,
        source: str,
    ) -> None:
        for item in records or []:
            if not isinstance(item, dict):
                continue
            record = DataFormatter.sanitize(dict(item))
            if not isinstance(record, dict):
                continue
            record.setdefault("source", source)
            key = self._action_record_key(record)
            if key in self._seen_action_record_keys:
                continue
            self._seen_action_record_keys.add(key)
            self.action_records.append(record)

    @staticmethod
    def _action_record_key(record: dict[str, Any]) -> str:
        action_call_id = record.get("action_call_id")
        if action_call_id is not None:
            return f"call:{ action_call_id }"
        action_id = str(record.get("action_id") or record.get("tool_name") or "action")
        status = str(record.get("status") or "")
        command_index = record.get("command_index")
        round_index = record.get("round_index")
        if isinstance(command_index, int) and not isinstance(command_index, bool):
            return f"position:{ round_index }:{ command_index }:{ action_id }:{ status }"
        data = record.get("data") if record.get("data") is not None else record.get("result")
        digest = str(DataFormatter.sanitize(data))
        return f"{ action_id }:{ status }:{ hash(digest) }"

    def scoped_action_artifact_recall_records(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.action_artifact_recall_records]

    def record_progress(
        self,
        *,
        stage: str,
        status: str = "progress",
        event_type: str | None = None,
        run_id: str | None = None,
        response_id: str | None = None,
        meta: dict[str, Any] | None = None,
        notify: bool = True,
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
        if notify:
            self._notify_progress(event)

    def set_progress_callback(self, callback: Callable[[dict[str, Any]], Any] | None):
        self._progress_callback = callback

    def _notify_progress(self, event: dict[str, Any]):
        callback = self._progress_callback
        if callback is None:
            return
        try:
            result = callback(dict(event))
        except Exception:
            return
        if result is None:
            return
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            if hasattr(result, "__await__"):
                task = loop.create_task(result)
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        except Exception:
            return

    def set_exchange_callback(
        self,
        callback: Callable[[str, list[dict[str, Any]], dict[str, Any]], Any] | None,
    ):
        self._exchange_callback = callback

    def notify_exchange(
        self,
        action: str,
        exchanges: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ):
        """Project a human-exchange lifecycle moment onto the owning execution.

        ``action`` is ``"pending"`` or ``"resolved"``; ``exchanges`` carries the
        normalized ExecutionExchangeView list. Fired by execution-handle owners
        (ActionFlow) through the contextvar seam; a missing callback means no
        AgentExecution owns this run and the notification is dropped.
        """
        callback = self._exchange_callback
        if callback is None:
            return
        try:
            result = callback(str(action), list(exchanges), dict(meta or {}))
        except Exception:
            return
        if result is None:
            return
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            if hasattr(result, "__await__"):
                task = loop.create_task(result)
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        except Exception:
            return

    async def async_notify_exchange(
        self,
        action: str,
        exchanges: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ):
        """Awaited variant of :meth:`notify_exchange` for async emit ordering."""
        callback = self._exchange_callback
        if callback is None:
            return
        try:
            result = callback(str(action), list(exchanges), dict(meta or {}))
            if hasattr(result, "__await__"):
                await result
        except Exception:
            return

    def raise_if_limit_exceeded(self):
        limit_events = [
            *self._model_request_budget.limit_events,
            *self.limit_events,
        ]
        if not limit_events:
            return
        event = limit_events[-1]
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
