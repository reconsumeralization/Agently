"""Deep research — prompt-only Skill producing a multi-dimension report.

Run:
    python examples/agent_auto_orchestration/14_deep_research.py
    python examples/agent_auto_orchestration/14_deep_research.py "6G technology landscape 2025-2026"
    python examples/agent_auto_orchestration/14_deep_research.py --lang zh "AI agent frameworks"
    AGENTLY_RESEARCH_TOPIC="RISC-V ecosystem" python examples/agent_auto_orchestration/14_deep_research.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

New-standard Skills model
-------------------------
The old design used staged Skill actions (plan → search → browse → synthesize →
cross-validate). Under the new standard the research methodology lives in a
single ``SKILL.md`` as guidance, and ONE prompt-only request produces the deep
report (decomposed into dimensions, with synthesis and open questions) shaped by
``output``. The HOST saves the report. Any external search would also
be a host tool; here the model reasons from its own knowledge and flags
uncertainty.

Expected key output from one real DeepSeek run:
    skill status: success
    dimensions: 3-5
    report length: >2,500 chars
    report saved: .../deep_research_report.md
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

DEFAULT_TOPIC = "The state of small language models (SLMs) for on-device AI in 2026"

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "deep-research"


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


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    topic, lang = parse_args()
    skill_id = install_skill()
    agent = Agently.create_agent("deep-researcher")

    divider = "=" * 60
    print(divider)
    print("Deep Research — prompt-only Skill")
    print(f"Topic: {topic}")
    if lang:
        print(f"Output language: {lang}")
    print(divider)
    print("Researching (streaming dimensions)...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_completed") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    lang_note = f"\n\nWrite the report in this language: {lang}." if lang else ""
    execution = await agent.async_run_skills_task(
        f"Produce a deep research report on: {topic}.{lang_note}",
        skills=[skill_id],
        mode="required",
        output={
            "dimensions": (
                [{
                    "name": (str, "Dimension name", True),
                    "analysis": (str, "Evidence-based analysis", True),
                    "uncertainty": (str, "Where knowledge is uncertain / may be dated"),
                }],
                "3-5 analyzed dimensions",
                True,
            ),
            "synthesis": (str, "Cross-cutting synthesis", True),
            "open_questions": ([str], "Open questions for a follow-up round", True),
            "report": (str, "Full markdown report", True),
        },
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    dims = result.get("dimensions", []) or []
    report = str(result.get("report", ""))

    print(f"\n  dimensions: {len(dims)}")
    for d in dims:
        print(f"    · {d.get('name', '—')}")
    print(f"  open questions: {len(result.get('open_questions', []) or [])}")

    out_path = Path(tempfile.mkdtemp(prefix="agently_research_")) / "deep_research_report.md"
    out_path.write_text(f"# Deep Research: {topic}\n\n{report}\n", encoding="utf-8")

    print(f"\nskill status: {execution.status}")
    print(f"dimensions: {len(dims)}")
    print(f"report length: {len(report):,} chars")
    print(f"report saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
