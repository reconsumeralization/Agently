"""Research report evaluator — prompt-only Skill scoring a report by dimension.

Run:
    python examples/agent_auto_orchestration/16_report_evaluation.py
    python examples/agent_auto_orchestration/16_report_evaluation.py path/to/report.md
    AGENTLY_EVAL_REPORT=path/to/report.md python .../16_report_evaluation.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: evaluate a research report against its topic across six dimensions.
If no report path is supplied, a built-in sample report is evaluated.

New-standard Skills model
-------------------------
The old design used Skill ``model`` stages (extract metadata → evaluate). Under
the new standard the Skill is pure ``SKILL.md`` guidance: ONE prompt-only request
extracts what the report claims and evaluates all six dimensions, returning
conceptual levels + issues (shaped by ``semantic_outputs``). The HOST maps levels
to a numeric overall score for reporting.

Expected key output from one real DeepSeek run:
    skill status: success
    dimensions evaluated: 6
    mapped overall score: 0-10
    verdict: <pass/revise/fail-style judgement>
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

DIMENSIONS = [
    "content relevance",
    "coverage completeness",
    "source authority",
    "depth balance",
    "internal consistency",
    "decision quality",
]
_LEVEL_SCORES = {"EXCELLENT": 9.0, "ADEQUATE": 6.0, "WEAK": 3.0, "FAILED": 0.0}

SAMPLE_REPORT = """\
# Research Report: Should we adopt Rust for our data-ingestion service?

Topic: Evaluate migrating our Python data-ingestion service to Rust.

## Findings
Rust offers memory safety without GC and strong performance for CPU-bound
ingestion. Tokio provides mature async I/O. Several teams report 3-5x throughput
gains on parsing-heavy workloads. The ecosystem for Kafka and Parquet is solid.

## Tradeoffs
Hiring and ramp-up are slower; the team has no Rust experience. Rewrite risk is
high for a service already meeting SLOs. Interop with existing Python tooling
requires FFI or a service boundary.

## Recommendation
Pilot Rust on the single hottest parsing path behind a service boundary; keep
the rest in Python. Re-evaluate after measuring throughput and on-call burden.
"""

SKILL_MD = """\
---
name: Research Report Evaluator
description: >-
  Evaluate a research report against its stated topic across six dimensions
  (relevance, completeness, source authority, depth balance, consistency,
  decision quality), assigning a conceptual quality level and issues to each.
  Use for report evaluation, research review, and quality assessment.
keywords: [report evaluation, research review, quality assessment, rubric]
---

# Research Report Evaluator

You are a rigorous research reviewer. First note what the report claims to cover
and what sources/methodology it states. Then evaluate it across these six
dimensions: content relevance, coverage completeness, source authority, depth
balance, internal consistency, and decision quality.

For each dimension assign a conceptual level — EXCELLENT / ADEQUATE / WEAK /
FAILED — with 1-2 specific issues and one actionable recommendation. Then give an
overall verdict (e.g. publish / revise / reject) with a one-paragraph rationale.

Judge only what the report actually contains. Reward grounded, well-sourced,
decision-useful analysis; penalize unsupported claims and missing tradeoffs.
"""


def install_skill() -> str:
    skill_src = Path(tempfile.mkdtemp(prefix="agently_skill_src_")) / "research-report-evaluator"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


def _load_report() -> tuple[str, str]:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else os.environ.get("AGENTLY_EVAL_REPORT", "")
    if path and Path(path).is_file():
        return Path(path).name, Path(path).read_text(encoding="utf-8")
    return "sample_report.md", SAMPLE_REPORT


async def main() -> None:
    provider = configure_model(temperature=0.2)
    print(f"Model provider: {provider}\n")

    report_name, report_text = _load_report()
    skill_id = install_skill()
    agent = Agently.create_agent("report-evaluator")

    divider = "=" * 60
    print(divider)
    print(f"Research Report Evaluator — prompt-only Skill")
    print(f"Report: {report_name} ({len(report_text):,} chars)")
    print(divider)
    print("Evaluating across 6 dimensions...\n")

    execution = await agent.async_run_skills_task(
        f"Evaluate this research report:\n\n{report_text}",
        skills=[skill_id],
        mode="required",
        semantic_outputs={
            "claimed_topic": (str, "The topic/question the report addresses", True),
            "dimensions": (
                [{
                    "dimension": (str, f"One of: {', '.join(DIMENSIONS)}", True),
                    "level": (str, "EXCELLENT / ADEQUATE / WEAK / FAILED", True),
                    "issues": ([str], "1-2 specific issues", True),
                    "recommendation": (str, "One actionable recommendation", True),
                }],
                "Evaluation of all six dimensions",
                True,
            ),
            "verdict": (str, "Overall verdict: publish / revise / reject", True),
            "rationale": (str, "One-paragraph rationale", True),
        },
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    dims = result.get("dimensions", []) or []
    scores = [_LEVEL_SCORES.get(str(d.get("level", "")).strip().upper(), 3.0) for d in dims]
    overall = round(sum(scores) / len(scores), 1) if scores else 0.0

    print(f"\n  topic: {str(result.get('claimed_topic', '—'))[:90]}")
    print("\n  dimension levels:")
    for d in dims:
        lvl = str(d.get("level", "—")).upper()
        print(f"    {d.get('dimension', '—')}: {lvl} ({_LEVEL_SCORES.get(lvl, 3.0)}/10, {len(d.get('issues', []) or [])} issues)")

    print(f"\nskill status: {execution.status}")
    print(f"dimensions evaluated: {len(dims)}")
    print(f"mapped overall score: {overall}/10")
    print(f"verdict: {result.get('verdict', '—')}")


if __name__ == "__main__":
    asyncio.run(main())
