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

from typing import Any, Literal
from typing_extensions import NotRequired, TypedDict


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


WorkspaceMode = Literal["read_only", "read_write"]


class WorkspaceBackendCapabilities(TypedDict):
    root: str
    mode: WorkspaceMode
    external_read: bool
    external_write: bool
    private_write: bool
    materialized_components: list[str]


class WorkspaceReferenceEnvelope(TypedDict):
    workspace_id: str
    kind: str
    collection: str
    record_id: str
    version: str | None
    content_ref: str | None
    digest: str | None
    size: int
    created_at: str
    policy_labels: list[str]
    backend_capabilities: dict[str, bool]


class WorkspaceContentSegment(TypedDict):
    ref: WorkspaceReferenceEnvelope
    content: str
    offset: int
    size: int
    total_size: int
    eof: bool
    digest: str | None
    content_type: str | None


WorkspaceFileOperation = Literal["read", "write", "export"]


class WorkspaceFileDiagnostic(TypedDict):
    code: str
    message: str
    handler_id: NotRequired[str | None]
    dependency: NotRequired[str | None]
    detail: NotRequired[dict[str, Any]]


class WorkspaceFileRef(TypedDict):
    path: str
    sha256: str
    type: NotRequired[Literal["file"]]
    workspace_id: NotRequired[str]
    execution_id: NotRequired[str | None]
    size: NotRequired[int]
    available: NotRequired[bool]
    bytes: NotRequired[int]
    media_type: NotRequired[str | None]
    content_kind: NotRequired[str]
    role: NotRequired[str]


class WorkspaceFileInfo(TypedDict):
    path: str
    extension: str
    media_type: str | None
    content_kind: str
    bytes: int
    sha256: str
    signatures: list[str]
    readable: bool
    writable: bool
    exists: bool


class WorkspaceFileReadResult(TypedDict):
    ok: bool
    readable: bool
    path: str
    content: str
    truncated: bool
    bytes: int
    offset: int
    read_bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    encoding: str | None
    handler_id: str
    extraction_method: str
    diagnostics: list[WorkspaceFileDiagnostic]
    file_refs: list[WorkspaceFileRef]
    attachments: NotRequired[list[dict[str, Any]]]


class WorkspaceFileSearchResult(TypedDict):
    path: str
    line: int
    text: str
    role: str
    content_state: str
    source: str
    query: str
    scope: dict[str, Any]
    locator_ref: dict[str, Any]
    snippet: str
    snippet_chars: int
    snippet_bytes: int
    truncated: bool
    line_start: int
    line_end: int
    bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    search_engine: str
    file_ref: WorkspaceFileRef


class WorkspaceFileWriteResult(TypedDict):
    ok: bool
    writable: bool
    path: str
    bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    encoding: str | None
    mode: str
    handler_id: str
    replacements: NotRequired[int]
    diagnostics: list[WorkspaceFileDiagnostic]
    file_refs: list[WorkspaceFileRef]


class WorkspaceFileExportResult(TypedDict):
    ok: bool
    exported: bool
    source_path: str
    output_path: str
    export_kind: str
    bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    handler_id: str
    diagnostics: list[WorkspaceFileDiagnostic]
    file_refs: list[WorkspaceFileRef]


class WorkspaceRuntimeEventRecord(TypedDict):
    id: str
    execution_id: str
    sequence: int
    event_id: str
    event_type: str
    state_version: int | None
    idempotency_key: str | None
    parent_id: str | None
    causation_id: str | None
    parent_signal_id: str | None
    node_id: str | None
    operator_id: str | None
    interrupt_id: str | None
    resume_request_id: str | None
    actor_id: str | None
    lease_owner_id: str | None
    aggregation_scope: str | None
    snapshot_ref: WorkspaceReferenceEnvelope | None
    exchange_id: str | None
    artifact_refs: list[WorkspaceReferenceEnvelope]
    event: dict[str, Any]
    created_at: str
    persisted_at: str | None


class WorkspaceLeaseRef(TypedDict):
    run_id: str
    owner_id: str
    lease_token: str
    lease_ttl: float
    lease_until: float
    claimed_at: str
    heartbeat_at: str
    released_at: str | None
    state_version: int | None


WorkspaceRetentionTerminalStatus = Literal["completed", "failed", "cancelled"]


class WorkspaceRetentionDiagnostic(TypedDict, total=False):
    code: str
    message: str
    retryable: bool
    entity: str
    detail: dict[str, Any]


class WorkspaceRetentionResult(TypedDict):
    status: Literal["applied", "deferred", "noop"]
    execution_id: str
    retained_refs: list[WorkspaceFileRef]
    retained_bytes: int
    deleted_bytes: int
    diagnostics: list[WorkspaceRetentionDiagnostic]


class WorkspaceSearchResult(TypedDict, total=False):
    ref: WorkspaceRecordRef
    score: float | None
    reason: str | None


class WorkspaceContextPlan(TypedDict):
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


class WorkspaceContextPackage(TypedDict):
    goal: str
    profile: str
    items: list[WorkspaceContextItem]
    omitted: list[WorkspaceContextOmission]
    diagnostics: dict[str, Any]
    skills_context_pack: NotRequired[dict[str, Any]]


WorkspaceRetrievalSource = Literal["record", "file"]
WorkspaceRetrievalSelection = Literal["length", "top_n"]
WorkspaceRetrievalMethod = Literal["auto", "keyword", "vector", "hybrid"]


class WorkspaceRetrievalItem(TypedDict, total=False):
    source: WorkspaceRetrievalSource
    candidate_id: str
    ref: WorkspaceRecordRef
    file: WorkspaceFileSearchResult
    kind: str | None
    summary: str
    content: str | None
    tags: list[str]
    score: float | None
    reason: str | None
    use: str
    chars: int
    body_state: str
    content_state: str
    original_ref: dict[str, Any]
    projection: dict[str, Any]
    raw_chars: int
    projected_chars: int
    truncated: bool


class WorkspaceRetrievalOmission(TypedDict):
    reason: str
    count: int


class WorkspaceRetrievalPackage(TypedDict):
    query: str | None
    profile: str
    selection: WorkspaceRetrievalSelection
    items: list[WorkspaceRetrievalItem]
    omitted: list[WorkspaceRetrievalOmission]
    diagnostics: dict[str, Any]
