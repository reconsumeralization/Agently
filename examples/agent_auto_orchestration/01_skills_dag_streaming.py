"""Release notes generator — Skills + DAG streaming with real model calls.

Run:
    python examples/agent_auto_orchestration/01_skills_dag_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: A DevOps engineer triggers release notes generation for v2.5.0.
The commit log below is mocked business data — it represents what a real CI/CD
system would pipe in. Model calls are NOT mocked; each stage calls the LLM to
classify, summarize, draft, and compile the release notes.

Expected key output from one real DeepSeek run:
    selected_route=skills
    stream_classify=True
    stream_summarize=True
    stream_draft=True
    stream_compile=True
    has_features=True
    has_fixes=True
    announcement_ready=True
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "release_notes"

# ═══════════════════════════════════════════════════════════════════════════════
# Mock business data — represents what a CI/CD pipeline would emit
# ═══════════════════════════════════════════════════════════════════════════════

MOCK_COMMITS = """
v2.5.0 commit log (2026-05-15 — 2026-05-21, branch: release/2.5.0)

feat: add dark mode support across all dashboard pages (a1b2c3d)
feat: new data export API with CSV and JSON format support (e4f5g6h)
feat: real-time WebSocket notification system for team collaboration (i7j8k9l)
feat: bulk user import from SAML/SSO directory (m0n1o2p)
feat: customizable dashboard widget layout with drag-and-drop (q3r4s5t)

fix: resolve login session timeout on mobile devices (u6v7w8x)
fix: correct chart rendering bug in Safari 18.x (y9z0a1b)
fix: patch XSS vulnerability in user profile markdown rendering (c2d3e4f)
fix: fix race condition in concurrent license assignment (g5h6i7j)
fix: handle empty state gracefully in team member list (k8l9m0n)

docs: update API reference for v2.5 endpoints (o1p2q3r)
docs: add dark mode theming guide for plugin developers (s4t5u6v)
docs: revise deployment checklist for Kubernetes 1.32 (w7x8y9z)

breaking: deprecated /api/v1/analytics — migrate to /api/v2/analytics (a0b1c2d)
breaking: remove legacy cookie-based auth, JWT required (e3f4g5h)

security: upgrade OpenSSL dependency to 3.4.1 (i6j7k8l)
security: enforce MFA for admin-role accounts (m9n0o1p)
"""

MOCK_VERSION = "v2.5.0"
MOCK_RELEASE_DATE = "2026-05-22"
MOCK_PREVIOUS_VERSION = "v2.4.1"
MOCK_TEAM = "Platform Engineering — Release Team Alpha"

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

RELEASE_NOTES_SKILL_YAML = """
skill_id: release-notes-generator
version: 0.2.0
display_name: Release Notes Generator
purpose: >
  Generate professional software release notes from commit logs,
  including feature summaries, fix lists, breaking change guidance,
  and a publishable announcement.
trust_level: local
activation:
  keywords: [release, notes, changelog, version, deploy, 发布, 版本]
requires:
  actions: [classify_changes, summarize_changes, draft_announcement, compile_release_notes]
stages:
  - id: classify_changes
    kind: action
    action: classify_changes
    input:
      commits: "${task}"
  - id: summarize_changes
    kind: action
    action: summarize_changes
    input:
      classified: "${state.classify_changes}"
  - id: validate_summary
    kind: validate
    validation:
      required_state: [classify_changes, summarize_changes]
  - id: draft_announcement
    kind: action
    action: draft_announcement
    input:
      summary: "${state.summarize_changes}"
      version: "${task}"
  - id: compile_release_notes
    kind: action
    action: compile_release_notes
    input:
      classified: "${state.classify_changes}"
      summary: "${state.summarize_changes}"
      announcement: "${state.draft_announcement}"
      version: "${task}"
"""

RELEASE_NOTES_SKILL_MD = """---
name: Release Notes Generator
description: Generate professional software release notes from commits.
keywords:
  - release
  - changelog
  - deploy
  - 发布
  - 版本
---

Generate structured release notes including feature highlights, bug fixes,
known issues, and upgrade instructions.
"""


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_skill() -> Path:
    skill_root = RUNTIME_ROOT / "release-notes-generator"
    _write_text(skill_root / "skill.yaml", RELEASE_NOTES_SKILL_YAML)
    _write_text(skill_root / "SKILL.md", RELEASE_NOTES_SKILL_MD)
    return skill_root


# ═══════════════════════════════════════════════════════════════════════════════
# Action implementations — real model calls, simulated I/O delay before each
# ═══════════════════════════════════════════════════════════════════════════════

async def classify_changes(commits: str = "") -> dict:
    """Classify commits into features, fixes, breaking changes, docs, security."""
    print("  → 正在分类提交变更（模拟从 GitLab API 拉取数据）...")
    await asyncio.sleep(0.3)  # simulated I/O: fetching from GitLab
    print("  → 分类变更类型（模型请求中）...")
    result = await (
        Agently.create_agent("release-classify")
        .input({"commits": commits})
        .instruct(
            "You are a release engineer reviewing commits for an upcoming release. "
            "Classify each change into: feature, fix, breaking_change, docs, security. "
            "Be thorough — security-related fixes should appear in BOTH 'fixes' and 'security'."
        )
        .output({
            "features": ([str], "Feature additions and enhancements"),
            "fixes": ([str], "Bug fixes and corrections"),
            "breaking_changes": ([str], "Breaking or backward-incompatible changes"),
            "docs": ([str], "Documentation updates"),
            "security": ([str], "Security-related changes"),
            "total_count": (int, "Total number of changes classified", True),
        })
        .async_start()
    )
    return result


async def summarize_changes(classified: object = None) -> dict:
    """Summarize classified changes into user-facing release note sections."""
    await asyncio.sleep(0.2)  # simulated I/O: loading previous release notes for comparison
    print("  → 汇总变更描述（模型请求中）...")
    data = classified if isinstance(classified, dict) else {}
    result = await (
        Agently.create_agent("release-summarize")
        .input({
            "features": data.get("features", []),
            "fixes": data.get("fixes", []),
            "breaking_changes": data.get("breaking_changes", []),
            "security": data.get("security", []),
            "previous_version": MOCK_PREVIOUS_VERSION,
        })
        .instruct(
            "You are a technical writer preparing release notes for end users. "
            "For each category, write concise user-friendly summaries. "
            "Features: describe the benefit. Fixes: note what was broken and the resolution. "
            "Breaking changes: MUST include clear migration steps. "
            "Security: note the risk addressed without revealing exploit details."
        )
        .output({
            "feature_highlights": ([str], "User-friendly feature descriptions"),
            "fix_summaries": ([str], "User-friendly fix descriptions"),
            "breaking_notes": ([str], "Breaking change descriptions with migration hints"),
            "security_notes": ([str], "Security fix summaries"),
        })
        .async_start()
    )
    return result


async def draft_announcement(summary: object = None, version: str = "") -> dict:
    """Draft a release announcement from the summarized changes."""
    await asyncio.sleep(0.2)  # simulated I/O: pulling team info and changelog template
    print("  → 撰写发布公告（模型请求中）...")
    data = summary if isinstance(summary, dict) else {}
    result = await (
        Agently.create_agent("release-announce")
        .input({
            "feature_highlights": data.get("feature_highlights", []),
            "fix_summaries": data.get("fix_summaries", []),
            "breaking_notes": data.get("breaking_notes", []),
            "security_notes": data.get("security_notes", []),
            "version": version,
            "release_date": MOCK_RELEASE_DATE,
            "team": MOCK_TEAM,
        })
        .instruct(
            "You are a developer relations writer drafting a release announcement. "
            "Write for an audience of enterprise DevOps teams. "
            "Start with a brief overview. Organize under: Highlights, Bug Fixes, "
            "Breaking Changes, Security. If there are breaking changes, add an "
            "'Upgrade Guide' section. End with a 'Get Started' call to action."
        )
        .output({
            "title": (str, "Release announcement title including version", True),
            "overview": (str, "Overview paragraph summarizing the release", True),
            "body": (str, "Full announcement body with markdown sections", True),
            "upgrade_guide": (str, "Upgrade instructions for breaking changes"),
        })
        .async_start()
    )
    return result


async def compile_release_notes(
    classified: object = None,
    summary: object = None,
    announcement: object = None,
    version: str = "",
) -> dict:
    """Compile and quality-check the final release notes package."""
    await asyncio.sleep(0.2)  # simulated I/O: writing to release registry
    print("  → 编译与质检发布说明（模型请求中）...")
    cl = classified if isinstance(classified, dict) else {}
    sm = summary if isinstance(summary, dict) else {}
    an = announcement if isinstance(announcement, dict) else {}

    result = await (
        Agently.create_agent("release-compile")
        .input({
            "total_changes": cl.get("total_count", 0),
            "feature_count": len(sm.get("feature_highlights", [])),
            "fix_count": len(sm.get("fix_summaries", [])),
            "breaking_count": len(sm.get("breaking_notes", [])),
            "security_count": len(sm.get("security_notes", [])),
            "title": an.get("title", ""),
            "overview": an.get("overview", ""),
        })
        .instruct(
            "You are a release manager doing final QA on release notes. "
            "Verify completeness and coherence. Flag any missing sections. "
            "Write a brief quality assessment."
        )
        .output({
            "ready": (bool, "True if release notes are complete and ready", True),
            "quality_notes": (str, "Quality assessment", True),
            "has_features": (bool, "Features section present", True),
            "has_fixes": (bool, "Fixes section present", True),
            "has_breaking": (bool, "Breaking changes section present", True),
            "has_security": (bool, "Security section present", True),
        })
        .async_start()
    )
    return result


def register_actions(agent) -> None:
    agent.register_action(
        name="classify_changes",
        desc="Classify commits into feature/fix/breaking/doc/security using AI.",
        kwargs={"commits": (str, "Commit log text.")},
        func=classify_changes,
    )
    agent.register_action(
        name="summarize_changes",
        desc="Summarize classified changes into user-friendly release note entries.",
        kwargs={"classified": (object, "Output from classify_changes.")},
        func=summarize_changes,
    )
    agent.register_action(
        name="draft_announcement",
        desc="Draft a professional release announcement from summaries.",
        kwargs={
            "summary": (object, "Output from summarize_changes."),
            "version": (str, "Version string."),
        },
        func=draft_announcement,
    )
    agent.register_action(
        name="compile_release_notes",
        desc="Compile and quality-check the final release notes.",
        kwargs={
            "classified": (object, "Output from classify_changes."),
            "summary": (object, "Output from summarize_changes."),
            "announcement": (object, "Output from draft_announcement."),
            "version": (str, "Version string."),
        },
        func=compile_release_notes,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main demo
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE_NARRATIVE = {
    "classify_changes": "变更分类完成",
    "summarize_changes": "变更描述汇总完成",
    "validate_summary": "汇总校验通过",
    "draft_announcement": "发布公告撰写完成",
    "compile_release_notes": "发布说明质检完成",
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    Agently.settings.set("skills.registry.root", str(RUNTIME_ROOT / "registry"))
    skill_root = prepare_skill()
    Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)

    agent = Agently.create_agent("release-notes-demo")
    register_actions(agent)

    divider = "=" * 60
    print(divider)
    print("Release Notes Generator — Skills + DAG Streaming")
    print(f"Version:  {MOCK_VERSION}")
    print(f"Date:     {MOCK_RELEASE_DATE}")
    print(f"Team:     {MOCK_TEAM}")
    print(f"Previous: {MOCK_PREVIOUS_VERSION}")
    print(f"Commits:  {MOCK_COMMITS.count(chr(10))} lines of commit log (mocked CI/CD data)")
    print(divider)
    print("Starting release notes pipeline...\n")

    await asyncio.sleep(0.3)  # simulated: agent startup

    execution = (
        agent
        .use_skills(["release-notes-generator"], mode="required")
        .input(MOCK_COMMITS)
        .create_execution()
    )

    stream_events: list[str] = []
    stage_step = 0

    async for item in execution.get_async_generator(type="instant"):
        if not item.is_complete:
            continue
        path = item.path
        stream_events.append(path)

        if path == "route.selected":
            route = (item.value or {}).get("selected_route", "skills")
            print(f"  [route] selected: {route}")

        elif path.startswith("skills.stages."):
            stage_id = path.split(".")[-1]
            stage_step += 1
            narrative = _STAGE_NARRATIVE.get(stage_id, stage_id)
            print(f"  [{stage_step}] {narrative}")

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    classified = data.get("classify_changes") or {}
    summary = data.get("summarize_changes") or {}
    announcement = data.get("draft_announcement") or {}
    compiled = data.get("compile_release_notes") or {}

    # Deliverable checklist
    print(f"\n{divider}")
    print("发布说明交付清单")
    print(divider)

    features = summary.get("feature_highlights", [])
    fixes = summary.get("fix_summaries", [])
    breaking = summary.get("breaking_notes", [])
    security = summary.get("security_notes", [])

    print(f"  总变更数: {classified.get('total_count', 0)}")
    print(f"  功能亮点: {len(features)} 项")
    for f in features[:3]:
        print(f"    · {f[:100]}")
    if len(features) > 3:
        print(f"    ... 另 {len(features) - 3} 项")

    print(f"  缺陷修复: {len(fixes)} 项")
    for f in fixes[:3]:
        print(f"    · {f[:100]}")
    if len(fixes) > 3:
        print(f"    ... 另 {len(fixes) - 3} 项")

    if breaking:
        print(f"  破坏性变更: {len(breaking)} 项")
        for b in breaking:
            print(f"    ⚠ {b[:100]}")

    if security:
        print(f"  安全修复: {len(security)} 项")

    print(f"\n  公告标题: {announcement.get('title', '—')}")
    overview = announcement.get("overview", "")
    if overview:
        print(f"  概述: {overview[:150]}...")

    print(f"\n{divider}")
    print("质检结果")
    print(divider)
    print(f"  可发布: {compiled.get('ready')}")
    print(f"  质检备注: {compiled.get('quality_notes', '—')[:200]}")

    selected_route = meta.get("route_plan", {}).get("selected_route", "")
    print(f"\nselected_route={selected_route}")
    print(f"stream_classify={'skills.stages.classify_changes' in stream_events}")
    print(f"stream_summarize={'skills.stages.summarize_changes' in stream_events}")
    print(f"stream_draft={'skills.stages.draft_announcement' in stream_events}")
    print(f"stream_compile={'skills.stages.compile_release_notes' in stream_events}")
    print(f"has_features={compiled.get('has_features')}")
    print(f"has_fixes={compiled.get('has_fixes')}")
    print(f"announcement_ready={compiled.get('ready')}")


if __name__ == "__main__":
    asyncio.run(main())
