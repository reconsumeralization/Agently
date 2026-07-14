from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_workspace_public_docs_describe_direct_root_and_lazy_private_state() -> None:
    english = _read("docs/en/requests/workspace.md")
    chinese = _read("docs/cn/requests/workspace.md")

    for document in (english, chinese):
        assert ".agently/files/" in document
        assert "workspace.db" in document
        assert "workspace_recovery" in document
        assert "terminal" in document.lower() or "终态" in document
        assert "cleanup" in document.lower() or "清理" in document


def test_session_memory_docs_do_not_treat_agently_private_state_as_workspace_root() -> None:
    english = _read("docs/en/requests/session-memory.md")
    chinese = _read("docs/cn/requests/session-memory.md")

    for document in (english, chinese):
        assert 'create_workspace("./support-memory")' in document
        assert 'use_workspace("./support-memory")' in document
        assert 'create_workspace("./.agently/support-memory")' not in document
        assert 'use_workspace("./.agently/support-memory")' not in document
        assert "vector_index.enabled" in document
        assert "workspace.db" in document


def test_auto_orchestration_docs_describe_memory_first_process_state() -> None:
    english = _read("docs/en/start/auto-orchestration.md")
    chinese = _read("docs/cn/start/auto-orchestration.md")

    assert "process state stays in memory and runtime logs by default" in english
    assert '"workspace_recovery": True' in english
    assert "persists a resumable snapshot after every completed iteration" not in english
    assert "write Workspace evidence" not in english

    assert "过程状态默认只保留在内存和运行日志中" in chinese
    assert '"workspace_recovery": True' in chinese
    assert "每次迭代完成后都会持久化一份可恢复快照" not in chinese
    assert "写入 Workspace evidence" not in chinese


def test_release_notes_and_compatibility_name_the_breaking_workspace_line() -> None:
    english = _read("docs/en/development/release-notes-4.1.4.2.md")
    chinese = _read("docs/cn/development/release-notes-4.1.4.2.md")
    compatibility = _read("compatibility/in-development.json")

    for document in (english, chinese, compatibility):
        assert "4.1.4.2" in document
        assert ".agently" in document
        assert "workspace_recovery" in document

    assert "reflection records are Workspace evidence" not in compatibility
    assert "The task strategy writes Workspace checkpoints" not in compatibility
    assert "AgentTask planning, observations, verification" in compatibility
    assert "stay in memory and runtime logs by default" in compatibility


def test_current_guidance_never_uses_private_state_as_the_workspace_root() -> None:
    current_guidance = "\n".join(
        _read(path)
        for path in (
            "docs/en/requests/workspace.md",
            "docs/cn/requests/workspace.md",
            "docs/en/requests/session-memory.md",
            "docs/cn/requests/session-memory.md",
            "docs/en/start/auto-orchestration.md",
            "docs/cn/start/auto-orchestration.md",
            "docs/en/development/skills-executor.md",
            "docs/cn/development/skills-executor.md",
        )
    )

    assert ".agently/tasks" not in current_guidance
    assert ".agently/support-memory" not in current_guidance
