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


def test_public_docs_define_stable_reference_and_content_identity_contracts() -> None:
    english = _read("docs/en/requests/workspace.md")
    chinese = _read("docs/cn/requests/workspace.md")

    assert "[[ref:ref_" in english
    assert "request-local display alias" in english
    assert "locator identity" in english
    assert "content-version identity" in english
    assert "reference identity" in english
    assert "External files remain read-only" in english
    assert "private identity metadata" in english

    assert "[[ref:ref_" in chinese
    assert "请求内显示别名" in chinese
    assert "locator 身份" in chinese
    assert "content-version 身份" in chinese
    assert "reference 身份" in chinese
    assert "外部文件仍保持只读" in chinese
    assert "私有身份元数据" in chinese


def test_public_docs_define_fail_closed_agent_task_terminal_contracts() -> None:
    action_english = _read("docs/en/actions/action-runtime.md")
    action_chinese = _read("docs/cn/actions/action-runtime.md")
    task_english = _read("docs/en/start/auto-orchestration.md")
    task_chinese = _read("docs/cn/start/auto-orchestration.md")
    coding_english = _read("docs/en/development/coding-agents.md")
    coding_chinese = _read("docs/cn/development/coding-agents.md")
    action_english_words = " ".join(action_english.split())
    action_chinese_words = " ".join(action_chinese.split())
    task_english_words = " ".join(task_english.split())
    task_chinese_words = " ".join(task_chinese.split())
    coding_english_words = " ".join(coding_english.split())
    coding_chinese_words = " ".join(coding_chinese.split())

    assert "Workspace readback cannot satisfy a specified Action" in action_english_words
    assert "unavailable required Action fails closed" in action_english_words
    assert "Workspace readback 不能满足指定 Action" in action_chinese_words
    assert "不可用的 required Action 会 fail closed" in action_chinese_words

    assert "one semantic terminal-verification request" in task_english_words
    assert "one versioned terminal-carrier inventory" in task_english_words
    assert "separate claim-inventory" in task_english_words
    assert "third occurrence" in task_english_words
    assert 'artifact_status="partial"' in task_english_words
    assert "只发起一次语义 verifier 请求" in task_chinese_words
    assert "一个带版本的 terminal-carrier inventory" in task_chinese_words
    assert "独立的 claim inventory" in task_chinese_words
    assert "第三次出现" in task_chinese_words
    assert 'artifact_status="partial"' in task_chinese_words

    assert "one semantic terminal-verifier request" in coding_english_words
    assert "structured repair contract" in coding_english_words
    assert "three-occurrence convergence" in coding_english_words
    assert "一次语义 terminal verifier request" in coding_chinese_words
    assert "结构化 repair contract" in coding_chinese_words
    assert "三次收敛" in coding_chinese_words


def test_current_guidance_does_not_recommend_durable_position_citations() -> None:
    current_guidance = "\n".join(
        _read(path)
        for path in (
            "docs/en/requests/workspace.md",
            "docs/cn/requests/workspace.md",
            "docs/en/start/auto-orchestration.md",
            "docs/cn/start/auto-orchestration.md",
            "docs/en/development/coding-agents.md",
            "docs/cn/development/coding-agents.md",
        )
    )

    assert "reuse an evidence-ledger `cite_as` such as `e1` as the token id" not in current_guidance
    assert "直接把 evidence ledger 的 `cite_as`（例如 `e1`）作为 token id" not in current_guidance
    assert "[[ref:e1]]" not in current_guidance
