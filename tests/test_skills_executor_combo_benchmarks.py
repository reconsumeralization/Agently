import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from dotenv import find_dotenv, load_dotenv


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = ROOT / "examples" / "skills_executor" / "05_combo_skillpack_diagnostics.py"


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location("combo_skillpack_diagnostics", BENCHMARK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_combo_benchmark_case_catalog_covers_recommended_realcases():
    benchmark = _load_benchmark_module()
    case_ids = {case.case_id for case in benchmark.CASES}

    assert case_ids == {
        "education_course_pack",
        "stock_research_pack",
        "travel_planning_pack",
        "research_to_briefing_pack",
        "webapp_acceptance_pack",
    }

    for case in benchmark.CASES:
        assert case.task
        assert case.source_groups
        assert case.expected_outputs
        assert case.min_selected_skills >= 3
        assert case.min_stages >= 5


def test_combo_benchmark_installs_available_skillpacks_without_model_calls(tmp_path):
    benchmark = _load_benchmark_module()
    source_status = benchmark._fetch_missing_sources(fetch_missing=False)
    installed = benchmark._install_available_skills(source_status, registry_root=tmp_path / "skills-registry")

    missing_groups = [group for group in benchmark.REPO_SPECS if source_status.get(group, "").startswith("missing:")]
    if missing_groups:
        pytest.skip(f"Missing optional benchmark source groups: { missing_groups }")

    for case in benchmark.CASES:
        candidate_skill_ids = benchmark._candidate_skill_ids(case, installed)
        assert candidate_skill_ids, f"{ case.case_id } has no candidate skills"

    education_candidates = set(
        benchmark._candidate_skill_ids(
            next(case for case in benchmark.CASES if case.case_id == "education_course_pack"),
            installed,
        )
    )
    assert "webapp-testing" not in education_candidates
    assert {"docx", "pdf", "pptx", "xlsx"}.issubset(education_candidates)

    travel_candidates = set(
        benchmark._candidate_skill_ids(
            next(case for case in benchmark.CASES if case.case_id == "travel_planning_pack"),
            installed,
        )
    )
    assert "travel-planner" in travel_candidates
    assert "webapp-testing" not in travel_candidates

    installed_ids = set(installed)
    assert {"docx", "xlsx", "pptx", "pdf", "webapp-testing"}.issubset(installed_ids)
    assert "travel-planner" in installed_ids
    assert "backwards-design-unit-planner" in installed_ids
    assert {"market-analyst-master", "financial-analyst-master", "sec-analyst-master"}.issubset(installed_ids)


@pytest.mark.skills_benchmark
def test_deepseek_combo_skillpack_benchmark_all_cases(tmp_path):
    if os.getenv("AGENTLY_RUN_SKILLS_BENCHMARKS") != "1":
        pytest.skip("Set AGENTLY_RUN_SKILLS_BENCHMARKS=1 to run DeepSeek-backed Skills benchmarks.")

    load_dotenv(find_dotenv(usecwd=True))
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("Missing DEEPSEEK_API_KEY.")

    benchmark = _load_benchmark_module()
    source_status = benchmark._fetch_missing_sources(fetch_missing=False)
    missing_groups = [group for group in benchmark.REPO_SPECS if source_status.get(group, "").startswith("missing:")]
    if missing_groups:
        pytest.skip(f"Missing optional benchmark source groups: { missing_groups }")

    installed = benchmark._install_available_skills(source_status, registry_root=tmp_path / "skills-registry")
    benchmark._configure_deepseek()

    report: dict[str, Any] = {
        "source_status": source_status,
        "installed_skills": installed,
        "cases": {},
    }
    failures: dict[str, Any] = {}
    for case in benchmark.CASES:
        candidate_skill_ids = benchmark._candidate_skill_ids(case, installed)
        outcome = benchmark._run_case(case, candidate_skill_ids)
        evaluation = benchmark._evaluate_case(case, candidate_skill_ids, outcome["result"])
        model_judge = benchmark._judge_case_with_model(case, candidate_skill_ids, outcome, evaluation)
        judge_passed = bool(model_judge.get("passes")) and all(
            bool(item.get("passed")) for item in model_judge.get("rule_results", [])
        )
        report["cases"][case.case_id] = {
            "candidate_skill_ids": candidate_skill_ids,
            "plan": outcome["plan"],
            "model_result": outcome["result"],
            "execution": outcome.get("execution"),
            "evaluation": evaluation,
            "model_judge": model_judge,
        }
        if evaluation["diagnostic_result"] != "pass":
            failures[case.case_id] = evaluation
        elif not judge_passed:
            failures[case.case_id] = model_judge

    benchmark.REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    benchmark.REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    assert failures == {}, f"Skills combo benchmark failures written to { benchmark.REPORT_PATH }: { list(failures) }"
