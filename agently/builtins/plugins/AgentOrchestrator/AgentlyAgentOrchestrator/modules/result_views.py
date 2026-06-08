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
from collections.abc import AsyncGenerator, Generator
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
    type: Literal["instant", "streaming_parse", "all"] | str | None = "instant",
    content: Any = None,
    **_: Any,
) -> AsyncGenerator[Any, None]:
    if content is not None and type is None:
        type = content
    if owner._completed:
        for item in owner.stream.items:
            yield ("agent_execution", item) if type == "all" else item
        return
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for item in owner.stream.items:
        await queue.put(item)
    owner.stream.queues.append(queue)
    start_task = asyncio.create_task(owner.async_start())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield ("agent_execution", item) if type == "all" else item
        await start_task
    finally:
        if queue in owner.stream.queues:
            owner.stream.queues.remove(queue)


def sync_generator(owner: "AgentExecution", *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
    return FunctionShifter.syncify_async_generator(owner.get_async_generator(*args, **kwargs))
