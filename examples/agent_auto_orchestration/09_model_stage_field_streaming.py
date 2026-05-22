"""Skill model stages — field-level delta streaming + file output.

Run:
    python examples/agent_auto_orchestration/09_model_stage_field_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates a Skill that uses native `kind: model` stages to
generate structured content and stream selected fields while the model is still
writing them.

A "product launch press kit" skill has three stages:
  1. generate_positioning  (model) — streams `positioning_text`
  2. generate_risks        (model) — streams `risks_text`
  3. save_press_kit        (action) — writes the assembled press kit to disk

The model stages call Agently's model pipeline through `SkillsExecutionContext`,
so the CLI can consume `skills.stages.<id>.fields.<field>` deltas with
`print(delta, end="")`-style rendering instead of waiting for a whole stage to
finish.

Capabilities demonstrated:
  - Multi-stage Skill with inter-stage data passing
  - Native Skill `kind: model` stage execution
  - Field-level delta streaming from skill model stages
  - File output from action stage
  - Rich live display with per-stage progress
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
from rich.spinner import Spinner
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — model stages stream fields; action stage writes file
# ═══════════════════════════════════════════════════════════════════════════════

PRESS_KIT_SKILL_YAML = """
skill_id: product-launch-press-kit
version: 1.0.0
display_name: Product Launch Press Kit Generator
purpose: >
  Generate a comprehensive press kit for a product launch: market positioning
  brief, risk assessment, and a compiled press-ready document saved to disk.
trust_level: local
kind: workflow
activation:
  keywords:
    - press kit
    - launch announcement
    - product launch
    - press release
requires:
  actions:
    - save_press_kit
stages:
  - id: generate_positioning
    kind: model
    purpose: >
      You are a product marketing strategist. Write a concise market
      positioning brief using the product information. Include a 2-3 paragraph
      market landscape summary, one crisp positioning statement, and 3-5
      concrete competitive differentiators. Name actual competitors from the
      input and avoid generic claims.
    input:
      product_info: "${task}"
    output_schema:
      positioning_text:
        type: str
        description: Market landscape, positioning statement, and differentiators.
  - id: generate_risks
    kind: model
    depends_on:
      - generate_positioning
    purpose: >
      You are a technical risk analyst. Generate a launch risk register using
      the product information and positioning context. Include 5-8 risks with
      severity, category, and a one-sentence description. For the top 3 risks,
      provide concrete mitigation actions with owner and timeline.
    input:
      product_info: "${task}"
      positioning_text: "${state.generate_positioning.positioning_text}"
    output_schema:
      risks_text:
        type: str
        description: Launch risk register and mitigation plan.
  - id: save_press_kit
    kind: action
    action: save_press_kit
    depends_on:
      - generate_positioning
      - generate_risks
    input:
      positioning_text: "${state.generate_positioning.positioning_text}"
      risks_text: "${state.generate_risks.risks_text}"
      product_name: "DevFlow Code Review AI"
semantic_outputs:
  press_kit: save_press_kit
tags:
  - press-kit
  - launch
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Mock product data
# ═══════════════════════════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════════════════════════
# Action implementation — writes model stage outputs to a file
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_save_press_kit(
    positioning_text: str = "",
    risks_text: str = "",
    product_name: str = "Product",
    **kwargs,
) -> dict[str, Any]:
    """Compile outputs into a press kit Markdown document and save to disk."""
    report = f"""# {product_name} — Launch Press Kit
Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

---

## Market Positioning Brief

{positioning_text or '*Not generated*'}

---

## Risk Register

{risks_text or '*Not generated*'}

---

## Attachments
- [ ] Product screenshots (3-5)
- [ ] Executive headshots
- [ ] Company boilerplate
- [ ] Customer testimonial quotes (beta program)

*Generated by Agently Skills Executor — action stages with internal model calls.*
"""

    reports_dir = Path.home() / ".agently_press_kits"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = product_name.lower().replace(" ", "-").replace("/", "-")
    filepath = reports_dir / f"{slug}_{timestamp}.md"
    filepath.write_text(report)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════


def _build_stage_panel(title: str, border: str, content: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if content:
        preview = content[:600]
        if len(content) > 600:
            preview += f"\n\n... ({len(content):,} chars total)"
        body = Text(preview)
    elif done:
        body = Text("  (no content generated)", style="dim")
    else:
        body = Text("  waiting...", style="dim")

    return Panel(body, title=f"{icon} {title}", border_style=border)


def _build_output_panel(file_path: str = "", done: bool = False) -> Panel:
    if done and file_path:
        return Panel(
            Text(f"  Press kit saved to:\n  [green]{file_path}[/]"),
            title="[bold green]✓[/] Output",
            border_style="green",
        )
    elif done:
        return Panel(Text("  file not saved.", style="red"), title="✗ Output", border_style="red")
    return Panel(Text("  waiting...", style="dim"), title="[dim]·[/] Output", border_style="dim")


def _build_layout(
    pos_panel: Panel, risk_panel: Panel, output_panel: Panel
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=3),
        Layout(name="bottom", ratio=1),
    )
    layout["top"].split_row(
        Layout(pos_panel, name="positioning"),
        Layout(risk_panel, name="risk"),
    )
    layout["bottom"].split_row(Layout(output_panel, name="output"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.4)

    # Set up skills registry and install the press kit skill
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    skill_dir = runtime_dir / "product-launch-press-kit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(PRESS_KIT_SKILL_YAML.strip())

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("press-kit-generator")

    # Only the file writer is an action. Content generation is handled by
    # native Skill model stages above.
    agent.register_action(
        name="save_press_kit",
        desc="Compile the generated content into a Markdown press kit and save to disk.",
        kwargs={
            "positioning_text": ("str", "Positioning brief text."),
            "risks_text": ("str", "Risk register text."),
            "product_name": ("str", "Product name for the document title."),
        },
        func=_action_save_press_kit,
    )

    execution = (
        agent
        .use_skills(["product-launch-press-kit"], mode="required")
        .input(PRODUCT_BRIEF)
        .create_execution()
    )

    # Track state
    completed_stages: set[str] = set()
    positioning_text: str | None = None
    risks_text: str | None = None
    saved_file: str = ""
    stage_running: dict[str, bool] = {}

    pos_panel = _build_stage_panel("Market Positioning", "blue", None, False, False)
    risk_panel = _build_stage_panel("Risk Register", "red", None, False, False)
    output_panel = _build_output_panel()
    layout = _build_layout(pos_panel, risk_panel, output_panel)

    with Live(layout, refresh_per_second=10, screen=True, transient=False) as live:
        async for item in execution.get_async_generator(type="instant"):
            # Track stage starts
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                task_path = item.path
                if "generate_positioning" in task_path:
                    stage_running["generate_positioning"] = True
                elif "generate_risks" in task_path:
                    stage_running["generate_risks"] = True
                elif "save_press_kit" in task_path:
                    stage_running["save_press_kit"] = True

            # Track stage completion via skills.stages.*
            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                if stage_id not in ("plan",):
                    completed_stages.add(stage_id)
                    stage_running[stage_id] = False

            # Stream model-stage field deltas as they arrive.
            if item.path == "skills.stages.generate_positioning.fields.positioning_text" and item.delta:
                positioning_text = (positioning_text or "") + item.delta
            elif item.path == "skills.stages.generate_risks.fields.risks_text" and item.delta:
                risks_text = (risks_text or "") + item.delta

            # Capture action results
            if item.path.startswith("actions.") and item.is_complete:
                action_name = item.path.split(".", 1)[1]
                val = item.value or {}
                result = val.get("result", val) if isinstance(val, dict) else val

                if action_name == "save_press_kit" and isinstance(result, dict):
                    saved_file = result.get("file_path", "")

            # Update panels
            pos_panel = _build_stage_panel(
                "Market Positioning", "blue", positioning_text,
                done="generate_positioning" in completed_stages,
                running=stage_running.get("generate_positioning", False),
            )
            risk_panel = _build_stage_panel(
                "Risk Register", "red", risks_text,
                done="generate_risks" in completed_stages,
                running=stage_running.get("generate_risks", False),
            )
            output_panel = _build_output_panel(
                saved_file, done="save_press_kit" in completed_stages
            )
            live.update(_build_layout(pos_panel, risk_panel, output_panel))

    # ── Final summary ──────────────────────────────────────────────────────
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\nroute: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"positioning length: {len(positioning_text or ''):,} chars")
    print(f"risks length: {len(risks_text or ''):,} chars")
    print(f"press kit saved: {saved_file or '(not saved)'}")

    if positioning_text:
        print(f"\n[Positioning brief preview]:")
        print(positioning_text[:400])
    if risks_text:
        print(f"\n[Risk register preview]:")
        print(risks_text[:400])


if __name__ == "__main__":
    asyncio.run(main())
