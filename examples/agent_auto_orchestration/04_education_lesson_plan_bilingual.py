"""Bilingual lesson plan — remote education Skills, run twice (ZH and EN input).

Run:
    python examples/agent_auto_orchestration/04_education_lesson_plan_bilingual.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: A K-12 educator asks for a bilingual (Chinese + English) lesson plan.
The same standard Skill handles both a Chinese-language and an English-language
request.

New-standard Skills model
-------------------------
This example uses real third-party education Skills from
`GarethManning/education-agent-skills` instead of a local demo Skill. The
selected remote Skills cover backwards design, language demand, vocabulary
tiering, retrieval practice, and formative assessment. The HOST writes each
package to disk.

Expected key output from one real DeepSeek run (per topic):
    skill status: success
    zh_objectives>=3
    en_objectives>=3
    vocabulary_pairs>=5
    package written: .../lesson_<n>.md
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "education_bilingual"

REMOTE_EDUCATION_SKILLS = [
    {"source": "GarethManning/education-agent-skills", "subpath": "skills/curriculum-assessment/backwards-design-unit-planner", "trust_level": "remote"},
    {"source": "GarethManning/education-agent-skills", "subpath": "skills/eal-language-development/language-demand-analyser", "trust_level": "remote"},
    {"source": "GarethManning/education-agent-skills", "subpath": "skills/eal-language-development/vocabulary-tiering-tool", "trust_level": "remote"},
    {"source": "GarethManning/education-agent-skills", "subpath": "skills/memory-learning-science/retrieval-practice-generator", "trust_level": "remote"},
    {"source": "GarethManning/education-agent-skills", "subpath": "skills/curriculum-assessment/formative-assessment-technique-selector", "trust_level": "remote"},
]


def _outline_schema() -> dict[str, Any]:
    return {
        "title": (str, "Lesson title", True),
        "grade_level": (str, "Applicable grade level", True),
        "objectives": ([str], "3 specific measurable learning objectives", True),
        "sections": (
            [{
                "name": (str, "Section name", True),
                "duration_min": (int, "Duration in minutes", True),
                "activities": (str, "Main teaching activity description", True),
            }],
            "4 teaching sections, total 40 minutes",
            True,
        ),
        "key_vocabulary": ([str], "5 key vocabulary terms", True),
    }


OUTPUT_SCHEMA: dict[str, Any] = {
    "zh_outline": (_outline_schema(), "Chinese lesson outline (中文教案)", True),
    "en_outline": (_outline_schema(), "English lesson outline", True),
    "vocabulary_pairs": (
        [{
            "zh": (str, "Chinese term", True),
            "en": (str, "English equivalent", True),
            "explanation": (str, "Brief learner-friendly explanation in English", True),
        }],
        "Paired bilingual vocabulary (at least 5)",
        True,
    ),
    "teacher_summary": (str, "Professional teacher summary tying the package together", True),
    "languages": ([str], "Languages covered, e.g. ['zh', 'en']", True),
    "package_complete": (bool, "True if both outlines + vocabulary + summary are present", True),
}


async def run_lesson_plan(agent, task: str, label: str, index: int) -> None:
    divider = "=" * 60
    print(f"\n{divider}\n{label}\n任务: {task}\n{divider}")
    print("运行 bilingual lesson skill (流式)...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_completed") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    execution = await agent.async_run_skills_task(
        task,
        mode="required",
        effort="normal",
        output=OUTPUT_SCHEMA,
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    zh = result.get("zh_outline") or {}
    en = result.get("en_outline") or {}
    pairs = result.get("vocabulary_pairs") or []

    print(f"\n  中文标题: {zh.get('title', '—')}  ({zh.get('grade_level', '—')})")
    print(f"  EN title: {en.get('title', '—')}  ({en.get('grade_level', '—')})")
    print(f"  词汇对照: {len(pairs)} 组")
    for p in pairs[:4]:
        print(f"    · {p.get('zh', '—')} / {p.get('en', '—')}")
    summary = str(result.get("teacher_summary", ""))
    if summary:
        print(f"\n  教师总结: {summary[:200]}")

    out_dir = RUNTIME_ROOT / "published"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"lesson_{index}.md"
    out_path.write_text(
        f"# {zh.get('title', '')} / {en.get('title', '')}\n\n{summary}\n",
        encoding="utf-8",
    )

    print(f"\nskill status: {execution.status}")
    print(f"zh_objectives={len(zh.get('objectives', []) or [])}")
    print(f"en_objectives={len(en.get('objectives', []) or [])}")
    print(f"vocabulary_pairs={len(pairs)}")
    print(f"package_complete={bool(result.get('package_complete'))}")
    print(f"package written: {out_path}")


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    Agently.skills_executor.configure(
        registry_root=str(RUNTIME_ROOT / "registry"),
        allowed_trust_levels=["local", "remote"],
    )
    agent = Agently.create_agent("education-bilingual")
    agent.use_skills(REMOTE_EDUCATION_SKILLS, mode="required")

    await run_lesson_plan(
        agent,
        task="帮我设计一节小学五年级的自然科学课，主题是光合作用",
        label="Chinese input → bilingual lesson plan",
        index=1,
    )
    await run_lesson_plan(
        agent,
        task="Create a Grade 5 science lesson on photosynthesis for bilingual students",
        label="English input → bilingual lesson plan",
        index=2,
    )


if __name__ == "__main__":
    asyncio.run(main())
