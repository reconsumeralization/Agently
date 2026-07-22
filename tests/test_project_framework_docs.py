from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/start/project-framework.md",
        "docs/cn/start/project-framework.md",
    ),
)
def test_project_framework_docs_publish_topology_first_template_contract(path: str) -> None:
    document = _read(path)

    for required in (
        "TOPOLOGY.md",
        "FastAPI",
        "FastMCP",
        "Dynamic Task",
        "skills/agently/assets/project-template",
    ):
        assert required in document

    assert "actions/mcp.py" not in document
    assert "attach_mcp" not in document


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/development/coding-agents.md",
        "docs/cn/development/coding-agents.md",
    ),
)
def test_coding_agent_docs_publish_seven_skill_catalog(path: str) -> None:
    document = _read(path)

    assert "`agently-design`" in document
    assert "7 skills" in document or "7 个 skills" in document
