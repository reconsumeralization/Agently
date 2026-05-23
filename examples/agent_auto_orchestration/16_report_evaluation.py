"""Report evaluation pipeline — assesses research reports against their original topics.

Run:
    python examples/agent_auto_orchestration/16_report_evaluation.py <report_file.md>

    # with explicit topic override (otherwise extracted from report frontmatter):
    python examples/agent_auto_orchestration/16_report_evaluation.py report.md --topic "RISC-V vs ARM"

    # specify output language for the evaluation:
    python examples/agent_auto_orchestration/16_report_evaluation.py report.md --lang zh

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This skill evaluates a research report across 6 quality dimensions:

  1. Content Relevance (signal-to-noise ratio)
  2. Coverage Completeness
  3. Source & Data Authority
  4. Depth Balance
  5. Internal Consistency
  6. Decision Quality (research process)

Each dimension receives a conceptual quality level, specific issues are flagged,
and actionable remediation steps are generated. Numeric display values are
derived by code after model output; the model does not directly score reports.

Skill stages (5):
  1. extract_metadata   (model)      — extract topic, dimensions, key claims from report
  2. evaluate_dimensions (model)      — grade all 6 dimensions with evidence
  3. synthesize_findings (model)      — produce cross-cutting analysis + overall level
  4. generate_remediation (model)     — produce specific, actionable fix recommendations
  5. save_evaluation    (action)      — format and save the evaluation to disk
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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

from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

REPORT_EVALUATION_SKILL_YAML = """
skill_id: report-evaluator
version: 1.0.0
display_name: Research Report Evaluator
purpose: >
  Evaluate a research report against its original topic across 6 quality
  dimensions: content relevance, coverage completeness, source authority,
  depth balance, internal consistency, and decision quality. Produces a
  structured evaluation matrix with conceptual levels, specific issues, and actionable
  remediation steps. Fully generic — works on any domain.
trust_level: local
kind: workflow
activation:
  keywords:
    - evaluate
    - assess
    - quality
    - audit
    - review report
requires:
  actions:
    - save_evaluation
stages:
  - id: extract_metadata
    kind: model
    purpose: >
      You are a metadata extraction specialist. Read the provided research
      report and extract structured information. Be precise and factual —
      only extract what is explicitly stated in the report.

      Extract:
      1. The research topic/question (as stated in the report)
      2. All research dimensions/axes the report claims to cover
      3. Key claims made (at least 5, up to 15) — direct quotes or close
         paraphrases with section references
      4. Data sources cited (names, URLs if present)
      5. Research methodology notes (rounds, dimensions, articles browsed,
         dives — if mentioned)
      6. Any self-acknowledged limitations or gaps

      If the report has frontmatter metadata (topic, date, mode, dimensions,
      rounds, browsed, dives), extract that too.
    input:
      report: "${task}"
    output_schema:
      topic:
        type: str
        description: The research topic/question as stated in the report
      dimensions_covered:
        type: str
        description: >
          JSON array of dimension names the report claims to cover.
          Example: ["Technical Architecture", "Market Adoption", "Licensing"]
      key_claims_json:
        type: str
        description: >
          JSON array of key claims. Each: {"claim": "...", "section": "...", "has_source": true/false}
      sources_cited:
        type: str
        description: JSON array of source names/URLs cited
      methodology_notes:
        type: str
        description: Methodology metadata extracted from the report
      self_acknowledged_gaps:
        type: str
        description: JSON array of gaps the report itself acknowledges
  - id: evaluate_dimensions
    kind: model
    depends_on:
      - extract_metadata
    purpose: >
      You are a rigorous research quality auditor. Evaluate the research report
      against the extracted metadata across 6 dimensions. For each dimension:

      Use conceptual quality levels instead of numeric scores. For each dimension provide:
      - level: EXCELLENT / ADEQUATE / WEAK / FAILED
      - Specific evidence from the report (quotes or section references)
      - Issues found (be specific — name the irrelevant content, missing angles,
        weak sources, etc.)
      - Severity: CRITICAL / MAJOR / MINOR

      Level definitions:
      - EXCELLENT: strong evidence, directly relevant, complete enough for the
        claimed scope, and no material unresolved issue.
      - ADEQUATE: usable with limitations; evidence mostly supports the claim,
        but some gaps or weaker source quality remain.
      - WEAK: materially incomplete, weakly sourced, generic, or only partially
        relevant; requires substantial remediation before use.
      - FAILED: absent, off-topic, unsupported, contradictory, or misleading.

      THE SIX DIMENSIONS:

      1. CONTENT RELEVANCE (signal-to-noise ratio)
         - What percentage of the report's substantive content is directly
           relevant to the stated topic?
         - Identify specific passages, sections, or claims that are off-topic,
           tangential, or belong to a different domain.
         - Flag padding, generic filler, or content that could apply to any topic.
         - EXCELLENT: tightly focused, every section serves the topic.

      2. COVERAGE COMPLETENESS
         - Does the report cover all dimensions it claims to cover?
         - Are there obvious missing angles not mentioned even in gaps?
         - For each claimed dimension: is the treatment substantive or superficial?
         - EXCELLENT: all claimed dimensions get meaningful treatment,
           gaps are honestly acknowledged.

      3. SOURCE & DATA AUTHORITY
         - Are cited sources authoritative? (peer-reviewed, official, primary
           vs. blogs, aggregators, advocacy)
         - Are quantitative claims backed by specific sources?
         - Are there unsupported assertions presented as fact?
         - EXCELLENT: claims trace to identifiable, reputable sources.

      4. DEPTH BALANCE
         - Do dimensions that need deep treatment get it?
         - Are surface-level dimensions appropriately treated as such?
         - Is there asymmetry that makes sense given the topic, or is it arbitrary?
         - EXCELLENT: depth variation is intentional and explained, not accidental.

      5. INTERNAL CONSISTENCY
         - Are there contradictions between sections?
         - Do the executive summary, per-dimension analysis, and conclusions align?
         - Are numbers/metrics consistent across the report?
         - EXCELLENT: no contradictions, consistent narrative throughout.

      6. DECISION QUALITY (research process)
         - If the report describes its research methodology: were the decisions
           sound? (dimension selection, depth allocation, sufficiency calls)
         - If the research process marked a dimension as needing DEEP_DIVE but
           no deep-dive was done and sufficiency was still claimed — that's a red flag.
         - If the methodology is not described, note that as a limitation.
         - EXCELLENT: methodology decisions are well-reasoned and internally
           consistent with the actual report content.

      Return a structured JSON evaluation.
    input:
      report: "${task}"
      topic: "${state.extract_metadata.topic}"
      dimensions_covered: "${state.extract_metadata.dimensions_covered}"
      key_claims: "${state.extract_metadata.key_claims_json}"
      sources: "${state.extract_metadata.sources_cited}"
      methodology: "${state.extract_metadata.methodology_notes}"
      self_gaps: "${state.extract_metadata.self_acknowledged_gaps}"
    output_schema:
      dimension_scores_json:
        type: str
        description: >
          JSON object with one key per dimension. Each value:
          {"level": "EXCELLENT|ADEQUATE|WEAK|FAILED", "evidence": ["quote or reference", ...],
           "issues": [{"description": "...", "severity": "CRITICAL|MAJOR|MINOR"}, ...],
           "assessment": "1-2 sentence summary"}
      overall_level:
        type: str
        description: Overall quality level. One of EXCELLENT, ADEQUATE, WEAK, FAILED.
  - id: synthesize_findings
    kind: model
    depends_on:
      - evaluate_dimensions
    purpose: >
      You are a research quality director. Synthesize the dimension-level
      evaluations into a coherent assessment. Produce:

      1. Cross-cutting patterns — issues or strengths that span multiple dimensions
      2. Root cause analysis — if there are systematic problems (e.g., search was
         too broad → relevance suffers → authority diluted), trace the chain
      3. Overall assessment — is this report trustworthy? Fit for what purpose?
      4. Confidence in the evaluation itself — what would you need to be more confident?

      Be direct and specific. Avoid generic praise or criticism.
    input:
      topic: "${state.extract_metadata.topic}"
      dimension_scores: "${state.evaluate_dimensions.dimension_scores_json}"
      overall_level: "${state.evaluate_dimensions.overall_level}"
      report_excerpt: "${task}"  # first 3000 chars for context
    output_schema:
      cross_cutting:
        type: str
        description: Cross-cutting patterns and root cause analysis
      overall_assessment:
        type: str
        description: 2-3 paragraph overall verdict
      evaluator_confidence:
        type: str
        description: What would increase confidence in this evaluation
  - id: generate_remediation
    kind: model
    depends_on:
      - synthesize_findings
    purpose: >
      You are a research methodology consultant. Based on the evaluation
      findings, generate specific, actionable remediation steps.

      Rules:
      - Each recommendation must be CONCRETE (not "improve sources" but
        "search for peer-reviewed papers on X via Google Scholar")
      - Each must be ACTIONABLE by a researcher or pipeline developer
      - Prioritize by impact: fix the issues that would most improve the report
      - Include both content fixes (what to re-research) and process fixes
        (how to change the research pipeline to prevent recurrence)

      If the report's issues are fundamental (e.g., completely off-topic),
      say so — don't suggest minor tweaks to a broken foundation.
    input:
      topic: "${state.extract_metadata.topic}"
      dimension_scores: "${state.evaluate_dimensions.dimension_scores_json}"
      cross_cutting: "${state.synthesize_findings.cross_cutting}"
      overall_assessment: "${state.synthesize_findings.overall_assessment}"
    output_schema:
      remediation_plan:
        type: str
        description: >
          Structured remediation plan with prioritized recommendations.
          Each recommendation: priority (P0/P1/P2), category (content/process),
          what to do, expected impact.
  - id: save_evaluation
    kind: action
    action: save_evaluation
    depends_on:
      - generate_remediation
    input:
      report: "${task}"
      topic: "${state.extract_metadata.topic}"
      dimension_scores: "${state.evaluate_dimensions.dimension_scores_json}"
      overall_level: "${state.evaluate_dimensions.overall_level}"
      cross_cutting: "${state.synthesize_findings.cross_cutting}"
      overall_assessment: "${state.synthesize_findings.overall_assessment}"
      remediation_plan: "${state.generate_remediation.remediation_plan}"
semantic_outputs:
  evaluation_report: save_evaluation
tags:
  - evaluation
  - quality
  - audit
  - workflow
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Action: save_evaluation
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_save_evaluation(
    report: str = "",
    topic: str = "",
    dimension_scores: str = "",
    overall_level: str = "",
    cross_cutting: str = "",
    overall_assessment: str = "",
    remediation_plan: str = "",
    **kwargs,
) -> dict[str, Any]:
    """Format and save the evaluation report."""

    # Parse dimension levels for display with deterministic mapped scores.
    dims_table = ""
    overall_score = 0.0
    try:
        scores = json.loads(dimension_scores) if isinstance(dimension_scores, str) else dimension_scores
        if isinstance(scores, dict):
            rows = []
            for dim, info in scores.items():
                score = _score_from_level_info(info)
                level = _level_from_info(info)
                bar = "█" * int(float(score)) + "░" * (10 - int(float(score)))
                rows.append(f"| {dim} | {level} | {score}/10 | {bar} |")
            overall_score = _overall_score_from_levels(scores)
            dims_table = "\n".join(rows)
    except (json.JSONDecodeError, TypeError):
        dims_table = "(could not parse scores)"
    if overall_score == 0.0 and overall_level:
        overall_score = _score_from_level(overall_level)

    doc = f"""# Research Report Evaluation

**Topic:** {topic[:200]}
**Evaluated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
**Overall Level:** {overall_level or '*Not available*'}
**Mapped Overall Score:** {overall_score}/10

---

## Dimension Levels

| Dimension | Level | Mapped Score | Distribution |
|-----------|-------|--------------|-------------|
{dims_table}

---

## Cross-Cutting Analysis

{cross_cutting or '*Not available*'}

---

## Overall Assessment

{overall_assessment or '*Not available*'}

---

## Remediation Plan

{remediation_plan or '*Not available*'}

---

## Evaluated Report (reference)

{report[:2000]}{'...' if len(report) > 2000 else ''}

---

*Generated by Agently Report Evaluation Pipeline*
"""

    evals_dir = Path.home() / ".agently_deep_research" / "evaluations"
    evals_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()[:60]).strip("-")
    filepath = evals_dir / f"eval-{slug}_{timestamp}.md"
    filepath.write_text(doc)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "overall_score": overall_score,
        "overall_level": overall_level,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════

def _score_color(score: float) -> str:
    if score >= 7:
        return "green"
    if score >= 4:
        return "yellow"
    return "red"


_EVALUATION_LEVEL_SCORES = {
    "EXCELLENT": 9.0,
    "ADEQUATE": 6.0,
    "WEAK": 3.0,
    "FAILED": 0.0,
}


def _level_from_info(info: Any) -> str:
    if isinstance(info, dict):
        value = info.get("level") or info.get("grade") or info.get("quality_level")
        if value is not None:
            normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
            aliases = {
                "HIGH": "EXCELLENT",
                "MEDIUM": "ADEQUATE",
                "LOW": "WEAK",
                "CRITICAL": "FAILED",
            }
            return aliases.get(normalized, normalized)
    return "WEAK"


def _score_from_level(level: str) -> float:
    return _EVALUATION_LEVEL_SCORES.get(str(level).strip().upper().replace("-", "_").replace(" ", "_"), 3.0)


def _score_from_level_info(info: Any) -> float:
    if isinstance(info, dict) and "score" in info:
        try:
            return float(info.get("score"))
        except (TypeError, ValueError):
            pass
    return _score_from_level(_level_from_info(info))


def _overall_score_from_levels(scores: dict[str, Any]) -> float:
    if not scores:
        return 0.0
    values = [_score_from_level_info(info) for info in scores.values()]
    return round(sum(values) / len(values), 2) if values else 0.0


def _build_score_table(scores: dict[str, Any] | None) -> Table:
    table = Table(title="Dimension Levels", border_style="blue")
    table.add_column("Dimension", style="cyan")
    table.add_column("Score", justify="center")
    table.add_column("Bar", justify="left")

    if scores:
        for dim, info in scores.items():
            s = _score_from_level_info(info)
            level = _level_from_info(info)
            bar = "█" * int(s) + "░" * (10 - int(s))
            color = _score_color(s)
            table.add_row(dim, f"[bold {color}]{level}[/] ({s}/10)", f"[{color}]{bar}[/]")
    else:
        for dim in ["Content Relevance", "Coverage Completeness", "Source Authority",
                     "Depth Balance", "Internal Consistency", "Decision Quality"]:
            table.add_row(dim, "[dim]?[/]", "[dim]░░░░░░░░░░[/]")
    return table


def _build_meta_panel(topic: str, overall: float | None, done: bool) -> Panel:
    lines = [f"Topic: {topic[:150]}{'...' if len(topic) > 150 else ''}"]
    if overall is not None:
        color = _score_color(overall)
        lines.append(f"Mapped Overall: [bold {color}]{overall}/10[/]")
    elif done:
        lines.append("Mapped Overall: [dim]--[/]")
    else:
        lines.append("Mapped Overall: [dim]evaluating...[/]")
    icon = "[bold green]✓[/]" if done else "[bold yellow]◎[/]"
    return Panel(Text.from_markup("\n".join(lines)), title=f"{icon} Report Evaluation", border_style="yellow")


def _build_stage_panel(stage_name: str, done: bool, running: bool, content: str | None) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
        body = Text((content or "")[:400])
    elif running:
        icon = "[bold yellow]◎[/]"
        body = Text(f"Running {stage_name}...", style="dim")
    else:
        icon = "[dim]·[/]"
        body = Text(f"Waiting...", style="dim")
    return Panel(body, title=f"{icon} {stage_name}", border_style="green" if done else "blue")


def _build_output_panel(file_path: str, done: bool) -> Panel:
    if done and file_path:
        return Panel(Text.from_markup(f"[green]{file_path}[/]"), title="[bold green]✓[/] Output", border_style="green")
    return Panel(Text("waiting...", style="dim"), title="[dim]·[/] Output", border_style="dim")


def _build_layout(
    meta: Panel, scores: Table,
    extract: Panel, evaluate: Panel, synthesize: Panel, remediate: Panel, output: Panel,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=2),
        Layout(name="mid", ratio=4),
        Layout(name="bottom", ratio=2),
    )
    layout["top"].split_row(Layout(meta, name="meta", ratio=1), Layout(scores, name="scores", ratio=2))
    layout["mid"].split_row(
        Layout(extract, name="extract", ratio=1),
        Layout(evaluate, name="evaluate", ratio=1),
        Layout(synthesize, name="synthesize", ratio=1),
    )
    layout["bottom"].split_row(
        Layout(remediate, name="remediate", ratio=2),
        Layout(output, name="output", ratio=1),
    )
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    import os

    args = sys.argv[1:]
    report_path = ""
    topic_override = ""
    language = os.environ.get("AGENTLY_RESEARCH_LANGUAGE", "").strip()
    i = 0
    while i < len(args):
        if args[i] in ("--topic", "-t") and i + 1 < len(args):
            topic_override = args[i + 1]
            i += 2
        elif args[i].startswith("--topic="):
            topic_override = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--lang", "-l") and i + 1 < len(args):
            language = args[i + 1]
            i += 2
        elif args[i].startswith("--lang="):
            language = args[i].split("=", 1)[1]
            i += 1
        elif not args[i].startswith("-"):
            report_path = args[i]
            i += 1
        else:
            i += 1

    if not report_path:
        print("Usage: python 16_report_evaluation.py <report_file.md> [--topic \"...\"] [--lang zh]")
        print()
        print("  report_file.md : path to a research report (Markdown)")
        print("  --topic, -t     : override the topic (otherwise extracted from report)")
        print("  --lang, -l      : output language for the evaluation")
        print()
        print("Environment:")
        print("  AGENTLY_EVAL_REPORT : path to report file (alternative to CLI arg)")
        sys.exit(1)

    # Resolve report path
    rp = Path(report_path)
    if not rp.exists():
        # Try env var
        alt = os.environ.get("AGENTLY_EVAL_REPORT", "")
        if alt:
            rp = Path(alt)
        if not rp.exists():
            print(f"Report file not found: {report_path}")
            sys.exit(1)

    report_text = rp.read_text()
    print(f"\n{'─'*60}")
    print(f"Report: {rp.name} ({len(report_text):,} chars)")
    print(f"{'─'*60}\n")

    provider = configure_model(temperature=0.2)  # lower temp for evaluation
    model_name = (
        os.environ.get("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
        if provider == "deepseek"
        else os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
    )
    print(f"Model: {provider} :: {model_name}\n")

    # Build task input: topic (if overridden) + report text
    task_input = report_text
    if topic_override:
        task_input = f"RESEARCH TOPIC: {topic_override}\n\n---\n\nREPORT:\n{report_text}"
        print(f"Topic override: {topic_override[:200]}\n")

    # Apply language
    LANG_NAMES = {
        "zh": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)",
        "en": "English", "ja": "Japanese", "ko": "Korean",
        "fr": "French", "de": "German", "es": "Spanish",
        "pt": "Portuguese", "ru": "Russian", "ar": "Arabic",
    }
    if language and language != "auto":
        lang_display = LANG_NAMES.get(language.lower(), language)
        task_input = (
            f"[OUTPUT LANGUAGE: {lang_display} — "
            f"ALL evaluation content MUST be written in {lang_display}]\n\n"
            f"{task_input}"
        )
        print(f"Output language: {lang_display}")
    else:
        print("Output language: auto")

    # ── Setup ──────────────────────────────────────────────────────────────
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_eval_"))
    registry_root = runtime_dir / "registry"
    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)

    skill_dir = runtime_dir / "report-evaluator"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(REPORT_EVALUATION_SKILL_YAML.strip())
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("report-evaluator-agent")
    agent.register_action(
        name="save_evaluation",
        desc="Save the evaluation report to disk.",
        kwargs={
            "report": ("str", "Original report text."),
            "topic": ("str", "Research topic."),
            "dimension_scores": ("str", "JSON dimension conceptual levels."),
            "overall_level": ("str", "Overall conceptual quality level."),
            "cross_cutting": ("str", "Cross-cutting analysis."),
            "overall_assessment": ("str", "Overall assessment."),
            "remediation_plan": ("str", "Remediation plan."),
        },
        func=_action_save_evaluation,
    )

    execution = (
        agent
        .use_skills(["report-evaluator"], mode="required")
        .input(task_input)
        .create_execution()
    )

    # ── State trackers ─────────────────────────────────────────────────────
    completed_stages: set[str] = set()
    stage_running: dict[str, bool] = {}

    topic_extracted = ""
    scores: dict[str, Any] = {}
    overall_score: float | None = None
    cross_cutting_text = ""
    assessment_text = ""
    remediation_text = ""
    saved_file = ""
    save_done = False

    # ── Initial panels ─────────────────────────────────────────────────────
    meta_panel = _build_meta_panel(topic_override or "(extracting...)", None, False)
    score_table = _build_score_table(None)
    extract_panel = _build_stage_panel("Extract Metadata", False, False, None)
    evaluate_panel = _build_stage_panel("Evaluate Dimensions", False, False, None)
    synthesize_panel = _build_stage_panel("Synthesize Findings", False, False, None)
    remediate_panel = _build_stage_panel("Generate Remediation", False, False, None)
    output_panel = _build_output_panel("", False)
    layout = _build_layout(meta_panel, score_table, extract_panel, evaluate_panel,
                           synthesize_panel, remediate_panel, output_panel)

    console = Console(force_terminal=True)
    with Live(layout, console=console, refresh_per_second=6, screen=False, transient=False) as live:
        gen = execution.get_async_generator(type="instant")
        next_item = asyncio.create_task(gen.__anext__())
        while True:
            try:
                item = await asyncio.wait_for(asyncio.shield(next_item), timeout=0.4)
            except asyncio.TimeoutError:
                live.update(
                    _build_layout(meta_panel, score_table, extract_panel,
                                  evaluate_panel, synthesize_panel, remediate_panel, output_panel),
                    refresh=True,
                )
                continue
            except StopAsyncIteration:
                break
            next_item = asyncio.create_task(gen.__anext__())

            # Stage starts
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in ("extract_metadata", "evaluate_dimensions", "synthesize_findings",
                            "generate_remediation", "save_evaluation"):
                    if sid in item.path:
                        stage_running[sid] = True

            # Stage completions
            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # Field deltas
            if item.path == "skills.stages.extract_metadata.fields.topic" and item.delta:
                topic_extracted += item.delta
            elif item.path == "skills.stages.synthesize_findings.fields.cross_cutting" and item.delta:
                cross_cutting_text += item.delta
            elif item.path == "skills.stages.synthesize_findings.fields.overall_assessment" and item.delta:
                assessment_text += item.delta
            elif item.path == "skills.stages.generate_remediation.fields.remediation_plan" and item.delta:
                remediation_text += item.delta

            # Action completions
            if item.path.startswith("actions.") and item.is_complete:
                val = item.value or {}
                result = val.get("result", val) if isinstance(val, dict) else val
                if isinstance(result, dict) and "file_path" in result:
                    saved_file = result.get("file_path", "")
                    save_done = True

            # Parse scores from evaluate_dimensions completion
            if "evaluate_dimensions" in completed_stages and not scores:
                for stage_id, stage_data in (item.value or {}).items():
                    if isinstance(stage_data, dict):
                        dims_json = stage_data.get("dimension_scores_json", "")
                        ov = stage_data.get("overall_level")
                        if isinstance(dims_json, str) and dims_json:
                            try:
                                scores = json.loads(dims_json)
                                if isinstance(scores, dict):
                                    overall_score = _overall_score_from_levels(scores)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        if overall_score is None and ov is not None:
                            overall_score = _score_from_level(str(ov))

            # Update panels
            meta_panel = _build_meta_panel(
                topic_override or topic_extracted, overall_score,
                "save_evaluation" in completed_stages,
            )
            score_table = _build_score_table(scores if scores else None)
            extract_panel = _build_stage_panel(
                "Extract Metadata", "extract_metadata" in completed_stages,
                stage_running.get("extract_metadata", False),
                f"Topic: {topic_extracted[:200]}" if topic_extracted else None,
            )
            evaluate_panel = _build_stage_panel(
                "Evaluate Dimensions", "evaluate_dimensions" in completed_stages,
                stage_running.get("evaluate_dimensions", False),
                f"Mapped overall: {overall_score}/10" if overall_score is not None else None,
            )
            synthesize_panel = _build_stage_panel(
                "Synthesize Findings", "synthesize_findings" in completed_stages,
                stage_running.get("synthesize_findings", False),
                cross_cutting_text[:400] if cross_cutting_text else None,
            )
            remediate_panel = _build_stage_panel(
                "Generate Remediation", "generate_remediation" in completed_stages,
                stage_running.get("generate_remediation", False),
                remediation_text[:400] if remediation_text else None,
            )
            output_panel = _build_output_panel(saved_file, save_done)
            live.update(
                _build_layout(meta_panel, score_table, extract_panel, evaluate_panel,
                              synthesize_panel, remediate_panel, output_panel),
                refresh=True,
            )

    # ── Final summary ─────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\n{'='*70}")
    print(f"route: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    if overall_score is not None:
        print(f"mapped overall score: {overall_score}/10")
    if scores:
        print(f"\ndimension levels:")
        for dim, info in scores.items():
            s = info.get("score", "?")
            if s == "?":
                s = _score_from_level_info(info)
            issues_count = len(info.get("issues", []))
            print(f"  {dim}: {_level_from_info(info)} ({s}/10 mapped, {issues_count} issues)")
    print(f"\nsaved: {saved_file or '(not saved)'}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
