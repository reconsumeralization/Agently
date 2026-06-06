"""Release notes generator — prompt-only Skill with field-level streaming.

Run:
    python examples/agent_auto_orchestration/01_skills_dag_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: A DevOps engineer triggers release notes generation for v2.5.0. The
commit log below is mocked business data — what a real CI/CD system would pipe
in. The model call is NOT mocked.

New-standard Skills model
-------------------------
The capability is a single standard ``SKILL.md`` (guidance only — no
``skill.yaml``, no stages, no embedded actions). Running it is ONE prompt-only
model request that returns the full structured release notes shaped by
``output``. We stream field-level deltas as the model fills each
section, then the HOST writes the published file to disk (the only side effect).

Expected key output from one real DeepSeek run:
    skill status: success
    has_features=True
    has_fixes=True
    announcement_ready=True
    release notes written: .../release_notes_v2.5.0.md
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

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
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "release-notes-generator"


def install_skill() -> str:
    skill_src = SKILL_SOURCE
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


# ═══════════════════════════════════════════════════════════════════════════════
# Host orchestration: run the Skill (prompt-only) + write the published file
# ═══════════════════════════════════════════════════════════════════════════════

OUTPUT_SCHEMA: dict[str, Any] = {
    "feature_highlights": ([str], "User-friendly feature descriptions"),
    "fix_summaries": ([str], "User-friendly fix descriptions"),
    "breaking_notes": ([str], "Breaking changes with migration steps"),
    "security_notes": ([str], "Security fix summaries"),
    "title": (str, "Release announcement title including version", True),
    "overview": (str, "Overview paragraph summarizing the release", True),
    "body": (str, "Full announcement body with markdown sections", True),
    "ready": (bool, "True if the release notes are complete and ready", True),
    "quality_notes": (str, "Final QA assessment", True),
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    skill_id = install_skill()
    agent = Agently.create_agent("release-notes-demo")

    divider = "=" * 60
    print(divider)
    print("Release Notes Generator — prompt-only Skill")
    print(f"Version:  {MOCK_VERSION}  ·  Date: {MOCK_RELEASE_DATE}  ·  Prev: {MOCK_PREVIOUS_VERSION}")
    print(f"Team:     {MOCK_TEAM}")
    print(f"Commits:  {MOCK_COMMITS.count(chr(10))} lines of commit log (mocked CI/CD data)")
    print(divider)
    print("Running release notes skill (streaming sections)...\n")

    streamed_fields: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_complete"):
            if path not in streamed_fields:
                streamed_fields.add(str(path))
                print(f"  [section ready] {path}")

    task = (
        f"Generate release notes for {MOCK_VERSION} (released {MOCK_RELEASE_DATE}, "
        f"previous {MOCK_PREVIOUS_VERSION}, team {MOCK_TEAM}).\n\nCommit log:\n{MOCK_COMMITS}"
    )

    execution = await agent.async_run_skills_task(
        task,
        skills=[skill_id],
        mode="required",
        output=OUTPUT_SCHEMA,
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    features = result.get("feature_highlights", []) or []
    fixes = result.get("fix_summaries", []) or []
    breaking = result.get("breaking_notes", []) or []
    security = result.get("security_notes", []) or []

    print(f"\n{divider}\n发布说明交付清单\n{divider}")
    print(f"  功能亮点: {len(features)} 项")
    for f in features[:3]:
        print(f"    · {str(f)[:100]}")
    print(f"  缺陷修复: {len(fixes)} 项")
    for f in fixes[:3]:
        print(f"    · {str(f)[:100]}")
    if breaking:
        print(f"  破坏性变更: {len(breaking)} 项")
        for b in breaking:
            print(f"    ⚠ {str(b)[:100]}")
    if security:
        print(f"  安全修复: {len(security)} 项")
    print(f"\n  公告标题: {result.get('title', '—')}")
    overview = str(result.get("overview", ""))
    if overview:
        print(f"  概述: {overview[:150]}...")

    # ── Host side effect: publish the release notes file ──
    out_dir = RUNTIME_ROOT / "published"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"release_notes_{MOCK_VERSION}.md"
    out_path.write_text(str(result.get("body") or result.get("overview") or ""), encoding="utf-8")

    print(f"\n{divider}\n质检结果\n{divider}")
    print(f"  可发布: {result.get('ready')}")
    print(f"  质检备注: {str(result.get('quality_notes', '—'))[:200]}")

    print(f"\nskill status: {execution.status}")
    print(f"has_features={bool(features)}")
    print(f"has_fixes={bool(fixes)}")
    print(f"announcement_ready={bool(result.get('ready'))}")
    print(f"release notes written: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
