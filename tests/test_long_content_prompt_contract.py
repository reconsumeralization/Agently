"""Synthetic transport tests of Host contracts, not semantic-quality evidence."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
)

from agently.builtins.plugins.AgentExecution.modules import long_content_flow as content
from agently.builtins.plugins.AgentExecution.modules.execution import AgentExecution
from agently.builtins.plugins.AgentExecution.modules.long_content_format import (
    normalize_chapter,
)


def plan(count: int = 2) -> dict[str, Any]:
    return {
        "document_title": "Document",
        "part_plan": [
            {"part_title": f"Chapter {index + 1}", "part_brief": f"Planned scope {index + 1}"} for index in range(count)
        ],
    }


@pytest.mark.asyncio
async def test_scope_summary_isolation_complete_directory_and_no_staging_selection(tmp_path):
    body = "# Chapter 1\n\nLead.\n\n## Detail\n\nText.\n\n### Nested\n\nCondition.\n"
    memory = "Actual conditional statement. " * 300
    agent = create_execution_agent(
        tmp_path, "contract", [plan(), {"body": body}, {"summary": memory}, {"body": "Final body."}]
    )
    run = (
        agent.create_execution("long_content")
        .input("Write a complete document.")
        .instruct("Global writing requirement.")
        .info({"source_fact": "Original fact."})
    )
    original = dict(run.prompt_snapshot)
    assert isinstance(run, AgentExecution)
    result = await run.async_get_data()
    requests = ScriptedExecutionRequester.requests
    assert len(requests) == 4
    first, summary, last = requests[1:]
    assert first["input"] == "Write a complete document."
    assert first["info"]["current_section"] == plan()["part_plan"][0]
    assert "Original fact." in str(first["info"])
    assert first["info"]["previous_tail"] == []
    assert first["info"]["已写章节的实际摘要"] == []
    assert "execution_stage_input" not in first
    assert "Global writing requirement." in str(first["instruct"])
    assert summary["instruct"] == content._SUMMARY_INSTRUCT
    assert summary["input"] == normalize_chapter(body, "Chapter 1")[0]
    assert "Global writing requirement." not in json.dumps(summary, default=str)
    assert summary["info"] == {
        "chapter_title": "Chapter 1",
        "sections": [{"title": "Detail", "level": 3}, {"title": "Nested", "level": 4}],
    }
    assert last["info"]["待写章节的计划"] == []
    assert last["info"]["已写章节的实际摘要"] == [
        {"chapter_title": "Chapter 1", "sections": summary["info"]["sections"], "summary": memory}
    ]
    assert "Planned scope 1" not in json.dumps(last["info"])
    assert run.prompt_snapshot == original
    assert "## Chapter 1\n\nLead." in result
    assert len(run._producer_state["content"]["drafts"]) == 2
    assert all("body" not in item for item in run._producer_state["content"]["drafts"])
    assert run._producer_state["content"]["drafts"][-1]["summary"] is None
    assert run.task_workspace.list_files() == ()
    assert run._long_output_meta == {}


@pytest.mark.asyncio
async def test_cumulative_budget_stops_before_missing_summary_dispatch(tmp_path):
    from agently.core import AgentExecutionLimitExceeded

    agent = create_execution_agent(tmp_path, "budget", [plan(), {"body": "First."}, {"summary": "First summary."}])
    run = agent.create_execution("long_content", limits={"max_model_requests": 2}).input("Write.")
    with pytest.raises(AgentExecutionLimitExceeded):
        await run.async_get_data()
    assert ScriptedExecutionRequester.model_dispatches == 2
    assert run.result is None


@pytest.mark.asyncio
async def test_readback_tampering_blocks_before_summary(tmp_path, monkeypatch):
    agent = create_execution_agent(tmp_path, "integrity", [plan(), {"body": "First."}])
    run = agent.create_execution("long_content").input("Write.")
    original = content._LongContentExecutionRuntime.store_body

    async def tamper(self, body, index):
        ref = await original(self, body, index)
        path = self.execution.task_workspace.resolve_file_path(ref["path"])
        path.write_text("tampered", encoding="utf-8")
        return ref

    monkeypatch.setattr(content._LongContentExecutionRuntime, "store_body", tamper)
    with pytest.raises(ValueError, match="readback identity"):
        await run.async_get_data()
    assert ScriptedExecutionRequester.model_dispatches == 2


@pytest.mark.parametrize(
    "body",
    [
        "# Chapter\n\nBody.\n\n## Detail\n\nText.\n",
        "Chapter\n=======\n\nBody.\n\nDetail\n------\n\nText.\n",
        "# Chapter\n\n# Chapter\n\nBody.\n",
        "# A\n\n###### Deep\n\nText.\n",
        "```markdown\n# Code title\n```\n\n> # Quoted\n\nBody.\n",
    ],
)
def test_formatting_is_idempotent_and_never_rewrites_paragraphs(body):
    normalized, directory, warnings = normalize_chapter(body, "Chapter")
    assert normalize_chapter(normalized, "Chapter")[0] == normalized
    assert "Body." in normalized if "Body." in body else "Text." in normalized
    if "# Code title" in body:
        assert normalized == body and directory == []
    if "###### Deep" in body:
        assert "heading_depth_not_representable; preserved_original_levels" in warnings
