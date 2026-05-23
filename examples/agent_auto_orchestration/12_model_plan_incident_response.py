"""Skill with model_plan stage — structured incident response planning.

Run:
    python examples/agent_auto_orchestration/12_model_plan_incident_response.py

Expected key output from a real run on 2026-05-22:
    route: skills
    stages completed: ['analyze_incident', 'generate_runbook', 'save_runbook']
    plan length: 4,001 chars
    runbook length: 2,282 chars
    document saved: /Users/moxin/.agently_incident_runbooks/inc-2026-05-0421_20260522_163656.md
    Note: the first model response did not parse as JSON; automatic retry succeeded.

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates `kind: model_plan` — a model-driven planning stage
that decomposes a complex incident into a structured response plan with steps,
dependencies, and deliverables. A downstream model stage reads the plan and
produces actionable runbook instructions. The result is saved to disk.

Stages:
  1. analyze_incident  (model_plan) — analyzes alert, produces structured plan
  2. generate_runbook  (model)      — converts plan into detailed runbook steps
  3. save_runbook      (action)     — writes the complete response doc to disk

Capabilities demonstrated:
  - `kind: model_plan` stage with structured plan output
  - Plan-to-execution pipeline (plan stage → action stage)
  - Field-level delta streaming from model_plan + model stages
  - Rich live display with plan/runbook/output panels
"""

from __future__ import annotations

import asyncio
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

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

INCIDENT_RESPONSE_SKILL_YAML = """
skill_id: incident-response-planner
version: 1.0.0
display_name: Incident Response Planner
purpose: >
  Analyze an infrastructure incident alert, produce a structured response plan
  with prioritized steps and dependencies, generate a detailed runbook, and
  save the complete incident response document to disk.
trust_level: local
kind: workflow
activation:
  keywords:
    - incident
    - alert
    - runbook
    - incident response
    - on call
requires:
  actions:
    - save_runbook
stages:
  - id: analyze_incident
    kind: model_plan
    purpose: >
      You are an SRE incident commander. Analyze the incident alert below and
      produce a structured response plan. Include:

      1. Severity assessment (P0/P1/P2/P3) with justification
      2. Impact radius (which services, users, regions are affected)
      3. Immediate mitigation actions (things to do right now)
      4. Investigation steps (what to investigate and in what order)
      5. Stakeholders to notify (teams, roles, external parties)
      6. Expected resolution timeline (best case / worst case)

      Be specific and actionable. Avoid generic advice.
    input:
      alert: "${task}"
    output_schema:
      plan:
        type: str
        description: Structured incident response plan covering all 6 areas above
  - id: generate_runbook
    kind: model
    depends_on:
      - analyze_incident
    purpose: >
      Convert the incident response plan into a detailed, step-by-step runbook.
      Each step must include: the action, the owner role (e.g. on-call SRE,
      database team, security), the expected outcome, and a verification check.
      Include rollback steps for any irreversible actions.

      Format as a clear, executable checklist that an on-call engineer can
      follow at 3 AM.
    input:
      plan: "${state.analyze_incident.plan}"
      alert: "${task}"
    output_schema:
      runbook:
        type: str
        description: Detailed step-by-step incident response runbook
  - id: save_runbook
    kind: action
    action: save_runbook
    depends_on:
      - generate_runbook
    input:
      plan_text: "${state.analyze_incident.plan}"
      runbook_text: "${state.generate_runbook.runbook}"
      incident_id: "INC-2026-05-0421"
semantic_outputs:
  response_doc: save_runbook
tags:
  - incident-response
  - runbook
  - model-plan
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Realistic incident alert
# ═══════════════════════════════════════════════════════════════════════════════

INCIDENT_ALERT = """[PAGERDUTY] Triggered - 2026-05-22 03:17:21 UTC

Alert: "payment-gateway-eu-west-1 — P95 latency > 10s for 5+ minutes"
Service: payment-processor (v4.12.1)
Cluster: eu-west-1 (prod)
Alert Source: Datadog APM
Trigger: p95_latency > 10,000ms sustained for 300s

Context from Runbook Bot:
- Last deploy: 2026-05-22 02:45 UTC (15 min before alert) — PR #8421 "Upgrade
  Stripe SDK 14.2 → 15.0, add idempotency key to refund path"
- Recent errors (last 15 min): 500s at 2.3% on POST /v2/refunds,
  504s at 0.8% on GET /v2/charges/:id
- DB connection pool (RDS pg-m5.2xl): 87% utilization, no deadlocks
- Redis (ElastiCache): cluster healthy, 12% memory, 0 rejected connections
- Stripe API status page: all green, no incident reported
- Affected merchants: 14 merchants reporting timeout errors in #inc-payments
- Affected end-users: estimated 1,200 end-user transactions pending

Known Dependencies:
- charges-service (healthy)
- fraud-detection (healthy, but 2-min-old results during incident)
- notification-service (healthy)
- audit-log (backpressure at 15% queue depth — normal is <5%)
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Action — save the runbook to disk
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_save_runbook(
    plan_text: str = "",
    runbook_text: str = "",
    incident_id: str = "INC-0000",
    **kwargs,
) -> dict[str, Any]:
    doc = f"""# Incident Response Document — {incident_id}
Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

---

## Response Plan

{plan_text or '*Not generated*'}

---

## Runbook

{runbook_text or '*Not generated*'}

---

## Post-Incident
- [ ] Schedule blameless postmortem within 5 business days
- [ ] Update runbook with lessons learned
- [ ] File action items as tickets with owners and due dates
"""

    reports_dir = Path.home() / ".agently_incident_runbooks"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = incident_id.lower()
    filepath = reports_dir / f"{slug}_{timestamp}.md"
    filepath.write_text(doc)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "incident_id": incident_id,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════


def _build_plan_panel(plan_text: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if plan_text:
        preview = plan_text[:500]
        if len(plan_text) > 500:
            preview += f"\n\n... ({len(plan_text):,} chars)"
        body = Text(preview)
    elif done:
        body = Text("(no plan)", style="dim")
    else:
        body = Text("Analyzing incident alert...", style="dim")

    return Panel(body, title=f"{icon} Response Plan (model_plan)", border_style="yellow")


def _build_runbook_panel(runbook: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if runbook:
        preview = runbook[:500]
        if len(runbook) > 500:
            preview += f"\n\n... ({len(runbook):,} chars)"
        body = Text(preview)
    elif done:
        body = Text("(no runbook)", style="dim")
    else:
        body = Text("Waiting on plan...", style="dim")

    return Panel(body, title=f"{icon} Runbook (model)", border_style="cyan")


def _build_output_panel(file_path: str, done: bool) -> Panel:
    if done and file_path:
        return Panel(
            Text(f"Saved: [green]{file_path}[/]"),
            title="[bold green]✓[/] Output",
            border_style="green",
        )
    return Panel(Text("waiting...", style="dim"), title="[dim]·[/] Output", border_style="dim")


def _build_layout(plan: Panel, runbook: Panel, output: Panel) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=1),
        Layout(name="middle", ratio=1),
        Layout(name="bottom", ratio=1),
    )
    layout["top"].split_row(Layout(plan, name="plan"))
    layout["middle"].split_row(Layout(runbook, name="runbook"))
    layout["bottom"].split_row(Layout(output, name="output"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.3)

    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    skill_dir = runtime_dir / "incident-response-planner"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(INCIDENT_RESPONSE_SKILL_YAML.strip())

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("incident-commander")

    agent.register_action(
        name="save_runbook",
        desc="Save the incident response plan and runbook to disk.",
        kwargs={
            "plan_text": ("str", "The response plan text."),
            "runbook_text": ("str", "The runbook text."),
            "incident_id": ("str", "Incident identifier."),
        },
        func=_action_save_runbook,
    )

    execution = (
        agent
        .use_skills(["incident-response-planner"], mode="required")
        .input(INCIDENT_ALERT)
        .create_execution()
    )

    completed_stages: set[str] = set()
    plan_done = False
    runbook_done = False
    plan_text: str | None = None
    runbook_text: str | None = None
    saved_file = ""
    save_done = False
    stage_running: dict[str, bool] = {}

    plan_panel = _build_plan_panel(None, False, False)
    runbook_panel = _build_runbook_panel(None, False, False)
    output_panel = _build_output_panel("", False)
    layout = _build_layout(plan_panel, runbook_panel, output_panel)

    with Live(layout, refresh_per_second=10, screen=True, transient=False) as live:
        async for item in execution.get_async_generator(type="instant"):
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in ("analyze_incident", "generate_runbook", "save_runbook"):
                    if sid in item.path:
                        stage_running[sid] = True

            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # Stream field deltas
            if item.path == "skills.stages.analyze_incident.fields.plan" and item.delta:
                plan_text = (plan_text or "") + item.delta
            elif item.path == "skills.stages.generate_runbook.fields.runbook" and item.delta:
                runbook_text = (runbook_text or "") + item.delta

            if "analyze_incident" in completed_stages:
                plan_done = True
            if "generate_runbook" in completed_stages:
                runbook_done = True

            if item.path.startswith("actions.") and item.is_complete:
                result = item.value.get("result", item.value) if isinstance(item.value, dict) else item.value
                if isinstance(result, dict) and result.get("file_path"):
                    saved_file = result.get("file_path", "")
                    save_done = True

            plan_panel = _build_plan_panel(
                plan_text, plan_done, stage_running.get("analyze_incident", False)
            )
            runbook_panel = _build_runbook_panel(
                runbook_text, runbook_done, stage_running.get("generate_runbook", False)
            )
            output_panel = _build_output_panel(saved_file, save_done)
            live.update(_build_layout(plan_panel, runbook_panel, output_panel))

    # ── Final summary ──────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\nroute: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"plan length: {len(plan_text or ''):,} chars")
    print(f"runbook length: {len(runbook_text or ''):,} chars")
    print(f"document saved: {saved_file or '(not saved)'}")

    if plan_text:
        print(f"\n[Response plan preview]:")
        print(plan_text[:400])
    if runbook_text:
        print(f"\n[Runbook preview]:")
        print(runbook_text[:400])


if __name__ == "__main__":
    asyncio.run(main())
