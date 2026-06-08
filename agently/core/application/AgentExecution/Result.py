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

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator


class AgentExecutionResult:
    """Reusable result facade for one AgentExecution."""

    def __init__(self, execution: Any):
        self.execution = execution
        self.execution_id = execution.id

    @property
    def result(self) -> "AgentExecutionResult":
        return self

    @property
    def status(self) -> str:
        return str(self.execution.status)

    @property
    def task_refs(self) -> dict[str, Any]:
        refs = getattr(self.execution, "task_refs", None)
        return dict(refs) if isinstance(refs, dict) else {}

    @property
    def full_result_data(self) -> dict[str, Any]:
        raw_result = getattr(self.execution, "result", None)
        if isinstance(raw_result, dict) and "result" in raw_result and "extra" in raw_result:
            return dict(raw_result)
        return {
            "result": raw_result,
            "extra": {
                "logs": getattr(self.execution, "logs", {}),
                "task_refs": self.task_refs,
            },
        }

    async def async_get_data(self, **kwargs: Any) -> Any:
        return await self.execution.async_get_data(**kwargs)

    def get_data(self, **kwargs: Any) -> Any:
        return self.execution.get_data(**kwargs)

    async def async_get_data_object(self, **kwargs: Any) -> Any:
        return await self.async_get_data(**kwargs)

    def get_data_object(self, **kwargs: Any) -> Any:
        return self.get_data(**kwargs)

    async def async_get_text(self, **kwargs: Any) -> str:
        return await self.execution.async_get_text(**kwargs)

    def get_text(self, **kwargs: Any) -> str:
        return self.execution.get_text(**kwargs)

    async def async_get_meta(self) -> dict[str, Any]:
        return await self.execution.async_get_meta()

    def get_meta(self) -> dict[str, Any]:
        return self.execution.get_meta()

    def get_async_generator(self, *args: Any, **kwargs: Any) -> "AsyncGenerator[Any, None]":
        return self.execution.get_async_generator(*args, **kwargs)

    def get_generator(self, *args: Any, **kwargs: Any) -> "Generator[Any, None, None]":
        return self.execution.get_generator(*args, **kwargs)

    async def async_get_status(self) -> str:
        await self.async_get_meta()
        return str(self.execution.status)

    def get_status(self) -> str:
        self.get_meta()
        return str(self.execution.status)

    async def async_resume(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "supported": False,
            "reason": "AgentExecution resume is reserved for resumable strategies.",
        }

    def resume(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "supported": False,
            "reason": "AgentExecution resume is reserved for resumable strategies.",
        }
