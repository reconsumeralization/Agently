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


class AgentTaskCarrierMixin(AgentTaskMixinBase):
    def _build_flat_work_unit_intent(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> WorkUnitIntent:
        effective_shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct")
        execution_policy = self._step_execution_policy()
        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, Mapping):
            step_scope = {}
        scoped_ids = self._normalize_string_list(step_scope.get("allowed_capability_ids"))
        return WorkUnitIntent(
            id=f"iter-{iteration_index}:flat-step",
            origin="flat_step",
            objective=str(plan.get("step_instruction") or ""),
            input_payload={
                "task_id": self.id,
                "iteration": iteration_index,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract(),
                "plan": DataFormatter.sanitize(plan),
                "execution_prompt": self._execution_prompt_context(),
                "scoped_retrieval": DataFormatter.sanitize(plan.get("scoped_retrieval", {})),
                "retrieval_policy": scoped_retrieval_policy(),
                "context_summary": {
                    "item_count": len(context_pack.get("items", [])),
                    "profile": context_pack.get("profile"),
                },
            },
            evidence_requirements=tuple(
                {"capability_id": capability_id, "source": "step_scope"} for capability_id in scoped_ids
            ),
            capability_scope=tuple(
                {
                    "action_id": capability_id,
                    "capability_id": capability_id,
                    "source": "AgentTaskLoop.step_scope",
                }
                for capability_id in scoped_ids
            ),
            delivery_contract={
                "execution_prompt": DataFormatter.sanitize(self._execution_prompt_context()),
                "deliverable_mode": plan.get("deliverable_mode"),
                "task_context_contract": self._task_context_contract(),
                "scoped_retrieval": DataFormatter.sanitize(plan.get("scoped_retrieval", {})),
            },
            runtime_preferences={
                "handler": "agent_task_bounded_step",
                "deliverable_mode": plan.get("deliverable_mode"),
                "preferred_execution_shape": effective_shape,
                "step_plan": execution_policy.get("step_plan", "direct"),
                "strategy": "flat",
            },
        )

    async def _run_work_unit_through_blocks(
        self,
        *,
        work_unit: WorkUnitIntent,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
        execution_id: str,
        handler: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
        start_payload: Mapping[str, Any],
    ) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        execution_plan = self._build_blocks_execution_plan(work_unit, plan, context_pack)
        output_policy = select_carrier_output_policy(work_unit)
        blocks_entrypoint = self._resolve_blocks()
        execution_graph = blocks_entrypoint.compile(
            {
                "execution_id": execution_id,
                "task_frame_id": execution_plan.task_frame_id,
                "plan_id": execution_plan.plan_id,
                "plan_blocks": [block.to_dict() for block in execution_plan.plan_blocks],
                "edges": [edge.to_dict() for edge in execution_plan.edges],
                "capability_resolution": self._blocks_capability_resolution(plan).to_dict(),
                "evidence_requirements": [dict(item) for item in execution_plan.evidence_requirements],
                "result_contracts": [dict(item) for item in execution_plan.result_contracts],
                "runtime_policy": {
                    "checkpoint_policy": dict(execution_plan.checkpoint_policy),
                    "carrier_output_policy": output_policy.to_dict(),
                },
                "budget": dict(plan.get("budget", {})) if isinstance(plan.get("budget"), Mapping) else {},
            }
        )
        flow = blocks_entrypoint.bind_runtime(execution_graph)

        handler_name = str(work_unit.runtime_preferences.get("handler") or "agent_task_bounded_step")
        blocks_execution = flow.create_execution(
            auto_close=False,
            workspace=self.workspace,
            runtime_resources={
                "blocks.handlers": {
                    "agent_task_bounded_step": handler,
                    handler_name: handler,
                }
            },
        )
        await blocks_execution.async_start(
            {
                "task_id": self.id,
                "work_unit": work_unit.to_dict(),
                "carrier_output_policy": output_policy.to_dict(),
                **dict(start_payload),
            }
        )
        snapshot = await blocks_execution.async_close()

        evidence = blocks_entrypoint.map_evidence(execution_graph, snapshot)
        block_result = dict(blocks_entrypoint.map_result(execution_graph, snapshot))
        block_output = self._extract_work_unit_block_output(snapshot, work_unit=work_unit)
        execution_result = block_output.get("execution_result")
        raw_meta = block_output.get("execution_meta")
        execution_meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
        if not execution_meta:
            selected_route = str(work_unit.runtime_preferences.get("plan_block_kind") or "agent_step")
            execution_meta = {
                "execution_id": f"{self.id}:{work_unit.id}:missing-block-meta",
                "status": "failed",
                "route": {"selected_route": selected_route, "status": "failed"},
                "logs": {
                    "action_logs": {},
                    "route_logs": {},
                    "errors": [{"message": "Block carrier work unit returned no execution_meta"}],
                },
            }
        carrier_meta = {
            "work_unit": work_unit.to_dict(),
            "execution_plan": execution_plan.to_dict(),
            "execution_block_graph": execution_graph.to_dict(),
            "output_policy": output_policy.to_dict(),
            "block_result": DataFormatter.sanitize(block_result),
            "snapshot_status": snapshot.get("status") if isinstance(snapshot, Mapping) else None,
        }
        work_unit_result = WorkUnitResult.from_output(
            intent=work_unit,
            output=execution_result,
            execution_meta=execution_meta,
            carrier_meta=carrier_meta,
        )
        execution_meta["block_carrier"] = self._compact_block_carrier_for_meta(
            work_unit=work_unit,
            work_unit_result=work_unit_result,
            output_policy=output_policy,
            block_result=block_result,
            snapshot=snapshot,
        )
        self._attach_blocks_evidence(
            execution_meta,
            execution_plan=execution_plan,
            execution_graph=execution_graph,
            evidence=evidence,
            block_result=block_result,
            snapshot=snapshot,
        )
        return execution_result, cast(dict[str, Any], execution_meta), work_unit_result

    def _create_bounded_child_execution(
        self,
        *,
        lineage: Mapping[str, Any],
        route_policy: Mapping[str, Any] | None = None,
        recall_records: Sequence[Mapping[str, Any]] | None = None,
        recall_source: str | None = None,
    ) -> Any:
        execution = self.agent.create_execution(
            lineage=dict(lineage),
            limits=self._child_execution_limits(),
            options=self._child_execution_options(),
        )
        self._bind_action_workspace(execution)
        if recall_records:
            set_recall_records = getattr(execution.execution_context, "set_action_artifact_recall_records", None)
            if callable(set_recall_records):
                set_recall_records(list(recall_records), source=recall_source or "AgentTask")
        if route_policy:
            apply_route_policy = getattr(execution, "route_policy", None)
            if callable(apply_route_policy):
                apply_route_policy(dict(route_policy))
        return execution

    async def _run_bounded_child_execution(
        self,
        *,
        execution: Any,
        language_policy: Mapping[str, Any],
        input_payload: Mapping[str, Any],
        instruction: str,
        output_schema: Mapping[str, Any],
        output_format: str,
        use_output: bool = True,
        carrier_output_policy: Mapping[str, Any] | None = None,
        started_event: str,
        started_payload: Mapping[str, Any],
        stream_bridge: Callable[[Any], Awaitable[None]],
        data_waiter: Callable[[Awaitable[Any]], Awaitable[Any]] | None = None,
        meta_waiter: Callable[[Awaitable[Any]], Awaitable[Any]] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        payload = dict(input_payload)
        if isinstance(carrier_output_policy, Mapping):
            payload["carrier_output_policy"] = DataFormatter.sanitize(dict(carrier_output_policy))
        execution.input(payload)
        execution.language(language_policy.get("language", "auto"))
        if use_output:
            execution.instruct(instruction)
            execution.output(dict(output_schema), format=output_format)
        else:
            execution.instruct(
                instruction
                + " For this work unit, return the natural-language body directly as plain text. "
                "Do not wrap the body in JSON, XML fields, YAML, Markdown frontmatter, or diagnostic labels."
            )
        await self._emit(
            started_event,
            {
                "execution_id": execution.id,
                **dict(started_payload),
            },
        )

        async def run_stream_bridge() -> None:
            await stream_bridge(execution)

        stream_task = asyncio.create_task(run_stream_bridge())
        try:
            # The child AgentExecution owns its own model/action/resource idle
            # limits. AgentTask must not reinterpret request_timeout_seconds as a
            # hard cap for the whole nested execution stream, otherwise a
            # still-progressing Search/Browse/Action step can be cancelled by
            # the parent before the child runtime reports its own status.
            data_awaitable = execution.async_get_data()
            result = await (data_waiter(data_awaitable) if data_waiter is not None else data_awaitable)
            meta_awaitable = execution.async_get_meta()
            meta = await (meta_waiter(meta_awaitable) if meta_waiter is not None else meta_awaitable)
            await stream_task
        except Exception:
            if not stream_task.done():
                stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stream_task
            raise
        return result, cast(dict[str, Any], meta)

    @staticmethod
    def _carrier_uses_control_output(carrier_output_policy: Mapping[str, Any] | None) -> bool:
        if not isinstance(carrier_output_policy, Mapping):
            return True
        control_format = str(carrier_output_policy.get("control_format") or "").strip()
        return bool(control_format)

    @staticmethod
    def _carrier_control_output_format(
        carrier_output_policy: Mapping[str, Any] | None,
        *,
        default: str = "json",
    ) -> str:
        if not isinstance(carrier_output_policy, Mapping):
            return default
        control_format = str(carrier_output_policy.get("control_format") or "").strip().lower()
        if control_format in {"json", "hybrid", "xml_field", "flat_markdown", "yaml_literal"}:
            return control_format
        return default

    @staticmethod
    def _carrier_output_policy_from_block_context(block_context: Mapping[str, Any]) -> Mapping[str, Any] | None:
        direct = block_context.get("carrier_output_policy")
        if isinstance(direct, Mapping):
            return direct
        block_input = block_context.get("input")
        if isinstance(block_input, Mapping):
            nested = block_input.get("carrier_output_policy")
            if isinstance(nested, Mapping):
                return nested
        return None

    def _build_blocks_execution_plan(
        self,
        work_unit_or_iteration: WorkUnitIntent | int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ):
        from agently.types.data import ExecutionPlan, ExecutionPlanEdge, PlanBlockInstance

        if isinstance(work_unit_or_iteration, WorkUnitIntent):
            work_unit = work_unit_or_iteration
        else:
            work_unit = self._build_flat_work_unit_intent(int(work_unit_or_iteration), plan, context_pack)
        runtime_preferences = dict(work_unit.runtime_preferences)
        effective_shape = str(
            runtime_preferences.get("preferred_execution_shape")
            or plan.get("effective_execution_shape")
            or plan.get("execution_shape")
            or "direct"
        )
        step_plan = str(
            runtime_preferences.get("step_plan") or self._step_execution_policy().get("step_plan", "direct")
        )
        handler_name = str(runtime_preferences.get("handler") or "agent_task_bounded_step")
        requested_plan_block_kind = str(runtime_preferences.get("plan_block_kind") or "agent_step").strip()
        plan_block_kind = (
            requested_plan_block_kind
            if requested_plan_block_kind
            in {
                "model_request",
                "action_call",
                "mcp_tool_call",
                "script_action",
                "workspace_operation",
                "skill_activation",
                "approval_wait",
                "external_wait",
                "validation",
                "observation",
                "flow_segment",
                "emit",
                "agent_step",
            }
            else "agent_step"
        )
        plan_block_id = plan_block_kind
        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        budget = plan.get("budget")
        agent_plan_block = PlanBlockInstance(
            id=f"{work_unit.id}:agent-step",
            plan_block_id=plan_block_id,
            kind=plan_block_kind,
            intent=work_unit.objective,
            bound_inputs={
                "task_id": self.id,
                "work_unit": work_unit.to_dict(),
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract(),
                "preferred_execution_shape": effective_shape,
                "step_plan": step_plan,
                "plan": DataFormatter.sanitize(plan),
                "execution_prompt": self._execution_prompt_context(),
                "scoped_retrieval": DataFormatter.sanitize(plan.get("scoped_retrieval", {})),
                "retrieval_policy": dict(work_unit.retrieval_policy),
                "context_summary": {
                    "item_count": len(context_pack.get("items", [])),
                    "profile": context_pack.get("profile"),
                },
            },
            output_contract={
                "execution_result": "bounded AgentExecution step result",
                "execution_meta": "bounded AgentExecution route metadata and evidence",
            },
            evidence_contract={
                "expected_evidence": DataFormatter.sanitize(list(work_unit.evidence_requirements))
                or str(plan.get("expected_evidence") or ""),
                "effective_execution_shape": effective_shape,
                "step_plan": step_plan,
            },
            runtime_preferences={"handler": handler_name, **runtime_preferences},
            budget=dict(budget) if isinstance(budget, Mapping) else {},
        )
        retrieval_blocks = self._build_scoped_retrieval_plan_blocks(work_unit, plan)
        retrieval_edges = tuple(
            ExecutionPlanEdge(
                from_plan_block=block.id,
                to_plan_block=agent_plan_block.id,
                kind="scoped_retrieval",
                binding={
                    "target_input": "scoped_retrieval_results",
                    "semantic_acceptance_owner": "planner_or_verifier",
                },
            )
            for block in retrieval_blocks
        )
        return ExecutionPlan(
            plan_id=f"{self.id}:{work_unit.id}:execution-plan",
            task_frame_id=f"{self.id}:{work_unit.id}:task-frame",
            plan_blocks=(*retrieval_blocks, agent_plan_block),
            edges=retrieval_edges,
            semantic_outputs={"step": agent_plan_block.id},
            evidence_requirements=tuple(
                {"capability_id": capability_id, "source": "step_scope"}
                for capability_id in self._normalize_string_list(step_scope.get("allowed_capability_ids"))
            ),
            result_contracts=(
                {
                    "name": "agent_task_step",
                    "requires": ["execution_result", "execution_meta"],
                },
            ),
            checkpoint_policy={
                "scope": "agent_task_work_unit",
                "work_unit_id": work_unit.id,
                "origin": work_unit.origin,
            },
        )

    @classmethod
    def _build_scoped_retrieval_plan_blocks(
        cls,
        work_unit: WorkUnitIntent,
        plan: Mapping[str, Any],
    ) -> tuple[Any, ...]:
        from agently.types.data import PlanBlockInstance

        scoped_retrieval = plan.get("scoped_retrieval")
        if not isinstance(scoped_retrieval, Mapping):
            return ()
        query_groups = scoped_retrieval.get("query_groups")
        if not isinstance(query_groups, Sequence) or isinstance(query_groups, (str, bytes, bytearray)):
            return ()
        blocks: list[PlanBlockInstance] = []
        for index, raw_group in enumerate(query_groups[:8]):
            if not isinstance(raw_group, Mapping):
                continue
            query = str(raw_group.get("query") or "").strip()
            if not query:
                continue
            expected_role = str(raw_group.get("expected_role") or "evidence_snippet").strip()
            filters = cls._scoped_retrieval_filters(raw_group)
            include_snippets = expected_role != "locator_ref"
            bound_inputs: dict[str, Any] = {
                "operation": "search",
                "query": query,
                "filters": filters,
                "max_results": raw_group.get("max_results", 8),
                "include_snippets": include_snippets,
                "snippet_limit": raw_group.get("snippet_limit", 1200),
                "snippet_offset": raw_group.get("snippet_offset", 0),
                "expected_role": expected_role,
                "query_group_index": index,
            }
            for key in ("path", "pattern"):
                value = raw_group.get(key)
                if value is not None:
                    bound_inputs[key] = value
            for key in ("search_surface", "include_hidden", "max_file_bytes", "context_lines"):
                value = raw_group.get(key)
                if value is not None:
                    bound_inputs[key] = value
            blocks.append(
                PlanBlockInstance(
                    id=f"{work_unit.id}:scoped-retrieval-{index}",
                    plan_block_id="workspace_operation",
                    kind="workspace_operation",
                    intent=f"Run scoped Workspace retrieval for query group {index + 1}.",
                    bound_inputs=bound_inputs,
                    output_contract={
                        "locator_refs": "bounded targets only; content not read",
                        "evidence_snippets": "bounded source text when requested",
                    },
                    evidence_contract={
                        "role_policy": scoped_retrieval_policy(),
                        "semantic_acceptance_owner": "planner_or_verifier",
                    },
                    runtime_preferences={
                        "retrieval_policy": scoped_retrieval_policy(),
                        "query_group_index": index,
                    },
                )
            )
        return tuple(blocks)

    @staticmethod
    def _scoped_retrieval_filters(group: Mapping[str, Any]) -> dict[str, Any]:
        raw_filters = group.get("filters")
        filters = {
            str(key): AgentTaskCarrierMixin._normalize_scoped_retrieval_filter_value(key, value)
            for key, value in dict(raw_filters).items()
        } if isinstance(raw_filters, Mapping) else {}
        for key in ("collection", "kind"):
            value = group.get(key)
            if value is not None:
                filters.setdefault(key, AgentTaskCarrierMixin._normalize_scoped_retrieval_filter_value(key, value))
        scope = group.get("scope")
        if isinstance(scope, Mapping):
            for key, value in scope.items():
                filters.setdefault(
                    f"scope.{key}",
                    AgentTaskCarrierMixin._normalize_scoped_retrieval_filter_value(f"scope.{key}", value),
                )
        meta = group.get("meta")
        if isinstance(meta, Mapping):
            for key, value in meta.items():
                filters.setdefault(
                    f"meta.{key}",
                    AgentTaskCarrierMixin._normalize_scoped_retrieval_filter_value(f"meta.{key}", value),
                )
        path = group.get("path")
        if path is not None:
            filters.setdefault("path", AgentTaskCarrierMixin._normalize_scoped_retrieval_filter_value("path", path))
        return filters

    @staticmethod
    def _normalize_scoped_retrieval_filter_value(key: Any, value: Any) -> Any:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            items = [item for item in value if item not in (None, "")]
            if len(items) == 1 and str(key) in {"id", "path", "collection", "kind"}:
                return items[0]
        return value

    @staticmethod
    def _scoped_retrieval_results_from_block_context(block_context: Mapping[str, Any]) -> list[dict[str, Any]]:
        state = block_context.get("state")
        if not isinstance(state, Mapping):
            return []
        results = state.get("execution_block_results")
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
            return []
        scoped_results: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "") != "workspace_operation":
                continue
            output = item.get("output")
            if not isinstance(output, Mapping):
                continue
            if str(output.get("operation") or "") != "search":
                continue
            scoped_results.append(
                {
                    "execution_block_id": item.get("execution_block_id"),
                    "source_plan_block_id": item.get("source_plan_block_id"),
                    "query": output.get("query"),
                    "filters": DataFormatter.sanitize(output.get("filters") or {}),
                    "bounded": DataFormatter.sanitize(output.get("bounded") or {}),
                    "locator_refs": DataFormatter.sanitize(output.get("locator_refs") or []),
                    "evidence_snippets": DataFormatter.sanitize(output.get("evidence_snippets") or []),
                    "diagnostics": DataFormatter.sanitize(output.get("diagnostics") or []),
                }
            )
        return scoped_results

    @staticmethod
    def _extract_work_unit_block_output(
        snapshot: Mapping[str, Any],
        *,
        work_unit: WorkUnitIntent,
    ) -> dict[str, Any]:
        blocks_state = snapshot.get("blocks", {})
        if not isinstance(blocks_state, Mapping):
            return {}
        results = blocks_state.get("execution_block_results", ())
        if not isinstance(results, (list, tuple)):
            return {}
        expected_prefix = f"{work_unit.id}:"
        fallback: dict[str, Any] = {}
        prefixed_fallback: dict[str, Any] = {}
        for item in results:
            if not isinstance(item, Mapping):
                continue
            output = item.get("output")
            output_dict = dict(output) if isinstance(output, Mapping) else {}
            if not fallback and output_dict:
                fallback = output_dict
            block_id = str(item.get("execution_block_id") or item.get("id") or "")
            if block_id.startswith(expected_prefix) and output_dict:
                if "execution_result" in output_dict or "execution_meta" in output_dict:
                    return output_dict
                if not prefixed_fallback:
                    prefixed_fallback = output_dict
            source_plan_block_id = str(item.get("source_plan_block_id") or "")
            if source_plan_block_id.startswith(expected_prefix) and output_dict:
                if "execution_result" in output_dict or "execution_meta" in output_dict:
                    return output_dict
                if not prefixed_fallback:
                    prefixed_fallback = output_dict
        if prefixed_fallback:
            return prefixed_fallback
        return fallback

    def _blocks_capability_resolution(self, plan: dict[str, Any]):
        from agently.types.data import CapabilityResolution

        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        scoped_ids = self._normalize_string_list(step_scope.get("allowed_capability_ids"))
        return CapabilityResolution(
            allowed_capabilities=tuple(scoped_ids),
            scoped_action_candidates=tuple(
                {"action_id": capability_id, "capability_id": capability_id, "source": "AgentTaskLoop.step_scope"}
                for capability_id in scoped_ids
            ),
            diagnostics=(
                {
                    "source": "AgentTaskLoop",
                    "step_execution_shape": str(
                        plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct"
                    ),
                    "grants_capability": False,
                },
            ),
        )

    @staticmethod
    def _compact_value_for_meta(value: Any, *, max_chars: int = 1200) -> Any:
        sanitized = _omit_agent_task_request_payloads_from_hot_path(value)
        if isinstance(sanitized, str):
            if len(sanitized) <= max_chars:
                return sanitized
            return {
                "preview": sanitized[:max_chars],
                "chars": len(sanitized),
                "truncated": True,
            }
        try:
            text = json.dumps(sanitized, ensure_ascii=False, default=str)
        except Exception:
            text = str(sanitized)
        if len(text) <= max_chars:
            return sanitized
        return {
            "preview": text[:max_chars],
            "chars": len(text),
            "truncated": True,
        }

    @classmethod
    def _compact_work_unit_for_meta(cls, work_unit: WorkUnitIntent | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(work_unit, WorkUnitIntent):
            work_unit_dict = work_unit.to_dict()
        elif isinstance(work_unit, Mapping):
            work_unit_dict = dict(work_unit)
        else:
            return {}
        delivery_contract = work_unit_dict.get("delivery_contract")
        if not isinstance(delivery_contract, Mapping):
            delivery_contract = {}
        execution_prompt = delivery_contract.get("execution_prompt")
        if not isinstance(execution_prompt, Mapping):
            execution_prompt = {}
        output_schema = execution_prompt.get("output")
        output_keys = list(output_schema.keys()) if isinstance(output_schema, Mapping) else []
        runtime_preferences = work_unit_dict.get("runtime_preferences")
        if not isinstance(runtime_preferences, Mapping):
            runtime_preferences = {}
        return {
            "id": work_unit_dict.get("id"),
            "origin": work_unit_dict.get("origin"),
            "objective": cls._compact_value_for_meta(work_unit_dict.get("objective") or "", max_chars=500),
            "input_ref_count": len(work_unit_dict.get("input_refs") or []),
            "evidence_requirement_count": len(work_unit_dict.get("evidence_requirements") or []),
            "capability_scope_count": len(work_unit_dict.get("capability_scope") or []),
            "quality_gate_count": len(work_unit_dict.get("quality_gates") or []),
            "delivery_contract": {
                "deliverable_mode": delivery_contract.get("deliverable_mode"),
                "output_format": execution_prompt.get("output_format"),
                "output_keys": output_keys[:20],
            },
            "runtime_preferences": {
                key: runtime_preferences.get(key)
                for key in (
                    "handler",
                    "plan_block_kind",
                    "preferred_execution_shape",
                    "step_plan",
                    "strategy",
                    "card_id",
                    "attempt_index",
                    "max_attempts",
                )
                if key in runtime_preferences
            },
        }

    @classmethod
    def _compact_work_unit_result_for_meta(cls, work_unit_result: WorkUnitResult | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(work_unit_result, WorkUnitResult):
            result_dict = work_unit_result.to_dict()
        elif isinstance(work_unit_result, Mapping):
            result_dict = dict(work_unit_result)
        else:
            return {}
        return {
            "id": result_dict.get("id"),
            "status": result_dict.get("status"),
            "summary": cls._compact_value_for_meta(result_dict.get("summary"), max_chars=1000),
            "candidate_final_result_present": bool(result_dict.get("candidate_final_result")),
            "artifact_manifest": cls._compact_value_for_meta(
                result_dict.get("artifact_manifest") or {}, max_chars=1000
            ),
            "evidence_count": len(result_dict.get("evidence") or []),
            "diagnostic_count": len(result_dict.get("diagnostics") or []),
        }

    @classmethod
    def _compact_block_carrier_for_meta(
        cls,
        *,
        work_unit: WorkUnitIntent,
        work_unit_result: WorkUnitResult,
        output_policy: CarrierOutputPolicy,
        block_result: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "work_unit": cls._compact_work_unit_for_meta(work_unit),
            "work_unit_result": cls._compact_work_unit_result_for_meta(work_unit_result),
            "output_policy": output_policy.to_dict(),
            "block_result": cls._compact_block_result_for_meta(block_result),
            "workspace_operations": cls._compact_workspace_operations_for_meta(snapshot),
            "snapshot_status": snapshot.get("status") if isinstance(snapshot, Mapping) else None,
        }

    @classmethod
    def _compact_execution_plan_for_meta(cls, execution_plan: Any) -> dict[str, Any]:
        plan_dict = DataFormatter.sanitize(execution_plan.to_dict())
        if not isinstance(plan_dict, dict):
            return {}
        compact = {
            key: plan_dict.get(key)
            for key in (
                "plan_id",
                "task_frame_id",
                "edges",
                "semantic_outputs",
                "evidence_requirements",
                "result_contracts",
                "checkpoint_policy",
            )
            if key in plan_dict
        }
        plan_blocks: list[dict[str, Any]] = []
        for block in plan_dict.get("plan_blocks") or []:
            if not isinstance(block, Mapping):
                continue
            bound_inputs = block.get("bound_inputs")
            compact_bound_inputs: dict[str, Any] = {}
            if isinstance(bound_inputs, Mapping):
                compact_bound_inputs = {
                    key: bound_inputs.get(key)
                    for key in (
                        "task_id",
                        "operation",
                        "query",
                        "filters",
                        "collection",
                        "kind",
                        "path",
                        "pattern",
                        "expected_role",
                        "max_results",
                        "include_snippets",
                        "snippet_limit",
                        "snippet_offset",
                        "preferred_execution_shape",
                        "step_plan",
                        "context_summary",
                    )
                    if key in bound_inputs
                }
                if isinstance(bound_inputs.get("work_unit"), Mapping):
                    compact_bound_inputs["work_unit"] = cls._compact_work_unit_for_meta(bound_inputs["work_unit"])
            plan_blocks.append(
                {
                    "id": block.get("id"),
                    "plan_block_id": block.get("plan_block_id"),
                    "kind": block.get("kind"),
                    "intent": cls._compact_value_for_meta(block.get("intent") or "", max_chars=500),
                    "bound_inputs": compact_bound_inputs,
                    "output_contract": block.get("output_contract"),
                    "evidence_contract": cls._compact_value_for_meta(
                        block.get("evidence_contract") or {},
                        max_chars=1000,
                    ),
                    "runtime_preferences": cls._compact_value_for_meta(
                        block.get("runtime_preferences") or {},
                        max_chars=1000,
                    ),
                    "budget": block.get("budget"),
                }
            )
        compact["plan_blocks"] = plan_blocks
        capability_resolution = plan_dict.get("capability_resolution")
        if isinstance(capability_resolution, Mapping):
            compact["capability_resolution"] = cls._compact_value_for_meta(capability_resolution, max_chars=1500)
        return compact

    @classmethod
    def _compact_execution_graph_for_meta(cls, execution_graph: Any) -> dict[str, Any]:
        graph_dict = DataFormatter.sanitize(execution_graph.to_dict())
        if not isinstance(graph_dict, dict):
            return {}
        compact = {
            key: graph_dict.get(key)
            for key in (
                "execution_id",
                "task_frame_id",
                "plan_id",
                "edges",
                "resource_requirements",
                "result_contracts",
            )
            if key in graph_dict
        }
        execution_blocks: list[dict[str, Any]] = []
        for block in graph_dict.get("execution_blocks") or []:
            if not isinstance(block, Mapping):
                continue
            execution_blocks.append(
                {
                    key: block.get(key)
                    for key in (
                        "id",
                        "execution_block_id",
                        "plan_block_id",
                        "kind",
                        "handler",
                        "runtime_binding",
                        "source_task_dag_node_id",
                    )
                    if key in block
                }
            )
        compact["execution_blocks"] = execution_blocks
        return compact

    @classmethod
    def _compact_block_result_for_meta(cls, block_result: Mapping[str, Any]) -> dict[str, Any]:
        compact = {key: block_result.get(key) for key in ("status", "diagnostics", "errors") if key in block_result}
        semantic_outputs = block_result.get("semantic_outputs")
        if isinstance(semantic_outputs, Mapping):
            compact["semantic_outputs"] = {
                str(key): cls._compact_value_for_meta(value, max_chars=1000) for key, value in semantic_outputs.items()
            }
        return compact

    @classmethod
    def _compact_blocks_evidence_for_meta(cls, evidence: Any) -> dict[str, Any]:
        evidence_dict = DataFormatter.sanitize(evidence.to_dict())
        if not isinstance(evidence_dict, dict):
            return {}
        compact = {key: evidence_dict.get(key) for key in ("status", "diagnostics", "errors") if key in evidence_dict}
        for key in ("execution_block_results", "plan_block_results"):
            compact_results: list[dict[str, Any]] = []
            for item in evidence_dict.get(key) or []:
                if not isinstance(item, Mapping):
                    continue
                output = item.get("output")
                output_summary: dict[str, Any] = {}
                if isinstance(output, Mapping):
                    if str(item.get("kind") or "") == "workspace_operation":
                        for output_key in ("operation", "query", "filters", "bounded", "diagnostics"):
                            if output_key in output:
                                output_summary[output_key] = cls._compact_value_for_meta(
                                    output.get(output_key),
                                    max_chars=1000,
                                )
                        locator_refs = output.get("locator_refs")
                        if isinstance(locator_refs, (list, tuple)):
                            output_summary["locator_ref_count"] = len(locator_refs)
                            if locator_refs:
                                output_summary["first_locator_ref"] = cls._compact_workspace_ref_or_snippet_for_meta(
                                    locator_refs[0],
                                    max_chars=1000,
                                )
                        evidence_snippets = output.get("evidence_snippets")
                        if isinstance(evidence_snippets, (list, tuple)):
                            output_summary["evidence_snippet_count"] = len(evidence_snippets)
                            if evidence_snippets:
                                output_summary["first_evidence_snippet"] = cls._compact_workspace_ref_or_snippet_for_meta(
                                    evidence_snippets[0],
                                    max_chars=1800,
                                )
                    execution_meta = output.get("execution_meta")
                    if isinstance(execution_meta, Mapping):
                        output_summary["execution_meta"] = {
                            meta_key: execution_meta.get(meta_key)
                            for meta_key in ("execution_id", "status")
                            if meta_key in execution_meta
                        }
                        route = execution_meta.get("route")
                        if isinstance(route, Mapping):
                            output_summary["execution_meta"]["route"] = {
                                route_key: route.get(route_key)
                                for route_key in ("selected_route", "status")
                                if route_key in route
                            }
                    execution_result = output.get("execution_result")
                    output_summary["execution_result"] = cls._compact_value_for_meta(
                        execution_result,
                        max_chars=1000,
                    )
                compact_results.append(
                    {
                        result_key: item.get(result_key)
                        for result_key in (
                            "id",
                            "plan_block_id",
                            "source_plan_block_id",
                            "execution_block_id",
                            "kind",
                            "status",
                        )
                        if result_key in item
                    }
                    | ({"output": output_summary} if output_summary else {})
                )
            compact[key] = compact_results
        return compact

    @classmethod
    def _compact_workspace_operations_for_meta(cls, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        blocks_state = snapshot.get("blocks", {}) if isinstance(snapshot, Mapping) else {}
        results = blocks_state.get("execution_block_results") if isinstance(blocks_state, Mapping) else None
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
            return []
        operations: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "") != "workspace_operation":
                continue
            output = item.get("output")
            if not isinstance(output, Mapping):
                continue
            output_summary: dict[str, Any] = {}
            for output_key in ("operation", "query", "filters", "bounded", "diagnostics"):
                if output_key in output:
                    output_summary[output_key] = cls._compact_value_for_meta(output.get(output_key), max_chars=1000)
            locator_refs = output.get("locator_refs")
            if isinstance(locator_refs, (list, tuple)):
                output_summary["locator_ref_count"] = len(locator_refs)
                if locator_refs:
                    output_summary["first_locator_ref"] = cls._compact_workspace_ref_or_snippet_for_meta(
                        locator_refs[0],
                        max_chars=1000,
                    )
            evidence_snippets = output.get("evidence_snippets")
            if isinstance(evidence_snippets, (list, tuple)):
                output_summary["evidence_snippet_count"] = len(evidence_snippets)
                if evidence_snippets:
                    output_summary["first_evidence_snippet"] = cls._compact_workspace_ref_or_snippet_for_meta(
                        evidence_snippets[0],
                        max_chars=1800,
                    )
            operations.append(
                {
                    result_key: item.get(result_key)
                    for result_key in (
                        "id",
                        "plan_block_id",
                        "source_plan_block_id",
                        "execution_block_id",
                        "kind",
                        "status",
                    )
                    if result_key in item
                }
                | ({"output": output_summary} if output_summary else {})
            )
        return operations

    @classmethod
    def _compact_workspace_ref_or_snippet_for_meta(cls, value: Any, *, max_chars: int) -> Any:
        if not isinstance(value, Mapping):
            return cls._compact_value_for_meta(value, max_chars=max_chars)
        compact: dict[str, Any] = {}
        for key in (
            "path",
            "line",
            "line_start",
            "line_end",
            "role",
            "content_state",
            "source",
            "query",
            "search_engine",
            "grep_tool",
            "bytes",
            "sha256",
        ):
            if key in value:
                compact[key] = value.get(key)
        content = value.get("content")
        if not isinstance(content, str):
            content = value.get("snippet")
        if not isinstance(content, str):
            content = value.get("text")
        if isinstance(content, str):
            compact["content"] = cls._compact_value_for_meta(content, max_chars=max_chars)
        return cls._compact_value_for_meta(compact or value, max_chars=max_chars)

    @staticmethod
    def _attach_blocks_evidence(
        execution_meta: dict[str, Any],
        *,
        execution_plan: Any,
        execution_graph: Any,
        evidence: Any,
        block_result: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> None:
        blocks_state = snapshot.get("blocks", {}) if isinstance(snapshot, Mapping) else {}
        snapshot_summary = {
            "status": snapshot.get("status") if isinstance(snapshot, Mapping) else None,
            "blocks": {
                "status": blocks_state.get("status") if isinstance(blocks_state, Mapping) else None,
                "replan_signals": (
                    DataFormatter.sanitize(blocks_state.get("replan_signals", []))
                    if isinstance(blocks_state, Mapping)
                    else []
                ),
            },
        }
        execution_meta["blocks"] = {
            "execution_plan": AgentTaskCarrierMixin._compact_execution_plan_for_meta(execution_plan),
            "execution_block_graph": AgentTaskCarrierMixin._compact_execution_graph_for_meta(execution_graph),
            "evidence": AgentTaskCarrierMixin._compact_blocks_evidence_for_meta(evidence),
            "result": AgentTaskCarrierMixin._compact_block_result_for_meta(block_result),
            "snapshot": snapshot_summary,
        }

    @staticmethod
    def _resolve_blocks():
        from agently.base import blocks

        return blocks

    def _bind_action_workspace(self, execution: Any) -> None:
        request = getattr(execution, "request", None)
        set_settings = getattr(request, "set_settings", None)
        if callable(set_settings):
            set_settings("action.workspace", self.workspace)


__all__ = ["AgentTaskCarrierMixin"]
