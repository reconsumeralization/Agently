import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.core import PluginManager, TaskWorkspacePolicyError
from agently.types.data import AgentlyRequestData, AgentArtifactContext, AgentReviewContext
from agently.utils import Settings


class ArtifactRequester:
    name = "ArtifactRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"input": self.prompt.get("input")},
            request_options={"stream": True},
            request_url="mock://agent-execution-artifact",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        content = (
            json.dumps({"name": "report", "count": 2})
            if request_data.data["input"] == "json"
            else "artifact body"
        )
        yield "message", content

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-agent-execution-artifact", "status": "completed"}


def create_artifact_agent(tmp_path: Path, name: str, *, mode: str = "read_only"):
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-PluginManager")
    plugin_manager.register("ModelRequester", ArtifactRequester, activate=True)
    workspace_root = tmp_path / name
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    ).use_task_workspace(workspace_root, mode=mode)
    return agent, workspace_root


@pytest.mark.asyncio
async def test_text_artifact_uses_read_only_fallback_and_preserves_business_result(tmp_path):
    agent, workspace_root = create_artifact_agent(tmp_path, "text-artifact")
    execution = agent.input("text").artifact("reports/result.md")

    assert await execution.async_get_data() == "artifact body"
    assert execution.status == "success"
    assert len(execution.artifact_results) == 1
    ref = execution.artifact_results[0]
    assert ref["path"] == f".agently/files/{execution.id}/reports/result.md"
    assert ref["role"] == "artifact"
    assert ref["complete_readback_verified"] is True
    assert (workspace_root / ref["path"]).read_text(encoding="utf-8") == "artifact body"
    assert execution.logs["artifact_refs"] == execution.artifact_results
    assert execution._terminal_retained_refs == execution.artifact_results
    paths = [item.path for item in execution.stream.items]
    assert paths.index("artifact.started") < paths.index("artifact.completed") < paths.index("result")


@pytest.mark.asyncio
async def test_json_artifact_uses_requested_path_in_read_write_workspace(tmp_path):
    agent, workspace_root = create_artifact_agent(tmp_path, "json-artifact", mode="read_write")
    execution = (
        agent.input("json")
        .output(
            {
                "name": (str, "Report name.", True),
                "count": (int, "Item count.", True),
            },
            format="json",
        )
        .artifact(Path("deliverables/result.json"))
    )

    expected = {"name": "report", "count": 2}
    assert await execution.async_get_data() == expected
    assert execution.artifact_results[0]["path"] == "deliverables/result.json"
    assert json.loads((workspace_root / "deliverables/result.json").read_text()) == expected
    assert (workspace_root / "deliverables/result.json").read_text() == json.dumps(
        expected,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


@pytest.mark.asyncio
async def test_multiple_artifacts_support_sync_text_and_async_bytes_handlers(tmp_path):
    agent, workspace_root = create_artifact_agent(tmp_path, "multiple-artifacts")
    calls: list[tuple[int, str]] = []

    def render_text(result: object, context: AgentArtifactContext) -> str:
        calls.append((context.index, context.path))
        return str(result).upper()

    async def render_bytes(result: object, context: AgentArtifactContext) -> bytes:
        calls.append((context.index, context.path))
        return f"binary:{result}".encode()

    execution = (
        agent.input("text")
        .artifact("result.txt", render_text)
        .artifact("result.bin", render_bytes)
    )

    assert await execution.async_get_data() == "artifact body"
    assert calls == [(1, "result.txt"), (2, "result.bin")]
    refs = execution.artifact_results
    assert len(refs) == 2
    assert (workspace_root / refs[0]["path"]).read_text() == "ARTIFACT BODY"
    assert (workspace_root / refs[1]["path"]).read_bytes() == b"binary:artifact body"


@pytest.mark.asyncio
async def test_artifacts_complete_before_review_and_are_visible_in_review_context(tmp_path):
    agent, _workspace_root = create_artifact_agent(tmp_path, "artifact-review-order")
    observed_refs: list[dict[str, object]] = []

    def reviewer(_result: object, context: AgentReviewContext) -> bool:
        observed_refs.extend(dict(ref) for ref in context.artifact_refs)
        return True

    execution = agent.input("text").review(reviewer).artifact("result.txt")

    assert await execution.async_get_data() == "artifact body"
    assert observed_refs == execution.artifact_results
    paths = [item.path for item in execution.stream.items]
    assert paths.index("artifact.completed") < paths.index("review.started")


@pytest.mark.asyncio
async def test_artifact_rejects_paths_outside_task_workspace(tmp_path):
    agent, _workspace_root = create_artifact_agent(tmp_path, "artifact-containment")
    outside = tmp_path / "outside.txt"
    execution = agent.input("text").artifact(outside)

    with pytest.raises(TaskWorkspacePolicyError, match="outside TaskWorkspace root"):
        await execution.async_get_data()

    assert execution.status == "error"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_artifact_digest_mismatch_fails_declared_delivery(tmp_path, monkeypatch):
    agent, _workspace_root = create_artifact_agent(tmp_path, "artifact-digest")
    execution = agent.input("text").artifact("result.txt")
    original_read = execution.task_workspace.read_file

    async def corrupt_readback(*args: Any, **kwargs: Any):
        return replace(await original_read(*args, **kwargs), sha256="0" * 64)

    monkeypatch.setattr(execution.task_workspace, "read_file", corrupt_readback)

    with pytest.raises(RuntimeError, match="physical readback"):
        await execution.async_get_data()

    assert execution.status == "error"
    assert execution.artifact_results == []


def test_artifact_rejects_invalid_declaration(tmp_path):
    agent, _workspace_root = create_artifact_agent(tmp_path, "invalid-artifact")

    with pytest.raises(ValueError, match="non-empty"):
        agent.input("text").artifact("")
    with pytest.raises(TypeError, match="callable or None"):
        agent.input("text").artifact("result.txt", "not-callable")  # type: ignore[arg-type]
