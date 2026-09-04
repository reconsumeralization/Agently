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

import inspect
from collections.abc import Mapping
from typing import Literal, TYPE_CHECKING, TypedDict, cast

from agently.core.application.AgentExecution import AgentVerificationError
from agently.types.data import AgentReviewContext, AgentReviewHandler, AgentReviewResult
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


class _AgentReviewDeclaration(TypedDict):
    required: bool
    handler: AgentReviewHandler | None


_MAX_SUMMARY_CHARS = 2_000
_MAX_LIST_ITEMS = 20
_MAX_ITEM_CHARS = 1_000


def declare_review(
    execution: "AgentExecution",
    *,
    required: bool,
    handler: "AgentReviewHandler | None",
) -> "AgentExecution":
    target = execution._reconfiguration_target()
    if handler is not None and not callable(handler):
        raise TypeError("Agent review handler must be callable or None.")
    target.review_declarations.append(
        {
            "required": bool(required),
            "handler": handler,
        }
    )
    return target


async def run_declared_reviews(execution: "AgentExecution", result: object) -> None:
    declarations = list(execution.review_declarations)
    for index, declaration in enumerate(declarations, start=1):
        required = bool(declaration.get("required"))
        handler = declaration["handler"]
        review_id = f"{execution.id}:review:{index}"
        source: Literal["handler", "model"] = "handler" if handler is not None else "model"
        handler_name = _handler_name(handler)
        context = AgentReviewContext(
            execution=execution,
            prompt=dict(execution.prompt_snapshot),
            goals=tuple(execution.goal_items),
            success_criteria=tuple(execution.success_criteria_items),
            artifact_refs=tuple(execution.artifact_results),
            task_workspace=execution.task_workspace,
            required=required,
            index=index,
        )
        event_base = {
            "review_id": review_id,
            "index": index,
            "required": required,
            "source": source,
            "handler": handler_name,
        }
        await execution.emit_stream(
            "review.started",
            event_base,
            route=execution.route_info.get("selected_route"),
            source="agent_review",
            meta={"review_id": review_id, "required": required},
        )
        raw_review = (
            await _run_model_review(execution, result, context)
            if handler is None
            else await _run_handler(handler, result, context)
        )
        normalized = _normalize_review(
            raw_review,
            review_id=review_id,
            index=index,
            required=required,
            source=source,
            handler_name=handler_name,
        )
        execution.review_results.append(normalized)
        _refresh_review_diagnostics(execution)
        await execution.emit_stream(
            "review.completed",
            normalized,
            route=execution.route_info.get("selected_route"),
            source="agent_review",
            meta={
                "review_id": review_id,
                "required": required,
                "passed": normalized["passed"],
            },
        )
        if required and not normalized["passed"]:
            execution.status = "blocked"
            execution.close_snapshot = {
                **dict(execution.close_snapshot),
                "status": "blocked",
                "reason": normalized.get("summary") or "Candidate failed required verification.",
                "verification": DataFormatter.sanitize(normalized),
            }
            await execution.emit_stream(
                "verification.failed",
                normalized,
                route=execution.route_info.get("selected_route"),
                source="agent_review",
                meta={"review_id": review_id, "required": True, "passed": False},
            )
            raise AgentVerificationError(normalized)


async def _run_handler(
    handler: "AgentReviewHandler",
    result: object,
    context: AgentReviewContext,
) -> object:
    value = handler(result, context)
    if inspect.isawaitable(value):
        return await value
    return value


async def _run_model_review(
    execution: "AgentExecution",
    result: object,
    context: AgentReviewContext,
) -> object:
    request = execution.agent.create_request()
    request.input(
        {
            "candidate": DataFormatter.sanitize(result),
            "verification_is_required": context.required,
        }
    )
    request.info(
        {
            "request_contract": DataFormatter.sanitize(dict(context.prompt)),
            "goals": list(context.goals),
            "success_criteria": list(context.success_criteria),
            "trusted_artifact_refs": DataFormatter.sanitize(list(context.artifact_refs)),
        }
    )
    request.instruct(
        "Review the candidate only against the supplied request contract, goals, success "
        "criteria, and trusted artifact references. Identify concrete material issues and "
        "actionable improvements. Do not invent missing requirements, revise the candidate, "
        "or provide hidden chain-of-thought. Set passed=false only when a stated requirement "
        "is materially unmet."
    )
    request.output(
        {
            "passed": (bool, "Whether the candidate meets the supplied contract.", True),
            "score": (float, "Quality score from 0.0 to 1.0.", True),
            "summary": (str, "Concise verdict grounded in the supplied contract.", True),
            "issues": ([str], "Concrete material issues; empty when none.", True),
            "suggestions": ([str], "Actionable improvements; empty when none.", True),
        },
        format="json",
    )
    result_handle = request.get_result()
    try:
        return await result_handle.async_get_data()
    finally:
        execution.record_model_response_id(result_handle.id)


def _normalize_review(
    value: object,
    *,
    review_id: str,
    index: int,
    required: bool,
    source: Literal["handler", "model"],
    handler_name: str | None,
) -> AgentReviewResult:
    if isinstance(value, bool):
        payload: Mapping[str, object] = {"passed": value}
    elif isinstance(value, Mapping):
        payload = cast(Mapping[str, object], value)
    else:
        raise TypeError("Agent review handler must return bool or a mapping with Boolean `passed`.")

    passed = payload.get("passed")
    if not isinstance(passed, bool):
        raise TypeError("Agent review result field `passed` must be Boolean.")

    score = payload.get("score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise TypeError("Agent review result field `score` must be a number from 0.0 to 1.0 or None.")
        score = float(score)
        if not 0.0 <= score <= 1.0:
            raise ValueError("Agent review result field `score` must be between 0.0 and 1.0.")

    normalized: AgentReviewResult = {
        "review_id": review_id,
        "index": index,
        "required": required,
        "source": source,
        "handler": handler_name,
        "passed": passed,
        "score": score,
        "summary": _bounded_text(payload.get("summary"), _MAX_SUMMARY_CHARS),
        "issues": _bounded_text_list(payload.get("issues")),
        "suggestions": _bounded_text_list(payload.get("suggestions")),
    }
    return normalized


def _bounded_text(value: object, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _bounded_text_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError("Agent review issues and suggestions must be lists of strings.")
    result: list[str] = []
    for item in value[:_MAX_LIST_ITEMS]:
        if not isinstance(item, str):
            raise TypeError("Agent review issues and suggestions must contain only strings.")
        text = item.strip()[:_MAX_ITEM_CHARS]
        if text:
            result.append(text)
    return result


def _handler_name(handler: AgentReviewHandler | None) -> str | None:
    if handler is None:
        return None
    return str(getattr(handler, "__name__", None) or handler.__class__.__name__)


def _refresh_review_diagnostics(execution: "AgentExecution") -> None:
    reviews = list(execution.review_results)
    execution.diagnostics["review"] = {
        "declared": len(execution.review_declarations),
        "completed": len(reviews),
        "passed": sum(1 for item in reviews if item.get("passed") is True),
        "failed": sum(1 for item in reviews if item.get("passed") is False),
        "required_failed": sum(
            1
            for item in reviews
            if item.get("required") is True and item.get("passed") is False
        ),
    }


__all__ = ["declare_review", "run_declared_reviews"]
