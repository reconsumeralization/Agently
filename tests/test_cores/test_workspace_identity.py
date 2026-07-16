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

import asyncio
import concurrent.futures
import importlib
import json
import multiprocessing
from pathlib import Path

import pytest

from agently.core.Workspace import LocalWorkspaceBackend, Workspace
from agently.core.Workspace.Errors import WorkspaceError, WorkspacePolicyError


def _allocate_workspace_identity_process(
    system_root: str,
    workspace_id: str,
    count: int,
) -> list[str]:
    from agently.core.Workspace.Identity.Catalog import WorkspaceIdentityCatalog

    async def allocate() -> list[str]:
        catalog = WorkspaceIdentityCatalog(system_root, workspace_id=workspace_id)
        return [(await catalog.allocate("record")).entity_id for _ in range(count)]

    return asyncio.run(allocate())


def test_base62_expands_without_a_fixed_width_limit() -> None:
    try:
        encoding = importlib.import_module("agently.core.Workspace.Identity.Encoding")
    except ModuleNotFoundError:
        encoding = None

    assert encoding is not None, "Workspace must own a private scoped-identity encoder"
    encode_base62 = encoding.encode_base62
    decode_base62 = encoding.decode_base62

    expected = {
        0: "0",
        1: "1",
        61: "z",
        62: "10",
        (62**4) - 1: "zzzz",
        62**4: "10000",
        62**8: "100000000",
        (2**130) + 17: None,
    }
    for value, encoded in expected.items():
        actual = encode_base62(value)
        if encoded is not None:
            assert actual == encoded
        assert decode_base62(actual) == value


@pytest.mark.asyncio
async def test_catalog_allocates_one_shared_sequence_without_sqlite(tmp_path: Path) -> None:
    try:
        catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    except ModuleNotFoundError:
        catalog_module = None

    assert catalog_module is not None, "Workspace must own a filesystem identity catalog"
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )

    record = await catalog.allocate("record")
    locator = await catalog.allocate("locator")
    version = await catalog.allocate("content_version")

    assert (record.entity_id, locator.entity_id, version.entity_id) == (
        "rec_1",
        "loc_2",
        "cv_3",
    )
    assert record.canonical_key == ("workspace", "workspace-alpha", "rec_1")
    state = json.loads((tmp_path / ".agently" / "identity" / "state.json").read_text(encoding="utf-8"))
    assert state == {
        "schema_version": "workspace_identity_state/v1",
        "workspace_id": "workspace-alpha",
        "high_water": "3",
        "revision": 3,
    }
    assert not (tmp_path / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_same_short_id_in_different_workspaces_has_a_distinct_canonical_key(
    tmp_path: Path,
) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    first_catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / "first" / ".agently",
        workspace_id="workspace-alpha",
    )
    second_catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / "second" / ".agently",
        workspace_id="workspace-beta",
    )

    first = await first_catalog.allocate("record")
    second = await second_catalog.allocate("record")

    assert first.entity_id == second.entity_id == "rec_1"
    assert first.canonical_key == ("workspace", "workspace-alpha", "rec_1")
    assert second.canonical_key == ("workspace", "workspace-beta", "rec_1")
    assert first.canonical_key != second.canonical_key


@pytest.mark.asyncio
async def test_task_range_lease_advances_high_water_and_abandoned_ids_are_not_reused(
    tmp_path: Path,
) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )

    first = await catalog.allocate("record")
    leased = await catalog.lease_task_range("agent_task_one", size=3)
    restarted = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    after_gap = await restarted.allocate("locator")

    assert first.entity_id == "rec_1"
    assert leased == (2, 4)
    assert after_gap.entity_id == "loc_5"
    task_manifests = list((tmp_path / ".agently" / "identity" / "tasks").glob("*/manifest.json"))
    assert len(task_manifests) == 1
    task_manifest = json.loads(task_manifests[0].read_text(encoding="utf-8"))
    assert task_manifest["task_id"] == "agent_task_one"
    assert task_manifest["leases"] == [{"start": "2", "end": "4"}]


@pytest.mark.asyncio
async def test_deleted_object_manifest_does_not_make_its_id_reusable(tmp_path: Path) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    deleted = await catalog.allocate("record")
    object_manifest = next((tmp_path / ".agently" / "identity" / "objects").rglob("1.json"))
    object_manifest.unlink()

    restarted = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    next_identity = await restarted.allocate("record")

    assert deleted.entity_id == "rec_1"
    assert next_identity.entity_id == "rec_2"


@pytest.mark.asyncio
async def test_catalog_fails_closed_for_invalid_state_or_disabled_private_persistence(
    tmp_path: Path,
) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    system_root = tmp_path / ".agently"

    disabled = catalog_module.WorkspaceIdentityCatalog(
        system_root,
        workspace_id="workspace-alpha",
        private_write=False,
    )
    with pytest.raises(WorkspacePolicyError, match="private"):
        await disabled.allocate("record")
    assert not system_root.exists()

    no_create = catalog_module.WorkspaceIdentityCatalog(
        system_root,
        workspace_id="workspace-alpha",
        create=False,
    )
    with pytest.raises(WorkspacePolicyError, match="create=False"):
        await no_create.allocate("record")
    assert not system_root.exists()

    catalog = catalog_module.WorkspaceIdentityCatalog(
        system_root,
        workspace_id="workspace-alpha",
    )
    await catalog.allocate("record")
    state_path = system_root / "identity" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["workspace_id"] = "workspace-beta"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="different Workspace"):
        await catalog.allocate("record")

    state["workspace_id"] = "workspace-alpha"
    state["high_water"] = "not-a-decimal"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="counters"):
        await catalog.allocate("record")


def test_catalog_allocates_disjoint_ids_across_processes(tmp_path: Path) -> None:
    system_root = str(tmp_path / ".agently")
    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(
                _allocate_workspace_identity_process,
                system_root,
                "workspace-alpha",
                8,
            )
            for _ in range(4)
        ]
        allocated = [entity_id for future in futures for entity_id in future.result(timeout=30)]

    assert len(allocated) == 32
    assert len(set(allocated)) == 32
    state = json.loads((tmp_path / ".agently" / "identity" / "state.json").read_text(encoding="utf-8"))
    assert state["high_water"] == "32"
    assert not (tmp_path / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_local_backend_uses_scoped_short_ids_for_new_records_and_links(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path, mode="read_write")

    first = await workspace.put("first", collection="observations")
    second = await workspace.put("second", collection="observations")
    link = await workspace.link(first, second, "supports")

    assert first["id"] == "rec_1"
    assert second["id"] == "rec_2"
    assert link["id"] == "lnk_3"
    assert (tmp_path / ".agently" / "identity" / "state.json").exists()
    assert (tmp_path / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_content_observation_separates_locator_from_immutable_versions(
    tmp_path: Path,
) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )

    first = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="sources/report.md",
        digest="a" * 64,
        size=10,
        payload_pointer={"type": "workspace_file", "path": "sources/report.md"},
    )
    unchanged = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="sources/report.md",
        digest="a" * 64,
        size=10,
        payload_pointer={"type": "workspace_file", "path": "sources/report.md"},
    )
    with pytest.raises(WorkspaceError, match="size"):
        await catalog.observe_content(
            locator_kind="path",
            normalized_locator="sources/report.md",
            digest="a" * 64,
            size=11,
            payload_pointer={"type": "workspace_file", "path": "sources/report.md"},
        )
    changed = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="sources/report.md",
        digest="b" * 64,
        size=12,
        payload_pointer={"type": "workspace_file", "path": "sources/report.md"},
    )
    same_bytes_elsewhere = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="archive/report.md",
        digest="b" * 64,
        size=12,
        payload_pointer={"type": "workspace_file", "path": "archive/report.md"},
    )

    assert (first.locator_id, first.content_version_id, first.created) == (
        "loc_1",
        "cv_2",
        True,
    )
    assert unchanged == catalog_module.ContentObservation(
        locator_id="loc_1",
        content_version_id="cv_2",
        digest="a" * 64,
        size=10,
        created=False,
    )
    assert (changed.locator_id, changed.content_version_id, changed.created) == (
        "loc_1",
        "cv_3",
        True,
    )
    assert (same_bytes_elsewhere.locator_id, same_bytes_elsewhere.content_version_id) == (
        "loc_4",
        "cv_5",
    )
    old_version = await catalog.resolve("cv_2")
    new_version = await catalog.resolve("cv_3")
    assert old_version["digest"] == "a" * 64
    assert new_version["digest"] == "b" * 64
    assert old_version["locator_id"] == new_version["locator_id"] == "loc_1"


@pytest.mark.asyncio
async def test_read_only_workspace_promotes_file_identity_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sources" / "report.md"
    source.parent.mkdir()
    source.write_text("first version", encoding="utf-8")
    workspace = Workspace(tmp_path)

    ordinary_read = await workspace.read_file("sources/report.md")
    assert ordinary_read["content"] == "first version"
    assert not (tmp_path / ".agently").exists()

    first = await workspace._promote_file_identity("sources/report.md", role="source")
    assert first.get("locator_id") == "loc_1"
    assert first.get("content_version_id") == "cv_2"
    assert first.get("role") == "source"
    assert source.read_text(encoding="utf-8") == "first version"

    source.write_text("second version", encoding="utf-8")
    second = await workspace._promote_file_identity("sources/report.md", role="source")

    assert second.get("locator_id") == first.get("locator_id")
    assert second.get("content_version_id") == "cv_3"
    assert second.get("content_version_id") != first.get("content_version_id")
    assert await workspace.backend._identity_catalog.resolve(str(first.get("content_version_id")))


@pytest.mark.asyncio
async def test_content_segments_and_typed_links_receive_independent_identities(
    tmp_path: Path,
) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    observed = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="sources/long.md",
        digest="a" * 64,
        size=100,
        payload_pointer={"type": "workspace_file", "path": "sources/long.md"},
    )

    segment = await catalog.add_segment(
        content_version_id=observed.content_version_id,
        ordinal=0,
        offset=0,
        length=50,
        digest="b" * 64,
        payload_pointer={"type": "workspace_file_range", "offset": 0, "length": 50},
    )
    link = await catalog.add_link(
        source_id=observed.content_version_id,
        target_id=segment.entity_id,
        relation="contains",
        role="source_segment",
    )

    assert segment.entity_id == "seg_3"
    assert link.entity_id == "lnk_4"
    segment_manifest = await catalog.resolve(segment.entity_id)
    link_manifest = await catalog.resolve(link.entity_id)
    assert segment_manifest["content_version_id"] == "cv_2"
    assert segment_manifest["ordinal"] == 0
    assert link_manifest["source_id"] == "cv_2"
    assert link_manifest["target_id"] == "seg_3"
    assert link_manifest["relation"] == "contains"
    assert link_manifest["role"] == "source_segment"


@pytest.mark.asyncio
async def test_equivalent_url_locators_share_one_normalized_identity(tmp_path: Path) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )

    first = await catalog.observe_content(
        locator_kind="url",
        normalized_locator="HTTPS://Example.COM:443/reports/./latest.md#section",
        digest="a" * 64,
        size=10,
        payload_pointer={"type": "remote_url"},
    )
    equivalent = await catalog.observe_content(
        locator_kind="url",
        normalized_locator="https://example.com/reports/latest.md",
        digest="a" * 64,
        size=10,
        payload_pointer={"type": "remote_url"},
    )

    assert equivalent.locator_id == first.locator_id
    assert equivalent.content_version_id == first.content_version_id
    assert equivalent.created is False


@pytest.mark.asyncio
async def test_durable_file_promotion_fails_closed_without_private_persistence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.md"
    source.write_text("source", encoding="utf-8")

    create_disabled = Workspace(tmp_path, create=False)
    with pytest.raises(WorkspacePolicyError, match="create=False"):
        await create_disabled._promote_file_identity("report.md", role="source")
    assert not (tmp_path / ".agently").exists()

    private_disabled_root = tmp_path / "private-disabled"
    private_disabled_root.mkdir()
    (private_disabled_root / "report.md").write_text("source", encoding="utf-8")
    private_disabled = Workspace(LocalWorkspaceBackend(private_disabled_root, mode="read_only"))
    assert private_disabled.capabilities()["private_write"] is False
    with pytest.raises(WorkspacePolicyError, match="private"):
        await private_disabled._promote_file_identity("report.md", role="source")

    class OpaqueBackend:
        root = tmp_path
        read_only = True

        @staticmethod
        def capabilities() -> dict[str, object]:
            return {"private_write": False, "materialized_components": []}

    opaque = Workspace(OpaqueBackend())  # type: ignore[arg-type]
    with pytest.raises(WorkspacePolicyError, match="cannot persist"):
        await opaque._promote_file_identity("report.md", role="source")


@pytest.mark.asyncio
async def test_stale_locator_index_fails_closed_instead_of_returning_a_deleted_version(
    tmp_path: Path,
) -> None:
    catalog_module = importlib.import_module("agently.core.Workspace.Identity.Catalog")
    catalog = catalog_module.WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    observed = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="stale.md",
        digest="a" * 64,
        size=1,
        payload_pointer={"type": "workspace_file", "path": "stale.md"},
    )
    _, sequence = catalog._parse_entity_id(observed.content_version_id)
    catalog._manifest_path("content_version", sequence).unlink()

    with pytest.raises((WorkspaceError, KeyError), match="does not exist"):
        await catalog.observe_content(
            locator_kind="path",
            normalized_locator="stale.md",
            digest="a" * 64,
            size=1,
            payload_pointer={"type": "workspace_file", "path": "stale.md"},
        )
