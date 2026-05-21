"""Executable Skills Executor realcase benchmark: education course pack.

Run:
    python examples/skills_executor/06_executable_education_course_pack.py

    The script also works when invoked by absolute path from outside the repo.

Expected key output from a real DeepSeek run:
    real_execution_result=pass
    dependency_action_status=success
    dependency_imports_available=True
    deterministic_passed=True
    model_judge_passes=True
    artifact_count=8

This benchmark exercises the execution-grade path after Skills planning:

    external SKILL.md packages
      -> Skills Executor plan
      -> local dependency-installer Skill
      -> controlled ensure_python_packages Action
      -> DeepSeek content generation with selected skill guidance
      -> controlled local artifact writers
      -> deterministic artifact validation
      -> Agently model judge for semantic content validation

The local writers are benchmark-owned controlled functions and their Python
package dependencies are repaired through a Skills Executor action stage before
artifact generation. The benchmark does not execute arbitrary third-party skill
package scripts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from agently.utils import LazyImport


def _load_combo_benchmark_module():
    module_path = ROOT / "examples" / "skills_executor" / "05_combo_skillpack_diagnostics.py"
    spec = importlib.util.spec_from_file_location("skills_executor_combo_benchmark", module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load combo benchmark module: { module_path }")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


combo = _load_combo_benchmark_module()


RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor" / "real_execution" / "education_course_pack"
REPORT_PATH = RUNTIME_ROOT / "execution_report.json"

DEPENDENCY_INSTALLER_SKILL_YAML = """
skill_id: benchmark-python-dependency-installer
display_name: Benchmark Python Dependency Installer
purpose: Ensure local Python packages required by artifact-generating Skills are available.
trust_level: local
requires:
  actions: [ensure_python_packages]
stages:
  - id: ensure_artifact_writer_dependencies
    kind: action
    action: ensure_python_packages
    input:
      packages:
        - package: python-docx
          import_name: docx
          version_constraint: ">=1.1.0"
          purpose: Write and validate teacher guide and assessment rubric docx artifacts.
        - package: openpyxl
          import_name: openpyxl
          version_constraint: ">=3.1.0"
          purpose: Write and validate vocabulary and progress tracker xlsx artifacts.
        - package: python-pptx
          import_name: pptx
          version_constraint: ">=1.0.0"
          purpose: Write and validate lesson slides pptx artifacts.
        - package: reportlab
          import_name: reportlab
          version_constraint: ">=4.0.0"
          purpose: Write printable student handout pdf artifact.
        - package: pypdf
          import_name: pypdf
          version_constraint: ">=5.0.0"
          purpose: Validate generated pdf artifacts.
"""


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_as_text(item) for item in value.values())
    if isinstance(value, list | tuple):
        return " ".join(_as_text(item) for item in value)
    return str(value)


def _configure_deepseek():
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DEEPSEEK_API_KEY. Put it in your shell or .env before running this benchmark.")
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat"),
            "model_type": "chat",
            "auth": api_key,
            "request_options": {"temperature": 0.0},
        },
    )
    Agently.set_settings("debug", False)


def _course_output_schema() -> dict[str, Any]:
    return {
        "course_plan": {
            "title": (str, "Course title.", True),
            "learner_profile": (str, "B1 adult business English learner profile.", True),
            "goals": [(str, "Course goal.", True)],
            "weeks": [({
                "week": (int, "Week number from 1 to 4.", True),
                "focus": (str, "Weekly focus.", True),
                "sessions": [({
                    "session": (int, "Session number inside the week.", True),
                    "objective": (str, "Session objective.", True),
                    "activities": [(str, "Classroom activity.", True)],
                    "retrieval_practice": (str, "Retrieval practice activity.", True),
                    "homework": (str, "Homework task.", True),
                    "formative_assessment": (str, "Formative assessment method.", True),
                }, "45-minute lesson session.", True)],
            }, "Weekly course plan.", True)],
        },
        "teacher_guide": {
            "overview": (str, "Teacher-facing overview.", True),
            "lesson_notes": [(str, "Teacher lesson note.", True)],
            "facilitation_tips": [(str, "Teacher facilitation tip.", True)],
        },
        "student_handout": {
            "overview": (str, "Student-facing overview.", True),
            "activities": [(str, "Student classroom activity.", True)],
            "homework": [(str, "Student homework item.", True)],
        },
        "lesson_slides": [({
            "title": (str, "Slide title.", True),
            "bullets": [(str, "Slide bullet.", True)],
        }, "Slide content.", True)],
        "vocabulary_bank": [({
            "term": (str, "Business English vocabulary item.", True),
            "meaning": (str, "Meaning suitable for a B1 learner.", True),
            "example": (str, "Example sentence.", True),
            "week": (int, "Week number.", True),
        }, "Vocabulary record.", True)],
        "assessment_rubric": [({
            "criterion": (str, "Assessment criterion.", True),
            "b1_descriptor": (str, "B1-level success descriptor.", True),
            "evidence": (str, "Evidence to collect.", True),
        }, "Rubric row.", True)],
        "progress_tracker": [({
            "week": (int, "Week number.", True),
            "skill": (str, "Tracked skill.", True),
            "target": (str, "Learning target.", True),
            "check_method": (str, "How progress is checked.", True),
        }, "Progress tracker row.", True)],
        "quality_notes": [(str, "Quality or alignment note.", True)],
    }


def _generate_course_package(plan_result: dict[str, Any], selected_skill_ids: list[str]) -> dict[str, Any]:
    agent = Agently.create_agent("education-course-pack-builder")
    agent.use_skills(selected_skill_ids, mode="model_decision", scope="session")
    return agent.input({
        "task": next(case.task for case in combo.CASES if case.case_id == "education_course_pack"),
        "selected_skill_ids": selected_skill_ids,
        "skills_execution_plan": plan_result,
        "required_artifacts": [
            "course_plan.json",
            "teacher_guide.docx",
            "student_handout.pdf",
            "lesson_slides.pptx",
            "vocabulary_bank.xlsx",
            "assessment_rubric.docx",
            "progress_tracker.xlsx",
            "skill_trace.json",
        ],
    }).instruct(
        "Generate the actual content for a 4-week B1 adult business English course package. "
        "The learner goal is to express opinions, ask follow-up questions, and summarize decisions in English meetings. "
        "Use the selected skills as guidance. Produce substantive course content, not just an outline. "
        "Use English for artifact content. Include 4 weeks, 3 sessions per week, retrieval practice, vocabulary, "
        "formative assessment, homework, and progress tracking."
    ).output(_course_output_schema()).start()


def _write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _install_dependency_skill(skill_root: Path):
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "skill.yaml").write_text(DEPENDENCY_INSTALLER_SKILL_YAML, encoding="utf-8")
    Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)


def _ensure_python_packages(packages: list[dict[str, str]]) -> dict[str, Any]:
    normalized = []
    for item in packages:
        if not isinstance(item, dict):
            continue
        package_name = str(item.get("package") or "").strip()
        import_name = str(item.get("import_name") or package_name).strip().replace("-", "_")
        if package_name and import_name:
            normalized.append({
                "package": package_name,
                "import_name": import_name,
                "version_constraint": str(item.get("version_constraint") or "").strip(),
                "purpose": str(item.get("purpose") or ""),
            })

    before = {item["import_name"]: _lazy_import_available(item) for item in normalized}
    missing = [item["package"] for item in normalized if not before[item["import_name"]]]
    pip_result: dict[str, Any] | None = None
    if missing:
        install_targets = [
            f"{ item['package'] }{ item['version_constraint'] }" if item["version_constraint"] else item["package"]
            for item in normalized
            if item["package"] in missing
        ]
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", *install_targets],
            check=False,
            capture_output=True,
            text=True,
        )
        pip_result = {
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
        if completed.returncode != 0:
            raise RuntimeError(f"pip install failed for { install_targets }: { completed.stderr[-1000:] }")

    after = {item["import_name"]: _lazy_import_available(item) for item in normalized}
    still_missing = [item for item in normalized if not after[item["import_name"]]]
    if still_missing:
        raise RuntimeError(f"Packages installed but imports are still missing: { still_missing }")
    return {
        "requested": normalized,
        "already_available": [item["package"] for item in normalized if before[item["import_name"]]],
        "installed": missing,
        "available_after": after,
        "pip": pip_result,
    }


def _lazy_import_available(item: dict[str, str]) -> bool:
    try:
        LazyImport.import_package(
            item["import_name"],
            auto_install=False,
            install_name=item["package"],
            version_constraint=item.get("version_constraint") or None,
        )
        return True
    except ImportError:
        return False


def _run_dependency_install_skill(skill_root: Path) -> Any:
    _install_dependency_skill(skill_root)
    agent = Agently.create_agent("education-course-dependency-installer")
    agent.action.register_action(
        action_id="ensure_python_packages",
        desc="Install missing local Python packages required by controlled artifact writers.",
        kwargs={"packages": (list, "Package specs with package, import_name, and purpose.")},
        func=_ensure_python_packages,
        side_effect_level="exec",
        replay_safe=False,
        expose_to_model=False,
        meta={"source": "skills_executor_real_execution_benchmark"},
    )
    return agent.run_skills_task(
        "Ensure Python artifact writer dependencies for the education course pack benchmark.",
        skills=["benchmark-python-dependency-installer"],
        mode="required",
    )


def _write_docx(path: Path, title: str, sections: list[tuple[str, list[str]]]):
    from docx import Document

    document = Document()
    document.add_heading(title, level=0)
    for heading, paragraphs in sections:
        document.add_heading(heading, level=1)
        for paragraph in paragraphs:
            document.add_paragraph(str(paragraph))
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


def _read_docx_text(path: Path) -> str:
    try:
        from docx import Document

        document = Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception:
        with zipfile.ZipFile(path) as package:
            return package.read("word/document.xml").decode("utf-8", errors="replace")


def _write_pdf(path: Path, title: str, paragraphs: list[str]):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
    for paragraph in paragraphs:
        story.append(Paragraph(escape(str(paragraph)), styles["BodyText"]))
        story.append(Spacer(1, 8))
    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=letter).build(story)


def _write_pptx(path: Path, slides: list[dict[str, Any]]):
    from pptx import Presentation

    presentation = Presentation()
    for item in slides[:16]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        title_shape = cast(Any, slide.shapes.title)
        title_shape.text = str(item.get("title") or "Lesson")
        body = cast(Any, slide.placeholders[1]).text_frame
        body.clear()
        bullets = _ensure_list(item.get("bullets"))[:6]
        if not bullets:
            bullets = ["B1 business English meeting practice"]
        for index, bullet in enumerate(bullets):
            paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
            paragraph.text = str(bullet)
            paragraph.level = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(path))


def _normalize_lesson_slides(package: dict[str, Any]) -> list[dict[str, Any]]:
    slides = [dict(item) for item in _ensure_list(package.get("lesson_slides")) if isinstance(item, dict)]
    if len(slides) >= 8:
        return slides

    course_plan = dict(package.get("course_plan") or {})
    for week in _ensure_list(course_plan.get("weeks")):
        week_data = dict(week) if isinstance(week, dict) else {}
        week_number = week_data.get("week")
        for session in _ensure_list(week_data.get("sessions")):
            session_data = dict(session) if isinstance(session, dict) else {}
            title = f"Week { week_number } Session { session_data.get('session') }: { session_data.get('objective') or 'Meeting English Practice' }"
            bullets = [
                str(session_data.get("objective") or "Practice B1 business English meeting language."),
                *[str(item) for item in _ensure_list(session_data.get("activities"))[:3]],
                f"Retrieval: { session_data.get('retrieval_practice') or 'Review previous meeting language.' }",
                f"Assessment: { session_data.get('formative_assessment') or 'Teacher observes meeting performance.' }",
            ]
            slides.append({"title": title, "bullets": bullets})
            if len(slides) >= 12:
                return slides
    return slides


def _write_xlsx(path: Path, sheet_name: str, headers: list[str], rows: list[list[Any]]):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = cast(Any, workbook.active)
    sheet.title = sheet_name[:31]
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _write_artifacts(package: dict[str, Any], plan_result: dict[str, Any], output_dir: Path) -> dict[str, str]:
    paths = {
        "course_plan": output_dir / "course_plan.json",
        "teacher_guide": output_dir / "teacher_guide.docx",
        "student_handout": output_dir / "student_handout.pdf",
        "lesson_slides": output_dir / "lesson_slides.pptx",
        "vocabulary_bank": output_dir / "vocabulary_bank.xlsx",
        "assessment_rubric": output_dir / "assessment_rubric.docx",
        "progress_tracker": output_dir / "progress_tracker.xlsx",
        "skill_trace": output_dir / "skill_trace.json",
    }

    course_plan = dict(package.get("course_plan") or {})
    teacher_guide = dict(package.get("teacher_guide") or {})
    student_handout = dict(package.get("student_handout") or {})

    _write_json(paths["course_plan"], course_plan)
    _write_docx(paths["teacher_guide"], str(course_plan.get("title") or "B1 Business English Course"), [
        ("Overview", [str(teacher_guide.get("overview") or "")]),
        ("Lesson Notes", [str(item) for item in _ensure_list(teacher_guide.get("lesson_notes"))]),
        ("Facilitation Tips", [str(item) for item in _ensure_list(teacher_guide.get("facilitation_tips"))]),
    ])
    _write_pdf(paths["student_handout"], "Student Handout: B1 Business English Meetings", [
        str(student_handout.get("overview") or ""),
        *[str(item) for item in _ensure_list(student_handout.get("activities"))],
        *[str(item) for item in _ensure_list(student_handout.get("homework"))],
    ])
    _write_pptx(paths["lesson_slides"], _normalize_lesson_slides(package))
    _write_xlsx(
        paths["vocabulary_bank"],
        "Vocabulary",
        ["Week", "Term", "Meaning", "Example"],
        [
            [item.get("week"), item.get("term"), item.get("meaning"), item.get("example")]
            for item in [dict(row) for row in _ensure_list(package.get("vocabulary_bank"))]
        ],
    )
    _write_docx(paths["assessment_rubric"], "Assessment Rubric", [
        (
            str(item.get("criterion") or "Criterion"),
            [str(item.get("b1_descriptor") or ""), f"Evidence: { item.get('evidence') or '' }"],
        )
        for item in [dict(row) for row in _ensure_list(package.get("assessment_rubric"))]
    ])
    _write_xlsx(
        paths["progress_tracker"],
        "Progress Tracker",
        ["Week", "Skill", "Target", "Check Method"],
        [
            [item.get("week"), item.get("skill"), item.get("target"), item.get("check_method")]
            for item in [dict(row) for row in _ensure_list(package.get("progress_tracker"))]
        ],
    )
    _write_json(paths["skill_trace"], {
        "selected_skill_ids": plan_result.get("selected_skill_ids"),
        "stage_plan": plan_result.get("stage_plan"),
        "skill_switches": plan_result.get("skill_switches"),
        "artifact_writer": "benchmark_controlled_local_writers",
        "third_party_skill_scripts_executed": False,
    })
    return {key: str(path) for key, path in paths.items()}


def _artifact_validation(paths: dict[str, str], package: dict[str, Any]) -> dict[str, Any]:
    from openpyxl import load_workbook
    from pptx import Presentation
    from pypdf import PdfReader

    path_map = {key: Path(value) for key, value in paths.items()}
    checks: dict[str, bool] = {
        "all_files_exist": all(path.exists() and path.stat().st_size > 100 for path in path_map.values()),
    }
    course_plan = json.loads(path_map["course_plan"].read_text(encoding="utf-8"))
    teacher_text = _read_docx_text(path_map["teacher_guide"]).lower()
    rubric_text = _read_docx_text(path_map["assessment_rubric"]).lower()
    vocabulary_book = load_workbook(path_map["vocabulary_bank"])
    progress_book = load_workbook(path_map["progress_tracker"])
    slides = Presentation(str(path_map["lesson_slides"]))
    pdf = PdfReader(str(path_map["student_handout"]))
    rubric_rows = [dict(row) for row in _ensure_list(package.get("assessment_rubric"))]

    checks.update({
        "course_plan_has_4_weeks": len(course_plan.get("weeks") or []) >= 4,
        "course_plan_has_12_sessions": sum(len(week.get("sessions") or []) for week in course_plan.get("weeks") or []) >= 12,
        "teacher_docx_has_model_content": len(teacher_text) > 500 and len(teacher_guide_sections := _ensure_list(package.get("teacher_guide", {}).get("lesson_notes"))) >= 4,
        "rubric_docx_has_descriptor_content": len(rubric_text) > 300 and all(
            str(item.get("criterion") or "").strip() and str(item.get("b1_descriptor") or "").strip()
            for item in rubric_rows
        ),
        "student_pdf_has_pages": len(pdf.pages) >= 1,
        "lesson_slides_count": len(slides.slides) >= 8,
        "vocabulary_rows": cast(Any, vocabulary_book.active).max_row >= 10,
        "progress_tracker_rows": cast(Any, progress_book.active).max_row >= 5,
        "package_mentions_retrieval": "retrieval" in _as_text(package).lower(),
        "package_mentions_follow_up": "follow" in _as_text(package).lower(),
    })
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_paths": paths,
    }


def _real_execution_judge_schema() -> dict[str, Any]:
    return {
        "rule_results": [({
            "rule_id": (str, "Stable rule id.", True),
            "evidence": [(str, "Evidence from generated content or artifact validation.", True)],
            "missing_or_weak_points": [(str, "Missing or weak points, empty when none.", False)],
            "reason": (str, "Concise rationale for this rule judgment.", True),
            "passed": (bool, "Final boolean for this rule. Keep last in each rule item.", True),
        }, "Per-rule content and artifact judgment.", True)],
        "overall_reason": (str, "Concise summary of the rule judgments.", True),
        "passes": (bool, "Final real execution benchmark result. Keep last.", True),
    }


def _judge_real_execution(package: dict[str, Any], artifact_validation: dict[str, Any], plan_result: dict[str, Any]) -> dict[str, Any]:
    judge = Agently.create_agent("education-real-execution-judge")
    return judge.input({
        "task": next(case.task for case in combo.CASES if case.case_id == "education_course_pack"),
        "selected_skill_ids": plan_result.get("selected_skill_ids"),
        "generated_course_package": package,
        "artifact_validation": artifact_validation,
        "rules": [
            "The generated content is a complete 4-week B1 adult business English course with 3 sessions per week.",
            "The course teaches meeting skills: expressing opinions, asking follow-up questions, and summarizing decisions.",
            "The course includes lesson activities, homework, vocabulary, retrieval practice, formative assessment, and progress tracking.",
            "The generated artifacts are real files and the deterministic artifact validation passed.",
            "The artifacts are mutually aligned rather than unrelated files.",
        ],
    }).instruct(
        "Judge the real execution result for an education Skill Pack benchmark. "
        "Use semantic content quality and artifact validation evidence. "
        "Do not use keyword matching as the primary judgment. "
        "Output evidence and concise reasons before final boolean fields."
    ).output(_real_execution_judge_schema()).start()


def run_real_execution_benchmark(
    *,
    output_root: Path | None = None,
    registry_root: Path | None = None,
    fetch_missing: bool = True,
) -> dict[str, Any]:
    _configure_deepseek()
    output_dir = output_root or RUNTIME_ROOT / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    source_status = combo._fetch_missing_sources(fetch_missing)
    missing_groups = [group for group in ["education", "anthropic"] if source_status.get(group, "").startswith("missing:")]
    if missing_groups:
        raise RuntimeError(f"Missing benchmark sources: { missing_groups }")

    installed = combo._install_available_skills(source_status, registry_root=registry_root or RUNTIME_ROOT / "registry")
    dependency_execution = _run_dependency_install_skill((registry_root or RUNTIME_ROOT / "registry") / "_local_dependency_skill")
    dependency_output = dependency_execution.output if isinstance(dependency_execution.output, dict) else {}
    dependency_passed = (
        dependency_execution.status == "success"
        and bool(dependency_output.get("ensure_artifact_writer_dependencies", {}).get("available_after"))
        and all(bool(item) for item in dependency_output.get("ensure_artifact_writer_dependencies", {}).get("available_after", {}).values())
    )
    if not dependency_passed:
        raise RuntimeError(f"Dependency installation skill failed: { dependency_execution.to_dict() }")

    case = next(item for item in combo.CASES if item.case_id == "education_course_pack")
    candidate_skill_ids = combo._candidate_skill_ids(case, installed)
    planning_outcome = combo._run_case(case, candidate_skill_ids)
    plan_result = planning_outcome["result"]
    selected_skill_ids = [str(item) for item in plan_result.get("selected_skill_ids") or []]

    package = _generate_course_package(plan_result, selected_skill_ids)
    artifact_paths = _write_artifacts(package, plan_result, output_dir)
    artifact_validation = _artifact_validation(artifact_paths, package)
    model_judge = _judge_real_execution(package, artifact_validation, plan_result)
    model_judge_passed = bool(model_judge.get("passes")) and all(
        bool(item.get("passed")) for item in model_judge.get("rule_results", [])
    )

    report = {
        "case_id": "education_course_pack",
        "source_status": source_status,
        "candidate_skill_ids": candidate_skill_ids,
        "selected_skill_ids": selected_skill_ids,
        "dependency_execution": dependency_execution.to_dict(),
        "planning_execution": planning_outcome.get("execution"),
        "artifact_paths": artifact_paths,
        "artifact_validation": artifact_validation,
        "model_judge": model_judge,
        "real_execution_result": "pass" if artifact_validation["passed"] and model_judge_passed else "fail",
    }
    report_path = output_dir / "execution_report.json"
    _write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-fetch-missing",
        action="store_true",
        help="do not git clone missing public skill repositories before running",
    )
    parser.add_argument("--output-dir", type=Path, default=RUNTIME_ROOT / "outputs")
    args = parser.parse_args()

    report = run_real_execution_benchmark(output_root=args.output_dir, fetch_missing=not args.no_fetch_missing)
    dependency_output = report["dependency_execution"]["output"]["ensure_artifact_writer_dependencies"]
    dependency_imports_available = all(bool(value) for value in dependency_output["available_after"].values())
    print(f"real_execution_result={ report['real_execution_result'] }")
    print(f"dependency_action_status={ report['dependency_execution']['action_logs'][0]['status'] }")
    print(f"dependency_imports_available={ dependency_imports_available }")
    print(f"deterministic_passed={ report['artifact_validation']['passed'] }")
    print(f"model_judge_passes={ report['model_judge'].get('passes') }")
    print(f"artifact_count={ len(report['artifact_paths']) }")
    print(f"report={ args.output_dir / 'execution_report.json' }")
    print()
    print("Natural language summary:")
    print(
        "Skills Executor installed/discovered the education and artifact Skills, "
        "ran a dependency-installer Skill through an action stage, verified local "
        "writer imports, generated the course package artifacts, and passed both "
        "deterministic artifact checks and model-based semantic judgment."
    )
    print(f"Judge reason: { report['model_judge'].get('overall_reason', '') }")


if __name__ == "__main__":
    main()
