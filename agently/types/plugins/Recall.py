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

from typing import Any, Protocol, runtime_checkable

from agently.types.data.workspace import WorkspaceContextPack, WorkspaceRecallPlan, WorkspaceRecordRef


@runtime_checkable
class RecallPlanner(Protocol):
    name: str

    async def plan(
        self,
        *,
        workspace: Any,
        goal: str,
        scope: dict[str, Any],
        budget: dict[str, Any],
        profile: str,
    ) -> WorkspaceRecallPlan: ...


@runtime_checkable
class Retriever(Protocol):
    name: str

    async def retrieve(
        self,
        *,
        workspace: Any,
        plan: WorkspaceRecallPlan,
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class ContextBuilder(Protocol):
    name: str

    async def build(
        self,
        *,
        workspace: Any,
        goal: str,
        profile: str,
        records: list[WorkspaceRecordRef],
        budget: dict[str, Any],
        diagnostics: dict[str, Any],
    ) -> WorkspaceContextPack: ...
