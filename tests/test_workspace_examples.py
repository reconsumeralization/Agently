from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOTS = (
    ROOT / "examples" / "workspace",
    ROOT / "examples" / "agent_task",
    ROOT / "examples" / "agent_auto_orchestration",
)


def _python_examples() -> list[Path]:
    return sorted(path for root in EXAMPLE_ROOTS for path in root.rglob("*.py"))


def test_workspace_related_examples_are_valid_python() -> None:
    for path in _python_examples():
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_workspace_related_examples_do_not_use_private_state_as_a_workspace_root() -> None:
    offenders: list[str] = []
    for path in _python_examples():
        source = path.read_text(encoding="utf-8")
        if ".agently/tasks" in source:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_workspace_examples_use_the_module_class_and_current_direct_root_api() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in _python_examples())
    assert "LazyWorkspace" not in sources
    assert "from agently.core.workspace" not in sources
    assert "Agently.create_workspace" in sources
    assert ".agently/files" in sources
