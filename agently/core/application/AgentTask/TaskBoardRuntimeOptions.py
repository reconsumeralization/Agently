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


_TASKBOARD_CARD_RETRYABLE_RESULT_CODES = {
    "agent_task.workspace_artifact.draft_empty",
    "agent_task.workspace_artifact.empty_body",
    "agent_task.workspace_artifact.readback_insufficient",
    "agent_task.workspace_artifact.structured_wrapper_body",
    "agent_task.workspace_artifact.structured_wrapper_draft",
    "taskboard.card.evidence_use_guard_blocking",
}


class AgentTaskTaskBoardRuntimeOptionsMixin(AgentTaskMixinBase):
    def _taskboard_effort(self) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping):
            effort = agent_task_options.get("effort")
            if effort is not None:
                return effort
        return "medium"

    def _taskboard_tick_timeout(self) -> float | None:
        return self._taskboard_option_timeout("taskboard_tick_timeout_seconds")

    def _taskboard_card_timeout(self) -> float | None:
        return self._taskboard_option_timeout("taskboard_card_timeout_seconds")

    def _taskboard_card_max_attempts(self) -> int:
        value = self._taskboard_option("taskboard_card_max_attempts")
        try:
            attempts = int(value) if value is not None else 2
        except (TypeError, ValueError):
            attempts = 2
        return min(max(1, attempts), 5)

    def _taskboard_card_error_retryable(self, error: Exception) -> bool:
        if self._is_timeout_error(error):
            return True
        text = f"{ error.__class__.__name__}: { str(error) }".lower()
        retry_markers = (
            "429",
            "chunked",
            "connect",
            "connection",
            "eof",
            "parse_failed",
            "rate limit",
            "request failed",
            "request_failed",
            "temporarily",
            "timeout",
            "tls",
        )
        return any(marker in text for marker in retry_markers)

    def _taskboard_card_result_retryable(
        self,
        *,
        status: str,
        diagnostics: Sequence[Any],
    ) -> bool:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"blocked", "failed"}:
            return False
        return bool(self._taskboard_card_retryable_result_codes(diagnostics))

    @staticmethod
    def _taskboard_card_retryable_result_codes(diagnostics: Sequence[Any]) -> list[str]:
        codes: list[str] = []
        for item in diagnostics:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip()
            if code in _TASKBOARD_CARD_RETRYABLE_RESULT_CODES and code not in codes:
                codes.append(code)
        return codes

    @staticmethod
    def _taskboard_card_result_diagnostic_preview(diagnostics: Sequence[Any]) -> list[dict[str, Any]]:
        preview: list[dict[str, Any]] = []
        for item in diagnostics:
            if not isinstance(item, Mapping):
                continue
            compact: dict[str, Any] = {}
            for key in ("code", "status", "message", "path", "source", "result_status"):
                value = item.get(key)
                if value in (None, "", [], {}):
                    continue
                compact[key] = str(value)[:800] if key == "message" else DataFormatter.sanitize(value)
            if compact:
                preview.append(compact)
            if len(preview) >= 8:
                break
        return preview

    def _taskboard_card_result_retry_diagnostic(
        self,
        *,
        card_id: str,
        status: str,
        diagnostics: Sequence[Any],
        attempt_index: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        retryable_codes = self._taskboard_card_retryable_result_codes(diagnostics)
        return {
            "type": "TaskBoardCardResultProtocolError",
            "code": "taskboard.card.result_protocol_retry",
            "message": "TaskBoard card result was blocked by retryable framework protocol diagnostics.",
            "card_id": card_id,
            "execution_strategy": self.execution_strategy,
            "stage": "taskboard_card",
            "result_status": str(status or "").strip().lower(),
            "retryable_codes": retryable_codes,
            "diagnostics": self._taskboard_card_result_diagnostic_preview(diagnostics),
            "attempt_index": attempt_index,
            "max_attempts": max_attempts,
            "retry_scheduled": attempt_index < max_attempts,
            "status": "retrying" if attempt_index < max_attempts else "failed",
        }

    def _taskboard_card_retry_diagnostic(
        self,
        *,
        card_id: str,
        error: Exception,
        execution_id: str | None,
        attempt_index: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
        return {
            "type": error.__class__.__name__,
            "code": "taskboard.card.timeout" if self._is_timeout_error(error) else "taskboard.card.execution_error",
            "message": message[:1000],
            "card_id": card_id,
            "execution_id": execution_id,
            "execution_strategy": self.execution_strategy,
            "stage": "taskboard_card",
            "attempt_index": attempt_index,
            "max_attempts": max_attempts,
            "retry_scheduled": attempt_index < max_attempts,
            "timeout_seconds": self._taskboard_card_timeout() if self._is_timeout_error(error) else None,
            "status": "retrying" if attempt_index < max_attempts else "failed",
        }

    def _taskboard_max_ticks(self) -> int | None:
        value = self._taskboard_option("taskboard_max_ticks")
        if value is None:
            return self.max_iterations
        try:
            ticks = int(value)
        except (TypeError, ValueError):
            return self.max_iterations
        return max(1, ticks)

    def _taskboard_max_ticks_source(self) -> str:
        if self._taskboard_option("taskboard_max_ticks") is not None:
            return "taskboard_option"
        if self.max_iterations is not None:
            return "explicit_max_iterations"
        return "unbounded_default"

    def _taskboard_concurrency(self) -> int | None:
        value = self._taskboard_option("taskboard_concurrency")
        if value is None:
            return None
        try:
            concurrency = int(value)
        except (TypeError, ValueError):
            return None
        return concurrency if concurrency > 0 else None

    def _taskboard_scheduler(self) -> str:
        value = self._taskboard_option("taskboard_scheduler")
        text = str(value or "batch").strip().lower().replace("-", "_")
        if text in {"frontier", "event_driven", "evented", "dynamic_frontier"}:
            return "frontier"
        return "batch"

    def _taskboard_option_timeout(self, key: str) -> float | None:
        value = self._taskboard_option(key)
        if value is None:
            return None
        return self._normalize_timeout(value)

    def _taskboard_option(self, key: str) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping) and key in agent_task_options:
            return agent_task_options.get(key)
        return self.options.get(key)


__all__ = ["AgentTaskTaskBoardRuntimeOptionsMixin"]
