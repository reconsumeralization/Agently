"""Multi-skill model-composed orchestration — LLM as skill planner & composer.

Run:
    python examples/agent_auto_orchestration/09_multi_skill_model_orchestration.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates the apex 4.1.3 capability: model-driven multi-skill
composition. Instead of a predetermined skill sequence, the LLM receives a pool
of available skills (each with declared inputs, outputs, and roles) and composes
a DAG that chains them optimally for the given task.

Three skills are available for a "product launch preparation" scenario:

  market-positioning  ─┐
  risk-assessment  ────┤── model-composed DAG ──→ launch readiness package
  launch-communications ─┘

Each skill has model-backed stages that produce structured outputs. The model
planner decides:
  - Which skills to use (1, 2, or all 3)
  - Their dependency order
  - How intermediate outputs feed into downstream skill inputs
  - What artifacts the combined pipeline should produce

Capabilities demonstrated:
  - Model-driven skill composition (planner_mode="model")
  - Multi-skill orchestration with inter-skill data flow
  - Skill cards with consumes/produces/stage_roles metadata
  - Structured output contracts
  - Rich live display showing the composed plan and execution progress
  - Agent auto-orchestration via agent.start()
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Three composable skills for product launch preparation
# ═══════════════════════════════════════════════════════════════════════════════

MARKET_POSITIONING_SKILL = """
skill_id: market-positioning
version: 1.0.0
display_name: Market Positioning Analysis
purpose: >
  Analyze a product's market positioning by examining its target audience,
  competitive landscape, and unique value proposition. Produces a positioning
  statement and go-to-market recommendations.
trust_level: local
kind: workflow
activation:
  keywords:
    - market positioning
    - competitive analysis
    - go-to-market
    - product launch
    - target audience
card:
  skill_id: market-positioning
  version: 1.0.0
  display_name: Market Positioning Analysis
  purpose: Analyze product market positioning, competitive landscape, and unique value proposition.
  stage_roles: [intake, analysis, output]
  consumes:
    - role: product_description
      type: text
    - role: target_market
      type: text
  produces:
    - role: market_analysis
      type: text
    - role: positioning_statement
      type: text
    - role: gtm_recommendations
      type: text
  artifact_types: [md, json]
  task_fit_examples:
    - "Analyze market positioning for a new B2B SaaS product"
    - "Create go-to-market strategy for enterprise launch"
    - "Evaluate competitive landscape for a developer tool"
  input_expectations: "Product description with features, target audience, and pricing model."
  output_expectations: "Structured market analysis with positioning statement and GTM recommendations."
  composition_hints:
    - "Should run early in pipeline — other skills depend on market context"
    - "Outputs positioning_statement that launch-comms skill can consume"
stages:
  - id: analyze_market
    kind: model
    purpose: >
      Analyze the market positioning for the product. Consider:
      - Target audience segments and their needs
      - Competitive alternatives and their positioning
      - The product's unique differentiators
      - Pricing strategy relative to market
      Produce a concise but thorough market analysis.
  - id: craft_positioning
    kind: model
    purpose: >
      Based on the market analysis, craft a crisp positioning statement and
      3-5 specific go-to-market recommendations. The positioning statement
      should follow the format: "For [target audience] who [need], [product]
      is a [category] that [key benefit]. Unlike [alternatives], [product]
      [unique differentiator]."
semantic_outputs:
  market_analysis: analyze_market
  positioning: craft_positioning
tags: [market, positioning, strategy, product-launch]
"""

RISK_ASSESSMENT_SKILL = """
skill_id: risk-assessment
version: 1.0.0
display_name: Launch Risk Assessment
purpose: >
  Identify, categorize, and prioritize risks associated with a product launch.
  Produces a risk matrix (probability × impact) with mitigation strategies
  for each high-priority risk.
trust_level: local
kind: workflow
activation:
  keywords:
    - risk assessment
    - risk analysis
    - launch risks
    - mitigation
    - risk matrix
card:
  skill_id: risk-assessment
  version: 1.0.0
  display_name: Launch Risk Assessment
  purpose: Identify and prioritize launch risks with mitigation strategies.
  stage_roles: [intake, analysis, output]
  consumes:
    - role: product_description
      type: text
    - role: market_analysis
      type: text
  produces:
    - role: risk_matrix
      type: structured
    - role: mitigation_plan
      type: text
  artifact_types: [md, json]
  task_fit_examples:
    - "Identify risks for launching a real-time collaboration feature"
    - "Create risk register for enterprise SaaS release"
    - "Assess compliance risks for financial product launch"
  input_expectations: "Product description and optional market analysis context."
  output_expectations: "Risk matrix with 8-12 risks, each with severity, probability, and mitigation."
  composition_hints:
    - "Can run in parallel with market-positioning if only product_description needed"
    - "Benefits from market_analysis input if market-positioning runs first"
stages:
  - id: identify_risks
    kind: model
    purpose: >
      Identify 8-12 specific risks for the product launch. Cover these categories:
      - Technical risks (scalability, reliability, security)
      - Market risks (adoption, competition, timing)
      - Operational risks (team capacity, dependencies, tooling)
      - Compliance/legal risks (data privacy, regulations, contracts)
      For each risk, estimate probability (low/medium/high) and impact (low/medium/high).
      Be specific — name actual components, services, or scenarios.
  - id: plan_mitigation
    kind: model
    purpose: >
      For each high or critical risk (high probability + high impact), propose
      a concrete mitigation strategy. Include: what action to take, who should
      own it, and a timeline (pre-launch, launch-day, or post-launch).
      For medium risks, suggest monitoring triggers.
      For low risks, note acceptance rationale.
semantic_outputs:
  risk_register: identify_risks
  mitigation: plan_mitigation
tags: [risk, assessment, launch, compliance]
"""

LAUNCH_COMMS_SKILL = """
skill_id: launch-communications
version: 1.0.0
display_name: Launch Communications Generator
purpose: >
  Draft launch communications materials: a press release / announcement blog
  post, an internal FAQ for the support team, and a customer-facing feature
  summary. Tailors messaging based on positioning and acknowledges risks
  transparently.
trust_level: local
kind: workflow
activation:
  keywords:
    - launch communications
    - press release
    - announcement
    - FAQ
    - blog post
    - changelog
card:
  skill_id: launch-communications
  version: 1.0.0
  display_name: Launch Communications Generator
  purpose: Draft press release, internal FAQ, and customer-facing feature summary for a product launch.
  stage_roles: [intake, creation, output]
  consumes:
    - role: positioning_statement
      type: text
    - role: risk_matrix
      type: structured
    - role: product_description
      type: text
  produces:
    - role: press_release
      type: text
    - role: internal_faq
      type: text
    - role: feature_summary
      type: text
  artifact_types: [md, json]
  task_fit_examples:
    - "Write press release for new developer tool launch"
    - "Prepare internal FAQ for enterprise feature release"
    - "Create customer-facing changelog for major version upgrade"
  input_expectations: "Positioning statement, risk matrix, and product description."
  output_expectations: "Press release, internal FAQ (8-10 questions), and customer-facing feature summary."
  composition_hints:
    - "Must run after market-positioning (needs positioning_statement)"
    - "Benefits from risk-assessment output for honest messaging"
    - "This should be the final skill in the pipeline"
stages:
  - id: draft_press_release
    kind: model
    purpose: >
      Draft a professional press release / announcement blog post for the product
      launch. Include: headline, subheadline, dateline, 3-4 body paragraphs
      covering the problem, solution, key features, and availability. Use the
      positioning statement to frame the narrative. If risk data is available,
      acknowledge key risks transparently in a "What We're Watching" section.
  - id: prepare_faq
    kind: model
    purpose: >
      Prepare an internal FAQ document for the customer support and sales teams.
      Include 8-10 questions and answers covering:
      - Pricing and availability
      - Migration path for existing customers
      - Known limitations and workarounds
      - Security and compliance posture
      - Competitive comparisons (honest strengths and weaknesses)
      - Escalation path for issues
      Use the risk matrix to inform the known limitations section.
  - id: customer_summary
    kind: model
    purpose: >
      Write a customer-facing feature summary (changelog style) that highlights
      what's new, what's improved, and any deprecations or breaking changes.
      Keep it concise and excited but honest. 2-3 paragraphs with bullet points
      for key features.
semantic_outputs:
  press_release: draft_press_release
  faq: prepare_faq
  customer_summary: customer_summary
tags: [communications, launch, press-release, faq]
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Mock product context (realistic seed data)
# ═══════════════════════════════════════════════════════════════════════════════

PRODUCT_CONTEXT = {
    "product_name": "DevFlow Code Review AI",
    "product_description": (
        "An AI-powered code review assistant that integrates with GitHub, GitLab, "
        "and Bitbucket. It automatically reviews pull requests for bugs, security "
        "vulnerabilities, performance issues, and style violations. It learns from "
        "the team's review history to match their standards and preferences. Key "
        "features: (1) real-time PR annotation, (2) cross-repository security "
        "scanning, (3) team-specific style learning, (4) automated fix suggestions "
        "with before/after diffs, (5) compliance report generation for SOC 2 and "
        "GDPR requirements."
    ),
    "target_market": (
        "Mid-market to enterprise engineering teams (50-2000 developers). Primary "
        "vertical: B2B SaaS companies with compliance requirements (SOC 2, GDPR, "
        "HIPAA). Initial focus on North American market, with EMEA expansion "
        "planned for Q4 2026."
    ),
    "pricing_tier": "Free for public repos, $29/dev/month for private repos, Enterprise at $49/dev/month with SSO and compliance reports.",
    "launch_date": "2026-07-15",
    "competitive_landscape": [
        "CodeRabbit ($15/dev/mo, general-purpose)",
        "GitHub Copilot Code Review (included with Copilot Enterprise)",
        "Amazon CodeGuru Reviewer (Java/Python only, AWS-centric)",
        "Snyk Code (security-focused, $25/dev/mo)",
    ],
    "team_size": 24,
    "existing_customers": 320,
    "beta_testers": 48,
    "beta_nps": 68,
}

# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════


def _build_composition_panel(plan: dict | None) -> Panel:
    if not plan:
        return Panel(
            Text("  waiting for model to compose skills...", style="dim"),
            title="Model-Composed Plan",
            border_style="dim",
        )

    content: list[Text] = []
    selected = plan.get("selected_skills", [])

    if selected:
        content.append(Text("\nSelected skills:", style="bold cyan"))
        for sk in selected:
            skill_id = sk.get("skill_id", "?") if isinstance(sk, dict) else str(sk)
            reason = ""
            if isinstance(sk, dict):
                reason = sk.get("selection_reason", "")
            content.append(Text(f"  ✓ {skill_id}", style="green"))
            if reason:
                content.append(Text(f"    {reason}", style="dim"))

    rejected = plan.get("rejected_skills", [])
    if rejected:
        content.append(Text("\nRejected:", style="bold red"))
        for rj in rejected:
            skill_id = rj.get("skill_id", "?") if isinstance(rj, dict) else str(rj)
            reason = ""
            if isinstance(rj, dict):
                reason = rj.get("rejection_reason", "")
            content.append(Text(f"  ✗ {skill_id}", style="red"))
            if reason:
                content.append(Text(f"    {reason}", style="dim"))

    stage_count = len(plan.get("composed_stage_graph", []))
    content.append(Text(f"\nComposed stages: {stage_count}", style="bold"))

    dependencies = plan.get("composed_stage_graph", []) if isinstance(plan.get("composed_stage_graph"), list) else []
    for stage in dependencies[:15]:
        sid = stage.get("id", "?") if isinstance(stage, dict) else str(stage)
        deps = stage.get("depends_on", []) if isinstance(stage, dict) else []
        dep_str = f" ← {', '.join(deps)}" if deps else ""
        content.append(Text(f"  · {sid}{dep_str}"))

    return Panel(Group(*content), title="Model-Composed Plan", border_style="magenta")


def _build_progress_table(completed_tasks: set[str], task_outputs: dict[str, Any]) -> Table:
    t = Table(title="Stage Execution", expand=True, show_header=True, header_style="bold")
    t.add_column("Stage", style="cyan", width=30)
    t.add_column("Status", width=14)
    t.add_column("Output Preview", style="white")

    all_task_ids = list(task_outputs.keys()) + list(completed_tasks)
    seen: set[str] = set()
    for tid in all_task_ids:
        if tid in seen:
            continue
        seen.add(tid)
        if tid in completed_tasks:
            status = "[green]✓ complete[/]"
        elif tid in task_outputs:
            status = "[yellow]◎ running[/]"
        else:
            status = "[dim]· waiting[/]"

        preview = ""
        output = task_outputs.get(tid, {})
        if isinstance(output, dict):
            # Find the first text field
            for k, v in output.items():
                if isinstance(v, str) and len(v) > 20:
                    preview = v[:100].replace("\n", " ") + "..."
                    break
                elif isinstance(v, str):
                    preview = v
                    break

        display_name = tid.replace("_", " ").title()
        t.add_row(f"  {display_name}", status, preview)

    if not seen:
        t.add_row("  (composing plan...)", "[yellow]◎[/]", "")

    return t


def _build_layout(composition: Panel, progress: Table) -> Layout:
    layout = Layout()
    layout.split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(Layout(composition, name="plan"))
    layout["right"].split_column(Layout(progress, name="progress"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.3)

    # Set up skills registry and install all three skills
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)

    for skill_name, skill_yaml in [
        ("market-positioning", MARKET_POSITIONING_SKILL),
        ("risk-assessment", RISK_ASSESSMENT_SKILL),
        ("launch-communications", LAUNCH_COMMS_SKILL),
    ]:
        skill_dir = runtime_dir / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(skill_yaml.strip())
        Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("product-launch-orchestrator")

    # Use all three skills in model_decision mode
    agent.use_skills(
        ["market-positioning", "risk-assessment", "launch-communications"],
        mode="model_decision",
        scope="request",
    )

    product_info = json.dumps(PRODUCT_CONTEXT, indent=2)

    execution = (
        agent
        .input(
            f"Prepare for the launch of {PRODUCT_CONTEXT['product_name']}. "
            f"Use all available skills to create a comprehensive launch readiness "
            f"package. The product details are:\n\n{product_info}"
        )
        .create_execution()
    )

    plan_data: dict | None = None
    completed_tasks: set[str] = set()
    task_outputs: dict[str, Any] = {}
    seen_plan = False

    composition = _build_composition_panel(None)
    progress = _build_progress_table(completed_tasks, task_outputs)
    layout = _build_layout(composition, progress)

    with Live(layout, refresh_per_second=8, screen=True, transient=False) as live:
        async for item in execution.get_async_generator(type="instant"):
            # Capture the model-composed plan
            if not seen_plan and item.path == "skills.plan" and item.is_complete:
                val = item.value or {}
                plan_data = val if isinstance(val, dict) else {}
                seen_plan = True
                composition = _build_composition_panel(plan_data)
                live.update(_build_layout(composition, progress))

            # Track stage progress
            if item.path.startswith("skills.stages.") and item.is_complete:
                stage_id = item.path.split(".")[2]
                if item.path.endswith(".complete"):
                    completed_tasks.add(stage_id)
                elif item.path.endswith(".result"):
                    task_outputs[stage_id] = item.value or {}

                progress = _build_progress_table(completed_tasks, task_outputs)
                live.update(_build_layout(composition, progress))

    # ── Final summary ──────────────────────────────────────────────────────
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\nroute: {route}")
    print(f"plan visible: {seen_plan}")

    if plan_data:
        selected = plan_data.get("selected_skills", [])
        print(f"skills selected: {len(selected)}")
        for sk in selected:
            sid = sk.get("skill_id", "?") if isinstance(sk, dict) else str(sk)
            print(f"  - {sid}")

        stages = plan_data.get("composed_stage_graph", [])
        print(f"composed stages: {len(stages)}")

    print(f"stages completed: {len(completed_tasks)}")

    # Print key outputs
    semantic = (data or {}).get("semantic_outputs", {})
    for key in ("positioning", "risk_register", "press_release"):
        output = semantic.get(key, {}).get("result", {})
        if output:
            # Find first text value
            text_val = ""
            for v in output.values():
                if isinstance(v, str) and len(v) > 50:
                    text_val = v
                    break
            print(f"\n[{key}]:")
            print(str(text_val)[:500] if text_val else str(output)[:500])


if __name__ == "__main__":
    asyncio.run(main())
