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

import uuid
from typing import Any, cast

from agently.types.data import ExecutionResourceHandle, ExecutionResourceRequirement


class ACPExecutionResourceProvider:
    name = "ACPExecutionResourceProvider"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    kind = "acp"

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    async def async_ensure(
        self,
        *,
        requirement: ExecutionResourceRequirement,
        policy: dict[str, Any],
        existing_handle: ExecutionResourceHandle | None = None,
    ) -> ExecutionResourceHandle:
        if existing_handle is not None and existing_handle.get("status") == "ready":
            return existing_handle
        config = dict(requirement.get("config", {}))
        agents = config.get("agents", [])
        if not isinstance(agents, list) or not agents:
            status = "failed"
            resource: dict[str, Any] = {"agents": [], "root": str(config.get("root", ""))}
        else:
            status = "ready"
            resource = {
                "agents": agents,
                "root": str(config.get("root", "")),
            }
        return cast(ExecutionResourceHandle, {
            "handle_id": f"acp:{ uuid.uuid4().hex }",
            "requirement_id": str(requirement.get("requirement_id", "")),
            "kind": "acp",
            "scope": requirement.get("scope", "action_call"),
            "owner_id": str(requirement.get("owner_id", "")),
            "resource_key": str(requirement.get("resource_key", "acp")),
            "status": status,
            "resource": resource,
            "policy": dict(policy),
            "ref_count": 1,
            "meta": {
                "root": resource.get("root", ""),
                "agent_count": len(agents) if isinstance(agents, list) else 0,
            },
        })

    async def async_health_check(self, handle: ExecutionResourceHandle) -> str:
        return "ready" if handle.get("status") == "ready" else "failed"

    async def async_release(self, handle: ExecutionResourceHandle) -> None:
        handle["status"] = "released"
