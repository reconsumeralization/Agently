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
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import suppress
from typing import Any, Literal, TYPE_CHECKING

from agently.core.application.AgentExecution.Stream import project_agent_execution_text_delta
from agently.types.data import AgentExecutionStreamData
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
            for projected in _project_stream_items(item, type):
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
            for projected in _project_stream_items(item, type):
                yield projected
        await start_task
    finally:
        if queue in owner.stream.queues:
            owner.stream.queues.remove(queue)


def _project_stream_items(item: Any, type: Any) -> Generator[Any, None, None]:
    if type == "all":
        yield ("agent_execution", item)
        return
    if type == "delta":
        projected = project_agent_execution_text_delta(item)
        if projected is not None:
            yield projected
        return
    yield item
    if type == "instant":
        projected = _project_instant_delta_item(item)
        if projected is not None:
            yield projected


def _project_instant_delta_item(item: Any) -> AgentExecutionStreamData | None:
    delta = project_agent_execution_text_delta(item)
    if delta is None:
        return None
    item_meta = getattr(item, "meta", None)
    meta_map = item_meta if isinstance(item_meta, Mapping) else {}
    stream_kind = meta_map.get("stream_kind")
    projection_meta: dict[str, Any] = {
        "stream_kind": "text_projection",
        "projection_source_path": str(getattr(item, "path", "") or ""),
        "projection_source_stream_kind": str(stream_kind) if stream_kind not in (None, "") else None,
    }
    for key in ("execution_id", "lineage"):
        if key in meta_map:
            projection_meta[key] = meta_map[key]
    event_type = getattr(item, "event_type", None)
    if event_type:
        projection_meta["projection_source_event_type"] = str(event_type)
    source = getattr(item, "source", None)
    if source:
        projection_meta["projection_source"] = str(source)
    return AgentExecutionStreamData(
        path="$delta",
        value=delta,
        delta=delta,
        event_type="delta",
        is_complete=False,
        source="agent_execution",
        route=getattr(item, "route", None),
        stage_id=getattr(item, "stage_id", None),
        task_id=getattr(item, "task_id", None),
        action_id=getattr(item, "action_id", None),
        graph_id=getattr(item, "graph_id", None),
        meta=projection_meta,
    )


def _retrieve_generator_start_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    with suppress(Exception):
        task.exception()


def sync_generator(owner: "AgentExecution", *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
    return FunctionShifter.syncify_async_generator(owner.get_async_generator(*args, **kwargs))
