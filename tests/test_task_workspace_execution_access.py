from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.TaskWorkspace import TaskWorkspace, TaskWorkspacePolicyError
from agently.types.data import (
    CodeExecutionBundle,
    CodeExecutionFile,
    CodeExecutionStep,
    TaskWorkspaceAccessRequirement,
)


def _python_bundle() -> CodeExecutionBundle:
    return CodeExecutionBundle.create(
        language="python",
        files=[CodeExecutionFile(path="main.py", content=b"print('ok')\n")],
        entrypoint="main.py",
        build_steps=[],
        run_step=CodeExecutionStep(argv=("python", "main.py"), role="run"),
        expected_outputs=["output/result.json"],
        provenance={"kind": "test"},
    )


@pytest.mark.asyncio
async def test_workspace_grant_precedes_materialization_and_contains_files(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="call-1",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )

    manifest = await workspace.materialize_execution_bundle(grant, _python_bundle())

    assert manifest.grant_id == grant.grant_id
    assert manifest.files[0].path.endswith("source/main.py")
    assert Path(manifest.files[0].host_path).is_relative_to(workspace.fallback_root)
    assert Path(manifest.files[0].host_path).read_bytes() == b"print('ok')\n"
    assert manifest.files[0].sha256 == _python_bundle().files[0].sha256
    workspace.close_execution_access(grant.grant_id)
    with pytest.raises(TaskWorkspacePolicyError, match="closed|grant"):
        await workspace.materialize_execution_bundle(grant, _python_bundle())


def test_read_only_workspace_rejects_read_write_execution_grant(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="run-1")

    with pytest.raises(TaskWorkspacePolicyError, match="read_write|permission"):
        workspace.issue_execution_access(
            action_call_id="call-1",
            requirement=TaskWorkspaceAccessRequirement(mode="read_write"),
        )


@pytest.mark.asyncio
async def test_bundle_can_be_materialized_only_once_per_grant(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="call-1",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    await workspace.materialize_execution_bundle(grant, _python_bundle())

    with pytest.raises(TaskWorkspacePolicyError, match="materialized|bundle"):
        await workspace.materialize_execution_bundle(grant, _python_bundle())


@pytest.mark.asyncio
async def test_output_collection_is_declared_and_digest_verified(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="call-1",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    manifest = await workspace.materialize_execution_bundle(grant, _python_bundle())
    output_root = next(root for root in grant.roots if root.role == "output")
    output = Path(output_root.host_path) / "result.json"
    output.write_text('{"ok": true}\n', encoding="utf-8")

    collected = await workspace.collect_execution_outputs(
        grant,
        ["output/result.json"],
    )

    assert manifest.expected_outputs == ("output/result.json",)
    assert collected[0].path.endswith("output/result.json")
    assert collected[0].sha256.startswith("sha256:")
    with pytest.raises(TaskWorkspacePolicyError, match="declared"):
        await workspace.collect_execution_outputs(grant, ["logs/private.log"])


def test_requirement_rejects_private_and_escape_paths() -> None:
    with pytest.raises(ValueError, match="path"):
        TaskWorkspaceAccessRequirement(mode="snapshot", input_paths=("../secret",))
    with pytest.raises(ValueError, match="private|path"):
        TaskWorkspaceAccessRequirement(mode="snapshot", input_paths=(".agently/data",))
