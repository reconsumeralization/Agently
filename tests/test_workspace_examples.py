from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOTS = (
    ROOT / "examples" / "task_workspace",
    ROOT / "examples" / "record_store",
    ROOT / "examples" / "agent_task",
    ROOT / "examples" / "agent_auto_orchestration",
    ROOT / "examples" / "blocks",
    ROOT / "examples" / "skills_executor",
)


def _python_examples() -> list[Path]:
    return sorted(path for root in EXAMPLE_ROOTS for path in root.rglob("*.py"))


def test_context_and_storage_examples_are_valid_python() -> None:
    for path in _python_examples():
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_current_examples_do_not_recommend_removed_owner_apis() -> None:
    offenders: dict[str, list[str]] = {}
    removed = (
        "Agently.create_workspace",
        ".use_workspace(",
        "agent.workspace",
        "from agently.core import WorkspaceManager",
        "skill_activation",
        "workspace_operation",
        "configure_skill_capabilities",
    )
    for path in _python_examples():
        source = path.read_text(encoding="utf-8")
        hits = [item for item in removed if item in source]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert offenders == {}


def test_examples_cover_each_new_owner_boundary() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in _python_examples())

    assert "TaskContext" in sources
    assert "ContextReader" in sources or ".reader(" in sources
    assert "TaskWorkspace" in sources
    assert "RecordStore" in sources
    assert "SkillLibrary" in sources or "skills_executor.install_skills" in sources
    assert "use_task_workspace" in sources
    assert "use_record_store" in sources
    assert '"kind": "context_read"' in sources
