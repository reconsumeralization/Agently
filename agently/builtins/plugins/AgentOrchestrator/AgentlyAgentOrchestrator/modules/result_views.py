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
import html
import json
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import suppress
from typing import Any, Literal, TYPE_CHECKING

from agently.utils import DataFormatter, FunctionShifter

from .diagnostics import build_execution_meta

if TYPE_CHECKING:
    from agently.types.data import OutputValidateHandler, RunContext

    from .execution import AgentExecution


async def async_get_data(
    owner: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"] = "parsed",
    ensure_keys: list[str] | None = None,
    ensure_all_keys: bool | None = None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
    key_style: Literal["dot", "slash"] = "dot",
    max_retries: int = 3,
    raise_ensure_failure: bool = True,
    parent_run_context: "RunContext | None" = None,
) -> Any:
    return await owner.async_start(
        type=type,
        ensure_keys=ensure_keys,
        ensure_all_keys=ensure_all_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
        parent_run_context=parent_run_context,
    )


async def async_get_text(
    owner: "AgentExecution",
    *,
    parent_run_context: "RunContext | None" = None,
    **kwargs: Any,
) -> str:
    data = await owner.async_get_data(parent_run_context=parent_run_context, **kwargs)
    if isinstance(data, str):
        return data
    return json.dumps(DataFormatter.sanitize(data), ensure_ascii=False)


async def async_get_meta(owner: "AgentExecution") -> dict[str, Any]:
    if not owner._completed:
        await owner.async_start()
    owner._refresh_diagnostics()
    return build_execution_meta(owner)


async def get_async_generator(
    owner: "AgentExecution",
    type: Literal["delta", "instant", "streaming_parse", "all"] | str | None = "delta",
    content: Any = None,
    **_: Any,
) -> AsyncGenerator[Any, None]:
    if content is not None and type is None:
        type = content
    if owner._completed:
        for item in owner.stream.items:
            projected = _project_stream_item(item, type)
            if projected is not None:
                yield projected
        return
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for item in owner.stream.items:
        await queue.put(item)
    owner.stream.queues.append(queue)
    start_task = asyncio.create_task(owner.async_start())
    start_task.add_done_callback(_retrieve_generator_start_exception)
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            projected = _project_stream_item(item, type)
            if projected is not None:
                yield projected
        await start_task
    finally:
        if queue in owner.stream.queues:
            owner.stream.queues.remove(queue)


def _project_stream_item(item: Any, type: Any) -> Any:
    if type == "all":
        return ("agent_execution", item)
    if type == "delta":
        path = str(getattr(item, "path", "") or "")
        value = getattr(item, "value", None)
        if _is_retry_status_marker_source(path, value):
            return _format_retry_marker(value)
        if getattr(item, "event_type", None) != "delta":
            return None
        delta = getattr(item, "delta", None)
        if delta is None:
            return None
        return str(delta)
    return item


def _is_retry_status_marker_source(path: str, value: Any) -> bool:
    return (
        (path == "$status" or path.endswith(".$status"))
        and isinstance(value, Mapping)
        and value.get("status") == "failed"
        and value.get("retry") is True
    )


def _format_retry_marker(value: Any) -> str:
    reason = value.get("reason") if isinstance(value, Mapping) else None
    text = str(reason).strip() if reason is not None else ""
    if not text:
        text = "Retrying model request."
    return f"<$retry>{html.escape(text, quote=False)}</$retry>"


def _retrieve_generator_start_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    with suppress(Exception):
        task.exception()


def sync_generator(owner: "AgentExecution", *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
    return FunctionShifter.syncify_async_generator(owner.get_async_generator(*args, **kwargs))
