# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import uuid

from .TaskShared import *


_LIFECYCLE_STAGE_NAMES = (
    "context.prepare",
    "work.plan",
    "work.execute",
    "outputs.materialize",
    "evidence.ingest",
    "terminal.verify",
)

_LIFECYCLE_STAGE_EVENTS = {
    "context.prepare": "agent_task.lifecycle.context.prepared",
    "work.plan": "agent_task.lifecycle.work.planned",
    "work.execute": "agent_task.lifecycle.work.executed",
    "outputs.materialize": "agent_task.lifecycle.outputs.materialized",
    "evidence.ingest": "agent_task.lifecycle.evidence.ingested",
    "terminal.verify": "agent_task.lifecycle.terminal.verified",
}


class AgentTaskLifecycleFlowMixin(AgentTaskMixinBase):
    """Own the visible TriggerFlow lifecycle and its versioned short signals."""

    _lifecycle_error: BaseException | None

    async def _allocate_lifecycle_identity(
        self,
        kind: str,
        *,
        prefix: str,
    ) -> str:
        _ = kind
        return f"{prefix}_{uuid.uuid4().hex}"

    async def _allocate_lifecycle_frame_id(self) -> str:
        return await self._allocate_lifecycle_identity("frame", prefix="frm")

    def _require_lifecycle_signal(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("AgentTask lifecycle signals must be mappings.")
        task_id = str(value.get("task_id") or "").strip()
        if task_id != self.id:
            raise ValueError("AgentTask lifecycle signal belongs to a different task.")
        frame_id = str(value.get("frame_id") or "").strip()
        if not frame_id:
            raise ValueError("AgentTask lifecycle signal requires frame_id.")
        current_frame_id = str(self._lifecycle_state.current_frame_id or "").strip()
        if current_frame_id and frame_id != current_frame_id:
            raise ValueError("AgentTask lifecycle signal belongs to a stale or different frame.")
        raw_state_version = value.get("state_version")
        raw_iteration = value.get("iteration")
        if raw_state_version is None or raw_iteration is None:
            raise ValueError(
                "AgentTask lifecycle signal requires integer state_version and iteration."
            )
        try:
            state_version = int(raw_state_version)
            iteration = int(raw_iteration)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "AgentTask lifecycle signal requires integer state_version and iteration."
            ) from error
        self._lifecycle_state.require_version(state_version)
        if iteration != self._lifecycle_state.iteration:
            raise ValueError("AgentTask lifecycle signal belongs to a stale iteration.")
        return {
            "task_id": task_id,
            "state_version": state_version,
            "frame_id": frame_id,
            "iteration": iteration,
            **{
                field: str(value.get(field) or "").strip()
                for field in ("plan_id", "work_result_id", "evidence_ref")
                if str(value.get(field) or "").strip()
            },
        }

    async def _open_lifecycle_frame(
        self,
        iteration: int,
        *,
        carry: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        frame_id = await self._allocate_lifecycle_frame_id()
        state_version = self._lifecycle_state.open_frame(
            frame_id,
            expected_version=self._lifecycle_state.state_version,
            iteration=iteration,
        )
        self._lifecycle_frames[frame_id] = {
            "task_id": self.id,
            "frame_id": frame_id,
            "iteration": iteration,
            "strategy": self.effective_execution_strategy,
            **dict(carry or {}),
        }
        return {
            "task_id": self.id,
            "state_version": state_version,
            "frame_id": frame_id,
            "iteration": iteration,
        }

    def _lifecycle_signal_from_data(
        self,
        data: TriggerFlowRuntimeData[Any, Any, Any],
    ) -> dict[str, Any]:
        for value in (data.value, data.input):
            if isinstance(value, Mapping) and value.get("frame_id"):
                return self._require_lifecycle_signal(value)
        raise ValueError("TriggerFlow lifecycle stage did not receive a frame signal.")

    def _advance_lifecycle_signal(
        self,
        signal: Mapping[str, Any],
        *,
        phase: str,
        prevalidated: bool = False,
    ) -> dict[str, Any]:
        normalized = (
            dict(signal)
            if prevalidated
            else self._require_lifecycle_signal(signal)
        )
        frame = self._lifecycle_frames.get(str(normalized.get("frame_id") or ""), {})
        state_version = self._lifecycle_state.advance(
            phase,
            expected_version=self._lifecycle_state.state_version,
            iteration=normalized["iteration"],
            current_plan_id=(
                str(frame.get("plan_id")) if frame.get("plan_id") else None
            ),
            work_result_id=(
                str(frame.get("work_result_id"))
                if frame.get("work_result_id")
                else None
            ),
            evidence_ref=(
                str(frame.get("evidence_ref"))
                if frame.get("evidence_ref")
                else None
            ),
        )
        return {
            **normalized,
            "state_version": state_version,
            **{
                field: str(frame.get(field))
                for field in ("plan_id", "work_result_id", "evidence_ref")
                if frame.get(field)
            },
        }

    async def _ensure_lifecycle_stage_identity(
        self,
        frame: dict[str, Any],
        *,
        phase: str,
    ) -> None:
        if phase == "work.plan" and not frame.get("plan_id"):
            frame["plan_id"] = await self._allocate_lifecycle_identity(
                "plan",
                prefix="pln",
            )
        elif phase == "work.execute" and not frame.get("work_result_id"):
            frame["work_result_id"] = await self._allocate_lifecycle_identity(
                "work_result",
                prefix="wrk",
            )
        elif phase == "evidence.ingest" and not frame.get("evidence_ref"):
            frame["evidence_ref"] = await self._allocate_lifecycle_identity(
                "evidence",
                prefix="evd",
            )

    def _build_flow(self):
        flow = TriggerFlow(name=f"agent-task-lifecycle-{self.id}")
        iteration_requested_event = (
            f"agent_task.lifecycle.iteration.requested.{self.id}"
        )
        transition_requested_event = (
            f"agent_task.lifecycle.transition.requested.{self.id}"
        )
        terminal_verification_retry_event = (
            f"agent_task.lifecycle.terminal.verification.retry.requested.{self.id}"
        )
        stage_output_events = {
            **_LIFECYCLE_STAGE_EVENTS,
            "terminal.verify": transition_requested_event,
        }

        async def lifecycle_start(data: TriggerFlowRuntimeData[Any, Any, Any]):
            await data.async_set_state("task_id", self.id, emit=False)
            await data.async_set_state(
                "agent_task.terminal_convergence",
                self._terminal_convergence_state.snapshot(),
                emit=False,
            )
            try:
                effective_strategy = (
                    await self._resolve_effective_execution_strategy()
                )
            except _AgentTaskDeadlineExceeded as error:
                await self._emit("agent_task.started", self._task_summary())
                await self._terminate_timed_out(
                    0,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state(
                    "agent_task.execution_strategy",
                    self.execution_strategy,
                    emit=False,
                )
                await data.async_set_state(
                    "agent_task.effective_execution_strategy",
                    self.effective_execution_strategy,
                    emit=False,
                )
                await data.async_set_state("agent_task.result", self.result, emit=False)
                await data.async_set_state("agent_task.status", self.status, emit=False)
                return {"terminal": True, "status": self.status}
            await data.async_set_state(
                "agent_task.execution_strategy",
                self.execution_strategy,
                emit=False,
            )
            await data.async_set_state(
                "agent_task.effective_execution_strategy",
                effective_strategy,
                emit=False,
            )
            await self._emit("agent_task.started", self._task_summary())
            start_iteration = self._resumed_from_iteration + 1
            if start_iteration > 1:
                await self._emit(
                    "agent_task.resumed",
                    {
                        "task_id": self.id,
                        "resumed_from_iteration": self._resumed_from_iteration,
                    },
                )
            signal = await self._open_lifecycle_frame(start_iteration)
            await data.async_set_state(
                "agent_task.lifecycle_topology",
                {
                    "nodes": ["lifecycle.start", *_LIFECYCLE_STAGE_NAMES, "transition.decide"],
                    "iteration_requested_event": iteration_requested_event,
                    "transition_requested_event": transition_requested_event,
                    "terminal_verification_retry_event": terminal_verification_retry_event,
                    "stage_events": dict(stage_output_events),
                    "signal_schema": {
                        "required": [
                            "task_id",
                            "state_version",
                            "frame_id",
                            "iteration",
                        ],
                        "optional": ["plan_id", "work_result_id", "evidence_ref"],
                    },
                    "taskboard_work_owner": {
                        "node": "work.execute",
                        "nested_flow": "task_board.lifecycle",
                        "outer_terminal_nodes": [
                            "outputs.materialize",
                            "evidence.ingest",
                            "terminal.verify",
                            "transition.decide",
                        ],
                    },
                },
                emit=False,
            )
            await data.async_emit_nowait(iteration_requested_event, signal)
            return signal

        def lifecycle_stage(
            phase: str,
            flat_handler_name: str,
            *,
            taskboard_handler_name: str | None = None,
        ):
            async def handler(data: TriggerFlowRuntimeData[Any, Any, Any]):
                try:
                    signal = self._lifecycle_signal_from_data(data)
                    frame = self._lifecycle_frames[signal["frame_id"]]
                    if self.effective_execution_strategy == "taskboard":
                        if taskboard_handler_name is not None:
                            taskboard_handler = getattr(self, taskboard_handler_name)
                            frame = await taskboard_handler(frame)
                    else:
                        flat_handler = getattr(self, flat_handler_name)
                        frame = await flat_handler(frame)
                    self._lifecycle_frames[signal["frame_id"]] = frame
                    await self._ensure_lifecycle_stage_identity(frame, phase=phase)
                    next_signal = self._advance_lifecycle_signal(
                        signal,
                        phase=phase,
                        prevalidated=True,
                    )
                    next_event = (
                        transition_requested_event
                        if frame.get("iteration_result") is not None
                        else stage_output_events[phase]
                    )
                    await data.async_emit_nowait(next_event, next_signal)
                    return next_signal
                except Exception as error:
                    self._lifecycle_error = error
                    raise

            return handler

        async def transition_decide(data: TriggerFlowRuntimeData[Any, Any, Any]):
            try:
                signal = self._lifecycle_signal_from_data(data)
                frame = self._lifecycle_frames[signal["frame_id"]]
                result = frame.get("iteration_result")
                if self.effective_execution_strategy == "taskboard":
                    frame = await self._taskboard_transition_decide_stage(frame)
                    self._lifecycle_frames[signal["frame_id"]] = frame
                    result = frame.get("iteration_result")
                else:
                    frame = await self._flat_transition_decide_stage(frame)
                    self._lifecycle_frames[signal["frame_id"]] = frame
                    result = frame.get("iteration_result")
                if not isinstance(result, Mapping):
                    raise ValueError("AgentTask lifecycle frame has no structured iteration result.")
                decided_signal = self._advance_lifecycle_signal(
                    signal,
                    phase="transition.decide",
                    prevalidated=True,
                )
                await data.async_set_state(
                    "agent_task.latest_iteration",
                    DataFormatter.sanitize(result),
                    emit=False,
                )
                await data.async_set_state(
                    "agent_task.terminal_convergence",
                    self._terminal_convergence_state.snapshot(),
                    emit=False,
                )
                if (
                    result.get("terminal") is False
                    and result.get("status") == "verification_retry"
                ):
                    frame.pop("iteration_result", None)
                    frame.pop("taskboard_transition_result", None)
                    self._lifecycle_frames[signal["frame_id"]] = frame
                    await data.async_emit_nowait(
                        terminal_verification_retry_event,
                        decided_signal,
                    )
                    return {
                        **decided_signal,
                        "terminal": False,
                        "status": "verification_retry",
                    }
                if result.get("terminal") is True:
                    await data.async_set_state("agent_task.result", self.result, emit=False)
                    await data.async_set_state("agent_task.status", self.status, emit=False)
                    self._lifecycle_frames.pop(signal["frame_id"], None)
                    return {**decided_signal, "terminal": True, "status": self.status}
                next_signal = await self._open_lifecycle_frame(
                    signal["iteration"] + 1,
                    carry=(
                        frame.get("next_frame_state")
                        if isinstance(frame.get("next_frame_state"), Mapping)
                        else None
                    ),
                )
                self._lifecycle_frames.pop(signal["frame_id"], None)
                await data.async_emit_nowait(iteration_requested_event, next_signal)
                return {**decided_signal, "terminal": False, "status": "continue"}
            except Exception as error:
                self._lifecycle_error = error
                raise

        flow.to(lifecycle_start, name="lifecycle.start")
        flat_stage_handlers = {
            "context.prepare": "_flat_context_prepare_stage",
            "work.plan": "_flat_work_plan_stage",
            "work.execute": "_flat_work_execute_stage",
            "outputs.materialize": "_flat_outputs_materialize_stage",
            "evidence.ingest": "_flat_evidence_ingest_stage",
            "terminal.verify": "_flat_terminal_verify_stage",
        }
        taskboard_stage_handlers = {
            "context.prepare": "_taskboard_context_prepare_stage",
            "work.plan": "_taskboard_work_plan_stage",
            "work.execute": "_taskboard_work_execute_stage",
            "outputs.materialize": "_taskboard_outputs_materialize_stage",
            "evidence.ingest": "_taskboard_evidence_ingest_stage",
            "terminal.verify": "_taskboard_terminal_verify_stage",
        }
        stage_input_events = {
            "context.prepare": iteration_requested_event,
            "work.plan": stage_output_events["context.prepare"],
            "work.execute": stage_output_events["work.plan"],
            "outputs.materialize": stage_output_events["work.execute"],
            "evidence.ingest": stage_output_events["outputs.materialize"],
            "terminal.verify": stage_output_events["evidence.ingest"],
        }
        for stage_name in _LIFECYCLE_STAGE_NAMES:
            if stage_name == "terminal.verify":
                stage_process = flow.when(
                    [
                        stage_input_events[stage_name],
                        terminal_verification_retry_event,
                    ],
                    mode="simple_or",
                )
            else:
                stage_process = flow.when(stage_input_events[stage_name])
            stage_process.to(
                lifecycle_stage(
                    stage_name,
                    flat_stage_handlers[stage_name],
                    taskboard_handler_name=taskboard_stage_handlers.get(stage_name),
                ),
                name=stage_name,
            )
        flow.when(transition_requested_event).to(
            transition_decide,
            name="transition.decide",
        )
        return flow


__all__: list[str] = []
