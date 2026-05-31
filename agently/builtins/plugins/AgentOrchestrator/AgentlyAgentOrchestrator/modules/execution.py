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

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, Literal, TYPE_CHECKING

from agently.core.AgentExecution import (
    AgentExecutionContext,
    AgentExecutionLimitExceeded,
    RuntimeStageStallError,
    merge_stream_meta,
    normalize_execution_limits,
    normalize_execution_lineage,
    normalize_execution_mode,
)
from agently.core.RuntimeContext import bind_runtime_context
from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter, FunctionShifter

from .routing import HybridRoutePlanner
from .routes import run_dynamic_task_route, run_model_request_route, run_skills_route
from .stream import AgentExecutionStream

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentExecutionMode,
        OutputValidateHandler,
        RunContext,
    )


class AgentExecution:
    """Response-style execution facade for one Agent turn."""

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        mode: "AgentExecutionMode | str" = "one_turn",
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        parent_run_context: "RunContext | None" = None,
    ):
        self.agent = agent
        self.id = uuid.uuid4().hex
        self.mode: "AgentExecutionMode" = normalize_execution_mode(str(mode))
        self.lineage: "AgentExecutionLineage" = normalize_execution_lineage(lineage)
        self.limits: "AgentExecutionLimits" = normalize_execution_limits(limits, mode=self.mode)
        self.workspace = getattr(agent, "workspace", None)
        self.execution_context = AgentExecutionContext(
            execution_id=self.id,
            mode=self.mode,
            lineage=self.lineage,
            limits=self.limits,
        )
        self.parent_run_context = parent_run_context
        self.route_info: dict[str, Any] = {}
        self.route_plan: dict[str, Any] = {}
        self.close_snapshot: dict[str, Any] = {}
        self.logs: dict[str, Any] = {
            "model_response_ids": [],
            "action_logs": [],
            "artifact_refs": [],
            "route_logs": {},
        }
        self.diagnostics: dict[str, Any] = {}
        self.workspace_refs: dict[str, Any] = {}
        self.result: Any = None
        self.status = "created"
        prompt_snapshot = agent.request.prompt.get()
        self.prompt_snapshot: dict[str, Any] = prompt_snapshot if isinstance(prompt_snapshot, dict) else {}

        self._started = False
        self._completed = False
        self._start_lock = asyncio.Lock()
        self.route_planner = HybridRoutePlanner(agent, prompt_snapshot=self.prompt_snapshot)
        self.stream = AgentExecutionStream(execution_id=self.id, execution_mode=self.mode, lineage=self.lineage)
        self._error: BaseException | None = None
        self._selected_route: tuple[str, dict[str, Any]] | None = None
        self._seen_action_log_keys: set[str] = set()

        self.start = FunctionShifter.syncify(self.async_start)
        self.get_data = FunctionShifter.syncify(self.async_get_data)
        self.get_text = FunctionShifter.syncify(self.async_get_text)
        self.get_meta = FunctionShifter.syncify(self.async_get_meta)
        self.record_workspace = FunctionShifter.syncify(self.async_record_workspace)
        self.get_generator = self._get_generator

    def task_target(self) -> str:
        return self.route_planner.task_target()

    async def emit_stream(
        self,
        path: str,
        value: Any,
        *,
        route: str | None = None,
        source: str | None = "agent_execution",
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        is_complete: bool = True,
        event_type: Literal["delta", "done"] = "done",
        delta: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        if path != "error":
            self.execution_context.record_progress(
                stage=path,
                status="completed" if is_complete else "progress",
                event_type=path,
                meta=meta,
            )
        stream_meta = merge_stream_meta(
            meta,
            execution_id=self.id,
            mode=self.mode,
            lineage=self.lineage,
        )
        return await self.stream.emit(
            path,
            value,
            delta=delta,
            route=route,
            source=source,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            is_complete=is_complete,
            event_type=event_type,
            meta=stream_meta,
        )

    async def close_streams(self):
        await self.stream.close()

    def dynamic_task_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.dynamic_task_candidates()

    def action_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.action_candidates()

    def skill_candidate_summary(self) -> dict[str, Any]:
        return self.route_planner.skill_candidate_summary()

    async def select_route(self) -> tuple[str, dict[str, Any]]:
        if self._selected_route is not None:
            return self._selected_route
        route, route_meta = await self.route_planner.select_route()
        self._selected_route = (route, route_meta)
        self.route_info = {
            "selected_route": route,
            "selected_by": route_meta.get("selected_by"),
            "options": DataFormatter.sanitize(route_meta),
            "reusable": True,
        }
        return self._selected_route

    async def _async_execute_route(
        self,
        *,
        type: Literal["original", "parsed", "all"],
        ensure_keys: list[str] | None,
        ensure_all_keys: bool | None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
        key_style: Literal["dot", "slash"],
        max_retries: int,
        raise_ensure_failure: bool,
    ) -> tuple[str, Any]:
        with bind_runtime_context(agent_execution_context=self.execution_context):
            self.execution_context.record_progress(stage="route_selection", status="started")
            route, route_meta = await self.select_route()
            self.execution_context.record_progress(stage="route_selection", status="completed")
            self.route_plan = self.route_planner.build_route_plan(
                execution_id=self.id,
                route=route,
                route_meta=route_meta,
            )
            self.route_info.setdefault("selected_route", route)
            self.route_info.setdefault("options", DataFormatter.sanitize(route_meta))
            self.route_info.setdefault("reusable", True)
            await self.emit_stream("route.selected", self.route_plan, route=route)
            if route == "skills":
                result = await run_skills_route(self, route_meta)
            elif route == "dynamic_task":
                result = await run_dynamic_task_route(self, route_meta)
            else:
                result = await run_model_request_route(
                    self,
                    type=type,
                    ensure_keys=ensure_keys,
                    ensure_all_keys=ensure_all_keys,
                    validate_handler=validate_handler,
                    key_style=key_style,
                    max_retries=max_retries,
                    raise_ensure_failure=raise_ensure_failure,
                )
            return route, result

    def record_model_response_id(self, response_id: str | None):
        if not response_id:
            return
        ids = self.logs.setdefault("model_response_ids", [])
        if isinstance(ids, list) and response_id not in ids:
            ids.append(response_id)
        self.logs.setdefault("model_response_id", response_id)

    async def record_action_log(
        self,
        log: Any,
        *,
        route: str,
        source: str = "action",
        emit: bool = True,
    ) -> dict[str, Any] | None:
        if not isinstance(log, dict):
            return None
        raw_model_digest = log.get("model_digest")
        model_digest: dict[str, Any] = raw_model_digest if isinstance(raw_model_digest, dict) else {}
        action_id = str(log.get("action_id") or log.get("tool_name") or model_digest.get("action_id") or "action")
        action_call_id = log.get("action_call_id") or model_digest.get("action_call_id")
        status = str(log.get("status") or model_digest.get("status") or "")
        artifact_refs = log.get("artifact_refs") or model_digest.get("artifact_refs") or []
        if not isinstance(artifact_refs, list):
            artifact_refs = []
        key = str(action_call_id or f"{ action_id }:{ len(self.logs.get('action_logs', [])) }")
        if key in self._seen_action_log_keys:
            return None
        self._seen_action_log_keys.add(key)
        data = log.get("data")
        if data is None:
            data = log.get("result")
        normalized = DataFormatter.sanitize(
            {
                "action_call_id": action_call_id,
                "action_id": action_id,
                "status": status,
                "success": log.get("success") if "success" in log else model_digest.get("success"),
                "source": source,
                "route": route,
                "data": data if isinstance(data, dict) else {},
                "model_digest": model_digest,
                "artifact_refs": artifact_refs,
                "raw": log,
            }
        )
        action_logs = self.logs.setdefault("action_logs", [])
        if isinstance(action_logs, list):
            action_logs.append(normalized)
        aggregated_artifact_refs = self.logs.setdefault("artifact_refs", [])
        if isinstance(aggregated_artifact_refs, list):
            for ref in artifact_refs:
                if ref not in aggregated_artifact_refs:
                    aggregated_artifact_refs.append(DataFormatter.sanitize(ref))
        if emit:
            await self.emit_stream(
                f"actions.{ action_id }",
                normalized,
                route=route,
                source=source,
                action_id=action_id,
            )
        return normalized

    async def bridge_task_dag_stream_item(self, item: Any, *, route: str):
        await self.stream.bridge_task_dag_item(item, route=route)

    async def bridge_model_stream_item(
        self,
        item: Any,
        *,
        route: str,
        source: str = "model_request",
        path_prefix: str | None = None,
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ):
        await self.stream.bridge_model_stream_item(
            item,
            route=route,
            source=source,
            path_prefix=path_prefix,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=meta,
        )

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> Any:
        async with self._start_lock:
            if self._completed:
                if self._error is not None:
                    raise self._error
                return self.result
            if self._started:
                while not self._completed:
                    await asyncio.sleep(0.01)
                if self._error is not None:
                    raise self._error
                return self.result
            self._started = True
            self.status = "running"
            try:
                self.execution_context.record_progress(
                    stage="agent_execution",
                    status="started",
                    event_type="agent_execution.started",
                    meta={"execution_id": self.id, "execution_mode": self.mode},
                )
                run_coro = self._async_execute_route(
                    type=type,
                    ensure_keys=ensure_keys,
                    ensure_all_keys=ensure_all_keys,
                    validate_handler=validate_handler,
                    key_style=key_style,
                    max_retries=max_retries,
                    raise_ensure_failure=raise_ensure_failure,
                )
                route, self.result = await self._await_route_with_limits(run_coro)
                if self.status == "running":
                    self.status = "success"
                await self.emit_stream("result", self.result, route=route, source="agent_execution")
                return self.result
            except RuntimeStageStallError as error:
                self.status = "timed_out" if error.status == "timed_out" else "stalled"
                self._error = error
                self._record_error_diagnostic(error)
                await self.emit_stream(
                    "error",
                    error.to_diagnostic(),
                    source="agent_execution",
                )
                raise
            except asyncio.TimeoutError as error:
                self.status = "timed_out"
                timeout_error = RuntimeStageStallError(
                    (
                        "AgentExecution hard deadline exceeded: "
                        f"max_seconds={ self.limits.get('max_seconds') }."
                    ),
                    stage=str((self.execution_context.last_progress_event or {}).get("stage") or "agent_execution"),
                    status="timed_out",
                    elapsed_seconds=None,
                    timeout_seconds=self.limits.get("max_seconds"),
                    last_progress_event=(self.execution_context.last_progress_event or {}).get("event_type"),
                )
                self._error = timeout_error
                self._record_error_diagnostic(timeout_error)
                await self.emit_stream(
                    "error",
                    timeout_error.to_diagnostic(),
                    source="agent_execution",
                )
                raise timeout_error from error
            except AgentExecutionLimitExceeded as error:
                self.status = "blocked"
                self._error = error
                self._record_error_diagnostic(error)
                await self.emit_stream(
                    "error",
                    {"type": error.__class__.__name__, "message": str(error), "limit_name": error.limit_name},
                    source="agent_execution",
                )
                raise
            except BaseException as error:
                self.status = "error"
                self._error = error
                self._record_error_diagnostic(error)
                await self.emit_stream(
                    "error",
                    {"type": error.__class__.__name__, "message": str(error)},
                    source="agent_execution",
                )
                raise
            finally:
                self._refresh_diagnostics()
                self._completed = True
                await self.close_streams()

    async def _await_route_with_limits(self, run_coro: Any):
        max_seconds = self.limits.get("max_seconds")
        max_no_progress_seconds = self.limits.get("max_no_progress_seconds")
        if max_seconds is None and max_no_progress_seconds is None:
            return await run_coro

        hard_deadline = (
            self.execution_context.started_at + float(max_seconds)
            if max_seconds is not None
            else None
        )
        idle_limit = float(max_no_progress_seconds) if max_no_progress_seconds is not None else None
        task = asyncio.create_task(run_coro)
        try:
            while True:
                now = time.monotonic()
                next_timeouts: list[float] = []
                if hard_deadline is not None:
                    next_timeouts.append(max(0.0, hard_deadline - now))
                if idle_limit is not None:
                    idle_deadline = self.execution_context.last_progress_at + idle_limit
                    next_timeouts.append(max(0.0, idle_deadline - now))
                if not next_timeouts:
                    return await task

                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=min(next_timeouts))
                except asyncio.TimeoutError as error:
                    if task.done():
                        return await task
                    now = time.monotonic()
                    if hard_deadline is not None and now >= hard_deadline:
                        await self._cancel_limited_task(task)
                        raise self._build_execution_stall_error(
                            status="timed_out",
                            message=(
                                "AgentExecution hard deadline exceeded: "
                                f"max_seconds={ max_seconds }."
                            ),
                            elapsed_seconds=now - self.execution_context.started_at,
                            idle_seconds=now - self.execution_context.last_progress_at,
                            timeout_seconds=float(max_seconds) if max_seconds is not None else None,
                        ) from error
                    if idle_limit is not None:
                        idle_seconds = now - self.execution_context.last_progress_at
                        if idle_seconds >= idle_limit:
                            await self._cancel_limited_task(task)
                            raise self._build_execution_stall_error(
                                status="stalled",
                                message=(
                                    "AgentExecution made no progress before idle deadline: "
                                    f"max_no_progress_seconds={ max_no_progress_seconds }."
                                ),
                                elapsed_seconds=now - self.execution_context.started_at,
                                idle_seconds=idle_seconds,
                                timeout_seconds=idle_limit,
                            ) from error
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    async def _cancel_limited_task(self, task: "asyncio.Task[Any]"):
        if task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _build_execution_stall_error(
        self,
        *,
        status: Literal["stalled", "timed_out"],
        message: str,
        elapsed_seconds: float | None,
        idle_seconds: float | None,
        timeout_seconds: float | None,
    ) -> RuntimeStageStallError:
        last_event = self.execution_context.last_progress_event or {}
        return RuntimeStageStallError(
            message,
            stage=str(last_event.get("stage") or "agent_execution"),
            status=status,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=idle_seconds,
            timeout_seconds=timeout_seconds,
            last_progress_event=(
                str(last_event.get("event_type"))
                if last_event.get("event_type") is not None
                else None
            ),
        )

    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> Any:
        return await self.async_start(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    async def async_get_text(self) -> str:
        data = await self.async_get_data()
        if isinstance(data, str):
            return data
        return json.dumps(DataFormatter.sanitize(data), ensure_ascii=False)

    async def async_get_meta(self) -> dict[str, Any]:
        if not self._completed:
            await self.async_start()
        self._refresh_diagnostics()
        return {
            "execution_id": self.id,
            "execution_mode": self.mode,
            "status": self.status,
            "lineage": DataFormatter.sanitize(self.lineage),
            "limits": DataFormatter.sanitize(self.limits),
            "route_plan": DataFormatter.sanitize(self.route_plan),
            "route": DataFormatter.sanitize(self.route_info),
            "close_snapshot": DataFormatter.sanitize(self.close_snapshot),
            "logs": DataFormatter.sanitize(self.logs),
            "diagnostics": DataFormatter.sanitize(self.diagnostics),
            "workspace_refs": DataFormatter.sanitize(self.workspace_refs),
        }

    async def async_record_workspace(
        self,
        *,
        collection: str = "observations",
        kind: str | None = "agent_execution_observation",
        content: Any = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        checkpoint: bool = False,
        checkpoint_state: dict[str, Any] | None = None,
        checkpoint_step_id: str | None = None,
        profile: str = "fast",
    ) -> dict[str, Any]:
        if self.workspace is None:
            raise RuntimeError(
                "AgentExecution has no Workspace binding. "
                "Call agent.use_workspace(...) before create_execution(...)."
            )
        if not self._completed:
            await self.async_get_data()
        self._refresh_diagnostics()

        record_scope = self._workspace_scope(scope)
        record_source = self._workspace_source(source)
        record_meta = {
            "execution_id": self.id,
            "execution_mode": self.mode,
            "lineage": DataFormatter.sanitize(self.lineage),
        }
        record_meta.update(dict(meta or {}))
        record_content = content if content is not None else self._default_workspace_content()
        record_summary = summary or self._default_workspace_summary(collection)

        record_ref = await self.workspace.ingest(
            content=record_content,
            collection=collection,
            kind=kind,
            scope=record_scope,
            source=record_source,
            summary=record_summary,
            meta=record_meta,
            profile=profile,
        )
        self._append_workspace_ref(collection, record_ref)

        checkpoint_ref = None
        if checkpoint:
            checkpoint_run_id = str(record_scope.get("task_id") or self.lineage.get("task_id") or self.id)
            checkpoint_ref = await self.workspace.checkpoint(
                checkpoint_run_id,
                checkpoint_state or self._default_checkpoint_state(record_ref),
                step_id=checkpoint_step_id or self.lineage.get("step_id"),
            )
            self._append_workspace_ref("checkpoints", checkpoint_ref)

        return DataFormatter.sanitize(
            {
                "record": record_ref,
                "checkpoint": checkpoint_ref,
                "workspace_refs": self.workspace_refs,
            }
        )

    async def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "all"] | str | None = "instant",
        content: Any = None,
        **_: Any,
    ) -> AsyncGenerator[Any, None]:
        if content is not None and type is None:
            type = content
        if self._completed:
            for item in self.stream.items:
                yield ("agent_execution", item) if type == "all" else item
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in self.stream.items:
            await queue.put(item)
        self.stream.queues.append(queue)
        start_task = asyncio.create_task(self.async_start())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield ("agent_execution", item) if type == "all" else item
            await start_task
        finally:
            if queue in self.stream.queues:
                self.stream.queues.remove(queue)

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return FunctionShifter.syncify_async_generator(self.get_async_generator(*args, **kwargs))

    def _refresh_diagnostics(self):
        context_diagnostics = self.execution_context.diagnostics()
        budget = context_diagnostics.get("budget", {})
        limit_events = context_diagnostics.get("limit_events", [])
        self.diagnostics["budget"] = budget
        if limit_events:
            self.diagnostics["limit_events"] = limit_events
        for key in ("stages", "last_progress"):
            value = context_diagnostics.get(key)
            if value:
                self.diagnostics[key] = value

    def _record_error_diagnostic(self, error: BaseException):
        errors = self.diagnostics.setdefault("errors", [])
        if isinstance(errors, list):
            item = (
                error.to_diagnostic()
                if isinstance(error, (AgentExecutionLimitExceeded, RuntimeStageStallError))
                else {"type": error.__class__.__name__, "message": str(error)}
            )
            errors.append(item)
            if isinstance(error, RuntimeStageStallError):
                target_key = "timeouts" if error.status == "timed_out" else "stalls"
                target = self.diagnostics.setdefault(target_key, [])
                if isinstance(target, list):
                    target.append(item)

    def raise_if_limit_exceeded(self):
        self.execution_context.raise_if_limit_exceeded()

    def _workspace_scope(self, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        lineage_scope = self.lineage.get("scope")
        merged = dict(lineage_scope) if isinstance(lineage_scope, dict) else {}
        for key in ("task_id", "iteration_id", "step_id"):
            value = self.lineage.get(key)
            if value is not None:
                merged.setdefault(key, value)
        merged.update(dict(scope or {}))
        return DataFormatter.sanitize(merged)

    def _workspace_source(self, source: dict[str, Any] | None = None) -> dict[str, Any]:
        default_source = {
            "type": "agent_execution",
            "execution_id": self.id,
            "execution_mode": self.mode,
            "task_id": self.lineage.get("task_id"),
            "iteration_id": self.lineage.get("iteration_id"),
            "step_id": self.lineage.get("step_id"),
        }
        default_source.update(dict(source or {}))
        return DataFormatter.sanitize(default_source)

    def _default_workspace_content(self) -> dict[str, Any]:
        return DataFormatter.sanitize(
            {
                "execution_id": self.id,
                "execution_mode": self.mode,
                "status": self.status,
                "lineage": self.lineage,
                "result": self.result,
                "route_plan": self.route_plan,
                "diagnostics": self.diagnostics,
            }
        )

    def _default_workspace_summary(self, collection: str) -> str:
        task_id = self.lineage.get("task_id") or self.id
        step_id = self.lineage.get("step_id") or self.mode
        return f"{ task_id } { step_id } AgentExecution { collection }"

    def _default_checkpoint_state(self, record_ref: dict[str, Any]) -> dict[str, Any]:
        return DataFormatter.sanitize(
            {
                "execution_id": self.id,
                "execution_mode": self.mode,
                "status": self.status,
                "lineage": self.lineage,
                "record_ref": record_ref,
                "diagnostics": self.diagnostics,
            }
        )

    def _append_workspace_ref(self, key: str, ref: dict[str, Any]):
        ref_id = ref.get("id")
        if not ref_id:
            return
        refs = self.workspace_refs.setdefault(key, [])
        if isinstance(refs, list) and ref_id not in refs:
            refs.append(ref_id)
