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

from collections.abc import Sequence
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.workspace import (
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetainedReference,
    WorkspaceRetentionAnchor,
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionPreview,
    WorkspaceRetentionResult,
    WorkspaceRuntimeEventRecord,
    WorkspaceScratchLease,
)


@runtime_checkable
class ScratchLeaseStore(Protocol):
    """Durable scratch lease facts for crash-safe scratch recovery.

    Scratch leases must be persisted as Workspace facts so TTL/startup cleanup
    and scope prune can recover crashed runs from lease records rather than
    filesystem heuristics such as mtime (spec sections 8.5 / 11.1).
    """

    async def register_scratch_lease(self, lease: WorkspaceScratchLease) -> WorkspaceScratchLease: ...

    async def get_scratch_lease(self, lease_id: str) -> WorkspaceScratchLease | None: ...

    async def list_scratch_leases(
        self,
        *,
        include_closed: bool = False,
        expired_before: str | None = None,
    ) -> list[WorkspaceScratchLease]: ...

    async def close_scratch_lease(
        self,
        lease_id: str,
        *,
        closed_at: str | None = None,
    ) -> WorkspaceScratchLease | None: ...


@runtime_checkable
class ContentStore(Protocol):
    async def write_content(self, relative_path: str, content: bytes) -> str: ...

    async def read_content(self, path: str) -> Any: ...

    async def read_content_segment(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment: ...

    def stream_content(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]: ...


@runtime_checkable
class MetadataStore(Protocol):
    async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef: ...

    async def get_record(self, record_id: str) -> WorkspaceRecordRef | None: ...


@runtime_checkable
class CheckpointStore(Protocol):
    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef: ...


@runtime_checkable
class DurableCheckpointStore(CheckpointStore, Protocol):
    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef: ...

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef: ...

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef: ...

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef: ...

    def capabilities(self) -> WorkspaceBackendCapabilities: ...


@runtime_checkable
class ExecutionSnapshotStore(Protocol):
    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None: ...

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...


@runtime_checkable
class RuntimeEventStore(Protocol):
    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        *,
        sequence: int | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        snapshot_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        exchange_id: str | None = None,
        state_version: int | None = None,
        parent_id: str | None = None,
        causation_id: str | None = None,
        parent_signal_id: str | None = None,
        node_id: str | None = None,
        operator_id: str | None = None,
        interrupt_id: str | None = None,
        resume_request_id: str | None = None,
        actor_id: str | None = None,
        lease_owner_id: str | None = None,
        aggregation_scope: str | None = None,
    ) -> WorkspaceRuntimeEventRecord: ...

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]: ...


@runtime_checkable
class RefResolver(Protocol):
    async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> WorkspaceReferenceEnvelope: ...

    async def read_bounded(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment: ...

    def stream_read(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]: ...


@runtime_checkable
class RetentionPolicy(Protocol):
    async def add_retention_anchor(
        self,
        execution_id: str,
        *,
        anchor_type: str,
        sequence: int | None = None,
        record_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        summary_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        preserved_event_ids: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionAnchor: ...

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]: ...

    async def inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: WorkspaceRetentionLifecycle,
        retained_refs: Sequence[WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: WorkspaceRetentionPolicy | None = None,
    ) -> WorkspaceRetentionPreview: ...

    async def apply_retention(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult: ...


@runtime_checkable
class ScopePruner(Protocol):
    async def prune_scope(
        self,
        scope: dict[str, Any],
        *,
        remove_files: bool = True,
    ) -> dict[str, Any]: ...


@runtime_checkable
class EvidenceLinker(Protocol):
    async def link_evidence(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        *,
        execution_id: str | None = None,
        operation_id: str | None = None,
        runtime_event_id: str | None = None,
        checkpoint_id: str | None = None,
        exchange_id: str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef: ...


@runtime_checkable
class TextIndex(Protocol):
    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class VectorIndex(Protocol):
    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class DBStoreProvider(Protocol):
    """Workspace record database provider.

    Implementations normalize record, search, link, checkpoint, runtime-event,
    retention, and scratch-lease behavior behind one adapter surface. Providers
    that cannot support an advanced capability should return the empty/absent
    value for that method instead of exposing a different interface.
    """

    name: str

    async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef: ...

    async def get_record(self, record_id: str) -> WorkspaceRecordRef | None: ...

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]: ...

    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef: ...

    async def link_evidence(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        *,
        execution_id: str | None = None,
        operation_id: str | None = None,
        runtime_event_id: str | None = None,
        checkpoint_id: str | None = None,
        exchange_id: str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef: ...

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]: ...

    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...

    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef: ...

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef: ...

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef: ...

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef: ...

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None: ...

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]: ...

    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        *,
        sequence: int | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        snapshot_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        exchange_id: str | None = None,
        state_version: int | None = None,
        parent_id: str | None = None,
        causation_id: str | None = None,
        parent_signal_id: str | None = None,
        node_id: str | None = None,
        operator_id: str | None = None,
        interrupt_id: str | None = None,
        resume_request_id: str | None = None,
        actor_id: str | None = None,
        lease_owner_id: str | None = None,
        aggregation_scope: str | None = None,
    ) -> WorkspaceRuntimeEventRecord: ...

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]: ...

    async def record_file_policy(
        self,
        metadata: WorkspaceFilePolicyMetadata,
    ) -> WorkspaceFilePolicyMetadata: ...

    async def get_file_policy(self) -> WorkspaceFilePolicyMetadata: ...

    async def add_retention_anchor(
        self,
        execution_id: str,
        *,
        anchor_type: str,
        sequence: int | None = None,
        record_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        summary_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        preserved_event_ids: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionAnchor: ...

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]: ...

    async def inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: WorkspaceRetentionLifecycle,
        retained_refs: Sequence[WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: WorkspaceRetentionPolicy | None = None,
    ) -> WorkspaceRetentionPreview: ...

    async def apply_retention(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult: ...

    async def prune_scope(
        self,
        scope: dict[str, Any],
        *,
        remove_files: bool = True,
    ) -> dict[str, Any]: ...

    async def register_scratch_lease(self, lease: WorkspaceScratchLease) -> WorkspaceScratchLease: ...

    async def get_scratch_lease(self, lease_id: str) -> WorkspaceScratchLease | None: ...

    async def list_scratch_leases(
        self,
        *,
        include_closed: bool = False,
        expired_before: str | None = None,
    ) -> list[WorkspaceScratchLease]: ...

    async def close_scratch_lease(
        self,
        lease_id: str,
        *,
        closed_at: str | None = None,
    ) -> WorkspaceScratchLease | None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStoreProvider(Protocol):
    name: str

    async def index_record(self, ref: WorkspaceRecordRef, embedding: list[float]) -> None: ...

    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class WorkspaceProviderFactory(Protocol):
    def __call__(
        self,
        *,
        root: Any | None = None,
        mode: str = "read_write",
        **options: Any,
    ) -> Any: ...


@runtime_checkable
class PolicyEngine(Protocol):
    def ensure_writable(self) -> None: ...

    def resolve_content_path(self, path: str) -> Any: ...

    async def filter_records(
        self,
        records: list[WorkspaceRecordRef],
        *,
        purpose: str = "prompt",
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class IngestionProfile(Protocol):
    name: str

    async def ingest(
        self,
        *,
        workspace: Any,
        content: Any,
        collection: str,
        kind: str | None,
        scope: dict[str, Any],
        source: dict[str, Any],
        summary: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef: ...


@runtime_checkable
class WorkspaceBackend(Protocol):
    @property
    def root(self) -> Any: ...

    @property
    def content_root(self) -> Any: ...

    @property
    def files_root(self) -> Any: ...

    @property
    def content(self) -> ContentStore: ...

    @property
    def metadata(self) -> MetadataStore: ...

    @property
    def checkpoint_store(self) -> CheckpointStore: ...

    @property
    def runtime_event_store(self) -> RuntimeEventStore: ...

    @property
    def ref_resolver(self) -> RefResolver: ...

    @property
    def retention_policy(self) -> RetentionPolicy: ...

    @property
    def evidence_linker(self) -> EvidenceLinker: ...

    @property
    def text_index(self) -> TextIndex: ...

    @property
    def policy(self) -> PolicyEngine: ...

    @property
    def vector_index(self) -> VectorIndex | None: ...

    async def put(
        self,
        content: Any,
        *,
        collection: str,
        kind: str | None = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef: ...

    async def get(self, ref_or_path: WorkspaceRecordRef | str) -> Any: ...

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any: ...

    async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> WorkspaceReferenceEnvelope: ...

    async def read_bounded(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment: ...

    def stream_read(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]: ...

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]: ...

    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef: ...

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]: ...

    async def link_evidence(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        *,
        execution_id: str | None = None,
        operation_id: str | None = None,
        runtime_event_id: str | None = None,
        checkpoint_id: str | None = None,
        exchange_id: str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef: ...

    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef: ...

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...

    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef: ...

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None: ...

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]: ...

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef: ...

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef: ...

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef: ...

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef: ...

    async def register_scratch_lease(self, lease: WorkspaceScratchLease) -> WorkspaceScratchLease: ...

    async def get_scratch_lease(self, lease_id: str) -> WorkspaceScratchLease | None: ...

    async def list_scratch_leases(
        self,
        *,
        include_closed: bool = False,
        expired_before: str | None = None,
    ) -> list[WorkspaceScratchLease]: ...

    async def close_scratch_lease(
        self,
        lease_id: str,
        *,
        closed_at: str | None = None,
    ) -> WorkspaceScratchLease | None: ...

    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        *,
        sequence: int | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        snapshot_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        exchange_id: str | None = None,
        state_version: int | None = None,
        parent_id: str | None = None,
        causation_id: str | None = None,
        parent_signal_id: str | None = None,
        node_id: str | None = None,
        operator_id: str | None = None,
        interrupt_id: str | None = None,
        resume_request_id: str | None = None,
        actor_id: str | None = None,
        lease_owner_id: str | None = None,
        aggregation_scope: str | None = None,
    ) -> WorkspaceRuntimeEventRecord: ...

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]: ...

    async def record_file_policy(
        self,
        *,
        action_file_root: str | None = None,
        allowed_roots: list[str] | None = None,
        root_source: str = "workspace",
        path_normalization: str = "resolve",
        symlink_policy: str = "resolved_within_root",
        case_policy: str = "platform_default",
        policy_labels: list[str] | None = None,
        links: dict[str, str] | None = None,
    ) -> WorkspaceFilePolicyMetadata: ...

    async def get_file_policy(self) -> WorkspaceFilePolicyMetadata: ...

    async def add_retention_anchor(
        self,
        execution_id: str,
        *,
        anchor_type: str,
        sequence: int | None = None,
        record_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        summary_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        preserved_event_ids: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionAnchor: ...

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]: ...

    async def inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: WorkspaceRetentionLifecycle,
        retained_refs: Sequence[WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: WorkspaceRetentionPolicy | None = None,
    ) -> WorkspaceRetentionPreview: ...

    async def apply_retention(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult: ...

    def capabilities(self) -> WorkspaceBackendCapabilities: ...


@runtime_checkable
class WorkspaceBackendProvider(Protocol):
    def __call__(
        self,
        *,
        root: Any | None = None,
        create: bool = True,
        mode: str = "read_write",
        **options: Any,
    ) -> WorkspaceBackend: ...
