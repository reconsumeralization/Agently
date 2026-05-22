"""Bilingual lesson plan generator — education business case with real model calls.

Run:
    python examples/agent_auto_orchestration/04_education_lesson_plan_bilingual.py

Environment:
    DEEPSEEK_API_KEY may be available in the shell or a .env file.
    If absent, set DYNAMIC_TASK_MODEL_PROVIDER=ollama and ensure the local
    Ollama OpenAI-compatible endpoint is running.

Scenario: An EdTech platform generates bilingual (Chinese/English) lesson
packages from a natural-language topic description. Each stage calls the
model to produce structured content: two outline stages generate
language-specific lesson designs, a vocabulary stage pairs terms across
languages, and a compile stage assembles the package and writes a
professional teacher summary.

Key mechanics demonstrated:
  - Real model calls inside action stages (async action functions)
  - Natural-language progress output during each stage
  - create_execution() streaming: task_dag.tasks.* fires as stages run;
    skills.stages.* and actions.* confirm completions afterward
  - Final deliverable checklist + AI-generated teacher summary

Expected key output from one real DeepSeek run (Chinese input):
    selected_route=skills
    stream_zh_outline=True
    stream_en_outline=True
    stream_vocabulary=True
    stream_compile=True
    zh_objectives=3
    en_objectives=3
    vocabulary_pairs>=5
    package_complete=True
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "education_bilingual"

BILINGUAL_LESSON_SKILL_YAML = """
skill_id: bilingual-lesson-plan
version: 0.2.0
display_name: Bilingual Lesson Plan Generator
purpose: >
  Generate structured bilingual Chinese and English lesson plans for K-12 educators
  using real AI content generation at each stage.
trust_level: local
activation:
  keywords: [lesson, plan, teach, education, curriculum, 教案, 课程, 教学, 教育]
requires:
  actions: [generate_zh_outline, generate_en_outline, build_bilingual_vocabulary, compile_lesson_package]
stages:
  - id: generate_zh_outline
    kind: action
    action: generate_zh_outline
    input:
      topic: "${task}"
  - id: generate_en_outline
    kind: action
    action: generate_en_outline
    input:
      topic: "${task}"
  - id: validate_outlines
    kind: validate
    validation:
      required_state: [generate_zh_outline, generate_en_outline]
  - id: build_bilingual_vocabulary
    kind: action
    action: build_bilingual_vocabulary
    input:
      zh_outline: "${state.generate_zh_outline}"
      en_outline: "${state.generate_en_outline}"
  - id: compile_lesson_package
    kind: action
    action: compile_lesson_package
    input:
      zh_outline: "${state.generate_zh_outline}"
      en_outline: "${state.generate_en_outline}"
      vocabulary: "${state.build_bilingual_vocabulary}"
      topic: "${task}"
  - id: emit_ready
    kind: emit
    data:
      summary: bilingual lesson plan package ready
      languages: [zh, en]
"""

BILINGUAL_LESSON_SKILL_MD = """---
name: Bilingual Lesson Plan Generator
description: Generate bilingual Chinese and English lesson plans for K-12 educators.
keywords:
  - lesson
  - education
  - curriculum
  - 教案
  - 教育
---

Generate complete bilingual lesson plan packages including learning objectives,
timed teaching sections, bilingual vocabulary banks, and a professional teacher summary.
Each stage calls the language model to produce structured, age-appropriate content.
"""


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_skill() -> Path:
    skill_root = RUNTIME_ROOT / "bilingual-lesson-plan"
    _write_text(skill_root / "skill.yaml", BILINGUAL_LESSON_SKILL_YAML)
    _write_text(skill_root / "SKILL.md", BILINGUAL_LESSON_SKILL_MD)
    return skill_root


# ---------- Real model action implementations ----------

async def generate_zh_outline(topic: str = "") -> dict:
    """Generate Chinese lesson outline via real model call."""
    print("  → 正在生成中文教案大纲（模型请求中）...")
    result = await (
        Agently.create_agent("lesson-zh-outline")
        .input({"topic": topic})
        .instruct(
            "你是一名专业的K-12课程设计专家。根据提供的课题，生成一份结构化的中文教案大纲。"
            "内容要专业、适龄、实用，并与标准课程目标对齐。"
        )
        .output({
            "title": (str, "课程标题", True),
            "grade_level": (str, "适用年级，例如：小学五年级", True),
            "objectives": ([str], "3条具体可衡量的学习目标", True),
            "sections": (
                [
                    {
                        "name": (str, "教学环节名称", True),
                        "duration_min": (int, "时长（分钟）", True),
                        "activities": (str, "主要教学活动描述", True),
                    }
                ],
                "4个教学环节，总时长40分钟",
                True,
            ),
            "key_vocabulary": ([str], "5个本节课核心中文词汇", True),
        })
        .async_start()
    )
    return result


async def generate_en_outline(topic: str = "") -> dict:
    """Generate English lesson outline via real model call."""
    print("  → Generating English lesson outline (model request)...")
    result = await (
        Agently.create_agent("lesson-en-outline")
        .input({"topic": topic})
        .instruct(
            "You are a professional K-12 curriculum designer. Based on the provided topic, "
            "generate a structured English lesson plan outline. "
            "Content should be professional, age-appropriate, and aligned with standard curriculum goals."
        )
        .output({
            "title": (str, "Lesson title", True),
            "grade_level": (str, "Applicable grade level, e.g. Grade 5", True),
            "objectives": ([str], "3 specific measurable learning objectives", True),
            "sections": (
                [
                    {
                        "name": (str, "Section name", True),
                        "duration_min": (int, "Duration in minutes", True),
                        "activities": (str, "Main teaching activity description", True),
                    }
                ],
                "4 teaching sections, total 40 minutes",
                True,
            ),
            "key_vocabulary": ([str], "5 key English vocabulary terms for this lesson", True),
        })
        .async_start()
    )
    return result


async def build_bilingual_vocabulary(
    zh_outline: object = None,
    en_outline: object = None,
) -> dict:
    """Build paired bilingual vocabulary bank via real model call."""
    print("  → Building bilingual vocabulary bank (model request)...")
    zh = zh_outline if isinstance(zh_outline, dict) else {}
    en = en_outline if isinstance(en_outline, dict) else {}

    result = await (
        Agently.create_agent("lesson-vocabulary")
        .input({
            "zh_vocabulary": zh.get("key_vocabulary", []),
            "en_vocabulary": en.get("key_vocabulary", []),
            "zh_title": zh.get("title", ""),
            "en_title": en.get("title", ""),
        })
        .instruct(
            "You are a bilingual education specialist. Create a paired Chinese-English vocabulary bank "
            "from the provided vocabulary lists. Pair each Chinese term with its English equivalent. "
            "For any unpaired terms, find the best cross-language match. "
            "Add 2 additional important vocabulary pairs relevant to the topic that are not in the lists. "
            "Each pair must include a concise, learner-friendly explanation."
        )
        .output({
            "pairs": (
                [
                    {
                        "zh": (str, "Chinese term", True),
                        "en": (str, "English equivalent", True),
                        "explanation": (str, "Brief learner-friendly explanation in English", True),
                    }
                ],
                "Bilingual vocabulary pairs (at least 5)",
                True,
            ),
            "pair_count": (int, "Total number of vocabulary pairs", True),
        })
        .async_start()
    )
    return result


async def compile_lesson_package(
    zh_outline: object = None,
    en_outline: object = None,
    vocabulary: object = None,
    topic: str = "",
) -> dict:
    """Compile lesson package and generate teacher summary via real model call."""
    print("  → Compiling package and generating teacher summary (model request)...")
    zh = zh_outline if isinstance(zh_outline, dict) else {}
    en = en_outline if isinstance(en_outline, dict) else {}
    vocab = vocabulary if isinstance(vocabulary, dict) else {}

    result = await (
        Agently.create_agent("lesson-compiler")
        .input({
            "topic": topic,
            "zh_title": zh.get("title", ""),
            "zh_grade": zh.get("grade_level", ""),
            "zh_objectives": zh.get("objectives", []),
            "zh_sections": [
                {"name": s.get("name", ""), "duration_min": s.get("duration_min", 0)}
                for s in zh.get("sections", [])
            ],
            "en_title": en.get("title", ""),
            "en_grade": en.get("grade_level", ""),
            "en_objectives": en.get("objectives", []),
            "en_sections": [
                {"name": s.get("name", ""), "duration_min": s.get("duration_min", 0)}
                for s in en.get("sections", [])
            ],
            "vocabulary_pairs": vocab.get("pairs", []),
            "vocabulary_pair_count": vocab.get("pair_count", 0),
        })
        .instruct(
            "You are a curriculum coordinator reviewing a completed bilingual lesson plan package. "
            "Evaluate the package completeness, write a professional teacher-facing summary in English, "
            "and note any bilingual coordination strengths or suggestions."
        )
        .output({
            "package_complete": (bool, "True when both outlines and vocabulary bank are present", True),
            "languages": ([str], "Language codes included, e.g. ['zh', 'en']", True),
            "total_duration_minutes": (int, "Total lesson duration in minutes (from zh sections)", True),
            "zh_summary": (str, "One-sentence summary of the Chinese lesson plan", True),
            "en_summary": (str, "One-sentence summary of the English lesson plan", True),
            "cross_language_notes": (str, "Notes on bilingual alignment, vocabulary coverage, and coordination", True),
            "teacher_summary": (
                str,
                "Professional natural-language summary for the teacher reviewing this package. "
                "Cover: what topic and grade this covers, key objectives, lesson structure, "
                "and vocabulary richness. 3-5 sentences.",
                True,
            ),
        })
        .async_start()
    )
    return result


def register_actions(agent) -> None:
    agent.register_action(
        name="generate_zh_outline",
        desc="Generate a structured Chinese K-12 lesson plan outline using AI.",
        kwargs={"topic": (str, "Topic and grade level description.")},
        func=generate_zh_outline,
    )
    agent.register_action(
        name="generate_en_outline",
        desc="Generate a structured English K-12 lesson plan outline using AI.",
        kwargs={"topic": (str, "Topic and grade level description.")},
        func=generate_en_outline,
    )
    agent.register_action(
        name="build_bilingual_vocabulary",
        desc="Build a paired Chinese-English vocabulary bank from two lesson outlines using AI.",
        kwargs={
            "zh_outline": (object, "Chinese lesson outline dict."),
            "en_outline": (object, "English lesson outline dict."),
        },
        func=build_bilingual_vocabulary,
    )
    agent.register_action(
        name="compile_lesson_package",
        desc="Compile the bilingual lesson package and generate a professional teacher summary using AI.",
        kwargs={
            "zh_outline": (object, "Chinese lesson outline dict."),
            "en_outline": (object, "English lesson outline dict."),
            "vocabulary": (object, "Bilingual vocabulary bank dict."),
            "topic": (str, "Original topic description."),
        },
        func=compile_lesson_package,
    )


# ---------- Stream event labels ----------

_STAGE_NARRATIVE = {
    "generate_zh_outline": "中文教案大纲已生成",
    "generate_en_outline": "英文教案大纲已生成",
    "validate_outlines": "双语大纲校验通过",
    "build_bilingual_vocabulary": "双语词汇表已构建",
    "compile_lesson_package": "教案编译完成，教师总结已生成",
    "emit_ready": "教案包就绪，等待交付",
}


# ---------- Main demo ----------

async def run_lesson_plan(agent, task: str, label: str) -> None:
    divider = "=" * 60
    print(f"\n{divider}")
    print(label)
    print(f"任务: {task}")
    print(divider)
    print("启动教案生成流程...\n")

    execution = (
        agent
        .use_skills(["bilingual-lesson-plan"], mode="required")
        .input(task)
        .create_execution()
    )

    stream_events: list[str] = []
    stage_step = 0

    async for item in execution.get_async_generator(type="instant"):
        if not item.is_complete:
            continue
        path = item.path
        stream_events.append(path)

        if path == "route.selected":
            route = (item.value or {}).get("selected_route", "skills")
            print(f"  [路由] 已选择: {route}")

        elif path.startswith("task_dag.tasks."):
            # Real-time task-level progress from the underlying DAG
            task_id = path.split(".")[-1]
            narrative = _STAGE_NARRATIVE.get(task_id, task_id)
            stage_step += 1
            print(f"  [{stage_step}] 完成: {narrative}")

        elif path.startswith("skills.stages."):
            # Post-execution confirmation from skill logs
            stage_id = path.split(".")[-1]
            if not any(e.startswith("task_dag.tasks.") and e.endswith(stage_id) for e in stream_events):
                stage_step += 1
                narrative = _STAGE_NARRATIVE.get(stage_id, stage_id)
                print(f"  [{stage_step}] ✓ {narrative}")

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    zh = data.get("generate_zh_outline") or {}
    en = data.get("generate_en_outline") or {}
    vocab = data.get("build_bilingual_vocabulary") or {}
    package = data.get("compile_lesson_package") or {}

    # Deliverable checklist
    print(f"\n{divider}")
    print("教案包交付清单")
    print(divider)

    zh_obj_count = len(zh.get("objectives", []))
    zh_sec_count = len(zh.get("sections", []))
    en_obj_count = len(en.get("objectives", []))
    en_sec_count = len(en.get("sections", []))
    vocab_count = vocab.get("pair_count", 0)
    total_min = package.get("total_duration_minutes", 0)

    print(f"  ✓ 中文教案")
    print(f"    标题:   {zh.get('title', '—')}")
    print(f"    年级:   {zh.get('grade_level', '—')}")
    print(f"    学习目标: {zh_obj_count} 条")
    for obj in zh.get("objectives", []):
        print(f"      · {obj}")
    print(f"    教学环节: {zh_sec_count} 个")
    for sec in zh.get("sections", []):
        print(f"      · {sec.get('name', '')} ({sec.get('duration_min', 0)} 分钟)")

    print(f"  ✓ English Lesson Plan")
    print(f"    Title:      {en.get('title', '—')}")
    print(f"    Grade:      {en.get('grade_level', '—')}")
    print(f"    Objectives: {en_obj_count} items")
    for obj in en.get("objectives", []):
        print(f"      · {obj}")
    print(f"    Sections:   {en_sec_count} sections")
    for sec in en.get("sections", []):
        print(f"      · {sec.get('name', '')} ({sec.get('duration_min', 0)} min)")

    print(f"  ✓ 双语词汇表: {vocab_count} 词对")
    pairs = vocab.get("pairs", [])
    for pair in pairs[:5]:
        zh_term = pair.get("zh", "")
        en_term = pair.get("en", "")
        explanation = pair.get("explanation", "")[:60]
        print(f"    · {zh_term} / {en_term}: {explanation}")
    if len(pairs) > 5:
        print(f"    · ... 及另 {len(pairs) - 5} 个词对")

    print(f"  ✓ 总课时: {total_min} 分钟")

    # AI-generated teacher summary
    print(f"\n{divider}")
    print("教师总结（AI 生成）")
    print(divider)
    teacher_summary = package.get("teacher_summary", "")
    if teacher_summary:
        print(teacher_summary)
    cross_notes = package.get("cross_language_notes", "")
    if cross_notes:
        print(f"\n双语协调说明: {cross_notes}")

    # Key output assertions
    selected_route = meta.get("route_plan", {}).get("selected_route", "")
    print(f"\nselected_route={selected_route}")
    print(f"stream_zh_outline={'skills.stages.generate_zh_outline' in stream_events}")
    print(f"stream_en_outline={'skills.stages.generate_en_outline' in stream_events}")
    print(f"stream_vocabulary={'skills.stages.build_bilingual_vocabulary' in stream_events}")
    print(f"stream_compile={'skills.stages.compile_lesson_package' in stream_events}")
    print(f"zh_objectives={zh_obj_count}")
    print(f"en_objectives={en_obj_count}")
    print(f"vocabulary_pairs={vocab_count}")
    print(f"package_complete={package.get('package_complete')}")
    print(f"package_languages={package.get('languages', [])}")


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    Agently.settings.set("skills.registry.root", str(RUNTIME_ROOT / "registry"))
    skill_root = prepare_skill()
    Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)

    agent = Agently.create_agent("education-bilingual")
    register_actions(agent)

    await run_lesson_plan(
        agent,
        task="帮我设计一节小学五年级的自然科学课，主题是光合作用",
        label="Chinese input → bilingual lesson plan",
    )

    await run_lesson_plan(
        agent,
        task="Create a Grade 5 science lesson on photosynthesis for bilingual students",
        label="English input → bilingual lesson plan",
    )


if __name__ == "__main__":
    asyncio.run(main())
