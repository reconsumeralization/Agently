from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from agently.core.Workspace import Workspace, WorkspaceManager
from agently.types.data import WorkspaceBackendCapabilities, WorkspaceFileRef, WorkspaceRecordRef
from agently.types.plugins import RuntimeEventStore, WorkspaceBackend


def test_workspace_file_ref_and_capabilities_are_direct_root_contracts() -> None:
    file_ref_annotations = inspect.get_annotations(WorkspaceFileRef)
    assert set(file_ref_annotations) >= {
        "type",
        "path",
        "workspace_id",
        "execution_id",
        "size",
        "sha256",
        "available",
    }
    capabilities_annotations = inspect.get_annotations(WorkspaceBackendCapabilities)
    assert set(capabilities_annotations) == {
        "root",
        "mode",
        "external_read",
        "external_write",
        "private_write",
        "materialized_components",
    }


def test_workspace_backend_protocol_is_minimal_and_has_no_layout_or_scratch_contract() -> None:
    required = {"root", "put", "get_data", "search", "capabilities"}
    assert required <= set(dir(WorkspaceBackend))
    for removed in (
        "content_root",
        "files_root",
        "content",
        "metadata",
        "open_scratch",
        "close_scratch",
        "inspect_retention",
        "apply_retention",
    ):
        assert not hasattr(WorkspaceBackend, removed)
    assert "Explicit audit sink" in (RuntimeEventStore.__doc__ or "")


class MinimalBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[str, Any] = {}

    async def put(self, content: Any, *, collection: str, **kwargs: Any) -> WorkspaceRecordRef:
        self.records["record-1"] = content
        return {
            "id": "record-1",
            "collection": collection,
            "kind": kwargs.get("kind"),
            "path": None,
            "sha256": None,
            "size": 0,
            "summary": "",
            "scope": {},
            "source": {},
            "created_at": "",
            "meta": {},
        }

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        record_id = ref_or_path.get("id") if isinstance(ref_or_path, dict) else ref_or_path
        assert isinstance(record_id, str)
        return self.records[record_id]

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]:
        _ = query, filters
        return []

    def capabilities(self) -> WorkspaceBackendCapabilities:
        return {
            "root": str(self.root),
            "mode": "read_write",
            "external_read": False,
            "external_write": False,
            "private_write": True,
            "materialized_components": [],
        }


@pytest.mark.asyncio
async def test_concrete_minimal_backend_is_accepted_without_optional_ports(tmp_path: Path) -> None:
    root = tmp_path / "remote-root"
    backend = MinimalBackend(root)
    workspace = Workspace(backend)
    ref = await workspace.put("value", collection="records")

    assert await workspace.get_data(ref) == "value"
    assert workspace.root == root.resolve()
    assert not root.exists()


def test_component_provider_factories_are_not_called_during_binding(tmp_path: Path) -> None:
    manager = WorkspaceManager()
    calls: list[str] = []
    root = tmp_path / "missing-project"

    def factory(**_: Any) -> Any:
        calls.append("vector")
        raise AssertionError("provider should remain lazy")

    manager.register_vector_store_provider("lazy-probe", factory)
    workspace = Workspace(root, manager, vector_store_provider="lazy-probe")

    assert workspace.capabilities()["materialized_components"] == []
    assert calls == []
    assert not root.exists()


def test_db_provider_contract_no_longer_requires_retention_or_scratch_methods() -> None:
    required = set(WorkspaceManager._DB_STORE_REQUIRED_METHODS)
    assert "put_record" in required
    assert "append_runtime_event" in required
    assert required.isdisjoint(
        {
            "inspect_retention",
            "apply_retention",
            "add_retention_anchor",
            "register_scratch_lease",
            "close_scratch_lease",
        }
    )
