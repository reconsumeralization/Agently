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
import os
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast, Literal, TYPE_CHECKING, TypeVar

from agently.core.orchestration import (
    TaskBoard,
    TriggerFlow,
    build_task_board_evidence_view,
    coerce_task_board_planning_result,
    resolve_task_board_planning_policy,
    task_board_planning_output_schema,
)
from agently.core.orchestration.TaskBoard.TaskBoardValidation import task_board_card_required
from agently.core.application.AgentExecution.Stream import project_agent_execution_text_delta
from agently.core.model.StructuredOutputParser import parse_output_contract_dict
from agently.types.data import AgentExecutionStreamData, ReplanSignal, TaskBoardCardResult, TaskBoardRevision
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataFormatter, FunctionShifter
from agently.utils.LanguagePolicy import (
    apply_language_policy_to_prompt,
    language_policy_from_prompt_snapshot,
    resolve_language_policy,
)

from .BlockCarrier import CarrierOutputPolicy, WorkUnitIntent, WorkUnitResult, select_carrier_output_policy

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import WorkspaceContextPackage, WorkspaceRecordRef
else:
    BaseAgent = Any
    WorkspaceContextPackage = dict[str, Any]
    WorkspaceRecordRef = dict[str, Any]


AgentTaskStatus = Literal[
    "created",
    "running",
    "completed",
    "blocked",
    "max_iterations",
    "timed_out",
    "capability_unavailable",
    "error",
]
AgentTaskExecutionStrategy = Literal["auto", "flat", "taskboard"]
AgentTaskEffectiveExecutionStrategy = Literal["flat", "taskboard"]

_AGENT_TASK_EXECUTION_STRATEGY_ALIASES = {
    "": "auto",
    "default": "auto",
    "automatic": "auto",
    "linear": "flat",
    "react": "flat",
    "flat_react": "flat",
    "task_board": "taskboard",
    "board": "taskboard",
    "taskboard_evidenceview": "taskboard",
}


_STEP_EXECUTION_SHAPES = {
    "direct",
    "actions",
    "skills",
    "dynamic_task",
    "execution_dag",
}

_DEGRADED_DAG_STEP_EXECUTION_SHAPES = {"dynamic_task", "execution_dag"}
_TASKBOARD_CONTROL_CARD_SHAPES = {
    "control",
    "model_control",
    "synthesis",
    "synthesize",
    "finalize",
    "final",
    "verification",
    "verify",
}
_TASKBOARD_READBACK_CARD_SHAPES = {
    "readback",
    "artifact_readback",
    "cold_readback",
    "evidence_readback",
}
_TASKBOARD_READBACK_PREVIEW_CHARS = 12000
_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS = 12000
_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS = 4
_TASKBOARD_SOURCE_REFS_MAX = 16
_TASKBOARD_PROMPT_RESULT_CHARS = 1600
_TASKBOARD_STREAM_SUMMARY_CHARS = 3000
_TASKBOARD_RECOVERABLE_CARD_STATUSES = {"failed", "error", "timed_out", "blocked"}
_WORKSPACE_ARTIFACT_PREVIEW_BYTES = 4000
_WORKSPACE_ARTIFACT_CONTENT_KEYS = ("content", "markdown", "body", "text")
_WORKSPACE_ARTIFACT_RESULT_BODY_KEYS = (
    "artifact_markdown",
    "artifact_html",
    "candidate_final_result",
    "final_result",
    "answer",
)

# Upper bound on the in-memory stream replay buffer for late subscribers.
_STREAM_REPLAY_LIMIT = 5000
_VERIFIER_PROMPT_VALUE_CHARS = 12000
_VERIFIER_PROMPT_ITEM_CHARS = 2400
_AGENT_TASK_ERROR_MESSAGE_CHARS = 2000
_AGENT_TASK_ERROR_PAYLOAD_MARKERS = (
    "\nrequest data:",
    "\nrequest body:",
    "\nrequest payload:",
    "\nraw request:",
    "\nprompt data:",
)
_AGENT_TASK_HOT_PATH_REQUEST_PAYLOAD_KEYS = {
    "requestdata",
    "requestbody",
    "requestpayload",
    "rawrequest",
    "promptdata",
    "providerrequest",
    "providerrequestdata",
    "modelrequestdata",
}
_AGENT_TASK_DEFAULT_MAX_ITERATIONS: int | None = None


def _normalize_agent_task_max_iterations(value: Any) -> int | None:
    if value is None:
        return _AGENT_TASK_DEFAULT_MAX_ITERATIONS
    try:
        iterations = int(value)
    except (TypeError, ValueError):
        return _AGENT_TASK_DEFAULT_MAX_ITERATIONS
    return max(1, iterations)


class _AgentTaskMixinMeta(type):
    def __getattr__(cls, name: str) -> Any:
        raise AttributeError(name)


class AgentTaskMixinBase(metaclass=_AgentTaskMixinMeta):
    """Typing aid for AgentTask mixins composed by the root AgentTask class."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


_AgentTaskT = TypeVar("_AgentTaskT", bound=AgentTaskMixinBase)


class _AgentTaskDeadlineExceeded(TimeoutError):
    def __init__(
        self,
        stage: str,
        *,
        reason: str | None = None,
        limit_name: str = "max_seconds",
        timeout_seconds: float | None = None,
    ):
        super().__init__(reason or f"AgentTask exceeded {limit_name} while running stage '{stage}'.")
        self.stage = stage
        self.reason = reason
        self.limit_name = limit_name
        self.timeout_seconds = timeout_seconds


def _compact_agent_task_error_message(
    message: Any,
    *,
    fallback: str = "",
    max_chars: int = _AGENT_TASK_ERROR_MESSAGE_CHARS,
) -> str:
    raw = str(message or fallback or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw:
        return fallback
    lower = raw.lower()
    payload_omitted = False
    end_index: int | None = None
    for marker in _AGENT_TASK_ERROR_PAYLOAD_MARKERS:
        marker_index = lower.find(marker)
        if marker_index >= 0 and (end_index is None or marker_index < end_index):
            end_index = marker_index
    if lower.startswith("request data:") or lower.startswith("request body:") or lower.startswith("request payload:"):
        end_index = 0
    if end_index is not None:
        raw_prefix = raw[:end_index].strip()
        raw = raw_prefix or fallback or "Provider request failed."
        payload_omitted = True

    compact_lines: list[str] = []
    prior_blank = False
    for line in raw.split("\n"):
        stripped = line.rstrip()
        if not stripped:
            if compact_lines and not prior_blank:
                compact_lines.append("")
            prior_blank = True
            continue
        compact_lines.append(stripped)
        prior_blank = False
    compact = "\n".join(compact_lines).strip() or fallback
    original_length = len(str(message or ""))
    truncated = len(compact) > max_chars
    if truncated:
        compact = compact[:max_chars].rstrip()
    suffix_parts: list[str] = []
    if payload_omitted:
        suffix_parts.append("provider payload omitted")
    if truncated:
        suffix_parts.append(f"truncated from {original_length} chars")
    if suffix_parts:
        suffix = f" [{'; '.join(suffix_parts)}]"
        if len(compact) + len(suffix) > max_chars:
            compact = compact[: max(0, max_chars - len(suffix))].rstrip()
        compact = f"{compact}{suffix}".strip()
    return compact or fallback


def _agent_task_hot_path_request_payload_omission(value: Any) -> dict[str, Any]:
    try:
        text = json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str)
        chars = len(text)
    except Exception:
        chars = len(str(value or ""))
    return {
        "omitted": True,
        "reason": "provider_request_payload_hot_path",
        "chars": chars,
    }


def _agent_task_hot_path_key_is_request_payload(key: Any) -> bool:
    normalized = "".join(char for char in str(key or "").lower() if char.isalnum())
    return normalized in _AGENT_TASK_HOT_PATH_REQUEST_PAYLOAD_KEYS


def _omit_agent_task_request_payloads_from_hot_path(value: Any, *, depth: int = 0) -> Any:
    """Remove full provider request payloads before planner/verifier hot paths."""

    sanitized = DataFormatter.sanitize(value)
    if sanitized is None or isinstance(sanitized, (bool, int, float)):
        return sanitized
    if isinstance(sanitized, bytes):
        return sanitized
    if isinstance(sanitized, str):
        lower = sanitized.lower()
        for marker in _AGENT_TASK_ERROR_PAYLOAD_MARKERS:
            marker_text = marker.strip()
            if lower.startswith(marker_text):
                return _agent_task_hot_path_request_payload_omission(sanitized)
            marker_index = lower.find(marker)
            if marker_index >= 0:
                prefix = sanitized[:marker_index].strip()
                return {
                    "preview": prefix[: _AGENT_TASK_ERROR_MESSAGE_CHARS] if prefix else "Provider request payload omitted.",
                    "omitted": True,
                    "reason": "provider_request_payload_hot_path",
                    "chars": len(sanitized),
                }
        return sanitized
    if depth >= 8:
        return sanitized
    if isinstance(sanitized, Mapping):
        compacted: dict[str, Any] = {}
        for key, item in sanitized.items():
            key_text = str(key)
            if _agent_task_hot_path_key_is_request_payload(key_text):
                compacted[key_text] = _agent_task_hot_path_request_payload_omission(item)
            else:
                compacted[key_text] = _omit_agent_task_request_payloads_from_hot_path(
                    item,
                    depth=depth + 1,
                )
        return compacted
    if isinstance(sanitized, Sequence) and not isinstance(sanitized, str | bytes | bytearray):
        return [
            _omit_agent_task_request_payloads_from_hot_path(item, depth=depth + 1)
            for item in sanitized
        ]
    return sanitized


def _compact_agent_task_error_info(error_info: Any) -> Any:
    if isinstance(error_info, Mapping):
        compacted = dict(error_info)
        if "message" in compacted:
            original_message = str(compacted.get("message") or "")
            fallback = str(compacted.get("type") or "ExecutionError")
            compacted_message = _compact_agent_task_error_message(original_message, fallback=fallback)
            compacted["message"] = compacted_message
            if compacted_message != original_message:
                compacted["message_compacted"] = True
                compacted["message_original_length"] = len(original_message)
        return compacted
    if isinstance(error_info, str):
        return _compact_agent_task_error_message(error_info)
    return error_info


__all__ = [
    "Any",
    "AsyncGenerator",
    "Awaitable",
    "Callable",
    "Generator",
    "Literal",
    "Mapping",
    "Path",
    "Sequence",
    "TypeVar",
    "asyncio",
    "cast",
    "json",
    "os",
    "suppress",
    "time",
    "uuid",
    "AgentExecutionStreamData",
    "AgentTaskEffectiveExecutionStrategy",
    "AgentTaskExecutionStrategy",
    "AgentTaskMixinBase",
    "AgentTaskStatus",
    "BaseAgent",
    "CarrierOutputPolicy",
    "DataFormatter",
    "FunctionShifter",
    "ReplanSignal",
    "TaskBoard",
    "TaskBoardCardResult",
    "TaskBoardRevision",
    "TriggerFlow",
    "TriggerFlowRuntimeData",
    "WorkspaceContextPackage",
    "WorkspaceRecordRef",
    "WorkUnitIntent",
    "WorkUnitResult",
    "_AGENT_TASK_EXECUTION_STRATEGY_ALIASES",
    "_AGENT_TASK_DEFAULT_MAX_ITERATIONS",
    "_AgentTaskT",
    "_AgentTaskDeadlineExceeded",
    "_AGENT_TASK_ERROR_MESSAGE_CHARS",
    "_DEGRADED_DAG_STEP_EXECUTION_SHAPES",
    "_STEP_EXECUTION_SHAPES",
    "_STREAM_REPLAY_LIMIT",
    "_TASKBOARD_CONTROL_CARD_SHAPES",
    "_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS",
    "_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS",
    "_TASKBOARD_PROMPT_RESULT_CHARS",
    "_TASKBOARD_READBACK_CARD_SHAPES",
    "_TASKBOARD_READBACK_PREVIEW_CHARS",
    "_TASKBOARD_RECOVERABLE_CARD_STATUSES",
    "_TASKBOARD_SOURCE_REFS_MAX",
    "_TASKBOARD_STREAM_SUMMARY_CHARS",
    "_VERIFIER_PROMPT_ITEM_CHARS",
    "_VERIFIER_PROMPT_VALUE_CHARS",
    "_WORKSPACE_ARTIFACT_CONTENT_KEYS",
    "_WORKSPACE_ARTIFACT_PREVIEW_BYTES",
    "_WORKSPACE_ARTIFACT_RESULT_BODY_KEYS",
    "_compact_agent_task_error_info",
    "_compact_agent_task_error_message",
    "_normalize_agent_task_max_iterations",
    "_omit_agent_task_request_payloads_from_hot_path",
    "apply_language_policy_to_prompt",
    "build_task_board_evidence_view",
    "coerce_task_board_planning_result",
    "language_policy_from_prompt_snapshot",
    "parse_output_contract_dict",
    "project_agent_execution_text_delta",
    "resolve_language_policy",
    "resolve_task_board_planning_policy",
    "select_carrier_output_policy",
    "task_board_card_required",
    "task_board_planning_output_schema",
]
