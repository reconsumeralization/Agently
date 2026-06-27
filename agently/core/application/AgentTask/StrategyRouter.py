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


class AgentTaskStrategyRouterMixin(AgentTaskMixinBase):
    @staticmethod
    def normalize_execution_strategy(value: Any = "auto") -> AgentTaskExecutionStrategy:
        text = str(value if value is not None else "auto").strip().lower().replace("-", "_")
        normalized = _AGENT_TASK_EXECUTION_STRATEGY_ALIASES.get(text, text)
        if normalized not in {"auto", "flat", "taskboard"}:
            raise ValueError("AgentTask execution must be one of: 'auto', 'flat', or 'taskboard'.")
        return cast(AgentTaskExecutionStrategy, normalized)

    async def _resolve_effective_execution_strategy(self) -> AgentTaskEffectiveExecutionStrategy:
        if self.effective_execution_strategy in {"flat", "taskboard"}:
            self._sync_execution_strategy_context(source="explicit_execution_strategy")
            return cast(AgentTaskEffectiveExecutionStrategy, self.effective_execution_strategy)
        if self.execution_strategy in {"flat", "taskboard"}:
            return self._set_effective_execution_strategy(
                cast(AgentTaskEffectiveExecutionStrategy, self.execution_strategy),
                source="explicit_execution_strategy",
            )

        try:
            analysis = await self._await_task_deadline(
                self._request_task_shape_analysis(),
                stage="task_shape_analysis",
            )
        except _AgentTaskDeadlineExceeded:
            raise
        except Exception as error:
            analysis = {
                "analysis": "",
                "execution_hint": {
                    "recommended_shape": "flat",
                    "confidence": "low",
                    "reasons": [],
                    "linear_evidence": [],
                    "branching_evidence": [],
                    "uncertainty": "task_shape_analysis failed; flat fallback selected",
                },
                "diagnostics": [
                    {
                        "code": "agent_task.task_shape_analysis.failed",
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    }
                ],
            }
        self.task_shape_analysis = self._normalize_task_shape_analysis(analysis)
        await self._record_task_shape_analysis()
        hint = self.task_shape_analysis.get("execution_hint")
        effective = "taskboard" if self._taskboard_hint_is_selectable(hint) else "flat"
        source = "task_shape_analysis" if effective == "taskboard" else "flat_fallback"
        return self._set_effective_execution_strategy(
            cast(AgentTaskEffectiveExecutionStrategy, effective),
            source=source,
        )

    def _set_effective_execution_strategy(
        self,
        value: AgentTaskEffectiveExecutionStrategy,
        *,
        source: str,
    ) -> AgentTaskEffectiveExecutionStrategy:
        self.effective_execution_strategy = value
        self.diagnostics["execution_strategy"] = {
            "requested": self.execution_strategy,
            "effective": value,
            "source": source,
            "task_shape_analysis_ref": (
                self.workspace_refs.get("strategy", [])[-1:] if self.workspace_refs.get("strategy") else []
            ),
        }
        self._sync_execution_strategy_context(source=source)
        return value

    def _sync_execution_strategy_context(self, *, source: str) -> None:
        try:
            from agently.core.runtime.RuntimeContext import get_current_agent_execution_context

            context = get_current_agent_execution_context()
            setter = getattr(context, "set_task_execution_strategy", None)
            if callable(setter):
                setter(
                    requested=self.execution_strategy,
                    effective=self.effective_execution_strategy,
                    source=source,
                )
        except Exception:
            return

    async def _request_task_shape_analysis(self) -> dict[str, Any]:
        request = self.agent.create_temp_request()
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "execution_strategy": self.execution_strategy,
                "execution_prompt": self._execution_prompt_context(),
                "planner_capabilities": self._planner_capabilities(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Analyze this task's execution shape for AgentTaskLoop strategy resolution. "
            "First write flexible natural-language analysis. Then provide execution_hint as a thin, non-binding "
            "structured hint. Do not decide final execution by keywords. Do not treat the hint as completion evidence. "
            "recommended_shape must be flat or taskboard. Prefer flat when confidence is low or uncertainty is material."
        )
        request.output(
            {
                "analysis": (str, "Free-form task-shape analysis.", True),
                "execution_hint": (
                    dict,
                    "Structured hint: recommended_shape, confidence, reasons, linear_evidence, branching_evidence, uncertainty.",
                    True,
                ),
            },
            format="json",
        )
        raw = await self._await_task_request(request.async_get_data(), stage="task_shape_analysis")
        if not isinstance(raw, Mapping):
            return {
                "analysis": str(raw or ""),
                "execution_hint": {"recommended_shape": "flat", "confidence": "low"},
                "diagnostics": [{"code": "agent_task.task_shape_analysis.invalid_type"}],
            }
        return dict(raw)

    def _normalize_task_shape_analysis(self, analysis: Any) -> dict[str, Any]:
        source = dict(analysis) if isinstance(analysis, Mapping) else {}
        raw_hint = source.get("execution_hint")
        hint = dict(raw_hint) if isinstance(raw_hint, Mapping) else {}
        diagnostics = list(source.get("diagnostics") or []) if isinstance(source.get("diagnostics"), list) else []
        try:
            recommended_shape = self.normalize_execution_strategy(hint.get("recommended_shape", "flat"))
            if recommended_shape == "auto":
                raise ValueError("execution_hint.recommended_shape must not be auto")
        except (TypeError, ValueError):
            recommended_shape = "flat"
            diagnostics.append({"code": "agent_task.task_shape_analysis.invalid_recommended_shape"})
        confidence = str(hint.get("confidence") or "low").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
            diagnostics.append({"code": "agent_task.task_shape_analysis.invalid_confidence"})
        normalized_hint = {
            "recommended_shape": recommended_shape,
            "confidence": confidence,
            "reasons": self._normalize_string_list(hint.get("reasons")),
            "linear_evidence": self._normalize_string_list(hint.get("linear_evidence")),
            "branching_evidence": self._normalize_string_list(hint.get("branching_evidence")),
            "uncertainty": str(hint.get("uncertainty") or "").strip(),
        }
        normalized = {
            "analysis": str(source.get("analysis") or "").strip(),
            "execution_hint": normalized_hint,
        }
        if diagnostics:
            normalized["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return DataFormatter.sanitize(normalized)

    def _taskboard_hint_is_selectable(self, hint: Any) -> bool:
        if not isinstance(hint, Mapping):
            return False
        if hint.get("recommended_shape") != "taskboard":
            return False
        policy = self._execution_strategy_policy()
        if self._normalize_bool(policy.get("allow_taskboard", True), default=True) is False:
            self.diagnostics.setdefault("execution_strategy_gates", []).append(
                {"gate": "allow_taskboard", "accepted": False}
            )
            return False
        threshold = str(policy.get("taskboard_confidence_threshold") or "medium").strip().lower()
        confidence = str(hint.get("confidence") or "low").strip().lower()
        order = {"low": 0, "medium": 1, "high": 2}
        if threshold not in order:
            threshold = "medium"
        if order.get(confidence, 0) < order[threshold]:
            self.diagnostics.setdefault("execution_strategy_gates", []).append(
                {
                    "gate": "taskboard_confidence_threshold",
                    "accepted": False,
                    "confidence": confidence,
                    "threshold": threshold,
                }
            )
            return False
        return True

    def _execution_strategy_policy(self) -> dict[str, Any]:
        raw = self._agent_task_option("execution_strategy_policy", None)
        if raw is None:
            raw = self._agent_task_option("strategy_policy", None)
        return dict(raw) if isinstance(raw, Mapping) else {}

    async def _record_task_shape_analysis(self) -> None:
        if not self.task_shape_analysis:
            return
        try:
            record_ref = await self.workspace.ingest(
                content={
                    "task_id": self.id,
                    "execution_strategy": self.execution_strategy,
                    "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
                    "completion_evidence": False,
                },
                collection="strategy",
                kind="agent_task_shape_analysis",
                summary=f"{self.id} task shape analysis",
                scope={"task_id": self.id},
                source={"type": "agent_task", "phase": "strategy"},
                meta={"task_id": self.id, "completion_evidence": False},
            )
            self._append_workspace_ref("strategy", record_ref)
        except Exception as error:
            self.diagnostics.setdefault("strategy_record_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                }
            )


__all__ = ["AgentTaskStrategyRouterMixin"]
