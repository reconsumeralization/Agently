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

from typing import Any
from typing_extensions import TypedDict


class WorkspaceRecordRef(TypedDict):
    id: str
    collection: str
    kind: str | None
    path: str | None
    sha256: str | None
    size: int
    summary: str
    scope: dict[str, Any]
    source: dict[str, Any]
    created_at: str
    meta: dict[str, Any]


class WorkspaceLinkRef(TypedDict):
    id: str
    source_id: str
    target_id: str
    relation: str
    created_at: str
    meta: dict[str, Any]


class WorkspaceBackendCapabilities(TypedDict):
    backend: str
    root: str
    content_root: str
    files_root: str
    read_only: bool
    components: dict[str, str | None]
    features: dict[str, bool]


class WorkspaceSearchResult(TypedDict, total=False):
    ref: WorkspaceRecordRef
    score: float | None
    reason: str | None


class WorkspaceRecallPlan(TypedDict):
    goal: str
    profile: str
    queries: list[str]
    filters: dict[str, Any]
    scope: dict[str, Any]
    budget: dict[str, Any]
    diagnostics: dict[str, Any]


class WorkspaceContextItem(TypedDict):
    ref: WorkspaceRecordRef
    kind: str | None
    summary: str
    content: str | None
    use: str


class WorkspaceContextOmission(TypedDict):
    reason: str
    count: int


class WorkspaceContextPack(TypedDict):
    goal: str
    profile: str
    items: list[WorkspaceContextItem]
    omitted: list[WorkspaceContextOmission]
    diagnostics: dict[str, Any]
