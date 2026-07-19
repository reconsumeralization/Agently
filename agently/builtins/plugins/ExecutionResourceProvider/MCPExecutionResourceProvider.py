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

import uuid
from typing import TYPE_CHECKING

from ._base import BuiltinExecutionResourceProvider

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


class MCPExecutionResourceProvider(BuiltinExecutionResourceProvider):
    name = "MCPExecutionResourceProvider"
    DEFAULT_SETTINGS = {}
    kind = "mcp"

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = (policy, existing_handle)
        config = requirement.get("config", {})
        return {
            "handle_id": f"mcp:{ uuid.uuid4().hex }",
            "resource": config.get("transport"),
            "status": "ready",
            "meta": {"provider": self.name},
        }

    async def async_health_check(self, handle: "ExecutionResourceHandle") -> "ExecutionResourceStatus":
        return "ready" if handle.get("resource") is not None else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        _ = handle
        return None
