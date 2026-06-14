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


class WorkspaceLeaseRef(TypedDict, total=False):
    run_id: str
    owner_id: str
    lease_token: str
    lease_ttl: float
    lease_until: float
    claimed_at: str
    heartbeat_at: str
    released_at: str | None
    state_version: int | None


class WorkspaceScratchLease(TypedDict, total=False):
    """Durable record of a scratch lease.

    Scratch leases are persisted as Workspace facts so crashed runs can be
    recovered by TTL/startup cleanup and scope prune using lease records rather
    than filesystem heuristics such as mtime (spec sections 8.5 / 11.1).
    """

    lease_id: str
    scope: dict[str, Any]
    local_path: str | None
    mount: dict[str, Any] | None
    purpose: str | None
    cleanup_policy: Literal["on_close", "on_scope_prune", "ttl"]
    expires_at: str | None
    read_only: bool
    policy_labels: list[str]
    created_at: str
    closed_at: str | None


class WorkspaceFilePolicyMetadata(TypedDict):
    content_root: str
    files_root: str
    action_file_root: str | None
    allowed_roots: list[str]
    root_source: str
    path_normalization: str
    symlink_policy: str
    case_policy: str
    policy_labels: list[str]
    links: dict[str, str]


class WorkspaceRetentionAnchor(TypedDict):
    id: str
    execution_id: str
    anchor_type: str
    sequence: int | None
    record_ref: WorkspaceReferenceEnvelope | None
    summary_ref: WorkspaceReferenceEnvelope | None
    preserved_event_ids: list[str]
    created_at: str
    meta: dict[str, Any]


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
