"""Market research brief — Actions + Skills streaming with real model calls.

Run:
    python examples/agent_auto_orchestration/03_actions_skills_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: A product manager requests a market research brief for an "AI code
review assistant" feature idea. The mocked business data below represents what
a real product analytics platform and competitor tracking system would provide.
The model generates all analysis content; business context data is pre-seeded.

Expected key output from one real DeepSeek run:
    selected_route=skills
    stream_gather_data=True
    stream_analyze_competitors=True
    stream_identify_opportunities=True
    stream_compile_brief=True
    has_competitors=True
    has_opportunities=True
    brief_complete=True
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

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
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

MARKET_RESEARCH_SKILL_YAML = """
skill_id: market-research-brief
version: 0.2.0
display_name: Market Research Brief Generator
purpose: >
  Generate a structured market research brief from a feature idea,
  including landscape analysis, competitor intelligence, opportunity
  assessment, and go-to-market recommendations.
trust_level: local
activation:
  keywords: [market, research, competitor, feature, product, launch, 市场, 竞品, 分析]
requires:
  actions: [gather_market_data, analyze_competitors, identify_opportunities, compile_research_brief]
stages:
  - id: gather_market_data
    kind: action
    action: gather_market_data
    input:
      topic: "${task}"
  - id: analyze_competitors
    kind: action
    action: analyze_competitors
    input:
      market_data: "${state.gather_market_data}"
      topic: "${task}"
  - id: validate_analysis
    kind: validate
    validation:
      required_state: [gather_market_data, analyze_competitors]
  - id: identify_opportunities
    kind: action
    action: identify_opportunities
    input:
      competitor_analysis: "${state.analyze_competitors}"
      topic: "${task}"
  - id: compile_research_brief
    kind: action
    action: compile_research_brief
    input:
      market_data: "${state.gather_market_data}"
      competitor_analysis: "${state.analyze_competitors}"
      opportunities: "${state.identify_opportunities}"
      topic: "${task}"
"""

MARKET_RESEARCH_SKILL_MD = """---
name: Market Research Brief
description: Generate a structured market research brief from a feature idea.
keywords:
  - market
  - research
  - competitor
  - product
  - 市场
  - 竞品
---

Generate a comprehensive market research brief including landscape analysis,
competitor profiles, opportunity identification, and strategic recommendations.
"""


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_skill() -> Path:
    skill_root = RUNTIME_ROOT / "market-research-brief"
    _write_text(skill_root / "skill.yaml", MARKET_RESEARCH_SKILL_YAML)
    _write_text(skill_root / "SKILL.md", MARKET_RESEARCH_SKILL_MD)
    return skill_root


# ═══════════════════════════════════════════════════════════════════════════════
# Action implementations — real model calls, simulated I/O delay before each
# ═══════════════════════════════════════════════════════════════════════════════


async def gather_market_data(topic: str = "") -> dict:
    """Gather market landscape data via model call, seeded with internal data."""
    print("  → 加载内部产品数据与市场信号（模拟从 Amplitude/G2/Crunchbase 拉取）...")
    await asyncio.sleep(0.4)  # simulated I/O: fetching from analytics, review sites, funding DBs
    print("  → 汇总市场格局数据（模型请求中）...")
    import json
    context = json.dumps(MOCK_BUSINESS_CONTEXT, ensure_ascii=False, indent=2)
    result = await (
        Agently.create_agent("research-gather")
        .input({
            "topic": topic,
            "business_context": context,
        })
        .instruct(
            "You are a market research analyst. Use the provided business context data "
            "(internal survey results, beta test metrics, market signals) to identify: "
            "the target market segment, estimated market size and growth rate, "
            "key industry trends, the primary user persona, and jobs-to-be-done. "
            "Incorporate the internal data points — they are real, not estimates. "
            "Be specific and reference the provided numbers where relevant."
        )
        .output({
            "market_segment": (str, "Target market segment description", True),
            "market_size_estimate": (str, "Estimated market size with growth rate", True),
            "key_trends": ([str], "3-5 key industry trends", True),
            "user_persona": (str, "Primary user persona with demographics", True),
            "jobs_to_be_done": ([str], "2-4 jobs-to-be-done this feature addresses", True),
            "internal_validation": (str, "How internal data supports or contradicts market signals", True),
        })
        .async_start()
    )
    return result


async def analyze_competitors(market_data: object = None, topic: str = "") -> dict:
    """Analyze competitive landscape via model call."""
    await asyncio.sleep(0.3)  # simulated I/O: scraping competitor websites, product pages
    print("  → 分析竞品格局（模型请求中）...")
    data = market_data if isinstance(market_data, dict) else {}
    import json
    signals = json.dumps(MOCK_BUSINESS_CONTEXT["market_signals"], ensure_ascii=False, indent=2)
    result = await (
        Agently.create_agent("research-competitors")
        .input({
            "topic": topic,
            "market_segment": data.get("market_segment", ""),
            "key_trends": data.get("key_trends", []),
            "market_signals": signals,
        })
        .instruct(
            "You are a competitive intelligence analyst. Identify 3-5 key competitors "
            "or alternatives. Use the provided market signals (GitHub Copilot CR, "
            "Amazon CodeGuru, GitLab Duo, recent funding, analyst coverage) as real data. "
            "For each competitor: describe their approach, estimate market position "
            "(leader/challenger/niche), list key strengths and weaknesses. "
            "Include the provided signals as evidence. Rate overall competitive intensity."
        )
        .output({
            "competitors": (
                [
                    {
                        "name": (str, "Competitor name", True),
                        "approach": (str, "Brief description of their approach", True),
                        "position": (str, "Market position: leader, challenger, or niche", True),
                        "strengths": ([str], "Key strengths", True),
                        "weaknesses": ([str], "Key weaknesses", True),
                    }
                ],
                "3-5 competitor profiles",
                True,
            ),
            "competitive_intensity": (str, "Overall intensity: high, medium, low", True),
            "differentiation_gaps": ([str], "Gaps competitors are not addressing", True),
        })
        .async_start()
    )
    return result


async def identify_opportunities(competitor_analysis: object = None, topic: str = "") -> dict:
    """Identify market opportunities via model call."""
    await asyncio.sleep(0.2)  # simulated I/O: cross-referencing with internal roadmap
    print("  → 识别市场机会（模型请求中）...")
    data = competitor_analysis if isinstance(competitor_analysis, dict) else {}
    gaps = data.get("differentiation_gaps", [])
    beta = MOCK_BUSINESS_CONTEXT["internal_data"]["beta_test_results"]
    result = await (
        Agently.create_agent("research-opportunities")
        .input({
            "topic": topic,
            "differentiation_gaps": gaps,
            "competitive_intensity": data.get("competitive_intensity", "medium"),
            "beta_results": json.dumps(beta, ensure_ascii=False),
        })
        .instruct(
            "You are a product strategist identifying market opportunities. "
            "Based on the competitive gaps, beta test results (47% time reduction, "
            "+23% defect catch rate, NPS 72), and market context, identify 3-5 "
            "specific opportunities for differentiation. For each: describe the "
            "value proposition, estimate addressable market, rate feasibility "
            "(high/medium/low), suggest a go-to-market approach. "
            "Use the beta data as evidence of product-market fit."
        )
        .output({
            "opportunities": (
                [
                    {
                        "name": (str, "Opportunity name", True),
                        "value_proposition": (str, "Specific value proposition", True),
                        "feasibility": (str, "Feasibility: high, medium, low", True),
                        "gtm_approach": (str, "Suggested go-to-market approach", True),
                    }
                ],
                "3-5 market opportunities",
                True,
            ),
            "top_recommendation": (str, "The single most promising opportunity and rationale", True),
            "beta_evidence_alignment": (str, "How beta results support the recommended direction", True),
        })
        .async_start()
    )
    return result


async def compile_research_brief(
    market_data: object = None,
    competitor_analysis: object = None,
    opportunities: object = None,
    topic: str = "",
) -> dict:
    """Compile the final research brief via model call."""
    await asyncio.sleep(0.2)  # simulated I/O: generating executive-ready report
    print("  → 编译研究简报（模型请求中）...")
    md = market_data if isinstance(market_data, dict) else {}
    ca = competitor_analysis if isinstance(competitor_analysis, dict) else {}
    op = opportunities if isinstance(opportunities, dict) else {}

    result = await (
        Agently.create_agent("research-compile")
        .input({
            "topic": topic,
            "market_segment": md.get("market_segment", ""),
            "market_size": md.get("market_size_estimate", ""),
            "trends": md.get("key_trends", []),
            "competitor_count": len(ca.get("competitors", [])),
            "competitive_intensity": ca.get("competitive_intensity", ""),
            "opportunity_count": len(op.get("opportunities", [])),
            "top_recommendation": op.get("top_recommendation", ""),
            "product_name": MOCK_BUSINESS_CONTEXT["product"]["name"],
            "current_arr": MOCK_BUSINESS_CONTEXT["product"]["current_arr"],
        })
        .instruct(
            "You are a research director reviewing a market research brief. "
            "Evaluate completeness and coherence. Write a 2-3 sentence executive "
            "summary suitable for a VP of Product. Confirm all required sections "
            "are present. Flag areas needing deeper investigation. "
            "Context: this is for DevFlow, a $14.2M ARR enterprise DevOps platform."
        )
        .output({
            "brief_complete": (bool, "True if all required sections are present", True),
            "executive_summary": (str, "2-3 sentence executive summary for VP Product", True),
            "has_competitor_section": (bool, "Competitor analysis is present", True),
            "has_opportunity_section": (bool, "Opportunity analysis is present", True),
            "further_research_needed": ([str], "Areas needing deeper investigation"),
            "recommended_next_step": (str, "Concrete next step for the product team", True),
        })
        .async_start()
    )
    return result


def register_actions(agent) -> None:
    agent.register_action(
        name="gather_market_data",
        desc="Gather market landscape data using AI, seeded with internal business data.",
        kwargs={"topic": (str, "Feature or product topic to research.")},
        func=gather_market_data,
    )
    agent.register_action(
        name="analyze_competitors",
        desc="Analyze competitive landscape with profiles, strengths, weaknesses, and gaps.",
        kwargs={
            "market_data": (object, "Output from gather_market_data."),
            "topic": (str, "Original research topic."),
        },
        func=analyze_competitors,
    )
    agent.register_action(
        name="identify_opportunities",
        desc="Identify market opportunities with value props, feasibility, and GTM approach.",
        kwargs={
            "competitor_analysis": (object, "Output from analyze_competitors."),
            "topic": (str, "Original research topic."),
        },
        func=identify_opportunities,
    )
    agent.register_action(
        name="compile_research_brief",
        desc="Compile and review the final market research brief.",
        kwargs={
            "market_data": (object, "Output from gather_market_data."),
            "competitor_analysis": (object, "Output from analyze_competitors."),
            "opportunities": (object, "Output from identify_opportunities."),
            "topic": (str, "Original research topic."),
        },
        func=compile_research_brief,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main demo
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE_NARRATIVE = {
    "gather_market_data": "市场格局数据汇总完成",
    "analyze_competitors": "竞品分析完成",
    "validate_analysis": "分析校验通过",
    "identify_opportunities": "市场机会识别完成",
    "compile_research_brief": "研究简报编译完成",
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    Agently.settings.set("skills.registry.root", str(RUNTIME_ROOT / "registry"))
    skill_root = prepare_skill()
    Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)

    agent = Agently.create_agent("market-research-demo")
    register_actions(agent)

    divider = "=" * 60
    print(divider)
    print("Market Research Brief Generator — Actions + Skills Streaming")
    print(f"Feature:    {MOCK_FEATURE_TOPIC}")
    print(f"Product:    {MOCK_BUSINESS_CONTEXT['product']['name']} ({MOCK_BUSINESS_CONTEXT['product']['current_arr']} ARR)")
    print(f"Users:      {MOCK_BUSINESS_CONTEXT['product']['current_users']}")
    print(f"Top pain:   {MOCK_BUSINESS_CONTEXT['internal_data']['pain_point_ranking']['code_review_bottleneck']}")
    print(f"Beta NPS:   {MOCK_BUSINESS_CONTEXT['internal_data']['beta_test_results']['nps']}")
    print(f"Key signal: {MOCK_BUSINESS_CONTEXT['market_signals']['analyst_coverage'][:80]}...")
    print(divider)
    print("Starting research pipeline...\n")

    await asyncio.sleep(0.3)  # simulated: agent startup, loading skill registry

    execution = (
        agent
        .use_skills(["market-research-brief"], mode="required")
        .input(MOCK_FEATURE_TOPIC)
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

    market_data = data.get("gather_market_data") or {}
    competitors = data.get("analyze_competitors") or {}
    opportunities = data.get("identify_opportunities") or {}
    brief = data.get("compile_research_brief") or {}

    print(f"\n{divider}")
    print("研究简报交付清单")
    print(divider)

    print(f"  市场细分:   {market_data.get('market_segment', '—')[:120]}")
    print(f"  市场规模:   {market_data.get('market_size_estimate', '—')}")
    print(f"  内部验证:   {market_data.get('internal_validation', '—')[:120]}")
    print(f"  关键趋势:")
    for trend in market_data.get("key_trends", [])[:3]:
        print(f"    · {trend[:100]}")

    comp_list = competitors.get("competitors", [])
    print(f"\n  竞品数量:   {len(comp_list)}")
    for comp in comp_list:
        print(f"    · {comp.get('name', '—')} — {comp.get('position', '—')}")
    print(f"  竞争强度:   {competitors.get('competitive_intensity', '—')}")
    gaps = competitors.get("differentiation_gaps", [])
    for gap in gaps[:3]:
        print(f"    · 缺口: {gap[:100]}")

    opp_list = opportunities.get("opportunities", [])
    print(f"\n  机会数量:   {len(opp_list)}")
    for opp in opp_list:
        print(f"    · {opp.get('name', '—')} [可行性: {opp.get('feasibility', '—')}]")
    print(f"  最优推荐:   {opportunities.get('top_recommendation', '—')[:120]}")
    print(f"  Beta 对齐:  {opportunities.get('beta_evidence_alignment', '—')[:120]}")

    print(f"\n{divider}")
    print("执行摘要（VP Product 级别）")
    print(divider)
    print(brief.get("executive_summary", "—"))
    print(f"\n  下一步建议: {brief.get('recommended_next_step', '—')}")

    selected_route = meta.get("route_plan", {}).get("selected_route", "")
    print(f"\nselected_route={selected_route}")
    print(f"stream_gather_data={'skills.stages.gather_market_data' in stream_events}")
    print(f"stream_analyze_competitors={'skills.stages.analyze_competitors' in stream_events}")
    print(f"stream_identify_opportunities={'skills.stages.identify_opportunities' in stream_events}")
    print(f"stream_compile_brief={'skills.stages.compile_research_brief' in stream_events}")
    print(f"has_competitors={len(comp_list) > 0}")
    print(f"has_opportunities={len(opp_list) > 0}")
    print(f"brief_complete={brief.get('brief_complete')}")


if __name__ == "__main__":
    asyncio.run(main())
