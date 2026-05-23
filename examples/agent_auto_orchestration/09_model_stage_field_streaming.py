"""Product launch press kit — prompt-only Skill with field streaming + host save.

Run:
    python examples/agent_auto_orchestration/09_model_stage_field_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: a PMM prepares a launch press kit for "DevFlow Code Review AI" — a
market positioning brief plus a launch risk register, compiled and saved.

New-standard Skills model
-------------------------
The old design used two Skill ``model`` stages + a ``save`` action. Under the new
standard the Skill is pure ``SKILL.md`` guidance: ONE prompt-only request returns
the positioning brief and the risk register (shaped by ``semantic_outputs``),
streamed field-by-field; the HOST writes the press kit to disk.

Expected key output from one real DeepSeek run:
    skill status: success
    positioning length: ~1,500-3,500 chars
    risks: 5-8
    press kit saved: .../press_kit_<stamp>.md
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_MD = """\
---
name: Product Launch Press Kit
description: >-
  Generate a product launch press kit from a product brief: a market positioning
  brief and a launch risk register with mitigations. Use for press kit, launch,
  positioning, and press release requests.
keywords: [press kit, launch, positioning, press release, risk register]
---

# Product Launch Press Kit

You are a product marketing lead + technical risk analyst. Given a product brief,
produce a launch press kit in ONE pass.

## Positioning brief
- A 2-3 paragraph market landscape summary.
- One crisp positioning statement.
- 3-5 differentiators vs the listed competitors, grounded in the brief's
  features, pricing, and beta results.

## Launch risk register
- 5-8 risks, each with severity (low/medium/high), category (e.g. market,
  technical, compliance, GTM), and a one-sentence description.
- For the top 3 risks, add a concrete mitigation.

Be specific to the product brief; do not invent features, pricing, or metrics
that are not present.
"""

PRODUCT_BRIEF = """Product: DevFlow Code Review AI
Category: AI-Powered Developer Tools
Launch Date: 2026-07-15
Pricing: Free for public repos, $29/dev/month for private repos, Enterprise at $49/dev/month

Key Features:
1. Real-time PR annotation — flags bugs, security issues, performance problems, and style violations
2. Cross-repository security scanning — detects vulnerable patterns across the entire codebase
3. Team-specific style learning — adapts to your team's conventions from review history
4. Automated fix suggestions — generates before/after diffs for common issues
5. Compliance report generation — SOC 2, GDPR, HIPAA audit-ready reports

Target Audience: Mid-market to enterprise engineering teams (50-2000 developers)
Primary Vertical: B2B SaaS companies with compliance requirements

Competitors:
- CodeRabbit ($15/dev/mo) — general-purpose, weaker compliance features
- GitHub Copilot Code Review — tied to GitHub ecosystem
- Amazon CodeGuru Reviewer — Java/Python only, AWS-centric
- Snyk Code ($25/dev/mo) — security-focused, no style or performance analysis

Beta Results (48 teams, 6 weeks): 47% reduction in time-to-merge, 31% fewer production bugs, NPS 72.
"""


def install_skill() -> str:
    skill_src = Path(tempfile.mkdtemp(prefix="agently_skill_src_")) / "product-launch-press-kit"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


def save_press_kit(out_dir: Path, positioning: str, risks: list[dict[str, Any]]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"press_kit_{stamp}.md"
    risk_lines = "\n".join(
        f"- [{r.get('severity', '—')}/{r.get('category', '—')}] {r.get('description', '')}" for r in risks
    )
    path.write_text(f"# Launch Press Kit\n\n## Positioning\n\n{positioning}\n\n## Risk Register\n\n{risk_lines}\n", encoding="utf-8")
    return path


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    skill_id = install_skill()
    agent = Agently.create_agent("press-kit-pmm")

    divider = "=" * 60
    print(divider)
    print("Product Launch Press Kit — prompt-only Skill")
    print(divider)
    print("Generating press kit for DevFlow Code Review AI...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_complete") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    execution = await agent.async_run_skills_task(
        f"Build a launch press kit from this product brief:\n\n{PRODUCT_BRIEF}",
        skills=[skill_id],
        mode="required",
        semantic_outputs={
            "positioning_text": (str, "Market landscape, positioning statement, and differentiators", True),
            "risks": (
                [{
                    "severity": (str, "low, medium, or high", True),
                    "category": (str, "Risk category", True),
                    "description": (str, "One-sentence risk description", True),
                    "mitigation": (str, "Mitigation (for top risks)"),
                }],
                "Launch risk register (5-8 risks)",
                True,
            ),
        },
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    positioning = str(result.get("positioning_text", ""))
    risks = result.get("risks", []) or []

    print(f"\n  positioning: {len(positioning):,} chars")
    print(f"  risks: {len(risks)}")
    for r in risks[:4]:
        print(f"    · [{r.get('severity', '—')}] {r.get('category', '—')}: {str(r.get('description', ''))[:80]}")

    out_path = save_press_kit(Path(tempfile.mkdtemp(prefix="agently_presskit_")), positioning, risks)

    print(f"\nskill status: {execution.status}")
    print(f"positioning length: {len(positioning):,} chars")
    print(f"risks: {len(risks)}")
    print(f"press kit saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
