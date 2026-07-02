"""Market research brief — prompt-only Skill with field-level streaming.

Run:
    python examples/agent_auto_orchestration/03_actions_skills_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: A product manager requests a market research brief for an "AI code
review assistant" feature idea. The mocked business data below represents what a
real product analytics platform and competitor tracking system would provide;
it is injected as task context. The model generates all analysis content.

New-standard Skills model
-------------------------
The capability is a single standard ``SKILL.md`` (guidance only). One prompt-only
request consumes the business context and returns the full structured brief
shaped by ``output`` (landscape, competitor profiles, opportunities,
executive summary). The HOST writes the brief file to disk.

Expected key output from one real DeepSeek run:
    skill status: success
    has_competitors=True
    has_opportunities=True
    brief_complete=True
    brief written: .../market_research_brief.md
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

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "market_research"

# ═══════════════════════════════════════════════════════════════════════════════
# Mock business data — product analytics, internal strategy, market signals
# ═══════════════════════════════════════════════════════════════════════════════

MOCK_FEATURE_TOPIC = "AI-powered code review assistant for enterprise DevOps teams"

MOCK_BUSINESS_CONTEXT = {
    "product": {
        "name": "DevFlow — Enterprise DevOps Platform",
        "current_users": 8500,
        "target_market": "Enterprise DevOps teams (500+ engineers)",
        "current_arr": "$14.2M",
        "growth_rate": "34% YoY",
        "top_3_requested_features": [
            "AI code review (requested by 62% of enterprise accounts)",
            "Multi-cloud deployment pipelines (47%)",
            "Compliance-as-code for SOC2/ISO27001 (41%)",
        ],
    },
    "internal_data": {
        "developer_survey_n": 1200,
        "pain_point_ranking": {
            "code_review_bottleneck": "#1 — 68% report PRs waiting >4 hours for review",
            "context_switching_cost": "#2 — 45% report losing >2h/day to review context switches",
            "inconsistent_standards": "#3 — 38% report style/standard violations in production",
        },
        "beta_test_results": {
            "pilot_users": 24,
            "pilot_weeks": 6,
            "time_saved_per_review": "47% reduction (from 52min to 28min avg)",
            "defect_catch_rate": "+23% vs manual review",
            "nps": 72,
        },
    },
    "market_signals": {
        "github_copilot_code_review": "Public beta launched 2026-04, 120K waitlist",
        "amazon_codeguru": "Security-focused, limited to AWS ecosystem",
        "gitlab_duo": "Integrated into GitLab Ultimate only, $99/user/mo",
        "recent_funding": [
            "CodeRabbit raised $28M Series A (2026-03)",
            "CodeClimate exited to Atlassian for $180M (2026-01)",
        ],
        "analyst_coverage": "Gartner named AI-augmented code review a top strategic technology trend for 2026",
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "market-research-brief"


def install_skill() -> str:
    skill_src = SKILL_SOURCE
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


OUTPUT_SCHEMA: dict[str, Any] = {
    "market_segment": (str, "Target market segment description", True),
    "market_size_estimate": (str, "Estimated market size with growth rate", True),
    "key_trends": ([str], "3-5 key industry trends", True),
    "competitors": (
        [{
            "name": (str, "Competitor name", True),
            "position": (str, "Market position: leader, challenger, or niche", True),
            "strengths": ([str], "Key strengths", True),
            "weaknesses": ([str], "Key weaknesses", True),
        }],
        "3-5 competitor profiles",
        True,
    ),
    "competitive_intensity": (str, "Overall intensity: high, medium, low", True),
    "differentiation_gaps": ([str], "Gaps competitors are not addressing", True),
    "opportunities": (
        [{
            "name": (str, "Opportunity name", True),
            "value_proposition": (str, "Specific value proposition", True),
            "feasibility": (str, "Feasibility: high, medium, low", True),
            "gtm_approach": (str, "Suggested go-to-market approach", True),
        }],
        "3-5 market opportunities",
        True,
    ),
    "top_recommendation": (str, "The single most promising opportunity and rationale", True),
    "executive_summary": (str, "2-3 sentence executive summary for VP Product", True),
    "recommended_next_step": (str, "Concrete next step for the product team", True),
    "brief_complete": (bool, "True if all required sections are present", True),
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    skill_id = install_skill()
    agent = Agently.create_agent("market-research-demo")

    divider = "=" * 60
    print(divider)
    print("Market Research Brief Generator — prompt-only Skill")
    print(f"Feature:    {MOCK_FEATURE_TOPIC}")
    print(f"Product:    {MOCK_BUSINESS_CONTEXT['product']['name']} ({MOCK_BUSINESS_CONTEXT['product']['current_arr']} ARR)")
    print(f"Top pain:   {MOCK_BUSINESS_CONTEXT['internal_data']['pain_point_ranking']['code_review_bottleneck']}")
    print(f"Beta NPS:   {MOCK_BUSINESS_CONTEXT['internal_data']['beta_test_results']['nps']}")
    print(divider)
    print("Running market research skill (streaming sections)...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_complete") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    task = (
        f"Produce a market research brief for: {MOCK_FEATURE_TOPIC}.\n\n"
        f"Business context (JSON):\n{json.dumps(MOCK_BUSINESS_CONTEXT, ensure_ascii=False, indent=2)}"
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
    comp_list = result.get("competitors", []) or []
    opp_list = result.get("opportunities", []) or []

    print(f"\n{divider}\n研究简报交付清单\n{divider}")
    print(f"  市场细分:   {str(result.get('market_segment', '—'))[:120]}")
    print(f"  市场规模:   {result.get('market_size_estimate', '—')}")
    print("  关键趋势:")
    for trend in (result.get("key_trends", []) or [])[:3]:
        print(f"    · {str(trend)[:100]}")
    print(f"\n  竞品数量:   {len(comp_list)}")
    for comp in comp_list:
        print(f"    · {comp.get('name', '—')} — {comp.get('position', '—')}")
    print(f"  竞争强度:   {result.get('competitive_intensity', '—')}")
    print(f"\n  机会数量:   {len(opp_list)}")
    for opp in opp_list:
        print(f"    · {opp.get('name', '—')} [可行性: {opp.get('feasibility', '—')}]")
    print(f"  最优推荐:   {str(result.get('top_recommendation', '—'))[:120]}")

    print(f"\n{divider}\n执行摘要（VP Product 级别）\n{divider}")
    print(str(result.get("executive_summary", "—")))
    print(f"\n  下一步建议: {result.get('recommended_next_step', '—')}")

    # ── Host side effect: write the brief file ──
    out_dir = RUNTIME_ROOT / "published"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "market_research_brief.md"
    out_path.write_text(
        f"# Market Research Brief — {MOCK_FEATURE_TOPIC}\n\n"
        f"{result.get('executive_summary', '')}\n\n"
        f"Top recommendation: {result.get('top_recommendation', '')}\n",
        encoding="utf-8",
    )

    print(f"\nskill status: {execution.status}")
    print(f"has_competitors={len(comp_list) > 0}")
    print(f"has_opportunities={len(opp_list) > 0}")
    print(f"brief_complete={bool(result.get('brief_complete'))}")
    print(f"brief written: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
