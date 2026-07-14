# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.Workspace import Workspace


@pytest.mark.asyncio
async def test_terminal_cleanup_keeps_only_verified_execution_products(tmp_path: Path) -> None:
    external = tmp_path / "project.txt"
    external.write_text("never delete", encoding="utf-8")
    workspace = Workspace(tmp_path)
    draft = await workspace.write_file("working/draft.md", "discard")
    final = await workspace.write_file("deliverables/final.md", "retain")
    draft_path = tmp_path / draft["path"]
    final_path = tmp_path / final["path"]

    result = await workspace._close_execution_files(
        retained_refs=[final["file_refs"][0]],
        status="completed",
    )

    assert not draft_path.exists()
    assert final_path.read_text(encoding="utf-8") == "retain"
    assert external.read_text(encoding="utf-8") == "never delete"
    assert result["status"] == "applied"
    assert result["deleted_bytes"] == len(b"discard")
    assert result["retained_bytes"] == len(b"retain")
    assert result["retained_refs"] == [final["file_refs"][0]]
    assert result["diagnostics"] == []


@pytest.mark.asyncio
async def test_terminal_cleanup_preserves_all_files_when_declared_product_is_unverifiable(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    draft = await workspace.write_file("working/draft.md", "draft stays retryable")
    final = await workspace.write_file("deliverables/final.md", "retain safely")
    invalid_ref = dict(final["file_refs"][0])
    invalid_ref["sha256"] = "0" * 64

    result = await workspace._close_execution_files(
        retained_refs=[invalid_ref],
        status="completed",
    )

    assert (tmp_path / draft["path"]).read_text(encoding="utf-8") == "draft stays retryable"
    assert (tmp_path / final["path"]).read_text(encoding="utf-8") == "retain safely"
    assert result["status"] == "deferred"
    assert result["retained_refs"] == []
    assert result["retained_bytes"] == 0
    assert result["deleted_bytes"] == 0
    assert result["diagnostics"][0]["retryable"] is True
    assert result["diagnostics"][0]["code"] == "workspace.file_ref.digest_mismatch"


@pytest.mark.asyncio
async def test_terminal_cleanup_removes_empty_private_file_carrier(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    written = await workspace.write_file("working/process.txt", "temporary")
    assert (tmp_path / written["path"]).is_file()

    result = await workspace._close_execution_files(
        retained_refs=[],
        status="failed",
    )

    assert result["status"] == "applied"
    assert not (tmp_path / ".agently").exists()
