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


class AgentTaskAcpRecoveryMixin(AgentTaskMixinBase):
    async def _maybe_run_acp_recovery(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        execution_result: Any,
        execution_meta: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        policy = self._acp_recovery_policy()
        if not policy:
            return execution_result, execution_meta
        if (
            self.max_iterations is None
            or iteration_index < self.max_iterations
        ) and self._normalize_bool(
            policy.get("after_retry_exhausted", True),
            default=True,
        ):
            return execution_result, execution_meta

        action = getattr(self.agent, "action", None)
        execute_action = getattr(action, "async_execute_action", None)
        if not callable(execute_action):
            self.diagnostics.setdefault("acp_recovery", []).append(
                {"status": "skipped", "reason": "agent_action_runtime_unavailable"}
            )
            return execution_result, execution_meta

        action_id = str(policy.get("action_id") or "acp_run_task").strip()
        agent_id = str(policy.get("agent_id") or "").strip()
        if not agent_id:
            agent_id = await self._select_acp_recovery_agent_id(policy, execute_action)
        if not agent_id:
            self.diagnostics.setdefault("acp_recovery", []).append({"status": "skipped", "reason": "no_acp_agent_id"})
            return execution_result, execution_meta

        recovery_task = str(policy.get("task") or "").strip()
        if not recovery_task:
            recovery_task = (
                "Recover this failed AgentTask bounded step. Preserve the original goal, success criteria, "
                "failed plan, and failure diagnostics. Return concrete evidence and any repaired result."
            )
        payload = {
            "agent_id": agent_id,
            "task": recovery_task,
            "working_subdir": str(policy.get("working_subdir") or ""),
            "context": {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "plan": DataFormatter.sanitize(plan),
                "failed_execution_result": DataFormatter.sanitize(execution_result),
                "failed_execution_meta": DataFormatter.sanitize(execution_meta),
            },
        }
        try:
            raw_acp_result = execute_action(action_id, payload)
            if asyncio.iscoroutine(raw_acp_result) or isinstance(raw_acp_result, Awaitable):
                acp_result = await raw_acp_result
            else:
                acp_result = raw_acp_result
        except Exception as error:
            diagnostic = {
                "status": "failed",
                "reason": "acp_action_failed",
                "type": error.__class__.__name__,
                "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "action_id": action_id,
                "agent_id": agent_id,
            }
            self.diagnostics.setdefault("acp_recovery", []).append(diagnostic)
            return execution_result, execution_meta

        recovery_ref = await self._record_acp_recovery(
            iteration_index,
            plan=plan,
            failed_execution_meta=execution_meta,
            acp_result=acp_result,
        )
        acp_result_map = acp_result if isinstance(acp_result, Mapping) else {}
        if self._should_record_process_reflection("acp_call", plan=plan):
            await self._record_reflection(
                iteration_index,
                phase="acp_call",
                subject_ref=recovery_ref,
                summary={
                    "assessment": "ACP fallback returned recovery evidence.",
                    "status": str(acp_result_map.get("status") or ""),
                    "agent_id": agent_id,
                    "action_id": action_id,
                    "completion_evidence": False,
                },
            )
        recovered_ok = bool(acp_result_map.get("ok")) or str(acp_result_map.get("status") or "") in {
            "success",
            "completed",
        }
        recovered_result = {
            "step_result": "ACP fallback completed." if recovered_ok else "ACP fallback returned diagnostics.",
            "evidence": ["ACP fallback evidence was recorded."],
            "remaining_work": [] if recovered_ok else ["Review ACP fallback diagnostics and replan."],
            "acp_recovery": DataFormatter.sanitize(acp_result),
        }
        recovered_meta = {
            "execution_id": f"{self.id}:iter-{iteration_index}:acp-recovery",
            "status": "success" if recovered_ok else "failed",
            "route": {
                "selected_route": "acp_recovery",
                "status": "success" if recovered_ok else "failed",
                "action_id": action_id,
                "agent_id": agent_id,
            },
            "logs": {
                "action_logs": {
                    action_id: {
                        "status": "success" if recovered_ok else "failed",
                        "result": DataFormatter.sanitize(acp_result),
                    }
                },
                "route_logs": {},
                "errors": [],
                "record_refs": {"acp_recovery": [recovery_ref] if recovery_ref else []},
            },
            "record_refs": {"acp_recovery": [recovery_ref] if recovery_ref else []},
            "diagnostics": {
                "acp_recovery": {
                    "action_id": action_id,
                    "agent_id": agent_id,
                    "recovered": recovered_ok,
                }
            },
        }
        return recovered_result, recovered_meta

    def _acp_recovery_policy(self) -> dict[str, Any]:
        raw = self._agent_task_option("acp_recovery", None)
        if raw is None:
            raw = self._agent_task_option("recovery_policy", None)
        if not isinstance(raw, Mapping):
            return {}
        policy = dict(raw)
        nested_acp = policy.get("acp")
        if isinstance(nested_acp, Mapping):
            policy = dict(nested_acp)
        mode = str(policy.get("mode") or policy.get("type") or "acp").strip().lower()
        if mode not in {"acp", "agent_client_protocol"}:
            return {}
        if self._normalize_bool(policy.get("enabled", True), default=True) is False:
            return {}
        return policy

    async def _select_acp_recovery_agent_id(self, policy: dict[str, Any], execute_action: Any) -> str:
        list_action_id = str(policy.get("list_action_id") or "acp_list_agents").strip()
        try:
            raw_listed = execute_action(list_action_id, {})
            if asyncio.iscoroutine(raw_listed) or isinstance(raw_listed, Awaitable):
                listed = await raw_listed
            else:
                listed = raw_listed
        except Exception as error:
            self.diagnostics.setdefault("acp_recovery", []).append(
                {
                    "status": "skipped",
                    "reason": "acp_list_agents_failed",
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                }
            )
            return ""
        candidates: Any = listed
        if isinstance(listed, Mapping):
            data = listed.get("data")
            if isinstance(data, Mapping):
                candidates = data.get("agents")
            else:
                candidates = listed.get("agents")
        if not isinstance(candidates, Sequence) or isinstance(candidates, str | bytes | bytearray):
            return ""
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            agent_id = str(item.get("agent_id") or item.get("id") or item.get("name") or "").strip()
            if agent_id:
                return agent_id
        return ""

    async def _record_acp_recovery(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        failed_execution_meta: dict[str, Any],
        acp_result: Any,
    ) -> "RecordRef | None":
        _ = plan, failed_execution_meta, acp_result
        return self._memory_process_ref(
            "agent_task_acp_recovery", iteration=iteration_index, phase="acp_recovery"
        )


__all__ = ["AgentTaskAcpRecoveryMixin"]
