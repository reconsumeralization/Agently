from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _current_owner_guidance() -> str:
    paths = (
        "docs/en/requests/workspace.md",
        "docs/cn/requests/workspace.md",
        "docs/en/requests/session-memory.md",
        "docs/cn/requests/session-memory.md",
        "docs/en/development/skills-executor.md",
        "docs/cn/development/skills-executor.md",
        "docs/en/reference/blocks-lifecycle.md",
        "docs/cn/reference/blocks-lifecycle.md",
    )
    return "\n".join(_read(path) for path in paths)


def test_context_file_and_record_docs_define_non_overlapping_owners() -> None:
    for path in ("docs/en/requests/workspace.md", "docs/cn/requests/workspace.md"):
        document = _read(path)
        assert "TaskContext" in document
        assert "ContextReader" in document
        assert "ContextPackage" in document
        assert "TaskWorkspace" in document
        assert "RecordStore" in document
        assert ".agently/task_workspaces/<agent-id>" in document
        assert ".agently/records/records.db" in document
        assert "record_store_recovery" in document
        assert "source_kinds" in document


def test_context_docs_keep_index_internal_and_use_descriptor_source_ports() -> None:
    english = _read("docs/en/requests/workspace.md")
    chinese = _read("docs/cn/requests/workspace.md")
    english_flat = " ".join(english.split())
    chinese_flat = " ".join(chinese.split())

    assert "internal `ContextIndex`" in english_flat
    assert "TaskContext owns" in english_flat
    assert "async_enumerate_descriptors" in english_flat
    assert "async_read_exact" in english_flat
    assert "not a closed enumeration" in english_flat
    assert "内部 `ContextIndex`" in chinese_flat
    assert "TaskContext 负责" in chinese_flat
    assert "async_enumerate_descriptors" in chinese_flat
    assert "async_read_exact" in chinese_flat
    assert "不是封闭枚举" in chinese_flat
    for stale_surface in ("ContextSourceCandidateWindow", "async_list_candidates"):
        assert stale_surface not in english
        assert stale_surface not in chinese


def test_context_docs_define_strict_document_and_image_admission() -> None:
    english = _read("docs/en/requests/workspace.md")
    chinese = _read("docs/cn/requests/workspace.md")

    assert "context_representation=parsed_text" in english
    assert "descriptor and exact read" in english
    assert "conflicting type signals" in english.lower()
    assert "OCR" in english
    assert "context_representation=parsed_text" in chinese
    assert "descriptor 与 exact read" in chinese
    assert "类型信号冲突" in chinese
    assert "OCR" in chinese


def test_session_memory_docs_route_task_recall_through_task_context_source() -> None:
    english = _read("docs/en/requests/session-memory.md")
    chinese = _read("docs/cn/requests/session-memory.md")

    assert "SessionMemory remains" in english
    assert "TaskContext source" in english
    assert "ContextReader" in english
    assert "SessionMemory 仍负责" in chinese
    assert "TaskContext source" in chinese
    assert "ContextReader" in chinese


def test_task_workspace_docs_stage_then_promote_terminal_artifacts() -> None:
    english = _read("docs/en/requests/workspace.md")
    chinese = _read("docs/cn/requests/workspace.md")
    english_flat = " ".join(english.split())
    chinese_flat = " ".join(chinese.split())

    assert "staged candidate" in english_flat
    assert "verifier acceptance" in english_flat
    assert "atomic promotion" in english_flat
    assert "暂存候选" in chinese_flat
    assert "verifier 验收" in chinese_flat
    assert "原子提升" in chinese_flat


def test_session_memory_docs_bind_record_store_without_task_workspace_dependency() -> None:
    for path in ("docs/en/requests/session-memory.md", "docs/cn/requests/session-memory.md"):
        document = _read(path)
        assert "use_record_store" in document
        assert "AgentlyMemory" in document
        assert "record_store.vector_index.enabled" in document
        assert ".agently/records/records.db" in document
        assert "use_workspace" not in document
        assert "create_workspace" not in document


def test_skills_docs_define_direct_reconnection_and_thin_facade() -> None:
    for path in (
        "docs/en/development/skills-executor.md",
        "docs/cn/development/skills-executor.md",
    ):
        document = _read(path)
        assert "SkillLibrary" in document
        assert "AgentExecution" in document
        assert "TaskContext" in document
        assert "Agently.skills_executor" in document
        assert "run_skills_task" in document
        assert "result-shaped adapter" in document
        assert "skill_activation" not in document
        assert "SkillsManager" not in document


def test_blocks_docs_expose_context_read_and_reject_removed_owners() -> None:
    for path in (
        "docs/en/reference/blocks-lifecycle.md",
        "docs/cn/reference/blocks-lifecycle.md",
    ):
        document = _read(path)
        assert "context_read" in document
        assert "caller-bound" in document or "调用方绑定" in document
        assert "TaskWorkspace" in document
        assert "RecordStore" in document
        assert "There is no `skill_activation`" in document or "不存在 `skill_activation`" in document
        assert "workspace_operation" in document


def test_current_owner_guidance_does_not_recommend_removed_apis() -> None:
    guidance = _current_owner_guidance()

    for removed in (
        "Agently.create_workspace",
        ".use_workspace(",
        "agent.workspace",
        "WorkspaceManager",
        "ContextBuilder",
        "configure_skill_capabilities",
    ):
        assert removed not in guidance


def test_release_notes_and_compatibility_name_breaking_owner_split() -> None:
    documents = (
        _read("docs/en/development/release-notes-4.1.4.2.md"),
        _read("docs/cn/development/release-notes-4.1.4.2.md"),
        _read("compatibility/in-development.json"),
    )
    for document in documents:
        assert "4.1.4.2" in document
        assert "TaskContext" in document
        assert "TaskWorkspace" in document
        assert "RecordStore" in document
        assert "SkillLibrary" in document
        assert "record_store_recovery" in document


def test_public_docs_define_reference_identity_boundaries() -> None:
    english = _read("docs/en/requests/workspace.md")
    chinese = _read("docs/cn/requests/workspace.md")

    assert "[[ref:ref_1]]" in english
    assert "request-local display alias" in english
    assert "locator" in english and "content-version" in english
    assert "[[ref:ref_1]]" in chinese
    assert "请求内显示别名" in chinese
    assert "locator" in chinese and "content-version" in chinese


def test_public_docs_define_fail_closed_terminal_contracts() -> None:
    action_english = " ".join(_read("docs/en/actions/action-runtime.md").split())
    action_chinese = " ".join(_read("docs/cn/actions/action-runtime.md").split())
    task_english = " ".join(_read("docs/en/start/auto-orchestration.md").split())
    task_chinese = " ".join(_read("docs/cn/start/auto-orchestration.md").split())
    coding_english = " ".join(_read("docs/en/development/coding-agents.md").split())
    coding_chinese = " ".join(_read("docs/cn/development/coding-agents.md").split())

    assert "TaskWorkspace readback cannot satisfy a specified Action" in action_english
    assert "unavailable required Action fails closed" in action_english
    assert "TaskWorkspace readback 不能满足指定 Action" in action_chinese
    assert "不可用的 required Action 会 fail closed" in action_chinese
    assert "one semantic terminal-verification request" in task_english
    assert "one versioned terminal-carrier inventory" in task_english
    assert 'artifact_status="partial"' in task_english
    assert "只发起一次语义 verifier 请求" in task_chinese
    assert "一个带版本的 terminal-carrier inventory" in task_chinese
    assert 'artifact_status="partial"' in task_chinese
    assert "one semantic terminal-verifier request" in coding_english
    assert "structured repair contract" in coding_english
    assert "一次语义 terminal verifier request" in coding_chinese
    assert "结构化 repair contract" in coding_chinese
