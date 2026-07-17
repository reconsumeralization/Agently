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

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


class BashExecutionResourceProvider:
    name = "BashExecutionResourceProvider"
    DEFAULT_SETTINGS = {}
    kind = "bash"

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
        _ = existing_handle
        from agently.builtins.actions import Cmd

        from ._boundary import materialize_task_workspace_boundary

        config = requirement.get("config", {})
        # Materialize the TaskWorkspace-issued file boundary in the provider context
        # before the executor runs; fail closed here if a supplied boundary cannot
        # be materialized, rather than inside the executor (spec section 8.6).
        boundary = materialize_task_workspace_boundary(
            [policy.get("task_workspace_roots"), config.get("allowed_workdir_roots")],
            label="bash execution resource",
        )
        resource = Cmd(
            allowed_cmd_prefixes=policy.get("allowed_cmd_prefixes", config.get("allowed_cmd_prefixes")),
            allowed_workdir_roots=[boundary] if boundary is not None else None,
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 20))),
            env=config.get("env", None),
            max_output_chars=int(policy.get("max_output_chars", config.get("max_output_chars", 20000))),
            output_artifact_dir=policy.get("output_artifact_dir", config.get("output_artifact_dir")),
        )
        return {
            "handle_id": f"bash:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {"provider": self.name},
        }

    async def async_health_check(self, handle: "ExecutionResourceHandle") -> "ExecutionResourceStatus":
        return "ready" if handle.get("resource") is not None else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        _ = handle
        return None
