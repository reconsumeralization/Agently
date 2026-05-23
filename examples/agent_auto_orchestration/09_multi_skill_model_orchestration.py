"""Multi-skill model composition — prompt-only Skills selected & synthesized.

Run:
    python examples/agent_auto_orchestration/09_multi_skill_model_orchestration.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: prepare a product launch for "DevFlow Code Review AI". Three standard
Skills are available — positioning, risk assessment, and launch comms.

New-standard Skills model
-------------------------
This is the multi-skill case. Three standard ``SKILL.md`` Skills are installed.
With ``mode="model_decision"`` the planner asks the model which Skills to select
and in what order (route arbitration over the decision cards). The executor then
runs ONE prompt-only request that injects the FULL guidance of every selected
Skill and instructs the model to synthesize them — no predetermined sequence,
no per-Skill DAG. ``semantic_outputs`` shapes the combined launch package.

Expected key output from one real DeepSeek run:
    skill status: success
    skills selected: 3  (positioning, risk, comms — model-ordered)
    has positioning / risk_register / press_release
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

# ═══════════════════════════════════════════════════════════════════════════════
# Three standard SKILL.md Skills — guidance only
# ═══════════════════════════════════════════════════════════════════════════════

POSITIONING_SKILL = """\
---
name: Market Positioning Analysis
description: >-
  Analyze product market positioning, competitive landscape, and unique value
  proposition. Use for positioning, market analysis, and differentiation.
keywords: [positioning, market analysis, competitive, value proposition, launch]
---

# Market Positioning Analysis

Given product context, produce: a 2-3 paragraph market landscape, one crisp
positioning statement, and 3-5 differentiators grounded in the product's
features, pricing, and competitive set.
"""

RISK_SKILL = """\
---
name: Launch Risk Assessment
description: >-
  Identify, categorize, and prioritize product launch risks, producing a risk
  matrix (probability × impact) with mitigation strategies. Use for launch risk,
  risk register, and go-to-market risk review.
keywords: [risk, launch risk, risk register, mitigation, go-to-market]
---

# Launch Risk Assessment

Given product context (and positioning if available), produce 5-8 launch risks.
For each: category, probability (low/med/high), impact (low/med/high), and a
mitigation. Call out the top 3 highest priority risks.
"""

COMMS_SKILL = """\
---
name: Launch Communications Generator
description: >-
  Draft launch communications: an announcement / press release, an internal
  support FAQ, and a customer-facing feature summary. Use for launch comms,
  press release, announcement, and messaging.
keywords: [launch comms, press release, announcement, FAQ, messaging]
---

# Launch Communications Generator

Given product context (and positioning + risks if available), draft: an
announcement / press-release blog post, a short internal support FAQ, and a
customer-facing feature summary. Tailor messaging to the positioning and avoid
over-claiming where risks are acknowledged.
"""

SKILLS = {
    "market-positioning-analysis": POSITIONING_SKILL,
    "launch-risk-assessment": RISK_SKILL,
    "launch-communications-generator": COMMS_SKILL,
}

PRODUCT_CONTEXT = {
    "product_name": "DevFlow Code Review AI",
    "product_description": (
        "An AI-powered code review assistant integrating with GitHub, GitLab, and "
        "Bitbucket. Reviews PRs for bugs, security vulnerabilities, performance, and "
        "style; learns team standards; offers automated fix suggestions and SOC 2 / "
        "GDPR compliance reports."
    ),
    "target_market": "Mid-market to enterprise engineering teams (50-2000 devs), B2B SaaS with compliance needs.",
    "pricing_tier": "Free for public repos, $29/dev/mo private, $49/dev/mo Enterprise (SSO + compliance).",
    "launch_date": "2026-07-15",
    "competitive_landscape": [
        "CodeRabbit ($15/dev/mo, general-purpose)",
        "GitHub Copilot Code Review (Copilot Enterprise)",
        "Amazon CodeGuru Reviewer (Java/Python only, AWS-centric)",
        "Snyk Code (security-focused, $25/dev/mo)",
    ],
    "beta_testers": 48,
    "beta_nps": 68,
}


def install_skills() -> list[str]:
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    src_root = Path(tempfile.mkdtemp(prefix="agently_skill_src_"))
    skill_ids: list[str] = []
    for slug, md in SKILLS.items():
        d = src_root / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(md, encoding="utf-8")
        contract = Agently.skills_executor.install_skills(d, trust_level="local", update=True)
        skill_ids.append(str(contract["skill_id"]))
    return skill_ids


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    skill_ids = install_skills()
    agent = Agently.create_agent("launch-orchestrator")

    divider = "=" * 60
    print(divider)
    print("Multi-skill model composition — prompt-only Skills")
    print(f"Installed skills: {', '.join(skill_ids)}")
    print(divider)

    task = (
        f"Prepare for the launch of {PRODUCT_CONTEXT['product_name']}. Produce a "
        "positioning brief, a launch risk register, and launch communications, "
        "synthesizing all relevant skills.\n\n"
        f"Product context (JSON):\n{json.dumps(PRODUCT_CONTEXT, ensure_ascii=False, indent=2)}"
    )

    # mode="model_decision": the planner asks the model which of the three Skills
    # to select and in what order before the single synthesizing request.
    plan = agent.resolve_skills_plan(task, skills=skill_ids, mode="model_decision")
    selected = [str(s.get("skill_id")) for s in plan.get("selected_skills", [])]
    print(f"\nmodel-selected skills ({len(selected)}): {', '.join(selected) or '(none)'}")

    execution = await agent.async_run_skills_task(
        task,
        skills=skill_ids,
        mode="model_decision",
        semantic_outputs={
            "positioning": (str, "Market landscape, positioning statement, differentiators", True),
            "risk_register": (
                [{
                    "risk": (str, "Risk", True),
                    "probability": (str, "low/med/high", True),
                    "impact": (str, "low/med/high", True),
                    "mitigation": (str, "Mitigation", True),
                }],
                "Launch risk register (5-8 risks)",
                True,
            ),
            "press_release": (str, "Announcement / press-release blog post", True),
            "support_faq": (str, "Internal support FAQ", True),
            "feature_summary": (str, "Customer-facing feature summary", True),
        },
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    risks = result.get("risk_register", []) or []
    print(f"\n  positioning: {len(str(result.get('positioning', ''))):,} chars")
    print(f"  risk register: {len(risks)} risks")
    for r in risks[:3]:
        print(f"    · [{r.get('probability', '—')}/{r.get('impact', '—')}] {str(r.get('risk', ''))[:70]}")
    print(f"  press release: {len(str(result.get('press_release', ''))):,} chars")

    print(f"\nskills selected: {len(selected)}")
    print(f"has_positioning={bool(result.get('positioning'))}")
    print(f"has_risk_register={bool(risks)}")
    print(f"has_press_release={bool(result.get('press_release'))}")


if __name__ == "__main__":
    asyncio.run(main())
