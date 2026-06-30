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

from .TaskShared import *


class AgentTaskObservationMixin(AgentTaskMixinBase):
    async def _record_decision(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> "WorkspaceRecordRef":
        record_ref = await self.workspace.ingest(
            content={
                "iteration": iteration_index,
                "plan": DataFormatter.sanitize(plan),
                "process_summary": self._process_summary_from_value(plan, stage="plan"),
                "context_pack_diagnostics": DataFormatter.sanitize(context_pack.get("diagnostics", {})),
                "context_item_count": len(context_pack.get("items", [])),
            },
            collection="decisions",
            kind="agent_task_decision",
            summary=f"{self.id} iteration {iteration_index} planning decision",
            scope={"task_id": self.id, "iteration": iteration_index},
            source={"type": "agent_task", "phase": "plan"},
            meta={"task_id": self.id, "iteration": iteration_index},
        )
        self._append_workspace_ref("decisions", record_ref)
        return record_ref

    async def _record_observation(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        decision_ref: "WorkspaceRecordRef",
        execution_result: Any,
        execution_meta: dict[str, Any],
    ) -> tuple["WorkspaceRecordRef", "WorkspaceRecordRef | None"]:
        record_ref = await self.workspace.ingest(
            content={
                "iteration": iteration_index,
                "plan": DataFormatter.sanitize(plan),
                "decision_ref": decision_ref,
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta": DataFormatter.sanitize(execution_meta),
                "process_summary": self._process_summary_from_value(execution_result, stage="execution"),
            },
            collection="observations",
            kind="agent_task_observation",
            summary=f"{self.id} iteration {iteration_index} execution observation",
            scope={"task_id": self.id, "iteration": iteration_index},
            source={"type": "agent_task", "phase": "execute", "execution_id": execution_meta.get("execution_id")},
            meta={"task_id": self.id, "iteration": iteration_index},
        )
        checkpoint_ref = await self.workspace.put_checkpoint(
            self.id,
            {
                "task_id": self.id,
                "iteration": iteration_index,
                "status": self.status,
                "decision_ref": decision_ref,
                "observation_ref": record_ref,
            },
            step_id=f"iteration-{iteration_index}",
        )
        decision_link = await self.workspace.link_evidence(
            record_ref,
            decision_ref,
            relation="implements_decision",
            execution_id=str(execution_meta.get("execution_id") or "") or None,
            checkpoint_id=checkpoint_ref.get("id"),
            meta={"owner": "AgentTask", "task_id": self.id, "iteration": iteration_index},
        )
        checkpoint_link = await self.workspace.link_evidence(
            record_ref,
            checkpoint_ref,
            relation="checkpointed_by",
            execution_id=str(execution_meta.get("execution_id") or "") or None,
            checkpoint_id=checkpoint_ref.get("id"),
            meta={"owner": "AgentTask", "task_id": self.id, "iteration": iteration_index},
        )
        self._append_workspace_ref("observations", record_ref)
        self._append_workspace_ref("checkpoints", checkpoint_ref)
        self._append_workspace_ref("evidence_links", decision_link)
        self._append_workspace_ref("evidence_links", checkpoint_link)
        await self._emit(
            "agent_task.checkpoint",
            {"iteration": iteration_index, "checkpoint": checkpoint_ref},
        )
        await self._emit_action_observation_events(iteration_index, execution_meta=execution_meta)
        return record_ref, checkpoint_ref

    async def _record_taskboard_checkpoint(
        self,
        *,
        stage: str,
        tick_index: int,
        revision: Any,
        runtime_topology: Mapping[str, Any] | None = None,
        terminal_reason: str | None = None,
        final_result: Mapping[str, Any] | None = None,
    ) -> tuple["WorkspaceRecordRef | None", "WorkspaceRecordRef | None"]:
        try:
            effective_revision = TaskBoardRevision.from_value(revision)
            revision_dict = effective_revision.to_dict()
            evidence_view = build_task_board_evidence_view(effective_revision).to_dict()
            revision_id = str(effective_revision.revision_id)
            step_id = f"taskboard-{stage}-{tick_index}-{revision_id}"
            record_ref = await self.workspace.ingest(
                content={
                    "schema_version": "agent_task_taskboard_checkpoint/v1",
                    "task_id": self.id,
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                    "status": self.status,
                    "revision": DataFormatter.sanitize(revision_dict),
                    "evidence_view": DataFormatter.sanitize(evidence_view),
                    "runtime_topology": DataFormatter.sanitize(runtime_topology or {}),
                    "terminal_reason": terminal_reason,
                    "final_result": DataFormatter.sanitize(final_result or {}),
                },
                collection="observations",
                kind="agent_task_taskboard_checkpoint",
                summary=f"{self.id} TaskBoard {stage} checkpoint {revision_id}",
                scope={
                    "task_id": self.id,
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                    "revision_id": revision_id,
                },
                source={"type": "agent_task", "phase": "taskboard_checkpoint", "stage": stage},
                meta={
                    "task_id": self.id,
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                    "revision_id": revision_id,
                },
            )
            checkpoint_ref = await self.workspace.put_checkpoint(
                self.id,
                {
                    "schema_version": "agent_task_taskboard_checkpoint/v1",
                    "task_id": self.id,
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                    "step_id": step_id,
                    "status": self.status,
                    "revision_id": revision_id,
                    "revision_ref": record_ref.get("id"),
                    "terminal_reason": terminal_reason,
                    "final_status": (final_result or {}).get("status"),
                    "accepted": (final_result or {}).get("accepted"),
                },
                step_id=step_id,
            )
            checkpoint_link = await self.workspace.link_evidence(
                record_ref,
                checkpoint_ref,
                relation="checkpointed_by",
                checkpoint_id=checkpoint_ref.get("id"),
                meta={
                    "owner": "AgentTask",
                    "task_id": self.id,
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                },
            )
            self._append_workspace_ref("observations", record_ref)
            self._append_workspace_ref("checkpoints", checkpoint_ref)
            self._append_workspace_ref("evidence_links", checkpoint_link)
            await self._emit(
                "agent_task.checkpoint",
                {"iteration": tick_index, "strategy": "taskboard", "checkpoint": checkpoint_ref},
            )
            await self._emit(
                "agent_task.taskboard.checkpoint",
                {
                    "stage": stage,
                    "tick_index": tick_index,
                    "revision_id": revision_id,
                    "checkpoint": checkpoint_ref,
                    "revision_ref": record_ref,
                },
            )
            await self._write_taskboard_resume_snapshot(
                stage=stage,
                tick_index=tick_index,
                revision=effective_revision,
                evidence_view=evidence_view,
                runtime_topology=runtime_topology or {},
                terminal_reason=terminal_reason,
                final_result=final_result,
            )
            return record_ref, checkpoint_ref
        except Exception as error:
            self.diagnostics.setdefault("taskboard_checkpoint_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "stage": stage,
                    "tick_index": tick_index,
                }
            )
            return None, None

    async def _record_verification(
        self,
        iteration_index: int,
        verification: dict[str, Any],
        observation_ref: "WorkspaceRecordRef",
    ) -> "WorkspaceRecordRef":
        record_ref = await self.workspace.ingest(
            content={
                "iteration": iteration_index,
                "verification": DataFormatter.sanitize(verification),
                "observation_ref": observation_ref,
                "process_summary": self._process_summary_from_value(verification, stage="verification"),
            },
            collection="verification",
            kind="agent_task_verification",
            summary=f"{self.id} iteration {iteration_index} verification",
            scope={"task_id": self.id, "iteration": iteration_index},
            source={"type": "agent_task", "phase": "verify"},
            meta={"task_id": self.id, "iteration": iteration_index},
        )
        evidence_link = await self.workspace.link_evidence(
            record_ref,
            observation_ref,
            relation="verifies_observation",
            meta={"owner": "AgentTask", "task_id": self.id, "iteration": iteration_index},
        )
        self._append_workspace_ref("verification", record_ref)
        self._append_workspace_ref("evidence_links", evidence_link)
        return record_ref

    def _reflection_density(self) -> str:
        agent_task_options = self.options.get("agent_task")
        effort = agent_task_options.get("effort") if isinstance(agent_task_options, Mapping) else None
        effort = effort if isinstance(effort, Mapping) else {}
        density = str(effort.get("reflection_density") or "").strip().lower()
        if density in {"final", "major_node", "action"}:
            return density
        name = str(effort.get("name") or self._taskboard_effort() or "medium").strip().lower()
        if name in {"minimal", "low", "fast"}:
            return "final"
        if name in {"high", "max"}:
            return "action"
        return "major_node"

    def _should_record_process_reflection(self, phase: str, *, plan: dict[str, Any]) -> bool:
        density = self._reflection_density()
        if density == "action":
            return phase in {"bounded_step", "major_node", "taskboard_card", "acp_call"}
        if density == "major_node":
            return phase in {"major_node", "taskboard_card"}
        if phase != "major_node":
            return False
        marker = plan.get("important") or plan.get("importance") or plan.get("reflection_required")
        return bool(marker is True or str(marker).strip().lower() in {"important", "high", "required"})

    def _bounded_step_reflection_summary(
        self,
        *,
        plan: dict[str, Any],
        execution_meta: dict[str, Any],
        execution_failed: bool,
    ) -> dict[str, Any]:
        return {
            "assessment": (
                "bounded step failed and requires repair" if execution_failed else "bounded step produced evidence"
            ),
            "status": "failed" if execution_failed else "observed",
            "execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "")),
            "execution_id": execution_meta.get("execution_id"),
            "route": execution_meta.get("route", {}),
            "completion_evidence": False,
        }

    @staticmethod
    def _major_node_reflection_summary(*, verification: dict[str, Any]) -> dict[str, Any]:
        return {
            "assessment": str(verification.get("reason") or ""),
            "status": "accepted" if verification.get("is_complete") else "needs_replan",
            "missing_criteria": DataFormatter.sanitize(verification.get("missing_criteria", [])),
            "acceptance_delta": DataFormatter.sanitize(verification.get("acceptance_delta", [])),
            "completion_evidence": False,
        }

    async def _record_reflection(
        self,
        iteration_index: int,
        *,
        phase: str,
        subject_ref: "WorkspaceRecordRef | None",
        summary: dict[str, Any],
    ) -> "WorkspaceRecordRef | None":
        content = {
            "task_id": self.id,
            "iteration": iteration_index,
            "phase": phase,
            "reflection_density": self._reflection_density(),
            "summary": DataFormatter.sanitize(summary),
            "subject_ref": DataFormatter.sanitize(subject_ref),
            "completion_evidence": False,
        }
        try:
            record_ref = await self.workspace.ingest(
                content=content,
                collection="reflections",
                kind="agent_task_reflection",
                summary=f"{self.id} iteration {iteration_index} {phase} reflection",
                scope={"task_id": self.id, "iteration": iteration_index},
                source={"type": "agent_task", "phase": "reflect", "reflection_phase": phase},
                meta={"task_id": self.id, "iteration": iteration_index, "completion_evidence": False},
            )
            if subject_ref:
                evidence_link = await self.workspace.link_evidence(
                    record_ref,
                    subject_ref,
                    relation="reflects_on",
                    meta={
                        "owner": "AgentTask",
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "completion_evidence": False,
                    },
                )
                self._append_workspace_ref("evidence_links", evidence_link)
            self._append_workspace_ref("reflections", record_ref)
            reflection_summary = {
                "iteration": iteration_index,
                "phase": phase,
                "record_ref": record_ref,
                "summary": DataFormatter.sanitize(summary),
                "completion_evidence": False,
            }
            self.reflections.append(reflection_summary)
            await self._emit(
                f"agent_task.iteration.{iteration_index}.reflection.{phase}",
                {"record": record_ref, "summary": reflection_summary["summary"]},
            )
            return record_ref
        except Exception as error:
            self.diagnostics.setdefault("reflection_record_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "phase": phase,
                }
            )
            return None

    async def _ensure_final_reflection(self) -> None:
        if any(item.get("phase") == "final" for item in self.reflections if isinstance(item, dict)):
            return
        await self._record_reflection(
            max(0, len(self.iterations)),
            phase="final",
            subject_ref=None,
            summary={
                "assessment": str((self.result or {}).get("reason") or self.status),
                "status": self.status,
                "accepted": bool((self.result or {}).get("accepted")),
                "artifact_status": (self.result or {}).get("artifact_status"),
                "completion_evidence": False,
            },
        )

    def _append_workspace_ref(self, collection: str, ref: dict[str, Any] | None):
        if not ref:
            return
        bucket = self.workspace_refs.setdefault(collection, [])
        ref_id = str(ref.get("id") or "")
        if ref_id and ref_id not in bucket:
            bucket.append(ref_id)

    async def async_meta(self) -> dict[str, Any]:
        if not self._completed:
            await self.async_run()
        return {
            "task_id": self.id,
            "status": self.status,
            "goal": self.goal,
            "success_criteria": DataFormatter.sanitize(self.success_criteria),
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
            "max_iterations": self.max_iterations,
            "iterations": DataFormatter.sanitize(self.iterations),
            "reflections": DataFormatter.sanitize(self.reflections),
            "resumed_from_iteration": self._resumed_from_iteration,
            "resumed_iteration_summaries": DataFormatter.sanitize(self._resumed_iteration_summaries),
            "result": DataFormatter.sanitize(self.result),
            "diagnostics": DataFormatter.sanitize(self.diagnostics),
            "workspace_refs": DataFormatter.sanitize(self.workspace_refs),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def _meta(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_meta())
        return self.async_meta()

    async def get_async_generator(
        self,
        type: Literal["delta", "instant", "streaming_parse", "all"] | str | None = "delta",
        content: Any = None,
        **__,
    ) -> AsyncGenerator[Any, None]:
        if content is not None and type is None:
            type = content
        if self._completed:
            for item in self._stream_items:
                projected = self._project_stream_item(item, type)
                if projected is not None:
                    yield projected
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in self._stream_items:
            await queue.put(item)
        self._stream_queues.append(queue)
        start_task = asyncio.create_task(self.async_run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                projected = self._project_stream_item(item, type)
                if projected is not None:
                    yield projected
            await start_task
        finally:
            if queue in self._stream_queues:
                self._stream_queues.remove(queue)

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return FunctionShifter.syncify_async_generator(self.get_async_generator(*args, **kwargs))

    @staticmethod
    def _project_stream_item(item: Any, type: Any) -> Any:
        if type == "all":
            return ("agent_task", item)
        if type == "delta":
            return project_agent_execution_text_delta(item)
        return item

    async def _emit_progress(
        self,
        iteration: int | None,
        stage: str,
        message: str,
    ) -> AgentExecutionStreamData | None:
        if not self._stream_progress_enabled():
            return None
        if self._progress_model_key() is not None:
            return None
        emit_coro = self._emit(
            (
                f"agent_task.progress.{stage}"
                if iteration is None
                else f"agent_task.iteration.{iteration}.progress.{stage}"
            ),
            {
                "message": message,
                "iteration": iteration,
                "stage": stage,
                "status": self.status,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "progress",
                "progress_source": "template",
            },
        )
        if self._stream_progress_background_enabled():
            task = asyncio.create_task(emit_coro)
            self._track_background_stream_task(task)
            return None
        return await emit_coro

    async def _emit_snapshot(
        self,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
        *,
        message: str,
    ) -> AgentExecutionStreamData | None:
        if not self._stream_snapshots_enabled():
            return None
        item = await self._emit(
            f"agent_task.iteration.{iteration}.snapshot.{stage}",
            {
                "message": message,
                "iteration": iteration,
                "stage": stage,
                "snapshot": snapshot,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "snapshot",
            },
        )
        self._schedule_model_progress_from_snapshot(
            iteration=iteration,
            stage=stage,
            snapshot=snapshot,
        )
        return item

    def _agent_task_option(self, key: str, default: Any = None) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict) and key in agent_task_options:
            return agent_task_options.get(key)
        return self.options.get(key, default)

    def _stream_progress_enabled(self) -> bool:
        return self._normalize_bool(self._agent_task_option("stream_progress", False), default=False)

    def _stream_progress_background_enabled(self) -> bool:
        return self._normalize_bool(
            self._agent_task_option("stream_progress_background", True),
            default=True,
        )

    def _stream_snapshots_enabled(self) -> bool:
        return self._normalize_bool(self._agent_task_option("stream_snapshots", True), default=True)

    def _progress_model_key(self) -> str | None:
        model_key = self._agent_task_option("progress_model_key", None) or self._agent_task_option(
            "stream_progress_model_key", None
        )
        if model_key is None:
            return None
        normalized = str(model_key).strip()
        return normalized or None

    def _progress_timeout_seconds(self) -> float:
        timeout = self._agent_task_option("progress_timeout_seconds", 20)
        normalized = self._normalize_timeout(timeout)
        return 20.0 if normalized is None else normalized

    def _schedule_model_progress_from_snapshot(
        self,
        *,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
    ) -> None:
        if not self._stream_progress_enabled():
            return
        model_key = self._progress_model_key()
        if model_key is None:
            return
        task = asyncio.create_task(
            self._emit_model_progress_from_snapshot(
                iteration=iteration,
                stage=stage,
                snapshot=self._operator_safe_progress_snapshot(stage, DataFormatter.sanitize(snapshot)),
                model_key=model_key,
            )
        )
        self._track_background_stream_task(task)

    async def _emit_model_progress_from_snapshot(
        self,
        *,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
        model_key: str,
    ) -> AgentExecutionStreamData | None:
        try:
            request = self.agent.create_temp_request(model_key=model_key)
            progress_language = self._progress_language()
            request.set_settings("runtime.side_channel", True)
            request.set_settings("model_request.side_channel", True)
            request.input(
                {
                    "task_id": self.id,
                    "goal": self.goal,
                    "success_criteria": self.success_criteria,
                    "iteration": iteration,
                    "stage": stage,
                    "status": self.status,
                    "progress_language": progress_language,
                    "snapshot": snapshot,
                }
            )
            request.instruct(
                "Summarize AgentTask progress for a human operator using only the provided snapshot and task metadata. "
                "Do not add new facts, do not infer hidden results, and keep the message concise. "
                f"Write the message in this language: { progress_language }."
            )
            request.output(
                {
                    "message": (str, "One concise natural-language progress update.", True),
                },
                format="json",
            )
            result = request.get_result()
            streamed_message = ""
            final_stream_value = ""
            async for item in result.get_async_generator(type="instant"):
                raw_path = str(getattr(item, "path", "") or getattr(item, "wildcard_path", "") or "")
                if raw_path != "message" and not raw_path.endswith(".message"):
                    continue
                delta = getattr(item, "delta", None)
                value = getattr(item, "value", None)
                event_type = getattr(item, "event_type", None)
                if isinstance(delta, str) and delta:
                    streamed_message += delta
                    await self._emit_progress_delta(
                        iteration=iteration,
                        stage=stage,
                        delta=delta,
                        message_so_far=streamed_message,
                        model_key=model_key,
                        language=progress_language,
                    )
                elif event_type == "delta" and isinstance(value, str) and value:
                    suffix = value[len(streamed_message) :] if value.startswith(streamed_message) else value
                    if suffix:
                        streamed_message += suffix
                        await self._emit_progress_delta(
                            iteration=iteration,
                            stage=stage,
                            delta=suffix,
                            message_so_far=streamed_message,
                            model_key=model_key,
                            language=progress_language,
                        )
                elif bool(getattr(item, "is_complete", False)) and isinstance(value, str):
                    final_stream_value = value
            raw = await asyncio.wait_for(result.async_get_data(), timeout=self._progress_timeout_seconds())
            message = ""
            if isinstance(raw, dict):
                message = str(raw.get("message") or "")
            else:
                message = str(raw or "")
            if not message.strip() and final_stream_value.strip():
                message = final_stream_value
            if not message.strip() and streamed_message.strip():
                message = streamed_message
            if not message.strip():
                return None
            return await self._emit(
                f"agent_task.iteration.{iteration}.progress.{stage}",
                {
                    "message": message.strip(),
                    "iteration": iteration,
                    "stage": stage,
                    "status": self.status,
                    "language": progress_language,
                },
                meta={
                    "task_id": self.id,
                    "status": self.status,
                    "iteration": iteration,
                    "stage": stage,
                    "stream_kind": "progress",
                    "progress_source": "model",
                    "progress_model_key": model_key,
                    "progress_language": progress_language,
                },
            )
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            self.diagnostics.setdefault("progress_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": message,
                    "iteration": iteration,
                    "stage": stage,
                    "model_key": model_key,
                }
            )
            return None

    async def _emit_progress_delta(
        self,
        *,
        iteration: int,
        stage: str,
        delta: str,
        message_so_far: str,
        model_key: str,
        language: str,
    ) -> AgentExecutionStreamData:
        return await self._emit(
            f"agent_task.iteration.{iteration}.progress.{stage}.message",
            {
                "message": message_so_far,
                "iteration": iteration,
                "stage": stage,
                "status": self.status,
                "language": language,
            },
            event_type="delta",
            delta=delta,
            is_complete=False,
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "progress_delta",
                "progress_source": "model",
                "progress_model_key": model_key,
                "progress_language": language,
            },
        )

    def _progress_language(self) -> str:
        language = self._language_policy().get("progress_language")
        if language in (None, "", "auto"):
            getter = getattr(getattr(self.agent, "settings", None), "get", None)
            if callable(getter):
                language = getter("agent_task.progress.language", "auto")
        normalized = str(language or "auto").strip()
        return normalized or "auto"

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on", "enabled"}:
                return True
            if lowered in {"0", "false", "no", "off", "disabled"}:
                return False
        return bool(value)

    def _track_background_stream_task(self, task: asyncio.Task[Any]) -> None:
        self._background_stream_tasks.add(task)

        def discard(done_task: asyncio.Task[Any]) -> None:
            self._background_stream_tasks.discard(done_task)
            try:
                error = done_task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self.diagnostics.setdefault("stream_errors", []).append(
                    {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    }
                )

        task.add_done_callback(discard)

    async def _cancel_background_stream_tasks(self) -> None:
        if not self._background_stream_tasks:
            return
        tasks = list(self._background_stream_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    def _operator_safe_progress_snapshot(cls, stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], cls._strip_developer_diagnostics({"stage": stage, **snapshot}))

    _DEVELOPER_DIAGNOSTIC_KEYS = frozenset(
        {
            "diagnostics",
            "context_pack_diagnostics",
            "fallback_reason",
            "errors",
            "progress_errors",
            "stream_errors",
        }
    )

    @classmethod
    def _strip_developer_diagnostics(cls, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key)
                if normalized_key in cls._DEVELOPER_DIAGNOSTIC_KEYS or "diagnostic" in normalized_key:
                    cleaned[normalized_key] = {"omitted": "developer_diagnostics"}
                    continue
                cleaned[normalized_key] = cls._strip_developer_diagnostics(item)
            return cleaned
        if isinstance(value, list):
            return [cls._strip_developer_diagnostics(item) for item in value]
        return value

    @classmethod
    def _execution_log_summary(cls, execution_meta: dict[str, Any]) -> dict[str, Any]:
        logs = execution_meta.get("logs", {})
        if not isinstance(logs, dict):
            logs = {}
        action_records = cls._collect_execution_action_records(execution_meta)
        action_ids = [record["id"] for record in action_records if record.get("id")]
        action_statuses = {record["id"]: record.get("status", "") for record in action_records if record.get("id")}
        source_refs = cls._collect_source_refs_from_action_records(action_records)
        raw_explicit_source_refs = logs.get("source_refs")
        if isinstance(raw_explicit_source_refs, Sequence) and not isinstance(
            raw_explicit_source_refs, str | bytes | bytearray
        ):
            source_refs = cast(
                list[dict[str, Any]],
                cls._dedupe_ref_records([*source_refs, *raw_explicit_source_refs]),
            )[:32]
        # Risk status is judged by the final status per action id (last record
        # wins), so an action that failed and then succeeded within the same step
        # is treated as recovered and does not block verification.
        failed_actions = cls._action_ids_by_final_status(action_statuses, {"failed", "failure", "error"})
        blocked_actions = cls._action_ids_by_final_status(action_statuses, {"blocked"})
        approval_required_actions = cls._action_ids_by_final_status(action_statuses, {"approval_required"})
        required_actions, required_skills = cls._required_capability_constraints(execution_meta)
        missing_required_actions = [action_id for action_id in required_actions if action_id not in action_ids]
        selected_skill_ids = cls._selected_skill_ids(logs)
        missing_required_skills = [skill_id for skill_id in required_skills if skill_id not in selected_skill_ids]
        succeeded_actions = cls._action_ids_by_final_status(
            action_statuses, {"success", "succeeded", "partial_success"}
        )
        route = execution_meta.get("route", {})
        artifact_refs = logs.get("artifact_refs", [])
        artifact_readbacks = cls._artifact_readback_evidence_ids(artifact_refs)
        workspace_refs = execution_meta.get("workspace_refs") or logs.get("workspace_refs", {})
        raw_errors = logs.get("errors", [])
        execution_errors: list[Any]
        if isinstance(raw_errors, list):
            execution_errors = [_compact_agent_task_error_info(item) for item in raw_errors]
        elif raw_errors:
            execution_errors = [_compact_agent_task_error_info(raw_errors)]
        else:
            execution_errors = []
        diagnostics = execution_meta.get("diagnostics", {})
        if isinstance(diagnostics, dict) and diagnostics.get("execution_error"):
            execution_errors.append(_compact_agent_task_error_info(diagnostics["execution_error"]))
        replan_signals = cls._collect_replan_signals(execution_meta)
        # Unified capability-evidence view (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # one capability id space across kinds plus per-kind evidence buckets. A
        # capability is "used" when it ran (action) or was selected (skill). The
        # artifacts/validations buckets are reserved: no structural producer feeds
        # them yet, so the verifier guard does not enforce those evidence kinds.
        capabilities_used: list[str] = []
        for capability_id in [*action_ids, *selected_skill_ids]:
            if capability_id and capability_id not in capabilities_used:
                capabilities_used.append(capability_id)
        return {
            "model_response_count": (
                len(logs.get("model_responses", [])) if isinstance(logs.get("model_responses", []), list) else 0
            ),
            "action_log_count": len(action_ids),
            "action_ids": action_ids,
            "action_statuses": action_statuses,
            "actions": action_records,
            "source_refs": source_refs,
            "failed_actions": failed_actions,
            "blocked_actions": blocked_actions,
            "approval_required_actions": approval_required_actions,
            "required_actions": required_actions,
            "missing_required_actions": missing_required_actions,
            "selected_skill_ids": selected_skill_ids,
            "required_skills": required_skills,
            "missing_required_skills": missing_required_skills,
            "capabilities_used": capabilities_used,
            "capability_evidence": {
                "actions": {"succeeded": succeeded_actions, "failed": failed_actions},
                "skills": {"selected": selected_skill_ids},
                "artifacts": {"readback": artifact_readbacks},
                "validations": {"passed": [], "failed": []},
            },
            "artifact_refs": DataFormatter.sanitize(artifact_refs),
            "workspace_refs": DataFormatter.sanitize(workspace_refs),
            "route": DataFormatter.sanitize(route),
            "status": str(execution_meta.get("status") or ""),
            "errors": DataFormatter.sanitize(execution_errors),
            "replan_signals": DataFormatter.sanitize(replan_signals),
        }

    @staticmethod
    def _collect_replan_signals(execution_meta: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_values: list[Any] = []
        direct_signal = execution_meta.get("replan_signal")
        if direct_signal is not None:
            raw_values.append(direct_signal)
        direct_signals = execution_meta.get("replan_signals")
        if isinstance(direct_signals, (list, tuple)):
            raw_values.extend(direct_signals)
        blocks = execution_meta.get("blocks")
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                diagnostics = evidence.get("diagnostics")
                if isinstance(diagnostics, (list, tuple)):
                    raw_values.extend(
                        item
                        for item in diagnostics
                        if isinstance(item, Mapping) and item.get("kind") == "replan_signal"
                    )
            snapshot = blocks.get("snapshot")
            snapshot_blocks = snapshot.get("blocks") if isinstance(snapshot, Mapping) else None
            if isinstance(snapshot_blocks, Mapping):
                replan_signals = snapshot_blocks.get("replan_signals")
                if isinstance(replan_signals, (list, tuple)):
                    raw_values.extend(replan_signals)

        signals: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for value in raw_values:
            if not isinstance(value, Mapping):
                continue
            candidate = dict(value)
            if candidate.get("kind") == "replan_signal":
                candidate.pop("kind", None)
            try:
                normalized = ReplanSignal.from_value(candidate).to_dict()
            except Exception as error:
                normalized = {
                    "status": "blocked",
                    "reason": "Invalid ReplanSignal payload: "
                    + _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "diagnostics": [
                        {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        }
                    ],
                }
            key = (str(normalized.get("status") or ""), str(normalized.get("reason") or ""))
            if key not in seen:
                seen.add(key)
                signals.append(normalized)
        return signals

    @classmethod
    def _required_capability_constraints(cls, execution_meta: dict[str, Any]) -> tuple[list[str], list[str]]:
        constraints = execution_meta.get("effective_options", {})
        if not isinstance(constraints, dict):
            constraints = execution_meta.get("options", {})
        if not isinstance(constraints, dict):
            return [], []
        capability_constraints = constraints.get("capability_constraints", {})
        if not isinstance(capability_constraints, dict):
            return [], []
        actions = capability_constraints.get("actions", {})
        skills = capability_constraints.get("skills", {})
        required_actions = actions.get("required", []) if isinstance(actions, dict) else []
        required_skills = skills.get("required", []) if isinstance(skills, dict) else []
        return (
            cls._normalize_string_list(required_actions),
            cls._normalize_string_list(required_skills),
        )

    @staticmethod
    def _selected_skill_ids(logs: dict[str, Any]) -> list[str]:
        route_logs = logs.get("route_logs", {})
        if not isinstance(route_logs, dict):
            return []
        plan = route_logs.get("plan", {})
        if not isinstance(plan, dict):
            return []
        selected = plan.get("selected_skills", [])
        if not isinstance(selected, list):
            return []
        skill_ids: list[str] = []
        for item in selected:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or item.get("id") or item.get("name") or "").strip()
            if skill_id and skill_id not in skill_ids:
                skill_ids.append(skill_id)
        return skill_ids

    @classmethod
    def _collect_execution_action_records(
        cls,
        execution_meta: Mapping[str, Any],
        *,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        logs = execution_meta.get("logs", {})
        records = cls._collect_action_records(logs if isinstance(logs, dict) else {})
        if depth >= 3:
            return cls._dedupe_action_records(records)

        blocks = execution_meta.get("blocks")
        if not isinstance(blocks, Mapping):
            return cls._dedupe_action_records(records)
        evidence = blocks.get("evidence")
        if not isinstance(evidence, Mapping):
            return cls._dedupe_action_records(records)
        for key in ("execution_block_results", "plan_block_results"):
            block_results = evidence.get(key)
            if not isinstance(block_results, Sequence) or isinstance(block_results, (str, bytes, bytearray)):
                continue
            for block_result in block_results:
                if not isinstance(block_result, Mapping):
                    continue
                output = block_result.get("output")
                if not isinstance(output, Mapping):
                    continue
                nested_meta = output.get("execution_meta")
                if isinstance(nested_meta, Mapping):
                    records.extend(cls._collect_execution_action_records(nested_meta, depth=depth + 1))
                nested_result = output.get("execution_result")
                if isinstance(nested_result, Mapping):
                    nested_result_meta = nested_result.get("execution_meta")
                    if isinstance(nested_result_meta, Mapping):
                        records.extend(cls._collect_execution_action_records(nested_result_meta, depth=depth + 1))
        return cls._dedupe_action_records(records)

    @staticmethod
    def _dedupe_action_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for record in records:
            action_id = str(record.get("id") or record.get("name") or "")
            call_id = str(record.get("action_call_id") or "")
            preview_sha = str(record.get("result_preview_sha256") or "")
            preview = str(record.get("result_preview") or "")
            if len(preview) > 120:
                preview = preview[:120]
            key = (action_id, call_id, preview_sha, preview)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(dict(record))
        return deduped

    async def _emit_action_observation_events(
        self,
        iteration_index: int | None,
        *,
        execution_meta: Mapping[str, Any],
        owner_context: Mapping[str, Any] | None = None,
    ) -> None:
        records = self._collect_execution_action_records(execution_meta)
        if not records:
            return
        if owner_context is None:
            owner_context = self._action_event_owner_context(iteration_index, execution_meta)
        for record in records:
            action_id = str(record.get("id") or record.get("name") or "").strip()
            if not action_id:
                continue
            await self._emit_normalized_action_event(
                "started",
                record,
                execution_meta=execution_meta,
                owner_context=owner_context,
            )
            if self._action_record_failed(record):
                await self._emit_normalized_action_event(
                    "failed",
                    record,
                    execution_meta=execution_meta,
                    owner_context=owner_context,
                )
            else:
                await self._emit_normalized_action_event(
                    "completed",
                    record,
                    execution_meta=execution_meta,
                    owner_context=owner_context,
                )

    @staticmethod
    def _action_event_owner_context(iteration_index: int | None, execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        block_carrier = execution_meta.get("block_carrier")
        work_unit: Mapping[str, Any] = {}
        if isinstance(block_carrier, Mapping):
            raw_work_unit = block_carrier.get("work_unit")
            if isinstance(raw_work_unit, Mapping):
                work_unit = raw_work_unit
        runtime_preferences = work_unit.get("runtime_preferences")
        if not isinstance(runtime_preferences, Mapping):
            runtime_preferences = {}
        return {
            "iteration": iteration_index,
            "origin": work_unit.get("origin"),
            "work_unit_id": work_unit.get("id"),
            "strategy": runtime_preferences.get("strategy"),
            "card_id": runtime_preferences.get("card_id"),
        }

    async def _emit_normalized_action_event(
        self,
        phase: Literal["started", "completed", "failed"],
        record: Mapping[str, Any],
        *,
        execution_meta: Mapping[str, Any],
        owner_context: Mapping[str, Any],
    ) -> None:
        if self._normalized_action_event_already_emitted(phase, record, execution_meta=execution_meta):
            return
        action_id = str(record.get("id") or record.get("name") or "").strip()
        action_call_id = str(record.get("action_call_id") or "").strip()
        status = str(record.get("status") or "").strip() or ("started" if phase == "started" else phase)
        payload: dict[str, Any] = {
            "action_id": action_id,
            "action_call_id": action_call_id or None,
            "status": "started" if phase == "started" else status,
            "action_type": str(record.get("action_type") or "").strip() or None,
            "kind": str(record.get("kind") or "").strip() or None,
            "execution_id": execution_meta.get("execution_id"),
            "route": DataFormatter.sanitize(execution_meta.get("route", {})),
            "projection_source": "execution_meta.action_logs",
            "posthoc_projection": True,
            **{key: value for key, value in owner_context.items() if value is not None},
        }
        if "input_preview" in record:
            payload["input_summary"] = record.get("input_preview")
        if phase in {"completed", "failed"}:
            payload["success"] = phase == "completed"
            if "result_preview" in record:
                payload["output_summary"] = record.get("result_preview")
            if "result_preview_meta" in record:
                payload["result_preview_meta"] = record.get("result_preview_meta")
            for key in (
                "artifact_refs",
                "file_refs",
                "usage",
                "estimated_input_chars",
                "estimated_output_chars",
                "elapsed_ms",
                "duration_ms",
                "warnings",
            ):
                if key in record:
                    payload[key] = record.get(key)
            source_refs = self._collect_source_refs_from_action_records([record])
            if source_refs:
                payload["source_refs"] = source_refs
        if phase == "failed":
            error = record.get("error")
            if error is not None:
                payload["error"] = self._compact_verifier_prompt_value(error, max_chars=600)
            if "retryable" in record:
                payload["retryable"] = bool(record.get("retryable"))
            payload["failure_category"] = self._action_failure_category(record, status=status)
        await self._emit(
            f"agent_task.action.{phase}",
            {key: value for key, value in payload.items() if value is not None},
            meta={
                "task_id": self.id,
                "status": self.status,
                "stream_kind": "action_observation",
                "action_id": action_id,
                "action_call_id": action_call_id or None,
                "phase": phase,
                "iteration": owner_context.get("iteration"),
                "origin": owner_context.get("origin"),
                "work_unit_id": owner_context.get("work_unit_id"),
                "strategy": owner_context.get("strategy"),
                "card_id": owner_context.get("card_id"),
                "projection_source": "execution_meta.action_logs",
            },
        )

    def _normalized_action_event_already_emitted(
        self,
        phase: str,
        record: Mapping[str, Any],
        *,
        execution_meta: Mapping[str, Any],
    ) -> bool:
        emitted = getattr(self, "_emitted_action_event_keys", None)
        if not isinstance(emitted, set):
            emitted = set()
            setattr(self, "_emitted_action_event_keys", emitted)
        action_id = str(record.get("id") or record.get("name") or "").strip()
        action_call_id = str(record.get("action_call_id") or "").strip()
        preview_key = str(record.get("result_preview_sha256") or "")
        if not preview_key:
            try:
                preview_key = json.dumps(
                    DataFormatter.sanitize(
                        {
                            "input_preview": record.get("input_preview"),
                            "result_preview": record.get("result_preview"),
                        }
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )[:160]
            except Exception:
                preview_key = f"{record.get('input_preview') or ''}|{record.get('result_preview') or ''}"[:160]
        key = (
            str(phase),
            str(execution_meta.get("execution_id") or ""),
            action_id,
            action_call_id,
            preview_key or str(record.get("status") or ""),
        )
        if key in emitted:
            return True
        emitted.add(key)
        return False

    @staticmethod
    def _action_record_failed(record: Mapping[str, Any]) -> bool:
        status = str(record.get("status") or "").strip().lower()
        if status in {"success", "succeeded", "completed", "complete", "partial_success", "ok"}:
            return False
        return status in {"failed", "error", "timed_out", "timeout", "blocked"} or record.get("error") is not None

    @classmethod
    def _action_failure_category(cls, record: Mapping[str, Any], *, status: str) -> str:
        status_text = str(status or "").strip().lower()
        error_text = str(record.get("error") or "").strip().lower()
        combined = f"{status_text}\n{error_text}"
        if "timeout" in combined or "timed out" in combined or "idle" in combined or "no progress" in combined:
            return "liveness"
        if "capability" in combined or "not allowed" in combined or "not permitted" in combined:
            return "capability"
        if "connection" in combined or "network" in combined or "provider" in combined or "service" in combined:
            return "infra"
        return "execution"

    @staticmethod
    def _dedupe_ref_records(records: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            if isinstance(record, Mapping):
                key = "|".join(
                    str(record.get(field) or "")
                    for field in ("artifact_id", "action_call_id", "path", "sha256", "source_url")
                )
                if not key.strip("|"):
                    key = json.dumps(DataFormatter.sanitize(record), ensure_ascii=False, sort_keys=True)
            else:
                key = str(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @classmethod
    def _collect_source_refs_from_action_records(cls, records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for record in records:
            action_id = str(record.get("id") or record.get("name") or "").strip()
            action_call_id = str(record.get("action_call_id") or "").strip()

            def collect(value: Any, *, path: str = "") -> None:
                if isinstance(value, Mapping):
                    for key, item in value.items():
                        key_text = str(key)
                        next_path = f"{path}.{key_text}" if path else key_text
                        if key_text in {"source_url", "selected_url", "requested_url", "url", "href", "path"}:
                            ref_value = str(item or "").strip()
                            if ref_value:
                                content_state = cls._source_ref_content_state(value, field=key_text)
                                refs.append(
                                    {
                                        "source": "action_result",
                                        "field": key_text,
                                        "path": next_path,
                                        "value": ref_value,
                                        "action_id": action_id,
                                        "action_call_id": action_call_id,
                                        "content_state": content_state,
                                        "evidence_boundary": (
                                            "content_preview_available"
                                            if content_state == "bounded_readback_available"
                                            else "discovery_or_materialization_only"
                                        ),
                                    }
                                )
                        collect(item, path=next_path)
                    return
                if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                    for index, item in enumerate(value[:24]):
                        collect(item, path=f"{path}[{index}]" if path else f"[{index}]")

            collect(record.get("result_preview"))
            collect(record.get("artifact_refs"))
            collect(record.get("file_refs"))
        return cast(list[dict[str, Any]], cls._dedupe_ref_records(refs))[:32]

    @staticmethod
    def _source_ref_content_state(container: Any, *, field: str) -> str:
        if not isinstance(container, Mapping):
            return "ref_only"
        for key in (
            "content",
            "content_preview",
            "content_snippet",
            "evidence_snippet",
            "excerpt",
            "snippet",
            "text",
        ):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return "bounded_readback_available"
        preview = container.get("preview")
        if isinstance(preview, str) and preview.strip() and any(
            key in container for key in ("bytes", "read_bytes", "sha256", "handler_id", "role", "source")
        ):
            return "bounded_readback_available"
        return "ref_only"

    @staticmethod
    def _dedupe_jsonable_records(records: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            try:
                key = json.dumps(DataFormatter.sanitize(record), ensure_ascii=False, sort_keys=True)
            except Exception:
                key = str(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @classmethod
    def _collect_action_records(cls, logs: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def add_entries(entries: Any) -> None:
            if isinstance(entries, dict):
                for action_id, record in entries.items():
                    if isinstance(record, dict):
                        records.append(cls._compact_action_record(action_id, record))
                    else:
                        records.append({"id": str(action_id), "name": str(action_id), "status": str(record or "")})
            elif isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict):
                        action_id = item.get("action_id") or item.get("id") or item.get("name") or ""
                        records.append(cls._compact_action_record(action_id, item))

        add_entries(logs.get("action_logs", {}))
        route_logs = logs.get("route_logs", {})
        if isinstance(route_logs, dict):
            add_entries(route_logs.get("action_logs", {}))
            route_output = route_logs.get("output", {})
            if isinstance(route_output, dict):
                add_entries(route_output.get("history", []))
        return records

    @classmethod
    def _compact_action_record(cls, action_id: Any, record: dict[str, Any]) -> dict[str, Any]:
        normalized_id = str(action_id or record.get("action_id") or record.get("id") or record.get("name") or "")
        status = str(record.get("status") or "").strip()
        if not status:
            if record.get("error"):
                status = "failed"
            elif "result" in record or "artifact" in record:
                status = "success"
        compact: dict[str, Any] = {
            "id": normalized_id,
            "name": str(record.get("name") or normalized_id),
            "status": status,
            "action_type": str(record.get("action_type") or record.get("type") or ""),
            "kind": str(record.get("kind") or ""),
        }
        action_call_id = str(record.get("action_call_id") or record.get("call_id") or "").strip()
        if action_call_id:
            compact["action_call_id"] = action_call_id

        raw = record.get("raw")
        if not isinstance(raw, Mapping):
            raw = {}
        model_digest = record.get("model_digest")
        if not isinstance(model_digest, Mapping) or not model_digest:
            raw_model_digest = raw.get("model_digest")
            if isinstance(raw_model_digest, Mapping) and raw_model_digest:
                model_digest = raw_model_digest
        digest = model_digest if isinstance(model_digest, Mapping) and model_digest else (raw or record)

        result_preview = digest.get("result_preview") if isinstance(digest, Mapping) else None
        if result_preview is None and isinstance(record.get("result_preview"), (Mapping, Sequence, str)):
            result_preview = record.get("result_preview")
        if result_preview is None and isinstance(digest, Mapping):
            for key in ("data", "result", "output"):
                fallback_preview = digest.get(key)
                if fallback_preview is not None:
                    result_preview = fallback_preview
                    break
        if result_preview is not None:
            compact["result_preview"] = cls._compact_action_preview_value(result_preview, max_chars=5200)
        result_preview_meta = digest.get("result_preview_meta") if isinstance(digest, Mapping) else None
        if result_preview_meta is None:
            result_preview_meta = record.get("result_preview_meta")
        if result_preview_meta is None and isinstance(result_preview, Mapping):
            result_preview_meta = {
                key: result_preview.get(key)
                for key in ("chars", "bytes", "sha256", "truncated", "read_bytes")
                if key in result_preview
            }
        if result_preview_meta is not None:
            compact["result_preview_meta"] = cls._compact_verifier_prompt_value(result_preview_meta, max_chars=500)

        input_preview = record.get("input") or record.get("kwargs") or raw.get("input") or raw.get("kwargs")
        if input_preview:
            compact["input_preview"] = cls._compact_verifier_prompt_value(input_preview, max_chars=500)

        for key in ("artifact_refs", "file_refs"):
            value = digest.get(key) if isinstance(digest, Mapping) else None
            if value is None:
                value = record.get(key)
            if value is None and raw:
                value = raw.get(key)
            if key == "artifact_refs" and isinstance(value, list):
                compact[key] = [cls._compact_artifact_ref_for_verifier(ref) for ref in value[:8]]
                if len(value) > 8:
                    compact[key].append({"omitted": len(value) - 8, "reason": "prompt_budget"})
            elif key == "file_refs" and value:
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=1200)

        for key in (
            "usage",
            "estimated_input_chars",
            "estimated_output_chars",
            "elapsed_ms",
            "duration_ms",
            "retryable",
            "warnings",
            "error",
        ):
            value = record.get(key)
            if value is None and isinstance(digest, Mapping):
                value = digest.get(key)
            if value is None and raw:
                value = raw.get(key)
            if value is not None:
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=1200)

        preview_meta = compact.get("result_preview_meta")
        if isinstance(preview_meta, Mapping):
            sha = preview_meta.get("sha256") or preview_meta.get("result_sha256")
            if sha:
                compact["result_preview_sha256"] = str(sha)
        return compact

    @staticmethod
    def _action_ids_by_status(records: list[dict[str, str]], statuses: set[str]) -> list[str]:
        result: list[str] = []
        for record in records:
            status = record.get("status", "").strip().lower()
            if status in statuses and record.get("id"):
                result.append(record["id"])
        return result

    @staticmethod
    def _action_ids_by_final_status(action_statuses: dict[str, str], statuses: set[str]) -> list[str]:
        return [
            action_id
            for action_id, status in action_statuses.items()
            if action_id and str(status).strip().lower() in statuses
        ]

    @staticmethod
    def _stream_path_token(value: Any) -> str:
        token = str(value or "").strip().replace("/", ".")
        return token or "item"

    async def _record_phase(
        self,
        phase: str,
        *,
        iteration: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        record = {
            "phase": phase,
            "iteration": iteration,
            "status": self.status,
            "diagnostics": DataFormatter.sanitize(diagnostics or {}),
        }
        self.diagnostics.setdefault("phases", []).append(record)
        return await self._emit(
            f"agent_task.phase.{phase}",
            record,
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "phase": phase,
                "stream_kind": "phase",
            },
        )

    async def _emit(
        self,
        path: str,
        value: Any,
        *,
        event_type: Literal["delta", "done"] = "done",
        delta: str | None = None,
        is_complete: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        stream_kind = meta.get("stream_kind") if isinstance(meta, dict) else None
        if stream_kind != "heartbeat":
            self._last_stream_emit_monotonic = time.monotonic()
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            delta=delta,
            is_complete=completed,
            event_type=event_type,
            source="agent_task",
            task_id=self.id,
            meta=meta or {"task_id": self.id, "status": self.status},
        )
        self._stream_items.append(item)
        # Bound the replay buffer so a very long task does not grow it without
        # limit. Late subscribers replay at most the most recent window.
        if len(self._stream_items) > _STREAM_REPLAY_LIMIT:
            del self._stream_items[: len(self._stream_items) - _STREAM_REPLAY_LIMIT]
        for queue in list(self._stream_queues):
            await queue.put(item)
        return item

    async def _close_streams(self):
        await self._cancel_background_stream_tasks()
        for queue in list(self._stream_queues):
            await queue.put(None)

    def _task_summary(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "max_iterations": self.max_iterations,
            "verify": self.verify,
        }


__all__ = ["AgentTaskObservationMixin"]
