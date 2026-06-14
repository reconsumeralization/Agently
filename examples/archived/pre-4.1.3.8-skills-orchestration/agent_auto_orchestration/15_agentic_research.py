"""Agentic research — host-orchestrated adaptive rounds over a prompt-only Skill.

Run:
    python examples/agent_auto_orchestration/15_agentic_research.py
    python examples/agent_auto_orchestration/15_agentic_research.py --lang zh "AI agent frameworks"
    AGENTLY_RESEARCH_TOPIC="RISC-V ecosystem" python examples/agent_auto_orchestration/15_agentic_research.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

New-standard Skills model
-------------------------
The old design baked adaptive multi-round logic into staged Skill execution.
Under the new standard the *Skill* is pure ``SKILL.md`` guidance (research +
self-assess sufficiency in one pass); the *HOST* owns the agentic loop. Each
round runs the prompt-only Skill, which returns a report plus a sufficiency
judgement and remaining gaps; the host decides whether to run another round
(up to a cap) feeding the gaps back in. Orchestration lives in the host, not the
Skill.

Expected key output from one real DeepSeek run:
    rounds run: 1-3 (model stops early when sufficient)
    final sufficient: True/False
    report saved: .../agentic_research_report.md
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

DEFAULT_TOPIC = "How are AI coding agents changing software team workflows in 2026?"
MAX_ROUNDS = 3

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "agentic-research"


def parse_args() -> tuple[str, str]:
    args = sys.argv[1:]
    lang = ""
    if "--lang" in args:
        i = args.index("--lang")
        lang = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    topic = " ".join(a for a in args if not a.startswith("-")).strip()
    topic = topic or os.environ.get("AGENTLY_RESEARCH_TOPIC", "").strip() or DEFAULT_TOPIC
    return topic, lang


def install_skill() -> str:
    skill_src = SKILL_SOURCE
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


OUTPUT_SCHEMA: dict[str, Any] = {
    "report": (str, "The current full research report (markdown)", True),
    "sufficient": (bool, "True if coverage is now comprehensive and decision-useful", True),
    "gaps": ([str], "Remaining gaps to investigate (empty if sufficient)", True),
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    topic, lang = parse_args()
    skill_id = install_skill()
    agent = Agently.create_agent("agentic-researcher")

    divider = "=" * 60
    print(divider)
    print("Agentic Research — host adaptive-rounds loop + prompt-only Skill")
    print(f"Topic: {topic}{('  (lang=' + lang + ')') if lang else ''}")
    print(divider)

    lang_note = f" Write the report in this language: {lang}." if lang else ""
    report, gaps, sufficient = "", [], False

    # ── HOST agentic loop: the model gates whether another round runs ──
    for round_no in range(1, MAX_ROUNDS + 1):
        if round_no == 1:
            task = f"Research this topic and assess sufficiency: {topic}.{lang_note}"
        else:
            task = (
                f"Topic: {topic}.{lang_note}\n\nPrior report:\n{report}\n\n"
                f"Gaps to close this round:\n- " + "\n- ".join(str(g) for g in gaps)
            )
        print(f"\n[round {round_no}/{MAX_ROUNDS}] running research skill...")
        execution = await agent.async_run_skills_task(
            task, skills=[skill_id], mode="required", output=OUTPUT_SCHEMA,
        )
        if execution.status != "success":
            print("  skill status:", execution.status, execution.output)
            return
        result = execution.output or {}
        report = str(result.get("report", report))
        sufficient = bool(result.get("sufficient"))
        gaps = result.get("gaps", []) or []
        print(f"  sufficient={sufficient}  remaining gaps={len(gaps)}")
        if sufficient or not gaps:
            break

    out_path = Path(tempfile.mkdtemp(prefix="agently_research_")) / "agentic_research_report.md"
    out_path.write_text(f"# Agentic Research: {topic}\n\n{report}\n", encoding="utf-8")

    print(f"\n{divider}")
    print(f"rounds run: {round_no}")
    print(f"final sufficient: {sufficient}")
    print(f"report length: {len(report):,} chars")
    print(f"report saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
