"""Skill with validate + emit stages — document compliance check pipeline.

Run:
    python examples/agent_auto_orchestration/13_validate_emit_compliance_check.py

Expected key output from a real run on 2026-05-22:
    route: skills
    stages completed: ['emit_summary', 'extract_clauses', 'flag_compliance_gaps', 'save_audit', 'validate_extraction']
    total clauses extracted: 8
    validation passed: True
    risk summary: Overall compliance risk is HIGH.
    audit report saved: /Users/moxin/.agently_compliance_audits/vendor-agreement_20260522_163818.md

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates `kind: validate` and `kind: emit` stages in a
compliance document review pipeline. A vendor agreement is processed through
extraction → validation → gap analysis → emit → save, with the validate stage
gating downstream execution on critical clause detection.

Stages:
  1. extract_clauses     (model)    — extracts structured clauses from contract
  2. validate_extraction  (validate) — gates on required clause types present
  3. flag_compliance_gaps (model)    — identifies regulatory/commercial gaps
  4. emit_summary         (emit)     — publishes structured report to stream
  5. save_audit           (action)   — writes the compliance audit to disk

Capabilities demonstrated:
  - `kind: model` extraction with structured output_schema
  - `kind: validate` gating (halts pipeline if critical clauses missing)
  - `kind: model` downstream analysis (depends on validation passing)
  - `kind: emit` streaming structured data for external consumers
  - Rich live display with stage-by-stage progress
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

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

COMPLIANCE_SKILL_YAML = """
skill_id: compliance-audit
version: 1.0.0
display_name: Document Compliance Audit
purpose: >
  Extract key clauses from a vendor agreement or contract, validate that
  critical clause categories are present, identify compliance gaps, emit a
  structured audit summary, and save the full report to disk.
trust_level: local
kind: workflow
activation:
  keywords:
    - compliance
    - contract review
    - audit
    - vendor agreement
    - regulatory check
requires:
  actions:
    - save_compliance_audit
stages:
  - id: extract_clauses
    kind: model
    purpose: >
      You are a legal document analyst. Extract ALL key clauses from the
      contract below. For each clause, provide: the clause title, a one-line
      summary, the category (e.g., payment_terms, liability, data_protection,
      termination, ip_rights, confidentiality, warranty, indemnification,
      governing_law), and a risk flag (low/medium/high).
    input:
      contract: "${task}"
    output_schema:
      clauses:
        type: list
        description: List of extracted clauses with title, summary, category, risk_flag
      total_clauses:
        type: int
        description: Total number of clauses found
      categories_found:
        type: list
        description: List of unique clause categories found
  - id: validate_extraction
    kind: validate
    validation:
      required_state:
        - extract_clauses
  - id: flag_compliance_gaps
    kind: model
    depends_on:
      - extract_clauses
      - validate_extraction
    purpose: >
      Review the extracted clauses against standard compliance requirements
      (GDPR, SOC 2, standard commercial terms). Identify:

      1. Missing clause categories (e.g., no data_protection clause when
         personal data is mentioned, no termination clause)
      2. High-risk clauses with insufficient protection
      3. Ambiguous or vague language that could create compliance exposure
      4. Recommended additions or rewrites with sample language

      Output a structured compliance gap report.
    input:
      clauses: "${state.extract_clauses.clauses}"
      categories_found: "${state.extract_clauses.categories_found}"
    output_schema:
      gaps:
        type: list
        description: Compliance gaps found with severity and recommendations
      risk_summary:
        type: str
        description: Overall compliance risk assessment summary
  - id: emit_summary
    kind: emit
    depends_on:
      - flag_compliance_gaps
    data:
      clauses_found: "${state.extract_clauses.total_clauses}"
      categories: "${state.extract_clauses.categories_found}"
      gaps: "${state.flag_compliance_gaps.gaps}"
      risk_summary: "${state.flag_compliance_gaps.risk_summary}"
  - id: save_audit
    kind: action
    action: save_compliance_audit
    depends_on:
      - flag_compliance_gaps
    input:
      clauses_data: "${state.extract_clauses.clauses}"
      gaps_data: "${state.flag_compliance_gaps.gaps}"
      risk_summary: "${state.flag_compliance_gaps.risk_summary}"
      doc_title: "Vendor Agreement"
semantic_outputs:
  audit_report: save_audit
tags:
  - compliance
  - audit
  - contract
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Sample vendor agreement (abridged)
# ═══════════════════════════════════════════════════════════════════════════════

VENDOR_AGREEMENT = """
VENDOR SERVICES AGREEMENT

This Vendor Services Agreement ("Agreement") is entered into as of May 15, 2026,
between AcmeTech Solutions Inc. ("Company") and DataPipe Analytics LLC ("Vendor").

1. SERVICES. Vendor shall provide real-time data processing and analytics
services as described in Exhibit A. Company grants Vendor access to Company's
production databases for the purpose of providing the Services.

2. PAYMENT. Company shall pay Vendor $18,500 per month, net 45. Late payments
shall accrue interest at 1.5% per month. Vendor may suspend services if any
invoice remains unpaid for more than 60 days.

3. DATA HANDLING. Vendor will process Company customer data including names,
email addresses, purchase history, and device identifiers. Vendor shall use
commercially reasonable efforts to protect such data. Vendor may use aggregated
and anonymized data for product improvement and benchmarking.

4. INTELLECTUAL PROPERTY. Any algorithms, models, or software developed by
Vendor in the course of providing the Services shall remain the sole property
of Vendor. Company retains ownership of its data and any derivative reports
generated by the Services.

5. TERM AND TERMINATION. This Agreement commences on the Effective Date and
continues for 12 months. Either party may terminate with 30 days written notice.
In the event of material breach, the non-breaching party may terminate
immediately upon written notice.

6. LIMITATION OF LIABILITY. VENDOR'S TOTAL LIABILITY UNDER THIS AGREEMENT SHALL
NOT EXCEED THE FEES PAID BY COMPANY IN THE 3 MONTHS PRECEDING THE CLAIM. IN NO
EVENT SHALL VENDOR BE LIABLE FOR INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES.

7. CONFIDENTIALITY. Each party agrees to maintain the confidentiality of the
other party's proprietary information. "Proprietary Information" does not
include information that is or becomes publicly available through no fault of
the receiving party.

8. GOVERNING LAW. This Agreement shall be governed by the laws of the State of
Delaware. Any disputes shall be resolved through binding arbitration in
Wilmington, Delaware.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Action — save the compliance audit
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_save_compliance_audit(
    clauses_data: Any = None,
    gaps_data: Any = None,
    risk_summary: str = "",
    doc_title: str = "Document",
    **kwargs,
) -> dict[str, Any]:
    clauses_str = json.dumps(clauses_data, indent=2, ensure_ascii=False) if isinstance(clauses_data, (dict, list)) else str(clauses_data or "")
    gaps_str = json.dumps(gaps_data, indent=2, ensure_ascii=False) if isinstance(gaps_data, (dict, list)) else str(gaps_data or "")

    report = f"""# Compliance Audit Report — {doc_title}
Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## Risk Summary
{risk_summary or '*Not available*'}

---

## Extracted Clauses
{clauses_str}

---

## Compliance Gaps & Recommendations
{gaps_str}
"""

    reports_dir = Path.home() / ".agently_compliance_audits"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = doc_title.lower().replace(" ", "-")
    filepath = reports_dir / f"{slug}_{timestamp}.md"
    filepath.write_text(report)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════


def _build_extract_panel(clauses_text: str | None, total: int | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if done and total is not None:
        body = Text(f"Extracted [bold]{total}[/] clauses")
        if clauses_text:
            body.append(f"\n\n{str(clauses_text)[:300]}")
    elif running:
        body = Text("Extracting clauses from contract...", style="dim")
    else:
        body = Text("Waiting...", style="dim")

    return Panel(body, title=f"{icon} Extract Clauses (model)", border_style="blue")


def _build_validate_panel(passed: bool, done: bool) -> Panel:
    if done and passed:
        icon = "[bold green]✓[/]"
        body = Text("[green]Validation passed[/] — required clauses present")
    elif done:
        icon = "[bold red]✗[/]"
        body = Text("[red]Validation failed[/] — missing required clauses")
    else:
        icon = "[dim]·[/]"
        body = Text("Waiting on extraction...", style="dim")
    return Panel(body, title=f"{icon} Validate (validate)", border_style="yellow")


def _build_gaps_panel(gaps_text: str | None, risk: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    parts = []
    if risk:
        parts.append(f"[bold]Risk:[/] {risk[:200]}")
    if gaps_text:
        parts.append(str(gaps_text)[:300])
    body = Text("\n\n".join(parts)) if parts else (Text("Analyzing gaps...", style="dim") if running else Text("Waiting...", style="dim"))
    return Panel(body, title=f"{icon} Flag Gaps (model)", border_style="red")


def _build_emit_panel(emitted: bool) -> Panel:
    if emitted:
        return Panel(
            Text("[green]Structured audit summary emitted to runtime stream[/]"),
            title="[bold green]✓[/] Emit (emit)",
            border_style="green",
        )
    return Panel(Text("Waiting...", style="dim"), title="[dim]·[/] Emit (emit)", border_style="dim")


def _build_output_panel(file_path: str, done: bool) -> Panel:
    if done and file_path:
        return Panel(
            Text(f"Saved: [green]{file_path}[/]"),
            title="[bold green]✓[/] Output",
            border_style="green",
        )
    return Panel(Text("waiting...", style="dim"), title="[dim]·[/] Output", border_style="dim")


def _build_layout(extract: Panel, validate: Panel, gaps: Panel, emit: Panel, output: Panel) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="row1", ratio=1),
        Layout(name="row2", ratio=1),
        Layout(name="row3", ratio=1),
    )
    layout["row1"].split_row(Layout(extract, name="extract"), Layout(validate, name="validate"))
    layout["row2"].split_row(Layout(gaps, name="gaps"), Layout(emit, name="emit"))
    layout["row3"].split_row(Layout(output, name="output"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.3)

    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    skill_dir = runtime_dir / "compliance-audit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(COMPLIANCE_SKILL_YAML.strip())

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("compliance-auditor")

    agent.register_action(
        name="save_compliance_audit",
        desc="Save the compliance audit report to disk.",
        kwargs={
            "clauses_data": ("any", "Extracted clauses data."),
            "gaps_data": ("any", "Compliance gaps data."),
            "risk_summary": ("str", "Overall risk assessment."),
            "doc_title": ("str", "Document title."),
        },
        func=_action_save_compliance_audit,
    )

    execution = (
        agent
        .use_skills(["compliance-audit"], mode="required")
        .input(VENDOR_AGREEMENT)
        .create_execution()
    )

    completed_stages: set[str] = set()
    stage_running: dict[str, bool] = {}
    clauses_data: str | None = None
    total_clauses: int | None = None
    validate_passed = False
    gaps_data: str | None = None
    risk_summary: str | None = None
    emit_done = False
    saved_file = ""
    save_done = False

    extract_panel = _build_extract_panel(None, None, False, False)
    validate_panel = _build_validate_panel(False, False)
    gaps_panel = _build_gaps_panel(None, None, False, False)
    emit_panel = _build_emit_panel(False)
    output_panel = _build_output_panel("", False)
    layout = _build_layout(extract_panel, validate_panel, gaps_panel, emit_panel, output_panel)

    with Live(layout, refresh_per_second=10, screen=True, transient=False) as live:
        async for item in execution.get_async_generator(type="instant"):
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in ("extract_clauses", "validate_extraction", "flag_compliance_gaps", "emit_summary", "save_audit"):
                    if sid in item.path:
                        stage_running[sid] = True

            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # Stream field deltas
            if item.path == "skills.stages.extract_clauses.fields.clauses" and item.delta:
                clauses_data = (clauses_data or "") + item.delta
            elif item.path == "skills.stages.extract_clauses.fields.total_clauses" and item.delta:
                tc = (str(total_clauses or "") + item.delta).strip()
                try:
                    total_clauses = int(tc)
                except ValueError:
                    pass
            elif item.path == "skills.stages.flag_compliance_gaps.fields.gaps" and item.delta:
                gaps_data = (gaps_data or "") + item.delta
            elif item.path == "skills.stages.flag_compliance_gaps.fields.risk_summary" and item.delta:
                risk_summary = (risk_summary or "") + item.delta

            # Capture emit completion
            if item.path.startswith("skills.stages.emit_summary"):
                if ".fields." not in item.path and item.is_complete:
                    emit_done = True
                elif item.is_complete and item.value:
                    emit_done = True

            if "validate_extraction" in completed_stages:
                validate_passed = True

            if item.path.startswith("actions.") and item.is_complete:
                result = item.value.get("result", item.value) if isinstance(item.value, dict) else item.value
                if isinstance(result, dict) and result.get("file_path"):
                    saved_file = result.get("file_path", "")
                    save_done = True

            extract_panel = _build_extract_panel(
                clauses_data, total_clauses,
                "extract_clauses" in completed_stages,
                stage_running.get("extract_clauses", False),
            )
            validate_panel = _build_validate_panel(validate_passed, "validate_extraction" in completed_stages)
            gaps_panel = _build_gaps_panel(
                gaps_data, risk_summary,
                "flag_compliance_gaps" in completed_stages,
                stage_running.get("flag_compliance_gaps", False),
            )
            emit_panel = _build_emit_panel(emit_done)
            output_panel = _build_output_panel(saved_file, save_done)
            live.update(_build_layout(extract_panel, validate_panel, gaps_panel, emit_panel, output_panel))

    # ── Final summary ──────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\nroute: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"total clauses extracted: {total_clauses}")
    print(f"validation passed: {validate_passed}")
    print(f"risk summary: {risk_summary or 'N/A'}")
    print(f"audit report saved: {saved_file or '(not saved)'}")

    if risk_summary:
        print(f"\n[Risk summary]:")
        print(risk_summary[:400])
    if gaps_data:
        print(f"\n[Gaps preview]:")
        print(str(gaps_data)[:400])


if __name__ == "__main__":
    asyncio.run(main())
