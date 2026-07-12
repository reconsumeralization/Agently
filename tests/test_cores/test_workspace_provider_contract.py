from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import inspect
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any, Literal, cast, get_args

import pytest
from typing_extensions import get_type_hints

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.core import LocalWorkspaceBackend, PluginManager, WorkspaceManager
from agently.core.Workspace.Retention import (
    canonical_retention_fingerprint,
    empty_retention_selection,
)
import agently.types.data as data_types
import agently.types.data.workspace as workspace_types
from agently.types.data import (
    AgentlyRequestData,
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetentionAnchor,
    WorkspaceRuntimeEventRecord,
    WorkspaceScratchLease,
)
from agently.utils import DataFormatter, Settings


workspace_plugin_types = importlib.import_module("agently.types.plugins.Workspace")


class ProviderProofRequester:
    name = "ProviderProofRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://workspace-provider-proof",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        yield "message", json.dumps({"answer": "remote-provider-proof", "status": "ready"})

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


def _create_agent(name: str):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", ProviderProofRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


class RemoteAuditWorkspaceBackend:
    """Protocol-level non-local provider proof used only by tests."""

    def __init__(self, tenant_id: str = "tenant-alpha"):
        self.workspace_id = f"remote_audit_{ tenant_id }"
        self.root = f"remote-audit://{ tenant_id }"
        self.content_root = f"{ self.root }/content"
        self.files_root = f"{ self.root }/files"
        self.content = self
        self.metadata = self
        self.checkpoint_store = self
        self.runtime_event_store = self
        self.ref_resolver = self
        self.retention_policy = self
        self.evidence_linker = self
        self.text_index = self
        self.policy = self
        self.vector_index = None
        self.operations: list[str] = []
        self._records: dict[str, WorkspaceRecordRef] = {}
        self._record_data: dict[str, Any] = {}
        self._record_text: dict[str, str] = {}
        self._content_blobs: dict[str, str] = {}
        self._path_to_id: dict[str, str] = {}
        self._links: list[WorkspaceLinkRef] = []
        self._checkpoints: dict[str, list[WorkspaceRecordRef]] = {}
        self._checkpoint_states: dict[str, list[dict[str, Any]]] = {}
        self._leases: dict[str, WorkspaceLeaseRef] = {}
        self._scratch_leases: dict[str, WorkspaceScratchLease] = {}
        self._runtime_events: dict[str, list[WorkspaceRuntimeEventRecord]] = {}
        self._runtime_event_idempotency: dict[tuple[str, str], WorkspaceRuntimeEventRecord] = {}
        self._retention_anchors: list[WorkspaceRetentionAnchor] = []
        self._file_policy: WorkspaceFilePolicyMetadata = {
            "content_root": self.content_root,
            "files_root": self.files_root,
            "action_file_root": None,
            "allowed_roots": [self.files_root],
            "root_source": "remote_provider",
            "path_normalization": "provider_private",
            "symlink_policy": "provider_private",
            "case_policy": "provider_private",
            "policy_labels": [],
            "links": {},
        }

    def _now(self) -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    def _next_id(self, prefix: str, collection: str | None = None) -> str:
        if prefix == "rec":
            return f"remote_rec_{ len(self._records) + 1 }"
        if prefix == "link":
            return f"remote_link_{ len(self._links) + 1 }"
        if prefix == "anchor":
            return f"remote_anchor_{ len(self._retention_anchors) + 1 }"
        if prefix == "scratch":
            return f"remote_scratch_{ len(self._scratch_leases) + 1 }"
        events = self._runtime_events.get(str(collection or "default"), [])
        return f"remote_event_{ len(events) + 1 }"

    def _serialize(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(DataFormatter.sanitize(value), sort_keys=True)

    def _record_id(self, value: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str) -> str:
        if isinstance(value, dict):
            if "id" in value:
                return str(value["id"])
            if "record_id" in value:
                return str(value["record_id"])
        if value in self._records:
            return str(value)
        if value in self._path_to_id:
            return self._path_to_id[value]
        raise FileNotFoundError(f"Remote audit record not found: { value }")

    def _optional_envelope(
        self,
        value: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None,
    ) -> WorkspaceReferenceEnvelope | None:
        if value is None:
            return None
        if isinstance(value, dict) and "record_id" in value:
            return value
        return self._envelope(self._record_id(value))

    @staticmethod
    def _checkpoint_state_version(state: Any) -> int | None:
        if not isinstance(state, dict):
            return None
        value = state.get("state_version")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _latest_checkpoint_state_version(self, run_id: str) -> int | None:
        states = self._checkpoint_states.get(run_id, [])
        if not states:
            return 0
        return self._checkpoint_state_version(states[-1])

    def _ensure_expected_checkpoint_state_version(
        self,
        run_id: str,
        expected_state_version: int | None,
    ):
        if expected_state_version is None:
            return
        current_state_version = self._latest_checkpoint_state_version(run_id)
        if current_state_version != expected_state_version:
            raise RuntimeError(
                f"Workspace checkpoint state version conflict for run '{ run_id }': "
                f"expected { expected_state_version }, current state version is { current_state_version }."
            )

    def _require_active_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        lease = self._leases.get(run_id)
        now = time.time()
        if lease is None or lease.get("released_at") is not None:
            raise RuntimeError(f"Workspace lease for run '{ run_id }' is not active.")
        if float(lease.get("lease_until") or 0) <= now:
            raise RuntimeError(f"Workspace lease for run '{ run_id }' has expired.")
        if lease.get("owner_id") != owner_id or lease.get("lease_token") != lease_token:
            raise RuntimeError(f"Workspace lease conflict for run '{ run_id }'.")
        return lease

    def _envelope(self, record_id: str) -> WorkspaceReferenceEnvelope:
        ref = self._records[record_id]
        return {
            "workspace_id": self.workspace_id,
            "kind": str(ref.get("kind") or ref["collection"]),
            "collection": ref["collection"],
            "record_id": record_id,
            "version": None,
            "content_ref": ref["path"],
            "digest": ref["sha256"],
            "size": ref["size"],
            "created_at": ref["created_at"],
            "policy_labels": list(ref.get("meta", {}).get("policy_labels", [])),
            "backend_capabilities": self.capabilities()["features"],
        }

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
    ) -> WorkspaceRecordRef:
        self.operations.append("put")
        record_id = self._next_id("rec")
        text = self._serialize(content)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        path = f"{ self.root }/{ collection }/{ record_id }"
        ref: WorkspaceRecordRef = {
            "id": record_id,
            "collection": collection,
            "kind": kind,
            "path": path,
            "sha256": digest,
            "size": len(text.encode("utf-8")),
            "summary": summary or f"{ collection } { record_id }",
            "scope": DataFormatter.sanitize(scope or {}),
            "source": DataFormatter.sanitize(source or {"type": "remote_audit_provider"}),
            "created_at": self._now(),
            "meta": DataFormatter.sanitize(meta or {}),
        }
        self._records[record_id] = ref
        self._record_data[record_id] = DataFormatter.sanitize(content)
        self._record_text[record_id] = text
        self._path_to_id[path] = record_id
        return ref

    async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef:
        self._records[ref["id"]] = ref
        self._record_data[ref["id"]] = ref
        self._record_text[ref["id"]] = self._serialize(ref)
        return ref

    async def get_record(self, record_id: str) -> WorkspaceRecordRef | None:
        return self._records.get(record_id)

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        self._record_text[ref["id"]] = content

    async def get(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        return self._record_text[self._record_id(ref_or_path)]

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        return self._record_data[self._record_id(ref_or_path)]

    async def write_content(self, relative_path: str, content: bytes) -> str:
        path = f"{ self.content_root }/{ relative_path.lstrip('/') }"
        self._content_blobs[path] = content.decode("utf-8")
        return path

    async def read_content(self, path: str) -> Any:
        if path in self._path_to_id:
            return await self.get(path)
        return self._content_blobs[path]

    async def read_content_segment(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        if path in self._path_to_id:
            return await self.read_bounded(path, offset=offset, limit=limit)
        content = self._content_blobs[path]
        end = None if limit is None else offset + limit
        segment = content[offset:end]
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {
            "ref": {
                "workspace_id": self.workspace_id,
                "kind": "content",
                "collection": "content",
                "record_id": path,
                "version": None,
                "content_ref": path,
                "digest": digest,
                "size": len(content),
                "created_at": self._now(),
                "policy_labels": [],
                "backend_capabilities": self.capabilities()["features"],
            },
            "content": segment,
            "offset": offset,
            "size": len(segment),
            "total_size": len(content),
            "eof": offset + len(segment) >= len(content),
            "digest": digest,
            "content_type": "text/plain",
        }

    async def stream_content(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        consumed = 0
        while limit is None or consumed < limit:
            current_limit = chunk_size if limit is None else min(chunk_size, limit - consumed)
            if current_limit <= 0:
                break
            segment = await self.read_content_segment(path, offset=offset + consumed, limit=current_limit)
            if not segment["content"]:
                break
            consumed += segment["size"]
            yield segment
            if segment["eof"]:
                break

    async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> WorkspaceReferenceEnvelope:
        return self._envelope(self._record_id(ref_or_id))

    async def read_bounded(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        record_id = self._record_id(ref_or_path)
        content = self._record_text[record_id]
        end = None if limit is None else offset + limit
        segment = content[offset:end]
        return {
            "ref": self._envelope(record_id),
            "content": segment,
            "offset": offset,
            "size": len(segment),
            "total_size": len(content),
            "eof": offset + len(segment) >= len(content),
            "digest": self._records[record_id]["sha256"],
            "content_type": "application/json",
        }

    async def stream_read(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        consumed = 0
        while limit is None or consumed < limit:
            current_limit = chunk_size if limit is None else min(chunk_size, limit - consumed)
            if current_limit <= 0:
                break
            segment = await self.read_bounded(ref_or_path, offset=offset + consumed, limit=current_limit)
            if not segment["content"]:
                break
            consumed += segment["size"]
            yield segment
            if segment["eof"]:
                break

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]:
        results = []
        for record_id, ref in self._records.items():
            if filters and any(ref.get(key) != value for key, value in filters.items()):
                continue
            text = self._record_text[record_id]
            if query is None or query in text or query in ref["summary"]:
                results.append(ref)
        return results

    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef:
        self.operations.append("link")
        link: WorkspaceLinkRef = {
            "id": self._next_id("link"),
            "source_id": self._record_id(source),
            "target_id": self._record_id(target),
            "relation": relation,
            "created_at": self._now(),
            "meta": DataFormatter.sanitize(meta or {}),
        }
        self._links.append(link)
        return link

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]:
        ref_id = self._record_id(ref_or_id) if ref_or_id is not None else None
        source_id = self._record_id(source) if source is not None else None
        target_id = self._record_id(target) if target is not None else None
        links = []
        for link in self._links:
            if ref_id is not None and ref_id not in {link["source_id"], link["target_id"]}:
                continue
            if source_id is not None and link["source_id"] != source_id:
                continue
            if target_id is not None and link["target_id"] != target_id:
                continue
            if relation is not None and link["relation"] != relation:
                continue
            links.append(link)
        return links

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
    ) -> WorkspaceLinkRef:
        self.operations.append("link_evidence")
        evidence = {
            "execution_id": execution_id,
            "operation_id": operation_id,
            "runtime_event_id": runtime_event_id,
            "checkpoint_id": checkpoint_id,
            "exchange_id": exchange_id,
            "artifact_refs": [self._optional_envelope(item) for item in artifact_refs or []],
        }
        link_meta = DataFormatter.sanitize(meta or {})
        link_meta["evidence"] = evidence
        return await self.link(source, target, relation, meta=link_meta)

    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        self._ensure_expected_checkpoint_state_version(run_id, expected_state_version)
        ref = await self.put(
            state,
            collection="checkpoints",
            kind="checkpoint",
            summary=f"Remote checkpoint for { run_id }",
            scope={"run_id": run_id, **({"step_id": step_id} if step_id else {})},
            source={"type": "remote_audit_provider", "name": "checkpoint"},
            meta={"checkpoint": True},
        )
        self._checkpoints.setdefault(run_id, []).append(ref)
        self._checkpoint_states.setdefault(run_id, []).append(state)
        return ref

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        self.operations.append("put_checkpoint")
        return await self.checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )

    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        self.operations.append("put_snapshot")
        return await self.put_checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        items = self._checkpoint_states.get(run_id, [])
        return items[-1] if items else None

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        items = self._checkpoints.get(run_id, [])
        return items[-1] if items else None

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        items = [
            item for item in reversed(self._checkpoints.get(run_id, []))
            if step_id is None or item["scope"].get("step_id") == step_id
        ]
        return items[:limit] if limit is not None else items

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        metadata = dict(metadata or {})
        scope = metadata.pop("scope", {})
        if not isinstance(scope, dict):
            scope = {}
        return await self.put(
            artifact,
            collection="artifacts",
            kind=str(metadata.pop("kind", "runtime_artifact")),
            summary=str(metadata.pop("summary", f"Artifact for { run_id }")),
            scope={"run_id": run_id, **scope},
            source={"type": "remote_audit_provider", "name": "artifact_ref"},
            meta={"artifact_ref": True, **metadata},
        )

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef:
        self._ensure_expected_checkpoint_state_version(run_id, expected_state_version)
        now = time.time()
        current = self._leases.get(run_id)
        if (
            current is not None
            and current.get("released_at") is None
            and float(current.get("lease_until") or 0) > now
            and current.get("owner_id") != owner_id
        ):
            raise RuntimeError(f"Workspace lease conflict for run '{ run_id }'.")
        lease: WorkspaceLeaseRef = {
            "run_id": run_id,
            "owner_id": owner_id,
            "lease_token": self._next_id("lease", run_id),
            "lease_ttl": float(ttl),
            "lease_until": now + float(ttl),
            "claimed_at": self._now(),
            "heartbeat_at": self._now(),
            "released_at": None,
            "state_version": self._latest_checkpoint_state_version(run_id),
        }
        self._leases[run_id] = lease
        return lease

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        lease = cast(WorkspaceLeaseRef, dict(self._require_active_lease(run_id, owner_id, lease_token)))
        lease["heartbeat_at"] = self._now()
        lease["lease_until"] = time.time() + float(lease.get("lease_ttl") or 0)
        self._leases[run_id] = lease
        return lease

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        lease = cast(WorkspaceLeaseRef, dict(self._require_active_lease(run_id, owner_id, lease_token)))
        lease["released_at"] = self._now()
        lease["lease_until"] = time.time()
        self._leases[run_id] = lease
        return lease

    async def register_scratch_lease(self, lease: WorkspaceScratchLease) -> WorkspaceScratchLease:
        lease_id = str(lease.get("lease_id") or self._next_id("scratch"))
        record: WorkspaceScratchLease = {
            "lease_id": lease_id,
            "scope": dict(lease.get("scope") or {}),
            "local_path": lease.get("local_path"),
            "mount": lease.get("mount"),
            "purpose": lease.get("purpose"),
            "cleanup_policy": lease.get("cleanup_policy") or "on_close",
            "expires_at": lease.get("expires_at"),
            "read_only": bool(lease.get("read_only", False)),
            "policy_labels": list(lease.get("policy_labels") or []),
            "created_at": lease.get("created_at") or self._now(),
            "closed_at": lease.get("closed_at"),
        }
        self._scratch_leases[lease_id] = record
        self.operations.append("register_scratch_lease")
        return record

    async def get_scratch_lease(self, lease_id: str) -> WorkspaceScratchLease | None:
        self.operations.append("get_scratch_lease")
        return self._scratch_leases.get(lease_id)

    async def list_scratch_leases(
        self,
        *,
        include_closed: bool = False,
        expired_before: str | None = None,
    ) -> list[WorkspaceScratchLease]:
        self.operations.append("list_scratch_leases")
        leases = list(self._scratch_leases.values())
        if not include_closed:
            leases = [lease for lease in leases if lease.get("closed_at") is None]
        if expired_before is not None:
            leases = [
                lease
                for lease in leases
                if lease.get("expires_at") is not None and str(lease.get("expires_at")) <= expired_before
            ]
        return leases

    async def close_scratch_lease(
        self,
        lease_id: str,
        *,
        closed_at: str | None = None,
    ) -> WorkspaceScratchLease | None:
        self.operations.append("close_scratch_lease")
        lease = self._scratch_leases.get(lease_id)
        if lease is None:
            return None
        if lease.get("closed_at") is None:
            lease = cast(WorkspaceScratchLease, {**lease, "closed_at": closed_at or self._now()})
            self._scratch_leases[lease_id] = lease
        return lease

    async def append_runtime_event(
        self,
        execution_id: str,
        event: Any,
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
    ) -> WorkspaceRuntimeEventRecord:
        self.operations.append("append_runtime_event")
        if idempotency_key is not None and (execution_id, idempotency_key) in self._runtime_event_idempotency:
            return self._runtime_event_idempotency[(execution_id, idempotency_key)]
        event_data = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
        event_data = DataFormatter.sanitize(event_data)
        meta = event_data.get("meta") if isinstance(event_data.get("meta"), dict) else {}
        records = self._runtime_events.setdefault(execution_id, [])
        if expected_sequence is not None and int(expected_sequence) != len(records) + 1:
            raise RuntimeError(
                f"Workspace runtime event sequence conflict for execution '{ execution_id }': "
                f"expected { expected_sequence }, next sequence is { len(records) + 1 }."
            )
        resolved_sequence = sequence if sequence is not None else len(records) + 1
        artifact_ref_envelopes = [
            envelope
            for item in artifact_refs or []
            if (envelope := self._optional_envelope(item)) is not None
        ]
        record: WorkspaceRuntimeEventRecord = {
            "id": self._next_id("event", execution_id),
            "execution_id": execution_id,
            "sequence": resolved_sequence,
            "event_id": str(event_data.get("event_id") or self._next_id("event", execution_id)),
            "event_type": str(event_data.get("event_type") or "runtime.event"),
            "state_version": state_version,
            "idempotency_key": idempotency_key,
            "parent_id": parent_id or meta.get("parent_event_id") or meta.get("parent_id"),
            "causation_id": causation_id or meta.get("causation_id"),
            "parent_signal_id": parent_signal_id or meta.get("parent_signal_id"),
            "node_id": node_id or meta.get("node_id"),
            "operator_id": operator_id or meta.get("operator_id"),
            "interrupt_id": interrupt_id or meta.get("interrupt_id"),
            "resume_request_id": resume_request_id or meta.get("resume_request_id"),
            "actor_id": actor_id or meta.get("actor_id"),
            "lease_owner_id": lease_owner_id or meta.get("lease_owner_id"),
            "aggregation_scope": aggregation_scope or meta.get("aggregation_scope"),
            "snapshot_ref": self._optional_envelope(snapshot_ref),
            "exchange_id": exchange_id,
            "artifact_refs": artifact_ref_envelopes,
            "event": event_data,
            "created_at": self._now(),
            "persisted_at": self._now(),
        }
        records.append(record)
        if idempotency_key is not None:
            self._runtime_event_idempotency[(execution_id, idempotency_key)] = record
        return record

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]:
        records = list(self._runtime_events.get(execution_id, []))
        if sequence_from is not None:
            records = [item for item in records if item["sequence"] >= sequence_from]
        if sequence_to is not None:
            records = [item for item in records if item["sequence"] <= sequence_to]
        if event_id is not None:
            records = [item for item in records if item["event_id"] == event_id]
        return records[:limit] if limit is not None else records

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
    ) -> WorkspaceFilePolicyMetadata:
        self._file_policy = {
            "content_root": self.content_root,
            "files_root": self.files_root,
            "action_file_root": action_file_root,
            "allowed_roots": allowed_roots or [self.files_root],
            "root_source": root_source,
            "path_normalization": path_normalization,
            "symlink_policy": symlink_policy,
            "case_policy": case_policy,
            "policy_labels": policy_labels or [],
            "links": links or {},
        }
        return self._file_policy

    async def get_file_policy(self) -> WorkspaceFilePolicyMetadata:
        return self._file_policy

    def ensure_writable(self) -> None:
        return None

    def resolve_content_path(self, path: str) -> Any:
        return path

    async def filter_records(
        self,
        records: list[WorkspaceRecordRef],
        *,
        purpose: str = "prompt",
    ) -> list[WorkspaceRecordRef]:
        return records

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
    ) -> WorkspaceRetentionAnchor:
        anchor: WorkspaceRetentionAnchor = {
            "id": self._next_id("anchor"),
            "execution_id": execution_id,
            "anchor_type": anchor_type,
            "sequence": sequence,
            "record_ref": self._optional_envelope(record_ref),
            "summary_ref": self._optional_envelope(summary_ref),
            "preserved_event_ids": preserved_event_ids or [],
            "created_at": self._now(),
            "meta": DataFormatter.sanitize(meta or {}),
        }
        self._retention_anchors.append(anchor)
        return anchor

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]:
        anchors = [
            item for item in self._retention_anchors
            if item["execution_id"] == execution_id and (anchor_type is None or item["anchor_type"] == anchor_type)
        ]
        return anchors[:limit] if limit is not None else anchors

    async def get_retention_lifecycle(
        self,
        execution_id: str,
        *,
        status: workspace_types.WorkspaceRetentionTerminalStatus,
        terminal_at: str,
    ) -> workspace_types.WorkspaceRetentionLifecycle:
        self.operations.append("get_retention_lifecycle")
        states = self._checkpoint_states.get(execution_id, [])
        state = states[-1] if states else {}
        interrupts = state.get("interrupts") if isinstance(state, dict) else None
        recovery_active = bool(
            isinstance(interrupts, dict)
            and any(
                isinstance(item, dict) and item.get("status") == "waiting"
                for item in interrupts.values()
            )
        )
        intervention = state.get("intervention") if isinstance(state, dict) else None
        intervention_ledger = (
            intervention.get("ledger") if isinstance(intervention, dict) else None
        )
        recovery_active = recovery_active or bool(
            isinstance(intervention_ledger, dict)
            and any(
                isinstance(item, dict) and item.get("status") == "pending"
                for item in intervention_ledger.values()
            )
        )
        lease = self._leases.get(execution_id)
        return {
            "execution_id": execution_id,
            "status": status,
            "terminal_at": terminal_at,
            "state_version": self._latest_checkpoint_state_version(execution_id),
            "recovery_active": recovery_active,
            "lease_active": bool(
                lease is not None
                and lease.get("released_at") is None
                and float(lease.get("lease_until") or 0) > time.time()
            ),
        }

    async def inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: workspace_types.WorkspaceRetentionLifecycle,
        retained_refs: Sequence[workspace_types.WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: workspace_types.WorkspaceRetentionPolicy | None = None,
    ) -> workspace_types.WorkspaceRetentionPreview:
        raise NotImplementedError

    async def apply_retention(
        self,
        preview: workspace_types.WorkspaceRetentionPreview,
    ) -> workspace_types.WorkspaceRetentionResult:
        raise NotImplementedError

    def capabilities(self) -> WorkspaceBackendCapabilities:
        component = type(self).__name__
        return {
            "backend": component,
            "root": self.root,
            "content_root": self.content_root,
            "files_root": self.files_root,
            "read_only": False,
            "components": {
                "content": component,
                "metadata": component,
                "checkpoint_store": component,
                "runtime_event_store": component,
                "ref_resolver": component,
                "retention_policy": component,
                "evidence_linker": component,
                "text_index": component,
                "vector_index": None,
            },
            "features": {
                "structured_records": True,
                "checkpoint_lookup": True,
                "links": True,
                "file_policy_metadata": True,
                "evidence_links": True,
                "supports_cas": True,
                "supports_lease": True,
                "supports_artifact_refs": True,
                "supports_event_sequence": True,
                "supports_range_read": True,
                "supports_stream_read": True,
                "supports_retention": True,
                "supports_compaction_anchor": True,
                "supports_remote_backend": True,
            },
        }


def test_workspace_retention_data_contract_is_exported_with_exact_shapes():
    expected_exports = (
        "WorkspaceRetentionCategory",
        "WorkspaceRetentionRepresentation",
        "WorkspaceRetentionTerminalStatus",
        "WorkspaceRetainedReference",
        "WorkspaceRetentionRule",
        "WorkspaceRetentionPolicy",
        "WorkspaceRetentionLifecycle",
        "WorkspaceRetentionDiagnostic",
        "WorkspaceRetentionAccounting",
        "WorkspaceRetentionPreview",
        "WorkspaceRetentionResult",
    )
    missing = [name for name in expected_exports if not hasattr(workspace_types, name)]
    assert missing == []
    assert all(getattr(data_types, name) is getattr(workspace_types, name) for name in expected_exports)

    assert get_args(workspace_types.WorkspaceRetentionCategory) == (
        "terminal_result",
        "artifacts",
        "runtime_events",
        "checkpoints",
        "records",
        "files",
        "scratch",
    )
    assert get_args(workspace_types.WorkspaceRetentionRepresentation) == (
        "discard",
        "summary",
        "hot",
        "cold",
    )
    assert get_args(workspace_types.WorkspaceRetentionTerminalStatus) == (
        "completed",
        "failed",
        "cancelled",
    )
    assert get_args(workspace_types.WorkspaceRetainedReference) == (
        workspace_types.WorkspaceRecordRef,
        workspace_types.WorkspaceReferenceEnvelope,
        workspace_types.WorkspaceFileRef,
    )

    expected_keys = {
        "WorkspaceRetentionRule": (
            {"category", "representation"},
            set(),
        ),
        "WorkspaceRetentionPolicy": (
            set(),
            {"rules", "inline_result_limit"},
        ),
        "WorkspaceRetentionLifecycle": (
            {
                "execution_id",
                "status",
                "terminal_at",
                "state_version",
                "recovery_active",
                "lease_active",
            },
            set(),
        ),
        "WorkspaceRetentionDiagnostic": (
            set(),
            {"code", "message", "retryable", "entity", "detail"},
        ),
        "WorkspaceRetentionAccounting": (
            {
                "entities",
                "logical_bytes_deleted",
                "physical_bytes_reclaimed",
                "physical_bytes_pending",
            },
            set(),
        ),
        "WorkspaceRetentionPreview": (
            {
                "status",
                "plan_fingerprint",
                "scope",
                "lifecycle",
                "policy",
                "retained_refs",
                "inline_result",
                "selected",
                "accounting",
                "diagnostics",
            },
            set(),
        ),
        "WorkspaceRetentionResult": (
            {
                "status",
                "plan_fingerprint",
                "manifest_ref",
                "retained_refs",
                "accounting",
                "diagnostics",
            },
            set(),
        ),
    }
    for name, (required, optional) in expected_keys.items():
        contract = getattr(workspace_types, name)
        assert contract.__required_keys__ == frozenset(required)
        assert contract.__optional_keys__ == frozenset(optional)

    hints = get_type_hints(workspace_types.WorkspaceRetentionPreview)
    assert hints["inline_result"] is Any
    assert hints["retained_refs"] == list[workspace_types.WorkspaceRetainedReference]
    assert hints["status"] == Literal["ready", "deferred"]


def test_workspace_retention_protocols_and_fake_provider_use_exact_signatures():
    protocols = (
        workspace_plugin_types.RetentionPolicy,
        workspace_plugin_types.DBStoreProvider,
        workspace_plugin_types.WorkspaceBackend,
        LocalWorkspaceBackend,
        RemoteAuditWorkspaceBackend,
    )
    for protocol in protocols:
        inspect_method = getattr(protocol, "inspect_retention", None)
        apply_method = getattr(protocol, "apply_retention", None)
        assert inspect_method is not None, protocol.__name__
        assert apply_method is not None, protocol.__name__

        inspect_signature = inspect.signature(inspect_method)
        assert tuple(inspect_signature.parameters) == (
            "self",
            "scope",
            "lifecycle",
            "retained_refs",
            "inline_result",
            "policy",
        )
        assert inspect_signature.parameters["scope"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name in ("lifecycle", "retained_refs", "inline_result", "policy"):
            assert inspect_signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert inspect_signature.parameters["retained_refs"].default == ()
        assert inspect_signature.parameters["inline_result"].default is None
        assert inspect_signature.parameters["policy"].default is None

        inspect_hints = get_type_hints(inspect_method)
        assert inspect_hints["inline_result"] is Any
        assert inspect_hints == {
            "scope": dict[str, Any],
            "lifecycle": workspace_types.WorkspaceRetentionLifecycle,
            "retained_refs": Sequence[workspace_types.WorkspaceRetainedReference],
            "inline_result": Any,
            "policy": workspace_types.WorkspaceRetentionPolicy | None,
            "return": workspace_types.WorkspaceRetentionPreview,
        }

        apply_signature = inspect.signature(apply_method)
        assert tuple(apply_signature.parameters) == ("self", "preview")
        assert apply_signature.parameters["preview"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert get_type_hints(apply_method) == {
            "preview": workspace_types.WorkspaceRetentionPreview,
            "return": workspace_types.WorkspaceRetentionResult,
        }


@pytest.mark.asyncio
async def test_local_workspace_backend_retention_inspection_and_apply_are_implemented(tmp_path):
    backend = LocalWorkspaceBackend(tmp_path / "retention-contract")
    assert hasattr(backend, "inspect_retention")
    assert hasattr(backend, "apply_retention")

    lifecycle: workspace_types.WorkspaceRetentionLifecycle = {
        "execution_id": "exec-contract",
        "status": "completed",
        "terminal_at": "2026-07-12T00:00:00+00:00",
        "state_version": 1,
        "recovery_active": False,
        "lease_active": False,
    }
    preview = await backend.inspect_retention(
        {"execution_id": "exec-contract"},
        lifecycle=lifecycle,
    )
    assert preview["status"] == "ready"
    assert all(values == [] for values in preview["selected"].values())
    applied = await backend.apply_retention(preview)
    assert applied["status"] == "applied"
    assert applied["manifest_ref"] is not None
    assert applied["manifest_ref"]["kind"] == "workspace_terminal_manifest"
    assert (await backend.apply_retention(preview))["status"] == "noop"


def test_workspace_manager_requires_terminal_retention_provider_methods():
    assert "inspect_retention" in WorkspaceManager._DB_STORE_REQUIRED_METHODS
    assert "apply_retention" in WorkspaceManager._DB_STORE_REQUIRED_METHODS


def test_workspace_vector_provider_contract_requires_deterministic_deletion():
    delete_method = getattr(workspace_plugin_types.VectorStoreProvider, "delete_records", None)
    assert delete_method is not None
    assert get_type_hints(delete_method) == {
        "record_ids": Sequence[str],
        "return": type(None),
    }
    assert "delete_records" in WorkspaceManager._VECTOR_STORE_REQUIRED_METHODS


def test_workspace_manager_requires_provider_methods_to_be_callable():
    provider = RemoteAuditWorkspaceBackend()
    provider.prune_scope = lambda *_args, **_kwargs: {}  # type: ignore[attr-defined]
    provider.inspect_retention = None  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="inspect_retention"):
        WorkspaceManager()._validate_db_store_provider(provider)


@pytest.mark.asyncio
async def test_workspace_retention_lifecycle_reads_generic_provider_snapshot_and_lease(tmp_path):
    provider = RemoteAuditWorkspaceBackend("retention-lifecycle")

    async def prune_scope(
        scope: dict[str, Any],
        *,
        remove_files: bool = True,
    ) -> dict[str, Any]:
        _ = scope, remove_files
        return {}

    provider.prune_scope = prune_scope  # type: ignore[attr-defined]
    workspace = WorkspaceManager().create(
        tmp_path / "retention-lifecycle",
        db_store_provider=provider,
        vector_store_provider="sqlite",
    )
    execution_id = "exec-provider-lifecycle"
    await workspace.put_snapshot(
        execution_id,
        {
            "state_version": 29,
            "interrupts": {},
            "intervention": {
                "ledger": {"revise": {"status": "pending"}},
            },
        },
    )
    await workspace.claim_lease(
        execution_id,
        "provider-worker",
        ttl=30,
        expected_state_version=29,
    )

    lifecycle = await workspace.get_retention_lifecycle(
        execution_id,
        status="completed",
        terminal_at="2026-07-12T09:00:00Z",
    )

    assert lifecycle == {
        "execution_id": execution_id,
        "status": "completed",
        "terminal_at": "2026-07-12T09:00:00Z",
        "state_version": 29,
        "recovery_active": True,
        "lease_active": True,
    }
    assert "get_retention_lifecycle" in provider.operations


def test_workspace_manager_proves_vector_delete_is_async_without_calling_it():
    delete_called = False

    class SyncDeleteVectorProvider:
        name = "sync-delete"

        async def index_record(self, ref: WorkspaceRecordRef, embedding: list[float]) -> None:
            _ = ref, embedding

        async def search_by_embedding(
            self,
            embedding: list[float],
            *,
            filters: dict[str, Any] | None = None,
            limit: int | None = None,
        ) -> list[WorkspaceRecordRef]:
            _ = embedding, filters, limit
            return []

        def delete_records(self, record_ids: Sequence[str]) -> None:
            nonlocal delete_called
            delete_called = True
            _ = record_ids

    with pytest.raises(TypeError, match="async.*delete_records"):
        WorkspaceManager()._validate_vector_store_provider(SyncDeleteVectorProvider())
    assert delete_called is False


@pytest.mark.asyncio
async def test_workspace_facade_defers_ready_plan_from_read_only_delegated_db_provider(tmp_path):
    class ReadOnlyReadyDBProvider(RemoteAuditWorkspaceBackend):
        def __init__(self):
            super().__init__("read-only-retention")
            self.read_only = True

        async def inspect_retention(
            self,
            scope: dict[str, Any],
            *,
            lifecycle: workspace_types.WorkspaceRetentionLifecycle,
            retained_refs: Sequence[workspace_types.WorkspaceRetainedReference] = (),
            inline_result: Any = None,
            policy: workspace_types.WorkspaceRetentionPolicy | None = None,
        ) -> workspace_types.WorkspaceRetentionPreview:
            self.operations.append("inspect_retention")
            selected = empty_retention_selection()
            selected["runtime_event_ids"] = ["remote-event-ready"]
            resolved_policy = cast(
                workspace_types.WorkspaceRetentionPolicy,
                policy or {"rules": []},
            )
            return {
                "status": "ready",
                "plan_fingerprint": canonical_retention_fingerprint(
                    scope,
                    lifecycle,
                    resolved_policy,
                    list(retained_refs),
                    selected,
                ),
                "scope": scope,
                "lifecycle": lifecycle,
                "policy": resolved_policy,
                "retained_refs": list(retained_refs),
                "inline_result": inline_result,
                "selected": selected,
                "accounting": {
                    "entities": {key: len(values) for key, values in selected.items()},
                    "logical_bytes_deleted": 1,
                    "physical_bytes_reclaimed": 0,
                    "physical_bytes_pending": 0,
                },
                "diagnostics": [],
            }

        async def prune_scope(
            self,
            scope: dict[str, Any],
            *,
            remove_files: bool = True,
        ) -> dict[str, Any]:
            _ = scope, remove_files
            return {}

    provider = ReadOnlyReadyDBProvider()
    workspace = WorkspaceManager().create(
        tmp_path / "read-only-delegated-db",
        db_store_provider=provider,
        vector_store_provider="sqlite",
    )

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-read-only-delegated"},
        lifecycle={
            "execution_id": "exec-read-only-delegated",
            "status": "completed",
            "terminal_at": "2026-07-12T00:00:00Z",
            "state_version": None,
            "recovery_active": False,
            "lease_active": False,
        },
    )

    assert provider.operations[-1] == "inspect_retention"
    assert preview["status"] == "deferred"
    assert all(values == [] for values in preview["selected"].values())
    assert "workspace.retention.provider_capability_missing" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_preview", ["shape", "echo", "fingerprint"])
async def test_workspace_facade_rejects_untrusted_delegated_preview(tmp_path, invalid_preview):
    class InvalidPreviewDBProvider(RemoteAuditWorkspaceBackend):
        async def inspect_retention(
            self,
            scope: dict[str, Any],
            *,
            lifecycle: workspace_types.WorkspaceRetentionLifecycle,
            retained_refs: Sequence[workspace_types.WorkspaceRetainedReference] = (),
            inline_result: Any = None,
            policy: workspace_types.WorkspaceRetentionPolicy | None = None,
        ) -> workspace_types.WorkspaceRetentionPreview:
            resolved_policy = cast(workspace_types.WorkspaceRetentionPolicy, policy)
            selected = empty_retention_selection()
            selected["runtime_event_ids"] = ["provider-event"]
            preview: dict[str, Any] = {
                "status": "ready",
                "plan_fingerprint": canonical_retention_fingerprint(
                    scope,
                    lifecycle,
                    resolved_policy,
                    list(retained_refs),
                    selected,
                ),
                "scope": dict(scope),
                "lifecycle": dict(lifecycle),
                "policy": dict(resolved_policy),
                "retained_refs": list(retained_refs),
                "inline_result": inline_result,
                "selected": selected,
                "accounting": {
                    "entities": {key: len(values) for key, values in selected.items()},
                    "logical_bytes_deleted": 1,
                    "physical_bytes_reclaimed": 0,
                    "physical_bytes_pending": 0,
                },
                "diagnostics": [],
            }
            if invalid_preview == "shape":
                preview.pop("accounting")
            elif invalid_preview == "echo":
                preview["scope"] = {"execution_id": "provider-substituted"}
                preview["plan_fingerprint"] = canonical_retention_fingerprint(
                    preview["scope"],
                    lifecycle,
                    resolved_policy,
                    list(retained_refs),
                    selected,
                )
            else:
                preview["plan_fingerprint"] = "0" * 64
            return cast(workspace_types.WorkspaceRetentionPreview, preview)

        async def prune_scope(
            self,
            scope: dict[str, Any],
            *,
            remove_files: bool = True,
        ) -> dict[str, Any]:
            _ = scope, remove_files
            return {}

    provider = InvalidPreviewDBProvider("untrusted-preview")
    workspace = WorkspaceManager().create(
        tmp_path / f"untrusted-preview-{invalid_preview}",
        db_store_provider=provider,
        vector_store_provider="sqlite",
    )
    scope = {"execution_id": "exec-untrusted-preview"}
    lifecycle: workspace_types.WorkspaceRetentionLifecycle = {
        "execution_id": "exec-untrusted-preview",
        "status": "completed",
        "terminal_at": "2026-07-12T00:00:00Z",
        "state_version": None,
        "recovery_active": False,
        "lease_active": False,
    }

    preview = await workspace.inspect_retention(scope, lifecycle=lifecycle)

    assert preview["status"] == "deferred"
    assert preview["scope"] == scope
    assert preview["lifecycle"] == lifecycle
    assert all(values == [] for values in preview["selected"].values())
    assert "workspace.retention.provider_capability_missing" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_workspace_facade_rejects_provider_that_drops_declared_retained_root(tmp_path):
    class DroppedRootDBProvider(RemoteAuditWorkspaceBackend):
        async def inspect_retention(
            self,
            scope: dict[str, Any],
            *,
            lifecycle: workspace_types.WorkspaceRetentionLifecycle,
            retained_refs: Sequence[workspace_types.WorkspaceRetainedReference] = (),
            inline_result: Any = None,
            policy: workspace_types.WorkspaceRetentionPolicy | None = None,
        ) -> workspace_types.WorkspaceRetentionPreview:
            assert len(retained_refs) == 1
            declared = retained_refs[0]
            assert "id" in declared
            selected = empty_retention_selection()
            selected["record_ids"] = [str(declared["id"])]
            selected["content_paths"] = [str(declared["path"])]
            resolved_policy = cast(workspace_types.WorkspaceRetentionPolicy, policy)
            return cast(workspace_types.WorkspaceRetentionPreview, {
                "status": "ready",
                "plan_fingerprint": canonical_retention_fingerprint(
                    scope,
                    lifecycle,
                    resolved_policy,
                    [],
                    selected,
                ),
                "scope": dict(scope),
                "lifecycle": dict(lifecycle),
                "policy": dict(resolved_policy),
                "retained_refs": [],
                "inline_result": inline_result,
                "selected": selected,
                "accounting": {
                    "entities": {key: len(values) for key, values in selected.items()},
                    "logical_bytes_deleted": 1,
                    "physical_bytes_reclaimed": 0,
                    "physical_bytes_pending": 0,
                },
                "diagnostics": [],
            })

        async def prune_scope(
            self,
            scope: dict[str, Any],
            *,
            remove_files: bool = True,
        ) -> dict[str, Any]:
            _ = scope, remove_files
            return {}

    provider = DroppedRootDBProvider("dropped-retained-root")
    workspace = WorkspaceManager().create(
        tmp_path / "dropped-retained-root",
        db_store_provider=provider,
        vector_store_provider="sqlite",
    )
    artifact = await workspace.put(
        {"artifact": "must remain retained"},
        collection="artifacts",
        kind="final_artifact",
        scope={"execution_id": "exec-dropped-root"},
    )

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-dropped-root"},
        lifecycle={
            "execution_id": "exec-dropped-root",
            "status": "completed",
            "terminal_at": "2026-07-12T00:00:00Z",
            "state_version": None,
            "recovery_active": False,
            "lease_active": False,
        },
        retained_refs=[artifact],
    )

    assert preview["status"] == "deferred"
    assert all(values == [] for values in preview["selected"].values())
    assert artifact in preview["retained_refs"]
    assert "workspace.retention.provider_capability_missing" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonicalization",
    ["cross_workspace_envelope", "plain_record"],
)
async def test_workspace_facade_rejects_provider_that_degrades_declared_envelope_identity(
    tmp_path,
    canonicalization,
):
    class DegradedEnvelopeDBProvider(RemoteAuditWorkspaceBackend):
        async def inspect_retention(
            self,
            scope: dict[str, Any],
            *,
            lifecycle: workspace_types.WorkspaceRetentionLifecycle,
            retained_refs: Sequence[workspace_types.WorkspaceRetainedReference] = (),
            inline_result: Any = None,
            policy: workspace_types.WorkspaceRetentionPolicy | None = None,
        ) -> workspace_types.WorkspaceRetentionPreview:
            assert len(retained_refs) == 1
            declared = retained_refs[0]
            assert "workspace_id" in declared
            if canonicalization == "cross_workspace_envelope":
                returned_ref = dict(declared)
                returned_ref["workspace_id"] = "remote_audit_other-workspace"
            else:
                returned_ref = dict(self._records[str(declared["record_id"])])
            canonical_refs = [
                cast(workspace_types.WorkspaceRetainedReference, returned_ref)
            ]
            selected = empty_retention_selection()
            resolved_policy = cast(workspace_types.WorkspaceRetentionPolicy, policy)
            return cast(
                workspace_types.WorkspaceRetentionPreview,
                {
                    "status": "ready",
                    "plan_fingerprint": canonical_retention_fingerprint(
                        scope,
                        lifecycle,
                        resolved_policy,
                        canonical_refs,
                        selected,
                    ),
                    "scope": dict(scope),
                    "lifecycle": dict(lifecycle),
                    "policy": dict(resolved_policy),
                    "retained_refs": canonical_refs,
                    "inline_result": inline_result,
                    "selected": selected,
                    "accounting": {
                        "entities": {
                            key: len(values) for key, values in selected.items()
                        },
                        "logical_bytes_deleted": 0,
                        "physical_bytes_reclaimed": 0,
                        "physical_bytes_pending": 0,
                    },
                    "diagnostics": [],
                },
            )

        async def prune_scope(
            self,
            scope: dict[str, Any],
            *,
            remove_files: bool = True,
        ) -> dict[str, Any]:
            _ = scope, remove_files
            return {}

    provider = DegradedEnvelopeDBProvider(f"degraded-envelope-{canonicalization}")
    workspace = WorkspaceManager().create(
        tmp_path / f"degraded-envelope-{canonicalization}",
        db_store_provider=provider,
        vector_store_provider="sqlite",
    )
    artifact = await workspace.put(
        {"artifact": "must retain workspace-bound envelope identity"},
        collection="artifacts",
        kind="final_artifact",
        scope={"execution_id": "exec-degraded-envelope"},
    )
    envelope = await workspace.ref_envelope(artifact)

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-degraded-envelope"},
        lifecycle={
            "execution_id": "exec-degraded-envelope",
            "status": "completed",
            "terminal_at": "2026-07-12T00:00:00Z",
            "state_version": None,
            "recovery_active": False,
            "lease_active": False,
        },
        retained_refs=[envelope],
    )

    assert preview["status"] == "deferred"
    assert all(values == [] for values in preview["selected"].values())
    assert envelope in preview["retained_refs"]
    assert "workspace.retention.provider_capability_missing" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_remote_audit_workspace_backend_proves_provider_contract_across_consumers():
    provider = RemoteAuditWorkspaceBackend()
    agent = _create_agent("remote-provider-proof").use_workspace(provider)
    workspace = agent.workspace
    assert workspace is not None
    assert workspace.backend is provider
    assert workspace.capabilities()["features"]["supports_remote_backend"] is True

    flow = TriggerFlow(name="remote-provider-proof")

    async def remember(data: TriggerFlowRuntimeData):
        await data.async_set_state("value", data.value)

    flow.to(remember)
    execution = flow.create_execution(runtime_resources={"durable_provider": workspace})

    snapshot = await execution.async_start("remote-value")
    snapshot_ref = await execution.async_save(
        step_id="distributed-proof",
        require_distributed_provider=True,
    )
    runtime_events = await workspace.query_runtime_events(execution.id)

    assert snapshot["value"] == "remote-value"
    assert snapshot_ref["collection"] == "checkpoints"
    assert snapshot_ref["source"]["type"] == "remote_audit_provider"
    assert runtime_events
    assert runtime_events[0]["event_type"] == "triggerflow.definition_declared"
    assert runtime_events[-1]["event_type"] == "triggerflow.execution_closed"
    assert "append_runtime_event" in provider.operations
    assert "put_snapshot" in provider.operations
    assert "put_checkpoint" in provider.operations

    agent_execution = (
        agent
        .input("prove provider")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "remote-provider-task", "step_id": "record"},
            limits={"max_model_requests": 1},
        )
    )
    data = await agent_execution.async_get_data()
    workspace_record = await agent_execution.async_record_workspace(
        content={"answer": data["answer"]},
        purpose="deliverable",
    )
    checkpoint = await workspace.put_checkpoint(
        agent_execution.id,
        {"record_ref": workspace_record["record"]},
        step_id="record",
    )
    evidence_link = await workspace.link_evidence(
        workspace_record["record"],
        checkpoint,
        relation="checkpointed_by",
        execution_id=agent_execution.id,
        checkpoint_id=checkpoint["id"],
    )
    evidence_links = await workspace.links(workspace_record["record"], relation="checkpointed_by")

    assert data["answer"] == "remote-provider-proof"
    assert workspace_record["checkpoint"] is None
    assert checkpoint["source"]["type"] == "remote_audit_provider"
    assert [item["id"] for item in evidence_links] == [evidence_link["id"]]
    assert evidence_links[0]["target_id"] == checkpoint["id"]
    assert evidence_links[0]["meta"]["evidence"]["execution_id"] == agent_execution.id
    assert "link_evidence" in provider.operations


@pytest.mark.asyncio
async def test_workspace_backend_provider_registration_resolves_custom_backend():
    provider_name = "remote-audit-provider-registration-test"
    factory_calls: list[dict[str, Any]] = []

    def provider_factory(
        *,
        root: Any | None = None,
        create: bool = True,
        mode: str = "read_write",
        tenant_id: str = "tenant-alpha",
        **options: Any,
    ) -> RemoteAuditWorkspaceBackend:
        factory_calls.append(
            {
                "root": root,
                "create": create,
                "mode": mode,
                "tenant_id": tenant_id,
                "options": options,
            }
        )
        return RemoteAuditWorkspaceBackend(tenant_id=tenant_id)

    Agently.workspace.register_backend_provider(provider_name, provider_factory)
    try:
        agent = _create_agent("registered-provider-proof").use_workspace(
            "logical-root",
            provider=provider_name,
            provider_options={"tenant_id": "tenant-registered"},
        )
        workspace = agent.workspace
        assert workspace is not None
        assert provider_name in Agently.workspace.list_backend_providers()
        assert factory_calls == [
            {
                "root": "logical-root",
                "create": True,
                "mode": "read_write",
                "tenant_id": "tenant-registered",
                "options": {},
            }
        ]
        assert cast(RemoteAuditWorkspaceBackend, workspace.backend).workspace_id == "remote_audit_tenant-registered"
        assert agent.settings.get("workspace.provider") == provider_name

        flow = TriggerFlow(name="registered-provider-proof")

        async def remember(data: TriggerFlowRuntimeData):
            await data.async_set_state("value", data.value)

        flow.to(remember)
        execution = flow.create_execution(
            runtime_resources={
                "workspace": workspace,
                "runtime_event_store": workspace,
            }
        )
        snapshot = await execution.async_start("registered-value")
        snapshot_ref = await execution.async_save(
            step_id="registered-provider",
            require_distributed_provider=True,
        )
        runtime_events = await workspace.query_runtime_events(execution.id)

        assert snapshot["value"] == "registered-value"
        assert snapshot_ref["source"]["type"] == "remote_audit_provider"
        assert runtime_events[-1]["event_type"] == "triggerflow.execution_closed"
    finally:
        Agently.workspace.unregister_backend_provider(provider_name)
    assert provider_name not in Agently.workspace.list_backend_providers()
