"""Combo Skill Pack diagnostics for Skills Executor.

Run:
    PYTHONPATH=. python examples/skills_executor/combo_skillpack_diagnostics.py

Optional fetch:
    PYTHONPATH=. python examples/skills_executor/combo_skillpack_diagnostics.py --fetch-missing

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Optional local checkouts:
      AGENTLY_SKILLS_REPO=../Agently-Skills
      ANTHROPIC_SKILLS_REPO=.example_runtime/skills_executor/anthropic-skills
      TRAVEL_PLANNER_SKILL_REPO=.example_runtime/skills_executor/external/travel-planner-skill
      EDUCATION_AGENT_SKILLS_REPO=.example_runtime/skills_executor/external/education-agent-skills
      OCTAGON_SKILLS_REPO=.example_runtime/skills_executor/external/octagon-skills
      CLAUDE_TRADING_SKILLS_REPO=.example_runtime/skills_executor/external/claude-trading-skills

Expected key output from a real DeepSeek run on a fully prepared checkout set:
    [CASE] education_course_pack
    diagnostic_result=pass
    [CASE] stock_research_pack
    diagnostic_result=pass
    [CASE] travel_planning_pack
    diagnostic_result=pass
    [CASE] research_to_briefing_pack
    diagnostic_result=pass
    [CASE] webapp_acceptance_pack
    diagnostic_result=pass

This example is intentionally diagnostic rather than a demo. It does not
hard-code domain execution logic, does not provide case-specific API recipes to
the model, and does not run repair rounds. The model must select and compose
from disclosed SkillCards and bounded primary SKILL.md guidance. The host only
checks whether the resulting orchestration plan preserves the boundaries that a
Skills Executor implementation must preserve.

Flow:
    local external SKILL.md checkouts
      |
      v
    Agently.skills.install(...) -> SkillContracts + SkillCards
      |
      v
    agent.use_skills(..., model_decision)
      |
      v
    DeepSeek returns a structured combo-skill orchestration plan
      |
      v
    host evaluator checks selection, stage switching, intermediate artifacts,
    approval gates, fallbacks, side-effect boundaries, and output coverage
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from pprint import pprint
from typing import Any

from dotenv import find_dotenv, load_dotenv

from agently import Agently


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor" / "combo_skillpacks"
DEFAULT_ANTHROPIC_REPO = ROOT / ".example_runtime" / "skills_executor" / "anthropic-skills"
DEFAULT_EXTERNAL_ROOT = RUNTIME_ROOT / "external"
REPORT_PATH = RUNTIME_ROOT / "combo_skillpack_diagnostics.json"

REPO_SPECS = {
    "anthropic": {
        "env": "ANTHROPIC_SKILLS_REPO",
        "default": DEFAULT_ANTHROPIC_REPO,
        "url": "https://github.com/anthropics/skills.git",
    },
    "agently": {
        "env": "AGENTLY_SKILLS_REPO",
        "default": (ROOT / ".." / "Agently-Skills").resolve(),
        "url": None,
    },
    "travel": {
        "env": "TRAVEL_PLANNER_SKILL_REPO",
        "default": DEFAULT_EXTERNAL_ROOT / "travel-planner-skill",
        "url": "https://github.com/ZawYePhyo/travel-planner-skill.git",
    },
    "education": {
        "env": "EDUCATION_AGENT_SKILLS_REPO",
        "default": DEFAULT_EXTERNAL_ROOT / "education-agent-skills",
        "url": "https://github.com/GarethManning/education-agent-skills.git",
    },
    "octagon": {
        "env": "OCTAGON_SKILLS_REPO",
        "default": DEFAULT_EXTERNAL_ROOT / "octagon-skills",
        "url": "https://github.com/OctagonAI/skills.git",
    },
    "trading": {
        "env": "CLAUDE_TRADING_SKILLS_REPO",
        "default": DEFAULT_EXTERNAL_ROOT / "claude-trading-skills",
        "url": "https://github.com/tradermonty/claude-trading-skills.git",
    },
}

ANTHROPIC_ARTIFACT_SKILLS = ["docx", "xlsx", "pptx", "pdf"]


@dataclass(frozen=True)
class ComboCase:
    case_id: str
    priority: int
    task: str
    source_groups: list[str]
    artifact_skill_ids: list[str] = field(default_factory=list)
    min_selected_skills: int = 3
    min_stages: int = 5
    expected_outputs: list[str] = field(default_factory=list)
    required_terms: list[str] = field(default_factory=list)
    requires_approval_boundary: bool = False
    requires_fallback: bool = False
    requires_current_data_boundary: bool = False
    requires_external_api_boundary: bool = False
    requires_compliance_boundary: bool = False
    requires_webapp_evidence: bool = False


CASES = [
    ComboCase(
        case_id="education_course_pack",
        priority=1,
        source_groups=["education", "anthropic"],
        artifact_skill_ids=["docx", "pdf", "pptx", "xlsx"],
        min_selected_skills=5,
        min_stages=8,
        expected_outputs=[
            "course_plan.json",
            "teacher_guide.docx",
            "student_handout.pdf",
            "lesson_slides.pptx",
            "vocabulary_bank.xlsx",
            "assessment_rubric.docx",
            "progress_tracker.xlsx",
            "skill_trace.json",
        ],
        required_terms=[
            "B1",
            "business English",
            "lesson",
            "vocabulary",
            "retrieval",
            "assessment",
            "progress",
        ],
        task=(
            "为一名 B1 水平的成人英语学习者设计一个 4 周商务英语课程。目标："
            "能在英文会议中表达观点、追问细节、总结决议。每周 3 次课，每次 "
            "45 分钟。需要完整教案、课堂活动、课后练习、词汇表、形成性评价。"
            "生成教师版 docx、学生讲义 PDF、课堂 slides、学习进度追踪 xlsx。"
        ),
    ),
    ComboCase(
        case_id="stock_research_pack",
        priority=2,
        source_groups=["octagon", "trading", "anthropic"],
        artifact_skill_ids=["docx", "xlsx"],
        min_selected_skills=4,
        min_stages=8,
        expected_outputs=[
            "stock_research_report.docx",
            "comparison_model.xlsx",
            "source_index.json",
            "compliance_notes.json",
        ],
        required_terms=["NVDA", "AMD", "AVGO", "SEC", "earnings", "risk", "source"],
        requires_current_data_boundary=True,
        requires_external_api_boundary=True,
        requires_compliance_boundary=True,
        requires_fallback=True,
        task=(
            "帮我分析 NVDA、AMD、AVGO 三家公司，输出一份对比研究报告。要求："
            "最近股价表现、市值和估值对比、收入、毛利、现金流、负债结构、"
            "最近 earnings call 中管理层关注点、SEC 风险因素、分析师目标价和评级变化。"
            "最后输出风险清单、观察指标、非投资建议结论，并生成 Markdown/Docx 报告和 Excel 对比表。"
        ),
    ),
    ComboCase(
        case_id="travel_planning_pack",
        priority=3,
        source_groups=["travel", "anthropic"],
        artifact_skill_ids=["xlsx", "pdf", "docx"],
        min_selected_skills=4,
        min_stages=7,
        expected_outputs=[
            "itinerary.md",
            "itinerary.pdf",
            "budget.xlsx",
            "travel_assumptions.json",
            "unresolved_questions.json",
            "execution_log.json",
        ],
        required_terms=["Tokyo", "rain", "budget", "transport", "approval", "fallback"],
        requires_approval_boundary=True,
        requires_current_data_boundary=True,
        requires_external_api_boundary=True,
        requires_fallback=True,
        task=(
            "帮我规划 2026 年 7 月东京 6 天 5 晚亲子旅行，2 个大人 1 个 8 岁小孩。"
            "要求：每天不要太赶，预算中等，住在交通方便的位置，包含雨天备选方案，"
            "输出每日行程、交通方式、餐饮建议、预算表，最后生成一份可打印行程 PDF "
            "和一份 Excel 预算表。在我确认前不要写入任何外部行程工具。"
        ),
    ),
    ComboCase(
        case_id="research_to_briefing_pack",
        priority=4,
        source_groups=["agently", "anthropic"],
        artifact_skill_ids=["docx", "xlsx", "pptx", "pdf"],
        min_selected_skills=4,
        min_stages=8,
        expected_outputs=[
            "research_notes.json",
            "source_index.json",
            "comparison_matrix.xlsx",
            "full_report.docx",
            "executive_summary.pdf",
            "briefing_deck.pptx",
            "qa_report.json",
        ],
        required_terms=["Agent Skills", "source", "comparison", "deck", "QA"],
        requires_current_data_boundary=True,
        requires_fallback=True,
        task=(
            "帮我做一份 2026 年 Agent Skills 生态调研报告。要求：调研 OpenAI、"
            "Anthropic、GitHub Copilot、OpenClaw、社区技能市场；输出引用来源；"
            "生成一份 3000 字 Markdown/Docx 报告、一份 Excel 对比表、一份 12 页 PPT "
            "汇报材料；最后把所有材料打包，并输出质量检查日志。"
        ),
    ),
    ComboCase(
        case_id="webapp_acceptance_pack",
        priority=5,
        source_groups=["anthropic", "agently"],
        artifact_skill_ids=["webapp-testing", "docx", "pptx"],
        min_selected_skills=3,
        min_stages=7,
        expected_outputs=[
            "test_plan.md",
            "screenshots/",
            "console_errors.json",
            "network_errors.json",
            "playwright_trace.zip",
            "bug_report.docx",
            "qa_summary.json",
        ],
        required_terms=["dev server", "login", "SSE", "screenshot", "console", "network"],
        requires_external_api_boundary=True,
        requires_fallback=True,
        requires_webapp_evidence=True,
        task=(
            "请对这个本地前端项目做一次用户路径验收测试：启动 dev server，打开登录页，"
            "模拟登录，创建一条学习任务，检查 SSE 流式更新是否显示，截图关键页面，"
            "记录 console error 和 network error，输出 bug_report.docx 和测试证据包。"
        ),
    ),
]

GROUP_KEYWORDS = {
    "agently": ["runtime", "request", "triggerflow"],
    "education": [
        "learner",
        "profile",
        "diagnostic",
        "backward",
        "design",
        "spaced",
        "retrieval",
        "vocabulary",
        "formative",
        "assessment",
        "lesson",
        "unit",
        "worksheet",
        "progress",
        "rubric",
    ],
    "octagon": [
        "master",
        "market",
        "stock",
        "quote",
        "performance",
        "financial",
        "earnings",
        "sec",
        "risk",
        "analyst",
        "balance",
        "cash",
        "income",
    ],
    "trading": [
        "market",
        "technical",
        "calendar",
        "screener",
        "strategy",
        "risk",
        "earnings",
        "flow",
    ],
}


def _repo_path(group: str) -> Path:
    spec = REPO_SPECS[group]
    configured = os.getenv(str(spec["env"]))
    return Path(configured).expanduser().resolve() if configured else Path(spec["default"]).expanduser().resolve()


def _configure_deepseek():
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DEEPSEEK_API_KEY. Put it in your shell or .env before running this example.")
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


def _run(command: list[str], *, cwd: Path):
    subprocess.run(command, cwd=cwd, check=True)


def _fetch_missing_sources(fetch_missing: bool) -> dict[str, str]:
    status: dict[str, str] = {}
    for group, spec in REPO_SPECS.items():
        path = _repo_path(group)
        if path.exists():
            status[group] = f"available:{ path }"
            continue
        url = spec["url"]
        if not fetch_missing or not url:
            status[group] = f"missing:{ path }"
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--depth", "1", str(url), str(path)], cwd=ROOT)
        status[group] = f"fetched:{ path }"
    return status


def _find_skill_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if (root / "SKILL.md").exists():
        return [root]
    return sorted(path.parent for path in root.rglob("SKILL.md") if path.is_file())


def _score_skill_path(path: Path, keywords: list[str]) -> int:
    text = str(path).lower().replace("_", "-")
    return sum(1 for keyword in keywords if keyword.lower() in text)


def _select_group_skill_dirs(group: str, *, limit: int = 12) -> list[Path]:
    root = _repo_path(group)
    if group == "anthropic":
        return [root / "skills" / skill_id for skill_id in [*ANTHROPIC_ARTIFACT_SKILLS, "webapp-testing"]]
    if group == "agently":
        return [root / "skills" / skill_id for skill_id in ["agently-runtime", "agently-request", "agently-triggerflow"]]
    if group == "travel":
        return [root] if (root / "SKILL.md").exists() else []

    skill_dirs = _find_skill_dirs(root)
    keywords = GROUP_KEYWORDS.get(group, [])
    if not keywords:
        return skill_dirs[:limit]

    scored = [(_score_skill_path(path, keywords), path) for path in skill_dirs]
    selected = [path for score, path in sorted(scored, key=lambda item: (-item[0], str(item[1]))) if score > 0]
    return selected[:limit]


def _install_available_skills(source_status: dict[str, str], *, registry_root: Path | None = None) -> dict[str, dict[str, Any]]:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    Agently.settings.set("skills.registry.root", str(registry_root or RUNTIME_ROOT / "registry"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.settings.set("skills.prompt.max_guidance_chars_per_skill", 2400)

    installed: dict[str, dict[str, Any]] = {}
    for group in REPO_SPECS:
        if source_status.get(group, "").startswith("missing:"):
            continue
        for skill_dir in _select_group_skill_dirs(group):
            if not (skill_dir / "SKILL.md").exists():
                continue
            try:
                contract = Agently.skills.install(skill_dir, trust_level="local", update=True)
            except Exception as error:
                installed[f"{ group }:{ skill_dir.name }"] = {
                    "skill_id": "",
                    "group": group,
                    "path": str(skill_dir),
                    "install_error": str(error),
                }
                continue
            skill_id = str(contract.get("skill_id"))
            installed[skill_id] = {
                "skill_id": skill_id,
                "group": group,
                "path": str(skill_dir),
                "purpose": contract.get("card", {}).get("purpose", ""),
            }
    return installed


def _candidate_skill_ids(case: ComboCase, installed: dict[str, dict[str, Any]]) -> list[str]:
    group_ids = [
        skill_id
        for skill_id, record in installed.items()
        if record.get("skill_id")
        and record.get("group") in case.source_groups
        and (record.get("group") != "anthropic" or skill_id in case.artifact_skill_ids)
    ]
    required_artifacts = [skill_id for skill_id in case.artifact_skill_ids if skill_id in installed]
    ordered = [*required_artifacts, *group_ids]
    unique: list[str] = []
    for skill_id in ordered:
        if skill_id not in unique:
            unique.append(skill_id)
    return unique


def _create_agent():
    agent = Agently.create_agent()
    agent.set_agent_prompt(
        "system",
        (
            "You are evaluating Agently Skills Executor orchestration. Treat "
            "the disclosed skills as optional behavior-loop candidates. Select "
            "only skills that fit the task, switch skills by stage when needed, "
            "and make boundaries between skills, actions/tools, external APIs, "
            "and artifacts explicit. Do not pretend unavailable side effects "
            "have happened."
        ),
    )
    return agent


def _run_case(case: ComboCase, candidate_skill_ids: list[str]) -> dict[str, Any]:
    agent = _create_agent()
    plan = agent.resolve_skill_plan(case.task, skills=candidate_skill_ids, mode="model_decision", scope="request")
    agent.use_skills(candidate_skill_ids, mode="model_decision", scope="request")
    result = (
        agent.input(
            {
                "task": case.task,
                "case_id": case.case_id,
                "candidate_skill_ids": candidate_skill_ids,
                "diagnostic_instruction": (
                    "Return the orchestration you would execute from the disclosed skills. "
                    "Do not add implementation hints that are not already in the skills. "
                    "Do not claim files or external writes are complete; describe the execution plan."
                ),
            }
        )
        .output(
            {
                "selected_skill_ids": [(str, "Skill ids selected for this task.", True)],
                "entry_skill_id": (str, "The first selected skill id, or none.", True),
                "stage_plan": [(str, "Ordered stage. Include skill handoff and dependency notes.", True)],
                "skill_switches": [(str, "Where execution changes from one skill/capability to another.", True)],
                "intermediate_artifacts": [(str, "Intermediate artifacts passed between stages.", True)],
                "external_side_effects": [(str, "External API/MCP/SaaS writes or local command effects.", True)],
                "approval_gates": [(str, "Human approval gates before side effects.", True)],
                "fallbacks": [(str, "Fallback, retry, or degraded-mode behavior.", True)],
                "expected_outputs": [(str, "Final output artifact path or result.", True)],
                "boundary_notes": [(str, "Skill vs action/tool/API/artifact boundary notes.", True)],
                "risks": [(str, "Execution risks, missing dependencies, or policy constraints.", True)],
            }
        )
        .start(max_retries=1, raise_ensure_failure=False)
    )
    return {"plan": plan, "result": result}


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _contains_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def _normalize_output_name(value: str) -> str:
    return value.lower().strip().rstrip("/")


_OUTPUT_TYPE_ALIASES = {
    "docx": ["docx", "word", "document", "教师版", "报告"],
    "pdf": ["pdf", "printable", "handout", "summary", "打印", "讲义"],
    "pptx": ["pptx", "powerpoint", "slides", "slide deck", "deck", "课件", "汇报"],
    "xlsx": ["xlsx", "excel", "spreadsheet", "workbook", "sheet", "表", "预算"],
    "json": ["json", "structured", "metadata", "trace", "log", "结构化", "日志"],
    "md": ["markdown", "md", "plan", "notes", "itinerary", "测试计划"],
    "dir": ["folder", "directory", "evidence", "screenshots", "截图", "证据包"],
    "zip": ["zip", "trace", "archive", "package", "压缩", "证据包"],
}

_OUTPUT_ROLE_ALIASES = {
    "itinerary": ["itinerary", "daily route", "行程", "每日"],
    "budget": ["budget", "cost", "expense", "预算", "费用"],
    "travel_assumptions": ["assumption", "travel assumption", "假设"],
    "unresolved_questions": ["unresolved", "open question", "missing information", "待确认", "缺失条件"],
    "execution_log": ["execution log", "skill trace", "trace", "日志"],
    "course_plan": ["course plan", "unit plan", "课程计划", "课程包"],
    "teacher_guide": ["teacher guide", "teacher version", "lesson plan", "教师版", "教案"],
    "student_handout": ["student handout", "learner handout", "worksheet", "学生讲义", "练习"],
    "lesson_slides": ["lesson slides", "slides", "slide deck", "课件"],
    "vocabulary_bank": ["vocabulary bank", "vocabulary", "词汇"],
    "assessment_rubric": ["assessment rubric", "rubric", "formative assessment", "评价量规"],
    "progress_tracker": ["progress tracker", "progress", "学习进度"],
    "skill_trace": ["skill trace", "execution trace", "trace", "技能轨迹"],
    "stock_research_report": ["stock research", "research report", "investment brief", "研究报告"],
    "comparison_model": ["comparison model", "comparison", "对比"],
    "source_index": ["source index", "sources", "citations", "来源", "引用"],
    "compliance_notes": ["compliance", "not investment advice", "non-investment", "合规", "不是投资建议"],
    "research_notes": ["research notes", "notes", "调研笔记"],
    "comparison_matrix": ["comparison matrix", "matrix", "对比表"],
    "full_report": ["full report", "report", "完整报告"],
    "executive_summary": ["executive summary", "summary", "摘要"],
    "briefing_deck": ["briefing deck", "deck", "slides", "汇报材料"],
    "qa_report": ["qa report", "quality check", "quality assurance", "质量检查"],
    "test_plan": ["test plan", "acceptance plan", "测试计划"],
    "screenshots": ["screenshot", "screenshots", "截图"],
    "console_errors": ["console error", "console log", "控制台"],
    "network_errors": ["network error", "network log", "网络"],
    "playwright_trace": ["playwright trace", "trace", "浏览器轨迹"],
    "bug_report": ["bug report", "defect report", "缺陷报告"],
    "qa_summary": ["qa summary", "test summary", "验收总结"],
}


def _output_role_and_type(output_name: str) -> tuple[str, str]:
    normalized = _normalize_output_name(output_name)
    if normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    if "." not in normalized:
        return normalized, "dir"
    role, suffix = normalized.rsplit(".", 1)
    return role, suffix


def _semantic_output_covered(output_name: str, expected_outputs: list[str], full_text: str) -> bool:
    normalized = _normalize_output_name(output_name)
    text = full_text.lower()
    output_text = " ".join(expected_outputs).lower()
    if normalized in output_text or normalized in text:
        return True

    role, output_type = _output_role_and_type(output_name)
    role_aliases = _OUTPUT_ROLE_ALIASES.get(role, [role.replace("_", " ")])
    type_aliases = _OUTPUT_TYPE_ALIASES.get(output_type, [output_type])
    searchable = f"{ output_text } { text }"
    role_covered = any(alias.lower() in searchable for alias in role_aliases)
    type_covered = any(alias.lower() in searchable for alias in type_aliases)
    return role_covered and type_covered


def _evaluate_case(case: ComboCase, candidate_skill_ids: list[str], result: dict[str, Any]) -> dict[str, Any]:
    selected_skill_ids = [str(item) for item in result.get("selected_skill_ids") or [] if str(item).strip()]
    stage_plan = result.get("stage_plan") or []
    expected_outputs = [_normalize_output_name(str(item)) for item in result.get("expected_outputs") or []]
    full_text = _flatten_text(result)

    output_coverage = {}
    for output_name in case.expected_outputs:
        output_coverage[output_name] = _semantic_output_covered(output_name, expected_outputs, full_text)

    checks = {
        "has_candidates": bool(candidate_skill_ids),
        "selects_multiple_skills": len(set(selected_skill_ids)) >= min(case.min_selected_skills, len(candidate_skill_ids)),
        "selected_skills_are_candidates": set(selected_skill_ids).issubset(set(candidate_skill_ids)),
        "has_stage_plan": len(stage_plan) >= case.min_stages,
        "records_skill_switches": bool(result.get("skill_switches")),
        "records_intermediate_artifacts": bool(result.get("intermediate_artifacts")),
        "records_boundaries": _contains_any(full_text, ["skill", "action", "tool", "api", "mcp", "artifact"]),
        "covers_expected_outputs": all(output_coverage.values()),
        "covers_required_terms": all(term.lower() in full_text.lower() for term in case.required_terms),
    }
    if case.requires_approval_boundary:
        checks["approval_before_side_effect"] = _contains_any(
            _flatten_text(result.get("approval_gates")) + " " + _flatten_text(result.get("external_side_effects")),
            ["approval", "approve", "confirm", "确认"],
        )
    if case.requires_fallback:
        checks["has_fallback_or_degraded_mode"] = bool(result.get("fallbacks")) and _contains_any(
            _flatten_text(result.get("fallbacks")), ["fallback", "retry", "degrade", "skip", "local", "降级", "重试"]
        )
    if case.requires_current_data_boundary:
        checks["flags_current_data_dependency"] = _contains_any(
            full_text, ["current", "live", "real-time", "recent", "source", "citation", "实时", "最新", "引用"]
        )
    if case.requires_external_api_boundary:
        checks["flags_external_api_or_environment"] = _contains_any(
            full_text, ["api", "mcp", "credential", "key", "browser", "playwright", "dev server", "external", "环境"]
        )
    if case.requires_compliance_boundary:
        checks["has_investment_compliance_boundary"] = _contains_any(
            full_text, ["not investment advice", "non-investment", "no buy", "no sell", "不是投资建议", "不构成投资建议"]
        )
    if case.requires_webapp_evidence:
        checks["has_webapp_evidence_plan"] = _contains_any(
            full_text, ["screenshot", "console", "network", "trace", "playwright", "截图"]
        )

    return {
        "diagnostic_result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "output_coverage": output_coverage,
        "selected_skill_ids": selected_skill_ids,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-missing", action="store_true", help="git clone missing public skill repositories")
    parser.add_argument("--case", choices=[case.case_id for case in CASES], help="run only one diagnostic case")
    parser.add_argument("--list-sources", action="store_true", help="print source status and exit before model calls")
    args = parser.parse_args()

    source_status = _fetch_missing_sources(args.fetch_missing)
    installed = _install_available_skills(source_status)

    print("[SOURCE_STATUS]")
    pprint(source_status)
    print("[INSTALLED_SKILLS]")
    pprint({skill_id: {"group": data.get("group"), "path": data.get("path")} for skill_id, data in installed.items()})

    if args.list_sources:
        return

    _configure_deepseek()

    report: dict[str, Any] = {
        "source_status": source_status,
        "installed_skills": installed,
        "cases": {},
    }
    selected_cases = [case for case in CASES if args.case in {None, case.case_id}]
    for case in selected_cases:
        print(f"\n[CASE] { case.case_id }")
        candidate_skill_ids = _candidate_skill_ids(case, installed)
        missing_groups = [group for group in case.source_groups if source_status.get(group, "").startswith("missing:")]
        if missing_groups:
            print(f"diagnostic_result=skip missing_groups={ missing_groups }")
            report["cases"][case.case_id] = {
                "diagnostic_result": "skip",
                "missing_groups": missing_groups,
                "candidate_skill_ids": candidate_skill_ids,
            }
            continue
        if not candidate_skill_ids:
            print("diagnostic_result=skip reason=no_candidate_skills")
            report["cases"][case.case_id] = {
                "diagnostic_result": "skip",
                "reason": "no_candidate_skills",
                "candidate_skill_ids": [],
            }
            continue

        outcome = _run_case(case, candidate_skill_ids)
        evaluation = _evaluate_case(case, candidate_skill_ids, outcome["result"])
        print(f"diagnostic_result={ evaluation['diagnostic_result'] }")
        print(f"selected_skill_ids={ evaluation['selected_skill_ids'] }")
        print("[CHECKS]")
        pprint(evaluation["checks"])
        report["cases"][case.case_id] = {
            "candidate_skill_ids": candidate_skill_ids,
            "plan": outcome["plan"],
            "model_result": outcome["result"],
            "evaluation": evaluation,
        }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[REPORT] { REPORT_PATH }")


if __name__ == "__main__":
    main()
