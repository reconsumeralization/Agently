"""Self-reflective research — host reflect→revise loop over a prompt-only Skill.

Run:
    python examples/agent_auto_orchestration/17_self_reflective_research.py
    python examples/agent_auto_orchestration/17_self_reflective_research.py "DePIN projects 2025"
    python examples/agent_auto_orchestration/17_self_reflective_research.py --lang zh "AI agent frameworks"
    AGENTLY_RESEARCH_TOPIC="RISC-V ecosystem" python examples/agent_auto_orchestration/17_self_reflective_research.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

New-standard Skills model
-------------------------
The old design baked research → evaluate → reflect → improve into staged Skill
execution. Under the new standard the *Skill* is pure ``SKILL.md`` guidance that
can either draft a report or critique-and-revise a prior draft; the *HOST* owns
the reflection loop and keeps the reflection trail. Each revision round runs the
prompt-only Skill and the host decides whether another revision is warranted
(up to a cap). Orchestration + trail = host; reasoning = Skill.

Expected key output from one real DeepSeek run:
    revisions: 1-2 (model stops when no material improvement remains)
    final needs_revision: False
    report saved: .../self_reflective_report.md  (with reflection trail)
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

DEFAULT_TOPIC = "What makes a durable moat for an AI application startup in 2026?"
MAX_REVISIONS = 2

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "self-reflective-research"


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
    "report": (str, "The current (possibly improved) report, markdown", True),
    "critique": (str, "Concise critique of the current version", True),
    "needs_revision": (bool, "True if another revision round is warranted", True),
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    topic, lang = parse_args()
    skill_id = install_skill()
    agent = Agently.create_agent("reflective-researcher")

    divider = "=" * 60
    print(divider)
    print("Self-Reflective Research — host reflect→revise loop + prompt-only Skill")
    print(f"Topic: {topic}{('  (lang=' + lang + ')') if lang else ''}")
    print(divider)

    lang_note = f" Write the report in this language: {lang}." if lang else ""
    report, critique, needs_revision = "", "", True
    trail: list[str] = []

    # v1 draft + up to MAX_REVISIONS reflection rounds — host-orchestrated.
    for rev in range(0, MAX_REVISIONS + 1):
        if rev == 0:
            task = f"Draft and self-critique a research report on: {topic}.{lang_note}"
            label = "draft v1"
        else:
            task = (
                f"Topic: {topic}.{lang_note}\n\nPrior draft:\n{report}\n\n"
                f"Your critique to address:\n{critique}\n\nProduce an improved version."
            )
            label = f"revision {rev}"
        print(f"\n[{label}] running skill...")
        execution = await agent.async_run_skills_task(
            task, skills=[skill_id], mode="required", output=OUTPUT_SCHEMA,
        )
        if execution.status != "success":
            print("  skill status:", execution.status, execution.output)
            return
        result = execution.output or {}
        report = str(result.get("report", report))
        critique = str(result.get("critique", ""))
        needs_revision = bool(result.get("needs_revision"))
        trail.append(f"## {label}\nCritique: {critique}\nneeds_revision={needs_revision}")
        print(f"  needs_revision={needs_revision}  critique: {critique[:100]}")
        if not needs_revision:
            break

    out_path = Path(tempfile.mkdtemp(prefix="agently_research_")) / "self_reflective_report.md"
    out_path.write_text(
        f"# Self-Reflective Research: {topic}\n\n{report}\n\n---\n\n# Reflection Trail\n\n"
        + "\n\n".join(trail),
        encoding="utf-8",
    )

    print(f"\n{divider}")
    print(f"revisions: {rev}")
    print(f"final needs_revision: {needs_revision}")
    print(f"report length: {len(report):,} chars")
    print(f"report saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
