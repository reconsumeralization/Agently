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

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING

from .Definition import build_callable_ref, is_callable_ref_exportable, render_callable_ref
from .Signal import TriggerFlowSignal, TriggerFlowSignalType

if TYPE_CHECKING:
    from agently.types.trigger_flow import TriggerFlowAllHandlers, TriggerFlowHandler
    from .Execution import TriggerFlowExecution


SignalAttemptStatus = Literal["accepted", "running", "completed", "failed", "interrupted"]


@dataclass
class TriggerFlowDynamicSignalBinding:
    binding_id: str
    trigger_type: TriggerFlowSignalType
    trigger_event: str
    handler: "TriggerFlowHandler | None"
    handler_ref: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    status: Literal["active", "missing_handler"] = "active"

    def to_snapshot(self):
        return {
            "binding_id": self.binding_id,
            "trigger_type": self.trigger_type,
            "trigger_event": self.trigger_event,
            "handler_ref": copy.deepcopy(self.handler_ref),
            "metadata": copy.deepcopy(self.metadata),
            "status": self.status,
        }


class TriggerFlowSignalNet:
    """Execution-owned dynamic event overlay for TriggerFlow.

    The blueprint keeps static flow definition. SignalNet records execution-time
    dynamic event bindings and signal attempts so save/load can reconstruct the
    runtime overlay without mutating the definition fingerprint.
    """

    def __init__(self, execution: "TriggerFlowExecution[Any, Any, Any]"):
        self._execution = execution
        self._dynamic_bindings: dict[TriggerFlowSignalType, dict[str, dict[str, TriggerFlowDynamicSignalBinding]]] = {
            "event": {},
            "runtime_data": {},
            "flow_data": {},
        }
        self._handler_registry: dict[str, tuple["TriggerFlowHandler", dict[str, Any], dict[str, Any]]] = {}
        self._signal_attempts: dict[str, dict[str, Any]] = {}

    def register_dynamic_handler(
        self,
        trigger_event: str,
        handler: "TriggerFlowHandler",
        *,
        trigger_type: TriggerFlowSignalType = "event",
        binding_id: str | None = None,
        handler_ref: dict[str, Any] | str | None = None,
        metadata: dict[str, Any] | None = None,
        durable: bool = True,
    ):
        normalized_ref = self._normalize_handler_ref(handler, binding_id=binding_id, handler_ref=handler_ref)
        if durable and not is_callable_ref_exportable(normalized_ref):
            raise ValueError(
                "TriggerFlow dynamic event handler must be a recoverable handler ref; "
                f"got { render_callable_ref(normalized_ref) }."
            )
        resolved_binding_id = str(binding_id or normalized_ref.get("name") or render_callable_ref(normalized_ref))
        binding_metadata = dict(metadata or {})
        self._handler_registry[resolved_binding_id] = (handler, copy.deepcopy(normalized_ref), binding_metadata)
        binding = TriggerFlowDynamicSignalBinding(
            binding_id=resolved_binding_id,
            trigger_type=trigger_type,
            trigger_event=str(trigger_event),
            handler=handler,
            handler_ref=copy.deepcopy(normalized_ref),
            metadata=binding_metadata,
        )
        self._put_binding(binding)
        return resolved_binding_id

    def unregister_dynamic_handler(
        self,
        binding_id: str,
        *,
        trigger_type: TriggerFlowSignalType | None = None,
        trigger_event: str | None = None,
    ):
        removed = False
        target_types = (trigger_type,) if trigger_type is not None else ("event", "runtime_data", "flow_data")
        for current_type in target_types:
            events = self._dynamic_bindings[current_type]
            target_events = (str(trigger_event),) if trigger_event is not None else tuple(events.keys())
            for current_event in target_events:
                if current_event in events and binding_id in events[current_event]:
                    del events[current_event][binding_id]
                    removed = True
                if current_event in events and not events[current_event]:
                    del events[current_event]
        self._handler_registry.pop(binding_id, None)
        return removed

    def iter_handlers(
        self,
        signal: TriggerFlowSignal,
        static_handlers: "TriggerFlowAllHandlers",
    ):
        for handler_id, handler in static_handlers[signal.trigger_type].get(signal.trigger_event, {}).items():
            yield handler_id, handler
        for binding_id, binding in self._dynamic_bindings[signal.trigger_type].get(signal.trigger_event, {}).items():
            if binding.handler is not None and binding.status == "active":
                yield binding_id, binding.handler

    def accept_signal(self, signal: TriggerFlowSignal):
        self._execution._accepted_signal_ids.add(signal.id)
        self._record_attempt(signal, "accepted")

    def is_accepted(self, signal_id: str):
        return signal_id in self._execution._accepted_signal_ids

    def mark_running(self, signal: TriggerFlowSignal):
        self._execution._accepted_signal_ids.discard(signal.id)
        self._record_attempt(signal, "running")

    def mark_completed(self, signal: TriggerFlowSignal):
        self._record_attempt(signal, "completed")

    def mark_failed(self, signal: TriggerFlowSignal, error: BaseException):
        self._record_attempt(signal, "failed", error=error)

    def mark_interrupted(self, signal: TriggerFlowSignal, reason: str = "interrupted"):
        self._record_attempt(signal, "interrupted", reason=reason)

    def to_snapshot(self):
        return {
            "version": 1,
            "bindings": [
                binding.to_snapshot()
                for events in self._dynamic_bindings.values()
                for bindings in events.values()
                for binding in bindings.values()
            ],
            "accepted_signal_ids": sorted(self._execution._accepted_signal_ids),
            "signal_attempts": [
                copy.deepcopy(attempt)
                for attempt in sorted(
                    self._signal_attempts.values(),
                    key=lambda item: (float(item.get("updated_at") or 0), str(item.get("signal_id") or "")),
                )
            ],
        }

    def load_snapshot(self, snapshot: dict[str, Any] | None):
        self._dynamic_bindings = {
            "event": {},
            "runtime_data": {},
            "flow_data": {},
        }
        self._signal_attempts = {}
        if not isinstance(snapshot, dict):
            return
        for attempt in snapshot.get("signal_attempts") or []:
            if isinstance(attempt, dict) and attempt.get("signal_id"):
                restored = copy.deepcopy(attempt)
                if restored.get("status") in {"accepted", "running"}:
                    restored["status"] = "interrupted"
                    restored["reason"] = "interrupted during TriggerFlow load"
                self._signal_attempts[str(restored["signal_id"])] = restored
        self._execution._accepted_signal_ids = set()
        for binding_state in snapshot.get("bindings") or []:
            if not isinstance(binding_state, dict):
                continue
            binding_id = str(binding_state.get("binding_id") or "")
            trigger_type = binding_state.get("trigger_type", "event")
            if trigger_type not in {"event", "runtime_data", "flow_data"} or not binding_id:
                continue
            registered = self._handler_registry.get(binding_id)
            handler = registered[0] if registered is not None else None
            handler_ref = (
                copy.deepcopy(registered[1])
                if registered is not None
                else copy.deepcopy(binding_state.get("handler_ref") or {})
            )
            metadata = (
                copy.deepcopy(registered[2])
                if registered is not None
                else copy.deepcopy(binding_state.get("metadata") or {})
            )
            binding = TriggerFlowDynamicSignalBinding(
                binding_id=binding_id,
                trigger_type=trigger_type,
                trigger_event=str(binding_state.get("trigger_event") or ""),
                handler=handler,
                handler_ref=handler_ref,
                metadata=metadata,
                status="active" if handler is not None else "missing_handler",
            )
            self._put_binding(binding)

    def snapshot_missing_handler_ids(self, snapshot: dict[str, Any] | None):
        if not isinstance(snapshot, dict):
            return []
        missing: list[str] = []
        for binding_state in snapshot.get("bindings") or []:
            if not isinstance(binding_state, dict):
                continue
            binding_id = str(binding_state.get("binding_id") or "")
            if binding_id and binding_id not in self._handler_registry:
                missing.append(binding_id)
        return sorted(set(missing))

    def _normalize_handler_ref(
        self,
        handler: "TriggerFlowHandler",
        *,
        binding_id: str | None,
        handler_ref: dict[str, Any] | str | None,
    ):
        if isinstance(handler_ref, dict):
            return copy.deepcopy(handler_ref)
        explicit_name = str(handler_ref or binding_id) if (handler_ref or binding_id) is not None else None
        return build_callable_ref(handler, explicit_name=explicit_name)

    def _put_binding(self, binding: TriggerFlowDynamicSignalBinding):
        events = self._dynamic_bindings[binding.trigger_type]
        if binding.trigger_event not in events:
            events[binding.trigger_event] = {}
        events[binding.trigger_event][binding.binding_id] = binding

    def _record_attempt(
        self,
        signal: TriggerFlowSignal,
        status: SignalAttemptStatus,
        *,
        error: BaseException | None = None,
        reason: str | None = None,
    ):
        now = time.time()
        existing = self._signal_attempts.get(signal.id, {})
        attempt = {
            **existing,
            "signal_id": signal.id,
            "trigger_type": signal.trigger_type,
            "trigger_event": signal.trigger_event,
            "source": signal.source,
            "meta": copy.deepcopy(signal.meta),
            "status": status,
            "updated_at": now,
        }
        if "created_at" not in attempt:
            attempt["created_at"] = now
        if error is not None:
            attempt["error"] = f"{ type(error).__name__}: { error }"
        if reason is not None:
            attempt["reason"] = reason
        self._signal_attempts[signal.id] = attempt
