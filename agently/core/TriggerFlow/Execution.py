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


import uuid
import asyncio
import warnings
import json
import yaml
import time
from pathlib import Path
from json import JSONDecodeError
from contextvars import ContextVar

from typing import Any, Literal, TYPE_CHECKING, overload, AsyncGenerator, Generator, Generic, TypeVar, cast

if TYPE_CHECKING:
    from .TriggerFlow import TriggerFlow
    from agently.types.trigger_flow import TriggerFlowAllHandlers
    from agently.types.data import RunContext, SerializableValue

from agently.utils import StateData, FunctionShifter, GeneratorConsumer, Settings
from agently.core.RuntimeContext import bind_runtime_context, get_current_chunk_run_context
from agently.types.trigger_flow import (
    TriggerFlowContractMetadata,
    TriggerFlowContractSpec,
    TriggerFlowInterruptEvent,
    TriggerFlowRuntimeData,
    RUNTIME_STREAM_STOP,
)
from agently.types.data import EMPTY, RunContext
from .Control import (
    TriggerFlowPauseSignal,
    TRIGGER_FLOW_STATUS_CANCELLED,
    TRIGGER_FLOW_STATUS_COMPLETED,
    TRIGGER_FLOW_STATUS_CREATED,
    TRIGGER_FLOW_STATUS_FAILED,
    TRIGGER_FLOW_STATUS_RUNNING,
    TRIGGER_FLOW_STATUS_WAITING,
    TRIGGER_FLOW_LIFECYCLE_CLOSED,
    TRIGGER_FLOW_LIFECYCLE_OPEN,
    TRIGGER_FLOW_LIFECYCLE_SEALED,
)
from .Signal import TriggerFlowSignal, TriggerFlowSignalType

InputT = TypeVar("InputT")
StreamT = TypeVar("StreamT")
ResultT = TypeVar("ResultT")
COMPAT_FINAL_RESULT_KEY = "$final_result"


class TriggerFlowExecution(Generic[InputT, StreamT, ResultT]):
    def __init__(
        self,
        *,
        handlers: "TriggerFlowAllHandlers",
        trigger_flow: "TriggerFlow[InputT, StreamT, ResultT]",
        id: str | None = None,
        skip_exceptions: bool = False,
        concurrency: int | None = None,
        run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 10.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ):
        # Basic Attributions
        self.id = id if id is not None else uuid.uuid4().hex
        self._handlers = handlers
        self._trigger_flow = trigger_flow
        self._runtime_data = StateData()
        self._runtime_resources = StateData(
            name=f"TriggerFlowExecution-{ self.id }-RuntimeResources",
            parent=self._trigger_flow._runtime_resources,
        )
        self._system_runtime_data = StateData()
        self._skip_exceptions = skip_exceptions
        self._concurrency_semaphore = asyncio.Semaphore(concurrency) if concurrency and concurrency > 0 else None
        self._concurrency_depth = ContextVar(
            f"trigger_flow_execution_concurrency_depth_{ self.id }",
            default=0,
        )
        self.run_context = (
            run_context
            if run_context is not None
            else RunContext.create(
                run_kind="workflow_execution",
                execution_id=self.id,
                meta={"flow_name": self._trigger_flow.name},
            )
        )
        self._runtime_started_emitted = False
        self._runtime_completed_emitted = False
        self._runtime_failed_emitted = False
        self._runtime_result_set_emitted = False
        self._runtime_definition_emitted = False
        self._auto_close = bool(auto_close)
        self._auto_close_timeout = auto_close_timeout
        self._lifecycle_state = TRIGGER_FLOW_LIFECYCLE_OPEN
        self._created_at = time.time()
        self._started_at: float | None = None
        self._last_activity_at: float | None = None
        self._sealed_at: float | None = None
        self._closed_at: float | None = None
        self._close_reason: str | None = None
        self._state_version = 0
        self._owner_id = owner_id
        self._lease_ttl = lease_ttl
        self._heartbeat_at: float | None = None
        self._lease_until: float | None = self._created_at + lease_ttl if lease_ttl is not None else None
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._task_origins: dict[asyncio.Task[Any], str] = {}
        self._accepted_signal_ids: set[str] = set()
        self._active_handler_count = 0
        self._auto_close_task: asyncio.Task[Any] | None = None
        self._close_started = False
        self._close_result: Any = None
        self._closed_event = asyncio.Event()
        self._runtime_stream_stopped = False

        # Settings
        self.settings = Settings(
            parent=self._trigger_flow.settings,
            name=f"TriggerFlowExecution-{ self.id }-Settings",
        )
        self.set_settings = self.settings.set_settings
        self.load_settings = self.settings.load

        # Emit
        self.emit = FunctionShifter.syncify(self.async_emit)
        self.emit_nowait = self._emit_nowait

        # Flow Data
        self._get_flow_data = self._trigger_flow._get_flow_data
        self._set_flow_data = self._trigger_flow._set_flow_data
        self._append_flow_data = self._trigger_flow._append_flow_data
        self._del_flow_data = self._trigger_flow._del_flow_data
        self._async_set_flow_data = self._trigger_flow._async_set_flow_data
        self._async_append_flow_data = self._trigger_flow._async_append_flow_data
        self._async_del_flow_data = self._trigger_flow._async_del_flow_data
        self.get_flow_data = self._trigger_flow.get_flow_data
        self.set_flow_data = self._trigger_flow.set_flow_data
        self.async_set_flow_data = self._trigger_flow.async_set_flow_data
        self.append_flow_data = self._trigger_flow.append_flow_data
        self.async_append_flow_data = self._trigger_flow.async_append_flow_data
        self.del_flow_data = self._trigger_flow.del_flow_data
        self.async_del_flow_data = self._trigger_flow.async_del_flow_data

        # Runtime Data
        self.get_state = self._get_state
        self.set_state = FunctionShifter.syncify(self.async_set_state)
        self.append_state = FunctionShifter.syncify(self.async_append_state)
        self.del_state = FunctionShifter.syncify(self.async_del_state)
        self.get_runtime_data = self._deprecated_get_runtime_data
        self.set_runtime_data = FunctionShifter.syncify(self.async_set_runtime_data)
        self.append_runtime_data = FunctionShifter.syncify(self.async_append_runtime_data)
        self.del_runtime_data = FunctionShifter.syncify(self.async_del_runtime_data)
        self.set_runtime_resource = self._set_runtime_resource
        self.get_runtime_resource = self._get_runtime_resource
        self.del_runtime_resource = self._del_runtime_resource
        self.update_runtime_resources = self._update_runtime_resources
        self.clear_runtime_resources = self._clear_runtime_resources

        # Runtime Stream
        self.put_into_stream = FunctionShifter.syncify(self.async_put_into_stream)
        self.stop_stream = FunctionShifter.syncify(self.async_stop_stream)

        # Pause / Continue
        self.pause_for = FunctionShifter.syncify(self.async_pause_for)
        self.continue_with = FunctionShifter.syncify(self.async_continue_with)

        # Result
        self.get_result = FunctionShifter.syncify(self.async_get_result)

        # Lifecycle
        self.seal = FunctionShifter.syncify(self.async_seal)
        self.unseal = FunctionShifter.syncify(self.async_unseal)
        self.close = FunctionShifter.syncify(self.async_close)

        # Execution Status
        self._started = False
        self._status = TRIGGER_FLOW_STATUS_CREATED
        self._system_runtime_data.set("status", self._status)
        self._system_runtime_data.set("lifecycle_state", self._lifecycle_state)
        self._system_runtime_data.set("state_version", self._state_version)
        self._system_runtime_data.set("interrupts", {})
        self._system_runtime_data.set("last_signal", None)
        self._system_runtime_data.set("result", EMPTY)
        self._system_runtime_data.set("result_ready", asyncio.Event())
        self._runtime_stream_queue = asyncio.Queue()
        self._runtime_stream_consumer: GeneratorConsumer | None = None

    def _to_serializable_value(self, value: Any):
        return json.loads(StateData({"value": value}).dump("json"))["value"]

    def _set_status(self, status: str):
        self._status = status
        self._system_runtime_data.set("status", status)

    def _bump_state_version(self):
        self._state_version += 1
        self._system_runtime_data.set("state_version", self._state_version)

    def _set_lifecycle_state(self, state: str):
        if self._lifecycle_state == state:
            return
        self._lifecycle_state = state
        self._system_runtime_data.set("lifecycle_state", state)
        self._bump_state_version()

    def _mark_activity(self):
        self._last_activity_at = time.time()
        self._ensure_auto_close_monitor()

    def get_lifecycle_state(self):
        return self._lifecycle_state

    def is_open(self):
        return self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_OPEN

    def is_sealed(self):
        return self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_SEALED

    def is_closed(self):
        return self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED

    def is_idle(self):
        return self._active_handler_count == 0 and not any(
            task is not self._auto_close_task and not task.done()
            for task in self._pending_tasks
        )

    def _warn_runtime_data_api(self, method_name: str):
        warnings.warn(
            f"TriggerFlowExecution.{ method_name }() is deprecated; "
            "use execution state APIs such as get_state()/set_state() instead.",
            DeprecationWarning,
            stacklevel=3,
        )

    def _deprecated_get_runtime_data(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
    ):
        self._warn_runtime_data_api("get_runtime_data")
        return self._get_state(key, default, inherit=inherit)

    def _get_state(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
    ):
        return self._runtime_data.get(key, default, inherit=inherit)

    def _runtime_state_snapshot(self):
        data = self._runtime_data.get(None, {}, inherit=False)
        return data if isinstance(data, dict) else {}

    def _compat_result_exists(self):
        if self._get_state(COMPAT_FINAL_RESULT_KEY, EMPTY, inherit=False) is not EMPTY:
            return True
        return self._system_runtime_data.get("result") is not EMPTY

    def _get_compat_result(self):
        compat_result = self._get_state(COMPAT_FINAL_RESULT_KEY, EMPTY, inherit=False)
        if compat_result is not EMPTY:
            return compat_result
        result = self._system_runtime_data.get("result")
        return None if result is EMPTY else result

    def _build_close_snapshot(self):
        snapshot = dict(self._runtime_state_snapshot())
        compat_result = self._get_compat_result()
        if compat_result is not None and COMPAT_FINAL_RESULT_KEY not in snapshot:
            snapshot[COMPAT_FINAL_RESULT_KEY] = compat_result
        return snapshot

    async def _async_wait_for_compat_result_or_close(self, *, timeout: float | None = None):
        if self._compat_result_exists():
            return self._resolve_compat_result_or_snapshot()
        if self._closed_event.is_set():
            return self._resolve_compat_result_or_snapshot()

        result_ready = self._system_runtime_data.get("result_ready")
        waiters: list[asyncio.Task[Any]] = []
        if isinstance(result_ready, asyncio.Event):
            waiters.append(asyncio.create_task(result_ready.wait()))
        waiters.append(asyncio.create_task(self._closed_event.wait()))

        try:
            if timeout is None:
                done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            else:
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

        if not done:
            warnings.warn(
                f"Can not get the compatibility result of trigger flow { self.id } because it took too long and timeout.\n"
                "Use close()/async_close(), reduce auto_close_timeout, or pass timeout=None to wait forever."
                f"Timeout: { timeout }"
            )
            return None
        return self._resolve_compat_result_or_snapshot()

    async def _async_wait_for_close_snapshot(self, *, timeout: float | None = None):
        if self._closed_event.is_set():
            return self._close_result if self._close_result is not None else self._build_close_snapshot()
        try:
            if timeout is None:
                await self._closed_event.wait()
            else:
                await asyncio.wait_for(self._closed_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            warnings.warn(
                f"Can not wait for trigger flow { self.id } to close because it took too long and timeout.\n"
                "Use close()/async_close(), reduce auto_close_timeout, or pass timeout=None to wait forever."
                f"Timeout: { timeout }"
            )
            return None
        return self._close_result if self._close_result is not None else self._build_close_snapshot()

    def _track_task(self, task: asyncio.Task[Any], *, origin: str):
        self._pending_tasks.add(task)
        self._task_origins[task] = origin

        def _forget_task(done_task: asyncio.Task[Any]):
            self._pending_tasks.discard(done_task)
            self._task_origins.pop(done_task, None)
            self._mark_activity()

        task.add_done_callback(_forget_task)
        self._ensure_auto_close_monitor()
        return task

    async def _drain_pending_tasks(self, *, timeout: float | None = None):
        current_task = asyncio.current_task()
        started_at = time.time()
        results: list[Any] = []

        while True:
            pending = [
                task
                for task in self._pending_tasks
                if task is not current_task and task is not self._auto_close_task and not task.done()
            ]
            if current_task in self._pending_tasks:
                pending = [
                    task
                    for task in pending
                    if not self._task_origins.get(task, "").startswith("emit")
                ]
            if not pending:
                return results

            if timeout is None:
                results.extend(await asyncio.gather(*pending, return_exceptions=True))
                continue

            remaining_timeout = timeout - (time.time() - started_at)
            if remaining_timeout <= 0:
                done: set[asyncio.Task[Any]] = set()
                remaining = set(pending)
            else:
                done, remaining = await asyncio.wait(pending, timeout=remaining_timeout)
            if remaining:
                warnings.warn(
                    f"TriggerFlow execution { self.id } closed before { len(remaining) } pending task(s) finished.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                await self._emit_runtime_event(
                    "triggerflow.pending_tasks_cancelled",
                    level="WARNING",
                    message=f"TriggerFlow execution '{ self.id }' cancelled pending tasks during close.",
                    payload={
                        "pending_task_count": len(remaining),
                        "timeout": timeout,
                    },
                )
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
            results.extend(
                task.result() if not task.cancelled() and task.exception() is None else task.exception()
                for task in done
            )
            if remaining:
                return results

    def _ensure_auto_close_monitor(self):
        if (
            not self._auto_close
            or self._auto_close_timeout is None
            or self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._auto_close_task is None or self._auto_close_task.done():
            self._auto_close_task = loop.create_task(self._auto_close_monitor())

    async def _auto_close_monitor(self):
        timeout = self._auto_close_timeout
        if timeout is None:
            return
        while self._auto_close and self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_CLOSED:
            sleep_seconds = min(max(timeout / 4, 0.05), 1.0)
            await asyncio.sleep(sleep_seconds)
            if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
                continue
            if self.is_waiting():
                continue
            if self._last_activity_at is None or not self.is_idle():
                continue
            if time.time() - self._last_activity_at < timeout:
                continue
            await self._emit_runtime_event(
                "triggerflow.auto_close_timeout",
                level="DEBUG",
                message=f"TriggerFlow execution '{ self.id }' reached auto-close idle timeout.",
                payload={
                    "timeout": timeout,
                    "last_activity_at": self._last_activity_at,
                },
            )
            await self.async_close(reason="auto_close_idle_timeout")
            break

    async def _reject_signal(self, signal: TriggerFlowSignal):
        warnings.warn(
            f"TriggerFlow execution { self.id } ignored event '{ signal.trigger_event }' "
            f"because lifecycle state is '{ self._lifecycle_state }'.",
            RuntimeWarning,
            stacklevel=3,
        )
        await self._emit_runtime_event(
            "triggerflow.event_rejected",
            level="WARNING",
            message=(
                f"TriggerFlow execution '{ self.id }' ignored event '{ signal.trigger_event }' "
                f"because lifecycle state is '{ self._lifecycle_state }'."
            ),
            payload={
                "lifecycle_state": self._lifecycle_state,
                "signal": signal.to_debug_dict(),
            },
        )

    def _accepts_signal_in_current_lifecycle(
        self,
        signal: TriggerFlowSignal,
        *,
        preaccepted: bool = False,
    ):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_OPEN:
            return True
        if preaccepted:
            return True
        return self._close_started and signal.source in {
            "chunk",
            "runtime_data",
            "flow_data",
            "interrupt",
        }

    async def async_seal(self, *, reason: str = "manual"):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            return self
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_SEALED:
            self._sealed_at = time.time()
            self._set_lifecycle_state(TRIGGER_FLOW_LIFECYCLE_SEALED)
            await self._emit_runtime_event(
                "triggerflow.execution_sealed",
                message=f"TriggerFlow execution '{ self.id }' sealed.",
                payload={
                    "reason": reason,
                    "sealed_at": self._sealed_at,
                },
            )
        return self

    async def async_unseal(self, *, reason: str = "manual"):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            warnings.warn(
                f"TriggerFlow execution { self.id } can not be unsealed because it is closed.",
                RuntimeWarning,
                stacklevel=2,
            )
            await self._emit_runtime_event(
                "triggerflow.unseal_rejected",
                level="WARNING",
                message=f"TriggerFlow execution '{ self.id }' can not be unsealed because it is closed.",
                payload={"reason": reason},
            )
            return self
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            self._set_lifecycle_state(TRIGGER_FLOW_LIFECYCLE_OPEN)
            self._mark_activity()
            await self._emit_runtime_event(
                "triggerflow.execution_unsealed",
                message=f"TriggerFlow execution '{ self.id }' unsealed.",
                payload={"reason": reason},
            )
        return self

    async def async_close(
        self,
        *,
        reason: str = "manual",
        timeout: float | None = None,
        seal: bool = True,
    ):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            return self._close_result
        if self._close_started:
            await self._closed_event.wait()
            return self._close_result

        self._close_started = True
        self._close_reason = reason
        if seal:
            await self.async_seal(reason=reason)

        await self._drain_pending_tasks(timeout=timeout)

        result = self._build_close_snapshot()
        if self._status not in {TRIGGER_FLOW_STATUS_FAILED, TRIGGER_FLOW_STATUS_CANCELLED}:
            self._set_status(TRIGGER_FLOW_STATUS_COMPLETED)
            if not self._runtime_completed_emitted:
                self._runtime_completed_emitted = True
                await self._emit_runtime_event(
                    "triggerflow.execution_completed",
                    message=f"TriggerFlow execution '{ self.id }' completed.",
                    payload={
                        "result": self._to_serializable_value(result),
                        "origin_chunk": self._get_origin_chunk_payload(),
                    },
                )

        await self.async_stop_stream()

        self._closed_at = time.time()
        self._close_result = result
        self._set_lifecycle_state(TRIGGER_FLOW_LIFECYCLE_CLOSED)
        await self._emit_runtime_event(
            "triggerflow.execution_closed",
            message=f"TriggerFlow execution '{ self.id }' closed.",
            payload={
                "reason": reason,
                "closed_at": self._closed_at,
                "result": self._to_serializable_value(result),
            },
        )
        self._closed_event.set()

        if self._auto_close_task is not None and self._auto_close_task is not asyncio.current_task():
            self._auto_close_task.cancel()
        return self._close_result

    async def _emit_runtime_event(
        self,
        event_type: str,
        *,
        level: str = "INFO",
        message: str | None = None,
        payload: Any = None,
        error: BaseException | None = None,
    ):
        from agently.base import async_emit_runtime

        await async_emit_runtime(
            {
                "event_type": event_type,
                "source": "TriggerFlowExecution",
                "level": level,
                "message": message,
                "payload": payload,
                "error": error,
                "run": self.run_context,
                "meta": {"execution_id": self.id},
            }
        )

    async def _emit_runtime_definition_event(self):
        if self._runtime_definition_emitted:
            return
        self._runtime_definition_emitted = True
        await self._emit_runtime_event(
            "triggerflow.definition_declared",
            message=f"TriggerFlow definition declared for execution '{ self.id }'.",
            payload={
                "flow_name": self._trigger_flow.name,
                "definition": self._to_serializable_value(
                    self._trigger_flow.get_flow_config(validate_serializable=False)
                ),
                "mermaid": {
                    "simplified": self._trigger_flow.to_mermaid(mode="simplified"),
                    "detailed": self._trigger_flow.to_mermaid(mode="detailed"),
                },
            },
        )

    def _get_handler_operator(self, handler_id: str):
        try:
            return self._trigger_flow._blue_print.definition.get_operator(handler_id)
        except KeyError:
            return None

    def _serialize_operator_signals(self, signals: Any):
        if not isinstance(signals, list):
            return []
        serialized: list[dict[str, Any]] = []
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            trigger_event = signal.get("trigger_event")
            trigger_type = signal.get("trigger_type")
            if not isinstance(trigger_event, str) or not isinstance(trigger_type, str):
                continue
            serialized_signal: dict[str, Any] = {
                "trigger_event": trigger_event,
                "trigger_type": trigger_type,
            }
            role = signal.get("role")
            if isinstance(role, str):
                serialized_signal["role"] = role
            signal_id = signal.get("id")
            if isinstance(signal_id, str):
                serialized_signal["id"] = signal_id
            serialized.append(serialized_signal)
        return serialized

    def _get_origin_chunk_payload(self):
        chunk_run_context = get_current_chunk_run_context()
        if chunk_run_context is None:
            return None
        return {
            "run_id": chunk_run_context.run_id,
            "chunk_id": chunk_run_context.meta.get("chunk_id"),
            "chunk_name": chunk_run_context.meta.get("chunk_name"),
            "operator_kind": chunk_run_context.meta.get("operator_kind"),
        }

    def _serialize_runtime_value(self, value: Any):
        try:
            return self._to_serializable_value(value)
        except Exception:
            return {
                "__repr__": repr(value),
                "__type__": type(value).__name__,
            }

    def _create_chunk_run_context(self, operator: dict[str, Any], signal: TriggerFlowSignal):
        operator_kind = str(operator.get("kind", "chunk"))
        operator_name = str(operator.get("name") or operator_kind)
        return self.run_context.create_child(
            run_kind="chunk_execution",
            execution_id=self.id,
            meta={
                "flow_name": self._trigger_flow.name,
                "chunk_id": str(operator.get("id", "")),
                "chunk_name": operator_name,
                "operator_kind": operator_kind,
                "trigger_event": signal.trigger_event,
                "trigger_type": signal.trigger_type,
                "signal_id": signal.id,
                "group_id": operator.get("group_id"),
                "group_kind": operator.get("group_kind"),
                "parent_group_id": operator.get("parent_group_id"),
                "parent_group_kind": operator.get("parent_group_kind"),
                "listen_signals": self._serialize_operator_signals(operator.get("listen_signals")),
                "emit_signals": self._serialize_operator_signals(operator.get("emit_signals")),
            },
        )

    async def _emit_chunk_runtime_event(
        self,
        event_type: str,
        chunk_run_context: RunContext,
        *,
        operator: dict[str, Any],
        signal: TriggerFlowSignal,
        level: str = "INFO",
        message: str | None = None,
        payload: Any = None,
        error: BaseException | None = None,
    ):
        from agently.base import async_emit_runtime

        operator_kind = str(operator.get("kind", "chunk"))
        operator_name = str(operator.get("name") or operator_kind)
        base_payload = {
            "chunk_id": str(operator.get("id", "")),
            "chunk_name": operator_name,
            "operator_kind": operator_kind,
            "trigger_event": signal.trigger_event,
            "trigger_type": signal.trigger_type,
            "signal_id": signal.id,
        }
        if isinstance(payload, dict):
            base_payload.update(payload)
        elif payload is not None:
            base_payload["value"] = payload
        await async_emit_runtime(
            {
                "event_type": event_type,
                "source": "TriggerFlowExecution",
                "level": level,
                "message": message,
                "payload": base_payload,
                "error": error,
                "run": chunk_run_context,
                "meta": {"execution_id": self.id},
            }
        )

    def get_status(self):
        return self._status

    def is_waiting(self):
        return self._status == TRIGGER_FLOW_STATUS_WAITING

    def _get_interrupts(self) -> dict[str, Any]:
        interrupts = self._system_runtime_data.get("interrupts", {}, inherit=False)
        return interrupts if isinstance(interrupts, dict) else {}

    def get_interrupt(self, interrupt_id: str):
        return self._get_interrupts().get(interrupt_id)

    def get_pending_interrupts(self):
        return {
            interrupt_id: interrupt
            for interrupt_id, interrupt in self._get_interrupts().items()
            if isinstance(interrupt, dict) and interrupt.get("status") == "waiting"
        }

    def _set_runtime_resource(self, key: str, value: Any):
        self._runtime_resources.set(str(key), value)
        return self

    def _get_runtime_resource(self, key: str, default: Any = None):
        return self._runtime_resources.get(str(key), default)

    def require_runtime_resource(self, key: str):
        key = str(key)
        if key not in self._runtime_resources:
            available = sorted(str(resource_key) for resource_key in self.get_runtime_resources().keys())
            raise KeyError(
                f"Execution { self.id } missing required runtime resource '{ key }'. "
                f"Available resources: { available }"
            )
        return self._runtime_resources.get(key)

    def _del_runtime_resource(self, key: str):
        self._runtime_resources.pop(str(key), None)
        return self

    def _update_runtime_resources(
        self,
        mapping: dict[str, Any] | None = None,
        **kwargs,
    ):
        if mapping is not None:
            for key, value in dict(mapping).items():
                self._set_runtime_resource(str(key), value)
        for key, value in kwargs.items():
            self._set_runtime_resource(str(key), value)
        return self

    def _clear_runtime_resources(self):
        self._runtime_resources.clear()
        return self

    def get_runtime_resources(self):
        resources = self._runtime_resources.get(None, {}, inherit=True)
        return resources if isinstance(resources, dict) else {}

    def _serialize_signal(self, signal: TriggerFlowSignal | dict[str, Any] | None):
        if signal is None:
            return None
        if isinstance(signal, TriggerFlowSignal):
            return self._to_serializable_value(signal.to_state_dict())
        return self._to_serializable_value(signal)

    def _restore_signal(self, signal_state: dict[str, Any] | None):
        if not isinstance(signal_state, dict):
            return None
        try:
            return TriggerFlowSignal(
                id=str(signal_state.get("id")),
                trigger_event=str(signal_state.get("trigger_event")),
                trigger_type=signal_state.get("trigger_type", "event"),
                value=signal_state.get("value"),
                layer_marks=list(signal_state.get("layer_marks", [])),
                source=str(signal_state.get("source", "runtime")),
                meta=dict(signal_state.get("meta", {})),
            )
        except Exception:
            return None

    def _build_signal(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: TriggerFlowSignalType = "event",
        source: str = "runtime",
        meta: dict[str, Any] | None = None,
    ):
        return TriggerFlowSignal.create(
            trigger_event=trigger_event,
            trigger_type=trigger_type,
            value=value,
            layer_marks=_layer_marks,
            source=source,
            meta=meta,
        )

    def _remember_signal(self, signal: TriggerFlowSignal):
        self._system_runtime_data.set("last_signal", signal.to_state_dict())

    def get_last_signal(self):
        return self._restore_signal(self._system_runtime_data.get("last_signal", None, inherit=False))

    def get_contract_metadata(self) -> TriggerFlowContractMetadata:
        return self._trigger_flow.get_contract_metadata()

    def get_contract(self) -> TriggerFlowContractSpec[InputT, StreamT, ResultT]:
        return self._trigger_flow.get_contract()

    def save(
        self,
        path: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
        require_idle: bool = False,
    ):
        if require_idle and not self.is_idle():
            raise RuntimeError(
                f"Can not save TriggerFlowExecution { self.id } with require_idle=True while tasks are active."
            )
        result = self._system_runtime_data.get("result")
        result_ready = result is not EMPTY
        state = {
            "execution_id": self.id,
            "status": self._status,
            "lifecycle_state": self._lifecycle_state,
            "auto_close": self._auto_close,
            "auto_close_timeout": self._auto_close_timeout,
            "created_at": self._created_at,
            "started_at": self._started_at,
            "last_activity_at": self._last_activity_at,
            "sealed_at": self._sealed_at,
            "closed_at": self._closed_at,
            "close_reason": self._close_reason,
            "state_version": self._state_version,
            "owner_id": self._owner_id,
            "lease_ttl": self._lease_ttl,
            "lease_until": self._lease_until,
            "heartbeat_at": self._heartbeat_at,
            "pending_task_count": len(
                [
                    task
                    for task in self._pending_tasks
                    if task is not self._auto_close_task and not task.done()
                ]
            ),
            "run_context": self.run_context.model_dump(mode="json"),
            "runtime_data": json.loads(self._runtime_data.dump("json")),
            "flow_data": json.loads(self._trigger_flow._flow_data.dump("json")),
            "interrupts": self._to_serializable_value(self._get_interrupts()),
            "last_signal": self._serialize_signal(self.get_last_signal()),
            "resource_keys": sorted(str(key) for key in self.get_runtime_resources().keys()),
            "result": {
                "ready": result_ready,
                "value": self._to_serializable_value(result) if result_ready else None,
            },
        }
        if path is None:
            return state

        target = Path(path)
        suffix = target.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            content = yaml.safe_dump(
                state,
                indent=2,
                allow_unicode=True,
                sort_keys=False,
            )
        else:
            content = json.dumps(
                state,
                indent=2,
                ensure_ascii=False,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return state

    def load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
    ):
        if isinstance(state, (str, Path)):
            path = Path(state)
            is_file = False
            try:
                is_file = path.exists() and path.is_file()
            except (OSError, ValueError):
                is_file = False
            if is_file:
                suffix = path.suffix.lower()
                content = path.read_text(encoding=encoding)
                if suffix in {".yaml", ".yml"}:
                    try:
                        state = yaml.safe_load(content)
                    except yaml.YAMLError as e:
                        raise ValueError(
                            f"Can not load TriggerFlowExecution state from YAML file '{ state }'.\nError: { e }"
                        )
                else:
                    try:
                        state = json.loads(content)
                    except JSONDecodeError as e:
                        raise ValueError(
                            f"Can not load TriggerFlowExecution state from JSON file '{ state }'.\nError: { e }"
                        )
            elif isinstance(state, str):
                original = state
                try:
                    state = json.loads(state)
                except JSONDecodeError:
                    try:
                        state = yaml.safe_load(state)
                    except yaml.YAMLError as e:
                        raise ValueError(
                            f"Can not load TriggerFlowExecution state from JSON/YAML content.\nError: { e }\nContent: { original }"
                        )
            else:
                raise TypeError(
                    f"Can not load TriggerFlowExecution state, expect dictionary/string/path but got: { type(state) }"
                )

        if state is None:
            raise TypeError("Can not load TriggerFlowExecution state, got None.")

        if not isinstance(state, dict):
            raise TypeError(f"Can not load TriggerFlowExecution state, expect dictionary but got: { type(state) }")

        runtime_data = state.get("runtime_data", {})
        if not isinstance(runtime_data, dict):
            raise TypeError(f"Can not load key 'runtime_data', expect dictionary but got: { type(runtime_data) }")

        flow_data = state.get("flow_data", {})
        if not isinstance(flow_data, dict):
            raise TypeError(f"Can not load key 'flow_data', expect dictionary but got: { type(flow_data) }")

        interrupts = state.get("interrupts", {})
        if not isinstance(interrupts, dict):
            raise TypeError(f"Can not load key 'interrupts', expect dictionary but got: { type(interrupts) }")

        last_signal_state = state.get("last_signal", None)
        if last_signal_state is not None and not isinstance(last_signal_state, dict):
            raise TypeError(
                f"Can not load key 'last_signal', expect dictionary/None but got: { type(last_signal_state) }"
            )

        result_state = state.get("result", {})
        if not isinstance(result_state, dict):
            raise TypeError(f"Can not load key 'result', expect dictionary but got: { type(result_state) }")

        execution_id = state.get("execution_id", self.id)
        if not isinstance(execution_id, str):
            raise TypeError(f"Can not load key 'execution_id', expect string but got: { type(execution_id) }")

        run_context_state = state.get("run_context", None)
        if run_context_state is not None and not isinstance(run_context_state, dict):
            raise TypeError(
                f"Can not load key 'run_context', expect dictionary/None but got: { type(run_context_state) }"
            )

        ready = bool(result_state.get("ready", False))
        result_value = result_state.get("value")
        status = str(state.get("status", TRIGGER_FLOW_STATUS_CREATED))
        lifecycle_state = str(state.get("lifecycle_state", TRIGGER_FLOW_LIFECYCLE_OPEN))
        if lifecycle_state not in {
            TRIGGER_FLOW_LIFECYCLE_OPEN,
            TRIGGER_FLOW_LIFECYCLE_SEALED,
            TRIGGER_FLOW_LIFECYCLE_CLOSED,
        }:
            lifecycle_state = TRIGGER_FLOW_LIFECYCLE_OPEN

        original_execution_id = self.id
        self.id = execution_id
        if original_execution_id != self.id:
            self._trigger_flow._executions.pop(original_execution_id, None)
            self._trigger_flow._executions[self.id] = self

        if run_context_state is not None:
            self.run_context = RunContext.model_validate(run_context_state)
        if self.run_context.execution_id is None:
            self.run_context.execution_id = self.id

        self._runtime_data.clear()
        self._runtime_data.update(runtime_data)

        self._trigger_flow._flow_data.clear()
        self._trigger_flow._flow_data.update(flow_data)

        result_ready = asyncio.Event()
        if ready:
            self._system_runtime_data.set("result", result_value)
            result_ready.set()
        else:
            self._system_runtime_data.set("result", EMPTY)
        self._system_runtime_data.set("result_ready", result_ready)
        self._system_runtime_data.set("interrupts", interrupts)
        self._system_runtime_data.set("last_signal", last_signal_state)
        self._set_status(status)
        self._auto_close = bool(state.get("auto_close", self._auto_close))
        self._auto_close_timeout = state.get("auto_close_timeout", self._auto_close_timeout)
        self._lifecycle_state = lifecycle_state
        self._system_runtime_data.set("lifecycle_state", lifecycle_state)
        self._created_at = float(state.get("created_at", self._created_at) or self._created_at)
        self._started_at = state.get("started_at", self._started_at)
        self._last_activity_at = state.get("last_activity_at", self._last_activity_at)
        self._sealed_at = state.get("sealed_at", self._sealed_at)
        self._closed_at = state.get("closed_at", self._closed_at)
        self._close_reason = state.get("close_reason", self._close_reason)
        self._state_version = int(state.get("state_version", self._state_version))
        self._system_runtime_data.set("state_version", self._state_version)
        self._owner_id = state.get("owner_id", self._owner_id)
        self._lease_ttl = state.get("lease_ttl", self._lease_ttl)
        self._lease_until = state.get("lease_until", self._lease_until)
        self._heartbeat_at = state.get("heartbeat_at", self._heartbeat_at)
        self._runtime_stream_stopped = lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED
        if lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            close_result = self._build_close_snapshot()
            self._closed_event.set()
            self._close_result = close_result
            self._close_started = True
        else:
            self._closed_event.clear()
            self._close_started = False
            self._close_result = None
        self._started = status != TRIGGER_FLOW_STATUS_CREATED or bool(runtime_data) or ready or bool(interrupts)
        self._runtime_started_emitted = self._started
        self._runtime_completed_emitted = status == TRIGGER_FLOW_STATUS_COMPLETED and ready
        self._runtime_failed_emitted = status == TRIGGER_FLOW_STATUS_FAILED
        if runtime_resources:
            self.update_runtime_resources(runtime_resources)
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_CLOSED:
            self._ensure_auto_close_monitor()

        return self

    # Set Concurrency
    def set_concurrency(self, concurrency):
        self._concurrency_semaphore = asyncio.Semaphore(concurrency) if concurrency and concurrency > 0 else None
        return self

    # Emit Event
    async def async_emit(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
        _source: str = "runtime",
        _meta: dict[str, Any] | None = None,
    ):
        signal = self._build_signal(
            trigger_event,
            value,
            _layer_marks,
            trigger_type=trigger_type,
            source=_source,
            meta=_meta,
        )
        return await self._async_dispatch_signal(signal)

    async def async_emit_nowait(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
        _source: str = "runtime",
        _meta: dict[str, Any] | None = None,
    ):
        signal = self._build_signal(
            trigger_event,
            value,
            _layer_marks,
            trigger_type=trigger_type,
            source=_source,
            meta=_meta,
        )
        if not self._accepts_signal_in_current_lifecycle(signal):
            await self._reject_signal(signal)
            return None
        self._accepted_signal_ids.add(signal.id)
        self._mark_activity()
        task = asyncio.create_task(self._async_dispatch_signal(signal))
        return self._track_task(task, origin=f"emit_nowait:{ trigger_type }:{ trigger_event }")

    def _emit_nowait(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
        _source: str = "runtime",
        _meta: dict[str, Any] | None = None,
    ):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return FunctionShifter.future(self.async_emit)(
                trigger_event,
                value,
                _layer_marks,
                trigger_type=trigger_type,
                _source=_source,
                _meta=_meta,
            )
        signal = self._build_signal(
            trigger_event,
            value,
            _layer_marks,
            trigger_type=trigger_type,
            source=_source,
            meta=_meta,
        )
        if not self._accepts_signal_in_current_lifecycle(signal):
            loop.create_task(self._reject_signal(signal))
            return None
        self._accepted_signal_ids.add(signal.id)
        self._mark_activity()
        task = loop.create_task(self._async_dispatch_signal(signal))
        return self._track_task(task, origin=f"emit_nowait:{ trigger_type }:{ trigger_event }")

    async def _resume_interrupts_for_signal(self, signal: TriggerFlowSignal):
        if signal.trigger_type != "event" or signal.source == "interrupt":
            return
        interrupts = self._get_interrupts().copy()
        resumed_interrupts: list[dict[str, Any]] = []
        for interrupt_id, interrupt_state in interrupts.items():
            if not isinstance(interrupt_state, dict):
                continue
            if interrupt_state.get("status") != "waiting":
                continue
            if interrupt_state.get("resume_event") != signal.trigger_event:
                continue
            interrupt = dict(interrupt_state)
            interrupt["status"] = "resumed"
            interrupt["response"] = signal.value
            interrupt["resumed_by_signal_id"] = signal.id
            interrupts[interrupt_id] = interrupt
            resumed_interrupts.append(interrupt)

        if not resumed_interrupts:
            return

        self._system_runtime_data.set("interrupts", interrupts)
        self._set_status(TRIGGER_FLOW_STATUS_RUNNING)
        self._bump_state_version()
        await self._emit_runtime_event(
            "triggerflow.execution_resumed",
            message=f"TriggerFlow execution '{ self.id }' resumed by event '{ signal.trigger_event }'.",
            payload={
                "signal": signal.to_debug_dict(),
                "interrupts": self._to_serializable_value(resumed_interrupts),
            },
        )
        for interrupt in resumed_interrupts:
            await self.async_put_into_stream(
                {
                    "type": "interrupt",
                    "action": "resume",
                    "execution_id": self.id,
                    "interrupt": self._to_serializable_value(interrupt),
                    "value": self._to_serializable_value(signal.value),
                },
                _skip_contract_validation=True,
            )

    async def _async_dispatch_signal(self, signal: TriggerFlowSignal):
        from agently.base import async_emit_runtime

        signal_preaccepted = signal.id in self._accepted_signal_ids
        if not self._accepts_signal_in_current_lifecycle(signal, preaccepted=signal_preaccepted):
            await self._reject_signal(signal)
            return None
        self._accepted_signal_ids.discard(signal.id)

        self._mark_activity()
        await self._resume_interrupts_for_signal(signal)
        self._remember_signal(signal)
        await async_emit_runtime(
            {
                "event_type": "triggerflow.signal",
                "source": "TriggerFlowExecution",
                "level": "DEBUG",
                "message": f"Dispatch signal '{ signal.trigger_event }'.",
                "payload": signal.to_debug_dict(),
                "run": self.run_context,
                "meta": {
                    "execution_id": self.id,
                },
            }
        )
        tasks = []
        handlers = self._handlers[signal.trigger_type]

        if signal.trigger_event in handlers:
            for handler_id, handler in handlers[signal.trigger_event].items():
                operator = self._get_handler_operator(handler_id)
                chunk_run_context = self._create_chunk_run_context(operator, signal) if operator is not None else None
                await async_emit_runtime(
                    {
                        "event_type": "triggerflow.handler_dispatch",
                        "source": "TriggerFlowExecution",
                        "level": "DEBUG",
                        "message": f"Dispatch handler '{ handler_id }' for signal '{ signal.trigger_event }'.",
                        "payload": {
                            "event": signal.trigger_event,
                            "type": signal.trigger_type,
                            "handler": handler_id,
                            "signal_id": signal.id,
                        },
                        "run": self.run_context,
                        "meta": {
                            "execution_id": self.id,
                        },
                    }
                )

                async def run_handler(handler_func, *, handler_id: str):
                    self._active_handler_count += 1
                    async def execute_handler():
                        if operator is not None and chunk_run_context is not None:
                            await self._emit_chunk_runtime_event(
                                "chunk.started",
                                chunk_run_context,
                                operator=operator,
                                signal=signal,
                                message=f"Chunk '{ chunk_run_context.meta.get('chunk_name', chunk_run_context.run_id) }' started.",
                                payload={
                                    "status": "running",
                                    "input": self._serialize_runtime_value(signal.value),
                                    "signal_source": signal.source,
                                    "signal_meta": self._serialize_runtime_value(signal.meta),
                                },
                            )
                        try:
                            with bind_runtime_context(
                                parent_run_context=(
                                    chunk_run_context if chunk_run_context is not None else self.run_context
                                ),
                                chunk_run_context=chunk_run_context,
                            ):
                                return await handler_func
                        except BaseException as error:
                            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                                raise
                            if operator is not None and chunk_run_context is not None:
                                await self._emit_chunk_runtime_event(
                                    "chunk.failed",
                                    chunk_run_context,
                                    operator=operator,
                                    signal=signal,
                                    level="ERROR",
                                    message=(
                                        f"Chunk '{ chunk_run_context.meta.get('chunk_name', chunk_run_context.run_id) }' failed."
                                    ),
                                    payload={
                                        "status": "failed",
                                        "input": self._serialize_runtime_value(signal.value),
                                        "signal_source": signal.source,
                                        "signal_meta": self._serialize_runtime_value(signal.meta),
                                    },
                                    error=error,
                                )
                            raise

                    try:
                        if self._concurrency_semaphore is None:
                            result = await execute_handler()
                        else:
                            depth = self._concurrency_depth.get()
                            token = self._concurrency_depth.set(depth + 1)
                            try:
                                if depth > 0:
                                    result = await execute_handler()
                                else:
                                    async with self._concurrency_semaphore:
                                        result = await execute_handler()
                            finally:
                                self._concurrency_depth.reset(token)

                        if operator is not None and chunk_run_context is not None:
                            await self._emit_chunk_runtime_event(
                                "chunk.completed",
                                chunk_run_context,
                                operator=operator,
                                signal=signal,
                                message=f"Chunk '{ chunk_run_context.meta.get('chunk_name', chunk_run_context.run_id) }' completed.",
                                payload={
                                    "status": "waiting" if self.is_waiting() else "completed",
                                    "returned_pause_signal": isinstance(result, TriggerFlowPauseSignal),
                                    "input": self._serialize_runtime_value(signal.value),
                                    "signal_source": signal.source,
                                    "signal_meta": self._serialize_runtime_value(signal.meta),
                                    "output": (
                                        None
                                        if isinstance(result, TriggerFlowPauseSignal)
                                        else self._serialize_runtime_value(result)
                                    ),
                                },
                            )
                        return result
                    finally:
                        self._active_handler_count -= 1
                        self._mark_activity()

                handler_task = FunctionShifter.asyncify(handler)(
                    TriggerFlowRuntimeData(
                        trigger_event=signal.trigger_event,
                        trigger_type=signal.trigger_type,
                        value=signal.value,
                        execution=self,
                        _layer_marks=signal.layer_marks.copy(),
                        signal=signal,
                        chunk_run_context=chunk_run_context,
                    )
                )
                tasks.append(
                    self._track_task(
                        asyncio.ensure_future(run_handler(handler_task, handler_id=handler_id)),
                        origin=f"handler:{ handler_id }",
                    )
                )

        if tasks:
            try:
                result = await asyncio.gather(*tasks, return_exceptions=self._skip_exceptions)
                self._mark_activity()
                return result
            except Exception as error:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._set_status(TRIGGER_FLOW_STATUS_FAILED)
                if not self._runtime_failed_emitted:
                    self._runtime_failed_emitted = True
                    await self._emit_runtime_event(
                        "triggerflow.execution_failed",
                        level="ERROR",
                        message=f"TriggerFlow execution '{ self.id }' failed.",
                        payload={"last_signal": signal.to_debug_dict()},
                        error=error,
                )
                raise
        self._mark_activity()
        return None

    # Change Runtime Data
    async def _async_change_runtime_data(
        self,
        operation: Literal["set", "append", "del"],
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        futures = []
        handlers = self._handlers["runtime_data"]

        match operation:
            case "set":
                self._runtime_data.set(key, value)
                value = self._runtime_data[key]
                self._bump_state_version()
            case "append":
                self._runtime_data.append(key, value)
                value = self._runtime_data[key]
                self._bump_state_version()
            case "del":
                if self._runtime_data.get(key, None):
                    del self._runtime_data[key]
                    value = None
                    self._bump_state_version()
                else:
                    return
        if emit:
            if key in handlers:
                futures.append(
                    self.async_emit(
                        key,
                        value,
                        trigger_type="runtime_data",
                        _source="runtime_data",
                    )
                )

            if futures:
                await asyncio.gather(*futures, return_exceptions=self._skip_exceptions)

    async def async_set_state(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        return await self._async_change_runtime_data("set", key, value, emit=emit)

    async def async_append_state(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        return await self._async_change_runtime_data("append", key, value, emit=emit)

    async def async_del_state(
        self,
        key: str,
        *,
        emit: bool = True,
    ):
        return await self._async_change_runtime_data("del", key, None, emit=emit)

    async def async_set_runtime_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        self._warn_runtime_data_api("async_set_runtime_data")
        return await self.async_set_state(key, value, emit=emit)

    async def async_append_runtime_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        self._warn_runtime_data_api("async_append_runtime_data")
        return await self.async_append_state(key, value, emit=emit)

    async def async_del_runtime_data(
        self,
        key: str,
        *,
        emit: bool = True,
    ):
        self._warn_runtime_data_api("async_del_runtime_data")
        return await self.async_del_state(key, emit=emit)

    def _warn_wait_for_result_deprecated(self, method_name: str):
        warnings.warn(
            f"TriggerFlowExecution.{ method_name }(..., wait_for_result=...) is deprecated. "
            "Execution start behavior is now driven by auto_close: "
            "auto-close executions wait for close; manual-close executions start and return the execution handle. "
            "Use create_execution()/start_execution() for explicit lifecycle control.",
            DeprecationWarning,
            stacklevel=3,
        )

    async def _async_run_start(self, initial_value: InputT | None = None):
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            signal = self._build_signal("START", initial_value, trigger_type="event", source="start")
            await self._reject_signal(signal)
            return self
        if self._started:
            return self

        self._started = True
        self._started_at = time.time()
        self._mark_activity()
        if self._status not in {
            TRIGGER_FLOW_STATUS_COMPLETED,
            TRIGGER_FLOW_STATUS_FAILED,
            TRIGGER_FLOW_STATUS_CANCELLED,
        }:
            self._set_status(TRIGGER_FLOW_STATUS_RUNNING)
        if not self._runtime_started_emitted:
            await self._emit_runtime_definition_event()
            self._runtime_started_emitted = True
            await self._emit_runtime_event(
                "triggerflow.execution_started",
                message=f"TriggerFlow execution '{ self.id }' started.",
                payload={"initial_value": initial_value},
            )
        initial_value = cast(InputT | None, self._trigger_flow._contract.validate_initial_input(initial_value))
        try:
            await self._async_dispatch_signal(
                self._build_signal(
                    "START",
                    initial_value,
                    trigger_type="event",
                    source="start",
                )
            )
        except Exception as error:
            if not self._runtime_failed_emitted:
                self._runtime_failed_emitted = True
                await self._emit_runtime_event(
                    "triggerflow.execution_failed",
                    level="ERROR",
                    message=f"TriggerFlow execution '{ self.id }' failed during start.",
                    payload={"initial_value": initial_value},
                    error=error,
                )
            raise
        return self

    @overload
    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[True] = True,
        timeout: float | None = None,
    ) -> ResultT: ...

    @overload
    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[False],
        timeout: float | None = None,
    ) -> None: ...

    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: bool = True,
        timeout: float | None = None,
    ) -> Any:
        if not self._auto_close:
            raise ValueError(
                "TriggerFlowExecution.start() with auto_close=False is not supported in sync mode. "
                "Use await execution.async_start(...) and close the execution explicitly."
            )
        return FunctionShifter.syncify(self.async_start)(
            initial_value,
            wait_for_result=wait_for_result,
            timeout=timeout,
        )

    @overload
    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[True] = True,
        timeout: float | None = None,
    ) -> ResultT: ...

    @overload
    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[False],
        timeout: float | None = None,
    ) -> None: ...

    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: bool = True,
        timeout: float | None = None,
    ) -> Any:
        if wait_for_result is False:
            self._warn_wait_for_result_deprecated("async_start")
        if timeout is not None:
            self._auto_close_timeout = timeout

        await self._async_run_start(initial_value)

        if self._auto_close:
            return await self._async_wait_for_close_snapshot()
        return self

    # Pause / Continue
    async def async_pause_for(
        self,
        *,
        type: str = "pause",
        payload: Any = None,
        resume_event: str | None = None,
        interrupt_id: str | None = None,
    ):
        interrupt_id = interrupt_id if interrupt_id is not None else uuid.uuid4().hex
        interrupts = self._get_interrupts().copy()
        interrupt = {
            "id": interrupt_id,
            "type": type,
            "payload": payload,
            "resume_event": resume_event,
            "status": "waiting",
        }
        interrupts[interrupt_id] = interrupt
        self._system_runtime_data.set("interrupts", interrupts)
        self._set_status(TRIGGER_FLOW_STATUS_WAITING)
        self._bump_state_version()
        self._mark_activity()
        await self._emit_runtime_event(
            "triggerflow.interrupt_raised",
            level="WARNING",
            message=f"TriggerFlow execution '{ self.id }' paused for interrupt '{ interrupt_id }'.",
            payload={"interrupt": self._to_serializable_value(interrupt)},
        )
        await self.async_put_into_stream(
            {
                "type": "interrupt",
                "action": "pause",
                "execution_id": self.id,
                "interrupt": self._to_serializable_value(interrupt),
                "signal": self._serialize_signal(self.get_last_signal()),
            },
            _skip_contract_validation=True,
        )
        return TriggerFlowPauseSignal(interrupt)

    async def async_continue_with(
        self,
        interrupt_id: str,
        value: Any = None,
    ):
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            warnings.warn(
                f"TriggerFlow execution { self.id } ignored continue_with() because lifecycle state is "
                f"'{ self._lifecycle_state }'.",
                RuntimeWarning,
                stacklevel=2,
            )
            await self._emit_runtime_event(
                "triggerflow.continue_rejected",
                level="WARNING",
                message=(
                    f"TriggerFlow execution '{ self.id }' ignored continue_with() because lifecycle state is "
                    f"'{ self._lifecycle_state }'."
                ),
                payload={
                    "lifecycle_state": self._lifecycle_state,
                    "interrupt_id": interrupt_id,
                },
            )
            return None
        interrupts = self._get_interrupts().copy()
        if interrupt_id not in interrupts:
            raise KeyError(f"Can not continue execution { self.id }, interrupt '{ interrupt_id }' not found.")
        interrupt = dict(interrupts[interrupt_id])
        if interrupt.get("status") != "waiting":
            raise ValueError(f"Can not continue execution { self.id }, interrupt '{ interrupt_id }' is not waiting.")
        interrupt["status"] = "resumed"
        interrupt["response"] = value
        interrupts[interrupt_id] = interrupt
        self._system_runtime_data.set("interrupts", interrupts)
        self._set_status(TRIGGER_FLOW_STATUS_RUNNING)
        self._bump_state_version()
        self._mark_activity()
        await self._emit_runtime_event(
            "triggerflow.execution_resumed",
            message=f"TriggerFlow execution '{ self.id }' resumed from interrupt '{ interrupt_id }'.",
            payload={
                "interrupt_id": interrupt_id,
                "value": self._to_serializable_value(value),
            },
        )
        await self.async_put_into_stream(
            {
                "type": "interrupt",
                "action": "resume",
                "execution_id": self.id,
                "interrupt": self._to_serializable_value(interrupt),
                "value": self._to_serializable_value(value),
            },
            _skip_contract_validation=True,
        )
        resume_event = interrupt.get("resume_event")
        if resume_event:
            await self._async_dispatch_signal(
                self._build_signal(
                    str(resume_event),
                    value,
                    trigger_type="event",
                    source="interrupt",
                    meta={"interrupt_id": interrupt_id},
                )
            )
        return interrupt

    # Runtime Stream
    async def async_put_into_stream(
        self,
        stream_item: StreamT | TriggerFlowInterruptEvent,
        *,
        _skip_contract_validation: bool = False,
        _origin_chunk: dict[str, Any] | None = None,
    ):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            warnings.warn(
                f"TriggerFlow execution { self.id } ignored stream item because it is closed.",
                RuntimeWarning,
                stacklevel=2,
            )
            await self._emit_runtime_event(
                "triggerflow.stream_item_rejected",
                level="WARNING",
                message=f"TriggerFlow execution '{ self.id }' ignored stream item because it is closed.",
                payload={
                    "item": self._to_serializable_value(stream_item),
                    "origin_chunk": _origin_chunk or self._get_origin_chunk_payload(),
                },
            )
            return None
        if not _skip_contract_validation:
            stream_item = cast(StreamT, self._trigger_flow._contract.validate_stream_item(stream_item))
        await self._runtime_stream_queue.put(stream_item)
        self._mark_activity()
        await self._emit_runtime_event(
            "triggerflow.stream_item_emitted",
            message=f"TriggerFlow execution '{ self.id }' emitted a stream item.",
            payload={
                "item": self._to_serializable_value(stream_item),
                "item_type": type(stream_item).__name__,
                "origin_chunk": _origin_chunk or self._get_origin_chunk_payload(),
            },
        )

    async def async_stop_stream(self):
        if self._runtime_stream_stopped:
            return
        self._runtime_stream_stopped = True
        await self._runtime_stream_queue.put(RUNTIME_STREAM_STOP)
        await self._emit_runtime_event(
            "triggerflow.stream_closed",
            message=f"TriggerFlow execution '{ self.id }' runtime stream closed.",
            payload={"execution_id": self.id},
        )

    async def _consume_runtime_stream(
        self,
        *,
        initial_value: InputT | None,
        timeout: float | None,
    ) -> AsyncGenerator[StreamT | TriggerFlowInterruptEvent, None]:
        temp_execution_task = None
        try:
            if not self._started:
                temp_execution_task = asyncio.create_task(
                    self._async_run_start(initial_value=initial_value)
                )
            while True:
                if timeout is not None:
                    try:
                        next_result = await asyncio.wait_for(
                            self._runtime_stream_queue.get(),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        warnings.warn(
                            f"Execution { self.id } runtime stream stopped because of timeout.\n"
                            f"Timeout seconds: { timeout }\n"
                            "You can use execution.get_async_runtime_stream(timeout=<int | None>) or execution.get_runtime_stream(timeout=<int | None>) to reset new timeout seconds or use None to wait forever."
                        )
                        break
                else:
                    next_result = await self._runtime_stream_queue.get()
                if next_result is not RUNTIME_STREAM_STOP:
                    yield next_result
                else:
                    break
        finally:
            if temp_execution_task:
                await temp_execution_task

    def get_async_runtime_stream(
        self,
        initial_value: InputT | None = None,
        *,
        timeout: float | None = 10,
    ) -> AsyncGenerator[StreamT | TriggerFlowInterruptEvent, None]:
        if self._runtime_stream_consumer is None:
            self._runtime_stream_consumer = GeneratorConsumer(
                self._consume_runtime_stream(
                    initial_value=initial_value,
                    timeout=timeout,
                )
            )
        return self._runtime_stream_consumer.get_async_generator()

    def get_runtime_stream(
        self,
        initial_value: InputT | None = None,
        *,
        timeout: float | None = 10,
    ) -> Generator[StreamT | TriggerFlowInterruptEvent, None, None]:
        if self._runtime_stream_consumer is None:
            self._runtime_stream_consumer = GeneratorConsumer(
                self._consume_runtime_stream(
                    initial_value=initial_value,
                    timeout=timeout,
                )
            )
        return self._runtime_stream_consumer.get_generator()

    # Result
    def _resolve_compat_result_or_snapshot(self):
        compat_result = self._get_compat_result()
        if compat_result is not None:
            return compat_result
        if self._close_result is not None:
            return self._close_result
        return self._build_close_snapshot()

    def set_result(self, result: ResultT, *, _origin_chunk: dict[str, Any] | None = None):
        warnings.warn(
            "TriggerFlowExecution.set_result() is deprecated; write execution state directly and let close() return "
            "the close snapshot. For compatibility, set_result() now writes '$final_result'.",
            DeprecationWarning,
            stacklevel=2,
        )
        result = cast(ResultT, self._trigger_flow._contract.validate_result(result))
        previous_result = self._get_compat_result()
        if previous_result is not None:
            warnings.warn(
                f"TriggerFlow execution { self.id } overwrote compatibility final result '{ COMPAT_FINAL_RESULT_KEY }'.",
                RuntimeWarning,
                stacklevel=2,
            )
        self._runtime_data.set(COMPAT_FINAL_RESULT_KEY, result)
        self._system_runtime_data.set("result", result)
        result_ready = self._system_runtime_data.get("result_ready")
        if isinstance(result_ready, asyncio.Event):
            result_ready.set()
        self._bump_state_version()
        self._mark_activity()
        if not self._runtime_result_set_emitted:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._runtime_result_set_emitted = True
                loop.create_task(
                    self._emit_runtime_event(
                        "triggerflow.result_set",
                        message=f"TriggerFlow execution '{ self.id }' set a result.",
                        payload={
                            "result": self._to_serializable_value(result),
                            "state_key": COMPAT_FINAL_RESULT_KEY,
                            "origin_chunk": _origin_chunk or self._get_origin_chunk_payload(),
                        },
                    )
                )

    async def async_get_result(self, *, timeout: float | None = None) -> ResultT | None:
        warnings.warn(
            "TriggerFlowExecution.get_result()/async_get_result() are compatibility APIs; "
            "prefer close()/async_close() and execution state APIs for lifecycle-oriented workflows. "
            "get_result() now returns '$final_result' when present, otherwise the close snapshot.",
            DeprecationWarning,
            stacklevel=2,
        )
        return cast(ResultT | None, await self._async_wait_for_compat_result_or_close(timeout=timeout))
