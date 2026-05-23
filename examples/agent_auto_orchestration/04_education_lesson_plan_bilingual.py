"""Bilingual lesson plan — prompt-only Skill, run twice (ZH and EN input).

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
A single standard ``SKILL.md`` (guidance only — no ``skill.yaml``, no stages).
One prompt-only request returns the complete bilingual package shaped by
``semantic_outputs`` (ZH outline, EN outline, paired vocabulary, teacher
summary). The HOST writes each package to disk.

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

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_MD = """\
---
name: Bilingual Lesson Plan Generator
description: >-
  Generate a complete bilingual (Chinese + English) K-12 lesson plan package
  from a topic: aligned learning objectives, timed teaching sections, a paired
  bilingual vocabulary bank, and a professional teacher summary. Use for lesson,
  教案, curriculum, and education requests.
keywords: [lesson, education, curriculum, bilingual, 教案, 教育]
---

# Bilingual Lesson Plan Generator

You are a professional K-12 curriculum designer and bilingual education
specialist. Given a topic (in Chinese or English), produce ONE complete bilingual
lesson package, age-appropriate and aligned with standard curriculum goals.

## Produce
1. A Chinese outline (中文教案): 标题、年级、3 条可衡量学习目标、4 个教学环节
   (含名称/时长分钟/活动)、5 个核心中文词汇。总时长 40 分钟。
2. An English outline mirroring the same lesson: title, grade level, 3 measurable
   objectives, 4 timed teaching sections, 5 key English vocabulary terms.
3. A paired bilingual vocabulary bank: align the ZH and EN terms, add 2 extra
   relevant pairs, each with a concise learner-friendly English explanation
   (at least 5 pairs total).
4. A short professional teacher summary tying the package together.

Keep both languages pedagogically equivalent.
"""


def install_skill() -> str:
    skill_src = RUNTIME_ROOT / "src" / "bilingual-lesson-plan"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    Agently.settings.set("skills.registry.root", tempfile.mkdtemp(prefix="agently_skills_reg_"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


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


SEMANTIC_OUTPUTS: dict[str, Any] = {
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


async def run_lesson_plan(agent, skill_id: str, task: str, label: str, index: int) -> None:
    divider = "=" * 60
    print(f"\n{divider}\n{label}\n任务: {task}\n{divider}")
    print("运行 bilingual lesson skill (流式)...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_complete") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    execution = await agent.async_run_skills_task(
        task,
        skills=[skill_id],
        mode="required",
        semantic_outputs=SEMANTIC_OUTPUTS,
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

    skill_id = install_skill()
    agent = Agently.create_agent("education-bilingual")

    await run_lesson_plan(
        agent, skill_id,
        task="帮我设计一节小学五年级的自然科学课，主题是光合作用",
        label="Chinese input → bilingual lesson plan",
        index=1,
    )
    await run_lesson_plan(
        agent, skill_id,
        task="Create a Grade 5 science lesson on photosynthesis for bilingual students",
        label="English input → bilingual lesson plan",
        index=2,
    )


if __name__ == "__main__":
    asyncio.run(main())
