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

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agently.types.data import (
        CodeExecutionBundle,
        CodeExecutionResult,
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceProviderProbe,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
        TaskWorkspaceAccessGrant,
        TaskWorkspaceExecutionManifest,
    )


@runtime_checkable
class CodeExecutionResource(Protocol):
    async def async_execute_code(
        self,
        *,
        bundle: "CodeExecutionBundle",
        manifest: "TaskWorkspaceExecutionManifest",
        grant: "TaskWorkspaceAccessGrant",
        timeout: int,
    ) -> "CodeExecutionResult": ...


@runtime_checkable
class ExecutionResourceProvider(Protocol):
    """Runtime contract for a directly registered resource provider.

    Plugin lifecycle hooks are intentionally not part of this protocol. A
    provider loaded through PluginManager must satisfy the plugin contract at
    that boundary, while ``ExecutionResourceManager.register_provider`` only
    requires the runtime behavior declared here.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def supported_kinds(self) -> tuple[str, ...]: ...

    async def async_probe(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
    ) -> "ExecutionResourceProviderProbe": ...

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle": ...

    async def async_health_check(
        self,
        handle: "ExecutionResourceHandle",
    ) -> "ExecutionResourceStatus": ...

    async def async_release(
        self,
        handle: "ExecutionResourceHandle",
    ) -> None: ...
